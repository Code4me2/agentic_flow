"""
A2A-to-Ollama Bridge v2

Full A2A protocol compliance with JSON-RPC methods, task lifecycle management,
and native Ollama MCP integration. No Google ADK dependencies.

Also exposes an MCP endpoint so this agent can be discovered and invoked
by other agents (e.g., a manager agent) via MCP tool calls.

A2A Spec Reference: https://a2a-protocol.org/latest/
MCP Spec Reference: https://modelcontextprotocol.io/

Endpoints:
    GET  /.well-known/agent.json  - Agent Card for discovery
    POST /a2a                     - JSON-RPC endpoint (SendMessage, GetTask, etc.)
    POST /mcp                     - MCP streamable-http endpoint
    GET  /health                  - Health check
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("a2a_bridge_v2")

# Configuration
OLLAMA_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8002"))
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
DEFAULT_MODEL = os.getenv("BRIDGE_MODEL", "ministral-3:14b")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))
AGENT_NAME = os.getenv("AGENT_NAME", "OllamaAgent")
AGENT_DESCRIPTION = os.getenv("AGENT_DESCRIPTION", "Local AI agent with native MCP tool support")

# MCP Configuration
DEFAULT_TOOLS_PATH = os.getenv("TOOLS_PATH", "")
DEFAULT_MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "15"))
DEFAULT_TOOL_TIMEOUT = int(os.getenv("TOOL_TIMEOUT", "30000"))

# Static Tools Configuration
# Comma-separated list of tools to expose via MCP tools/list
# If empty or not set, all tools are exposed (JIT discovery mode)
# Example: STATIC_TOOLS="invoke,get_task_result" for minimal footprint
STATIC_TOOLS = os.getenv("STATIC_TOOLS", "")
STATIC_TOOLS_LIST = [t.strip() for t in STATIC_TOOLS.split(",") if t.strip()] if STATIC_TOOLS else []

# Agent Config File (optional — overrides env vars for name/description/skills)
AGENT_CONFIG_PATH = os.getenv("AGENT_CONFIG", "")
_agent_config = None
if AGENT_CONFIG_PATH:
    try:
        with open(AGENT_CONFIG_PATH) as f:
            _agent_config = json.load(f)
        AGENT_NAME = _agent_config.get("name", AGENT_NAME)
        AGENT_DESCRIPTION = _agent_config.get("description", AGENT_DESCRIPTION)
        logger.info(f"Loaded agent config from {AGENT_CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"Failed to load agent config: {e}")

# Worker MCP Servers Configuration
# JSON list of MCP server configs for worker agents to use during task execution.
# When set, workers can discover and call remote MCP tools via JIT discovery.
# Example: WORKER_MCP_SERVERS='[{"name":"calendar","transport":"http","url":"http://host:8085/mcp"}]'
_worker_mcp_raw = os.getenv("WORKER_MCP_SERVERS", "")
WORKER_MCP_SERVERS = json.loads(_worker_mcp_raw) if _worker_mcp_raw else []

# Push Notification Configuration
NOTIFICATION_MAX_RETRIES = int(os.getenv("NOTIFICATION_MAX_RETRIES", "5"))
NOTIFICATION_TIMEOUT = float(os.getenv("NOTIFICATION_TIMEOUT", "10"))


# =============================================================================
# Push Notification Types
# =============================================================================

@dataclass
class WebhookSubscription:
    """Subscription for push notifications"""
    id: str
    webhook_url: str
    secret: str  # HMAC secret for signature verification
    task_id: Optional[str] = None  # None means all tasks
    events: list[str] = field(default_factory=lambda: ["task.completed", "task.failed"])
    created_at: datetime = field(default_factory=datetime.now)
    active: bool = True


class NotificationStore:
    """Storage for webhook subscriptions"""

    def __init__(self):
        self._subscriptions: dict[str, WebhookSubscription] = {}
        self._task_subscriptions: dict[str, list[str]] = {}  # task_id -> [subscription_ids]
        self._lock = asyncio.Lock()

    async def register(
        self,
        webhook_url: str,
        task_id: Optional[str] = None,
        events: Optional[list[str]] = None
    ) -> WebhookSubscription:
        """Register a new webhook subscription"""
        async with self._lock:
            sub_id = str(uuid.uuid4())
            secret = secrets.token_hex(32)

            subscription = WebhookSubscription(
                id=sub_id,
                webhook_url=webhook_url,
                secret=secret,
                task_id=task_id,
                events=events or ["task.completed", "task.failed"],
            )

            self._subscriptions[sub_id] = subscription

            # Index by task_id for fast lookup
            if task_id:
                if task_id not in self._task_subscriptions:
                    self._task_subscriptions[task_id] = []
                self._task_subscriptions[task_id].append(sub_id)

            logger.info(f"Registered webhook subscription {sub_id} for task={task_id}")
            return subscription

    async def get_subscriptions_for_task(self, task_id: str) -> list[WebhookSubscription]:
        """Get all active subscriptions for a task"""
        subscriptions = []

        # Task-specific subscriptions
        if task_id in self._task_subscriptions:
            for sub_id in self._task_subscriptions[task_id]:
                sub = self._subscriptions.get(sub_id)
                if sub and sub.active:
                    subscriptions.append(sub)

        # Global subscriptions (task_id=None)
        for sub in self._subscriptions.values():
            if sub.task_id is None and sub.active:
                subscriptions.append(sub)

        return subscriptions

    async def unregister(self, subscription_id: str) -> bool:
        """Unregister a subscription"""
        async with self._lock:
            if subscription_id in self._subscriptions:
                sub = self._subscriptions[subscription_id]
                sub.active = False

                # Remove from task index
                if sub.task_id and sub.task_id in self._task_subscriptions:
                    self._task_subscriptions[sub.task_id] = [
                        s for s in self._task_subscriptions[sub.task_id]
                        if s != subscription_id
                    ]

                del self._subscriptions[subscription_id]
                return True
            return False


# Global notification store
notifications = NotificationStore()


async def emit_notification(task_id: str, event: str, result: Optional[dict] = None):
    """
    Emit a push notification to all subscribers for a task.

    Args:
        task_id: The task that triggered the event
        event: Event type (e.g., "task.completed", "task.failed")
        result: Optional result data to include
    """
    subscriptions = await notifications.get_subscriptions_for_task(task_id)

    if not subscriptions:
        logger.debug(f"No subscriptions for task {task_id}")
        return

    logger.info(f"Emitting {event} notification for task {task_id} to {len(subscriptions)} subscriber(s)")

    for sub in subscriptions:
        if event not in sub.events:
            continue

        # Build notification payload
        payload = {
            "event": event,
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
            "agent": AGENT_NAME,
        }

        if result:
            payload["result"] = result

        # Start background task to deliver notification
        asyncio.create_task(
            deliver_notification(sub, payload)
        )


async def deliver_notification(
    subscription: WebhookSubscription,
    payload: dict,
    retry: int = 0
):
    """
    Deliver a notification to a webhook with retry logic.

    Uses exponential backoff: 1s, 2s, 4s, 8s, 16s
    """
    # Sign the payload
    payload_bytes = json.dumps(payload, sort_keys=True).encode()
    signature = hmac.new(
        subscription.secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Signature": f"sha256={signature}",
        "X-Subscription-Id": subscription.id,
        "X-Event": payload.get("event", "unknown"),
    }

    try:
        async with httpx.AsyncClient(timeout=NOTIFICATION_TIMEOUT) as client:
            response = await client.post(
                subscription.webhook_url,
                json=payload,
                headers=headers,
            )

            if response.status_code == 200:
                logger.info(f"Notification delivered to {subscription.webhook_url}")
                return True

            logger.warning(
                f"Notification delivery failed: {response.status_code} - {response.text[:100]}"
            )

    except Exception as e:
        logger.warning(f"Notification delivery error: {e}")

    # Retry with exponential backoff
    if retry < NOTIFICATION_MAX_RETRIES:
        delay = 2 ** retry  # 1, 2, 4, 8, 16 seconds
        logger.info(f"Retrying notification in {delay}s (attempt {retry + 1}/{NOTIFICATION_MAX_RETRIES})")
        await asyncio.sleep(delay)
        return await deliver_notification(subscription, payload, retry + 1)

    logger.error(f"Notification delivery failed after {NOTIFICATION_MAX_RETRIES} retries")
    return False


async def _post_callback(
    callback_url: str,
    task_id: str,
    tool_name: str,
    content: str,
    success: bool,
    session_id: Optional[str] = None,
) -> bool:
    """
    POST task result to callback URL (orchestrator webhook).

    Uses the orchestrator's expected format:
    {
        "task_id": "...",
        "agent": "AgentName",
        "tool": "tool_name",
        "success": true/false,
        "content": "result content",
        "session_id": "..." (optional, for routing)
    }
    """
    payload = {
        "task_id": task_id,
        "agent": AGENT_NAME,
        "tool": tool_name,
        "success": success,
        "content": content,
    }

    if session_id:
        payload["session_id"] = session_id

    try:
        async with httpx.AsyncClient(timeout=NOTIFICATION_TIMEOUT) as client:
            response = await client.post(callback_url, json=payload)

            if response.status_code == 200:
                logger.info(f"Callback delivered to {callback_url} for task {task_id}")
                return True

            logger.warning(f"Callback delivery failed: {response.status_code}")
            return False

    except Exception as e:
        logger.error(f"Callback delivery error: {e}")
        return False


# =============================================================================
# A2A Protocol Types
# =============================================================================

class TaskState(str, Enum):
    """A2A Task lifecycle states"""
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class PartType(str, Enum):
    """A2A Message Part types"""
    TEXT = "text"
    FILE = "file"
    DATA = "data"


class Part(BaseModel):
    """A2A Message Part - content unit within a message"""
    type: PartType = PartType.TEXT
    text: Optional[str] = None
    file: Optional[dict] = None  # {name, mimeType, bytes/uri}
    data: Optional[dict] = None  # Structured JSON data
    metadata: Optional[dict] = None


class Message(BaseModel):
    """A2A Message format"""
    role: str  # "user" or "agent"
    parts: list[Part]
    metadata: Optional[dict] = None


class Task(BaseModel):
    """A2A Task representation"""
    id: str
    state: TaskState = TaskState.SUBMITTED
    messages: list[Message] = []
    artifacts: list[dict] = []  # Output artifacts
    metadata: Optional[dict] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Skill(BaseModel):
    """Agent skill/capability"""
    id: str
    name: str
    description: str
    tags: list[str] = []
    examples: list[str] = []


class AgentCard(BaseModel):
    """A2A Agent Card for discovery"""
    name: str
    description: str
    url: str
    version: str = "1.0.0"
    capabilities: dict = {}
    skills: list[Skill] = []
    defaultInputModes: list[str] = ["text"]
    defaultOutputModes: list[str] = ["text"]
    provider: Optional[dict] = None


# =============================================================================
# JSON-RPC Types
# =============================================================================

class JSONRPCRequest(BaseModel):
    """JSON-RPC 2.0 Request"""
    jsonrpc: str = "2.0"
    method: str
    params: Optional[dict] = None
    id: Optional[str | int] = None


class JSONRPCResponse(BaseModel):
    """JSON-RPC 2.0 Response"""
    jsonrpc: str = "2.0"
    result: Optional[Any] = None
    error: Optional[dict] = None
    id: Optional[str | int] = None


class JSONRPCError:
    """Standard JSON-RPC error codes"""
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # A2A specific
    TASK_NOT_FOUND = -32001
    TASK_CANCELED = -32002


# =============================================================================
# Task Store
# =============================================================================

class TaskStore:
    """In-memory task state management"""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()

    async def create(self, task_id: Optional[str] = None) -> Task:
        async with self._lock:
            task_id = task_id or str(uuid.uuid4())
            task = Task(id=task_id)
            self._tasks[task_id] = task
            logger.debug(f"Created task {task_id}")
            return task

    async def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    async def update(self, task_id: str, **kwargs) -> Optional[Task]:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task:
                for key, value in kwargs.items():
                    if hasattr(task, key):
                        setattr(task, key, value)
                task.updated_at = datetime.now()
                logger.debug(f"Updated task {task_id}: {kwargs}")
            return task

    async def add_message(self, task_id: str, message: Message) -> Optional[Task]:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.messages.append(message)
                task.updated_at = datetime.now()
            return task

    async def list_tasks(self, state: Optional[TaskState] = None) -> list[Task]:
        tasks = list(self._tasks.values())
        if state:
            tasks = [t for t in tasks if t.state == state]
        return sorted(tasks, key=lambda t: t.updated_at, reverse=True)

    async def delete(self, task_id: str) -> bool:
        async with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                return True
            return False


# =============================================================================
# Bridge State
# =============================================================================

class BridgeState:
    """Maintains bridge state and Ollama connection health"""

    def __init__(self):
        self.healthy = False
        self.last_health_check: Optional[datetime] = None
        self.available_models: list[str] = []
        self.tasks = TaskStore()

    async def check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{OLLAMA_BASE}/api/tags")
                if response.status_code == 200:
                    data = response.json()
                    self.available_models = [m["name"] for m in data.get("models", [])]
                    self.healthy = True
                    self.last_health_check = datetime.now()
                    return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self.healthy = False
        return False


state = BridgeState()


# =============================================================================
# FastAPI App
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting A2A-Ollama Bridge v2 on {BRIDGE_HOST}:{BRIDGE_PORT}")
    logger.info(f"Ollama endpoint: {OLLAMA_BASE}")
    logger.info(f"Agent: {AGENT_NAME}")

    if await state.check_health():
        logger.info(f"Connected to Ollama. Models: {len(state.available_models)}")
    else:
        logger.warning("Could not connect to Ollama on startup")

    yield
    logger.info("Shutting down A2A-Ollama Bridge v2")


app = FastAPI(
    title="A2A-Ollama Bridge v2",
    description="Full A2A protocol compliance with native Ollama MCP",
    version="2.0.0",
    lifespan=lifespan
)


# =============================================================================
# A2A Discovery
# =============================================================================

@app.get("/.well-known/agent.json")
async def agent_card():
    """A2A Agent Card for discovery"""
    base_url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"

    # Build skills from config file or fall back to defaults
    if _agent_config and "skills" in _agent_config:
        skills = [
            Skill(
                id=s["id"],
                name=s["name"],
                description=s["description"],
                tags=s.get("tags", []),
                examples=s.get("examples", []),
            )
            for s in _agent_config["skills"]
        ]
    else:
        skills = [
            Skill(
                id="code-generation",
                name="Code Generation",
                description="Generate, modify, and explain code in multiple languages",
                tags=["coding", "programming", "development"],
                examples=[
                    "Write a Python function to parse JSON",
                    "Refactor this code to be more efficient",
                ]
            ),
            Skill(
                id="file-operations",
                name="File Operations",
                description="Read, write, and manage files via MCP filesystem tools",
                tags=["filesystem", "mcp", "files"],
                examples=[
                    "Read the contents of config.json",
                    "Create a new file with the given content",
                ]
            ),
            Skill(
                id="code-analysis",
                name="Code Analysis",
                description="Analyze codebases, find patterns, and suggest improvements",
                tags=["analysis", "review", "debugging"],
                examples=[
                    "Find all TODO comments in this project",
                    "What does this function do?",
                ]
            ),
        ]

    return AgentCard(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        url=base_url,
        version="2.0.0",
        capabilities={
            "streaming": True,
            "pushNotifications": True,
            "pushNotificationConfig": {
                "endpoint": f"{base_url}/notifications/register",
                "events": ["task.completed", "task.failed", "task.progress"],
            },
            "stateTransitionHistory": True,
        },
        skills=skills,
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        provider={
            "organization": "Local",
            "url": base_url,
        }
    ).model_dump()


@app.get("/health")
async def health():
    """Health check endpoint"""
    is_healthy = await state.check_health()
    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "ollama_connected": is_healthy,
        "last_check": state.last_health_check.isoformat() if state.last_health_check else None,
        "available_models": state.available_models,
        "active_tasks": len(await state.tasks.list_tasks()),
    }


# =============================================================================
# Push Notification Endpoints
# =============================================================================

class NotificationRegistration(BaseModel):
    """Request body for webhook registration"""
    webhook_url: str
    task_id: Optional[str] = None  # None = subscribe to all tasks
    events: Optional[list[str]] = None  # Default: ["task.completed", "task.failed"]


@app.post("/notifications/register")
async def register_notification(request: NotificationRegistration):
    """
    Register a webhook for push notifications.

    The webhook will receive POST requests with:
    - X-Signature: HMAC-SHA256 signature of the payload
    - X-Subscription-Id: The subscription ID
    - X-Event: The event type

    Returns:
        subscription_id: ID for this subscription
        secret: HMAC secret for verifying signatures
    """
    subscription = await notifications.register(
        webhook_url=request.webhook_url,
        task_id=request.task_id,
        events=request.events,
    )

    return {
        "subscription_id": subscription.id,
        "secret": subscription.secret,
        "events": subscription.events,
        "message": "Webhook registered successfully",
    }


@app.delete("/notifications/{subscription_id}")
async def unregister_notification(subscription_id: str):
    """Unregister a webhook subscription"""
    success = await notifications.unregister(subscription_id)

    if success:
        return {"message": "Subscription removed"}
    else:
        return JSONResponse(
            status_code=404,
            content={"error": "Subscription not found"}
        )


# =============================================================================
# A2A JSON-RPC Endpoint
# =============================================================================

@app.post("/a2a")
async def a2a_jsonrpc(request: Request):
    """
    A2A JSON-RPC endpoint

    Methods:
        message/send    - Send message to create/continue task
        message/stream  - Send message with streaming response
        tasks/get       - Get task by ID
        tasks/list      - List tasks
        tasks/cancel    - Cancel a task
    """
    try:
        body = await request.json()
        rpc_request = JSONRPCRequest(**body)
    except Exception as e:
        return jsonrpc_error(None, JSONRPCError.PARSE_ERROR, f"Parse error: {e}")

    method = rpc_request.method
    params = rpc_request.params or {}
    request_id = rpc_request.id

    logger.debug(f"JSON-RPC: {method} params={params}")

    # Route to handler
    handlers = {
        "message/send": handle_message_send,
        "message/stream": handle_message_stream,
        "tasks/get": handle_tasks_get,
        "tasks/list": handle_tasks_list,
        "tasks/cancel": handle_tasks_cancel,
    }

    handler = handlers.get(method)
    if not handler:
        return jsonrpc_error(request_id, JSONRPCError.METHOD_NOT_FOUND, f"Unknown method: {method}")

    try:
        result = await handler(params)

        # Streaming responses are handled differently
        if isinstance(result, StreamingResponse):
            return result

        return JSONResponse(content=JSONRPCResponse(
            id=request_id,
            result=result
        ).model_dump(exclude_none=True))

    except Exception as e:
        logger.error(f"Handler error: {e}", exc_info=True)
        return jsonrpc_error(request_id, JSONRPCError.INTERNAL_ERROR, str(e))


def jsonrpc_error(request_id: Optional[str | int], code: int, message: str) -> JSONResponse:
    """Create JSON-RPC error response"""
    return JSONResponse(content=JSONRPCResponse(
        id=request_id,
        error={"code": code, "message": message}
    ).model_dump(exclude_none=True))


# =============================================================================
# JSON-RPC Handlers
# =============================================================================

async def handle_message_send(params: dict) -> dict:
    """
    Handle message/send - create or continue a task

    Params:
        task_id: Optional[str] - existing task to continue
        message: Message - the message to send
        model: Optional[str] - model override
        tools_path: Optional[str] - MCP tools path
        mcp_servers: Optional[list] - explicit MCP servers
    """
    task_id = params.get("task_id")
    message_data = params.get("message", {})
    model = params.get("model", DEFAULT_MODEL)
    tools_path = params.get("tools_path", DEFAULT_TOOLS_PATH)
    mcp_servers = params.get("mcp_servers")

    # Get or create task
    if task_id:
        task = await state.tasks.get(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
    else:
        task = await state.tasks.create()
        task_id = task.id

    # Parse incoming message
    message = parse_message(message_data)
    await state.tasks.add_message(task_id, message)

    # Update task state
    await state.tasks.update(task_id, state=TaskState.WORKING)

    # Build Ollama request
    ollama_messages = build_ollama_messages(task.messages)
    ollama_request = {
        "model": model,
        "messages": ollama_messages,
        "stream": False,
        "task_id": task_id,
    }

    # Add MCP config
    if mcp_servers:
        ollama_request["mcp_servers"] = mcp_servers
    elif tools_path:
        ollama_request["tools_path"] = tools_path

    if tools_path or mcp_servers:
        ollama_request["max_tool_rounds"] = params.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)
        ollama_request["tool_timeout"] = params.get("tool_timeout", DEFAULT_TOOL_TIMEOUT)

    # Execute
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json=ollama_request,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                await state.tasks.update(task_id, state=TaskState.FAILED)
                raise ValueError(f"Ollama error: {response.text}")

            data = response.json()

            # Extract response
            msg = data.get("message", {})
            content = msg.get("content", "")
            task_status = data.get("task_status", "completed")

            # Create agent response message
            agent_message = Message(
                role="agent",
                parts=[Part(type=PartType.TEXT, text=content)]
            )
            await state.tasks.add_message(task_id, agent_message)

            # Update task state based on Ollama's task_status
            final_state = TaskState.COMPLETED if task_status == "completed" else TaskState.WORKING
            await state.tasks.update(task_id, state=final_state)

            # Get updated task
            task = await state.tasks.get(task_id)

            return {
                "task": task_to_dict(task),
                "message": agent_message.model_dump(),
            }

    except httpx.TimeoutException:
        await state.tasks.update(task_id, state=TaskState.FAILED)
        raise ValueError("Request timed out")
    except httpx.ConnectError:
        await state.tasks.update(task_id, state=TaskState.FAILED)
        raise ValueError("Failed to connect to Ollama")


async def handle_message_stream(params: dict) -> StreamingResponse:
    """
    Handle message/stream - streaming response via SSE

    Returns Server-Sent Events with JSON-RPC notifications
    """
    task_id = params.get("task_id")
    message_data = params.get("message", {})
    model = params.get("model", DEFAULT_MODEL)
    tools_path = params.get("tools_path", DEFAULT_TOOLS_PATH)
    mcp_servers = params.get("mcp_servers")

    # Get or create task
    if task_id:
        task = await state.tasks.get(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
    else:
        task = await state.tasks.create()
        task_id = task.id

    # Parse and add message
    message = parse_message(message_data)
    await state.tasks.add_message(task_id, message)
    await state.tasks.update(task_id, state=TaskState.WORKING)

    # Build request
    ollama_messages = build_ollama_messages(task.messages)
    ollama_request = {
        "model": model,
        "messages": ollama_messages,
        "stream": True,
        "task_id": task_id,
    }

    if mcp_servers:
        ollama_request["mcp_servers"] = mcp_servers
    elif tools_path:
        ollama_request["tools_path"] = tools_path

    if tools_path or mcp_servers:
        ollama_request["max_tool_rounds"] = params.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)

    return StreamingResponse(
        stream_response(task_id, ollama_request),
        media_type="text/event-stream"
    )


async def stream_response(task_id: str, ollama_request: dict) -> AsyncGenerator[str, None]:
    """Stream Ollama response as SSE events"""
    accumulated_content = ""

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_BASE}/api/chat",
                json=ollama_request,
                timeout=REQUEST_TIMEOUT
            ) as response:

                if response.status_code != 200:
                    error_text = await response.aread()
                    await state.tasks.update(task_id, state=TaskState.FAILED)
                    yield sse_event("error", {"error": error_text.decode()})
                    return

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = chunk.get("message", {})
                    content = msg.get("content", "")
                    done = chunk.get("done", False)
                    task_status = chunk.get("task_status", "working")

                    if content:
                        accumulated_content += content
                        yield sse_event("content", {
                            "task_id": task_id,
                            "content": content,
                            "task_status": task_status,
                        })

                    if done:
                        # Save complete message
                        agent_message = Message(
                            role="agent",
                            parts=[Part(type=PartType.TEXT, text=accumulated_content)]
                        )
                        await state.tasks.add_message(task_id, agent_message)

                        # Update state
                        final_state = TaskState.COMPLETED if task_status == "completed" else TaskState.WORKING
                        await state.tasks.update(task_id, state=final_state)

                        task = await state.tasks.get(task_id)
                        yield sse_event("done", {
                            "task": task_to_dict(task),
                        })

    except Exception as e:
        await state.tasks.update(task_id, state=TaskState.FAILED)
        yield sse_event("error", {"error": str(e)})


def sse_event(event_type: str, data: dict) -> str:
    """Format SSE event"""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def handle_tasks_get(params: dict) -> dict:
    """Get task by ID"""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("task_id required")

    task = await state.tasks.get(task_id)
    if not task:
        raise ValueError(f"Task not found: {task_id}")

    return {"task": task_to_dict(task)}


async def handle_tasks_list(params: dict) -> dict:
    """List tasks with optional state filter"""
    state_filter = params.get("state")
    task_state = TaskState(state_filter) if state_filter else None

    tasks = await state.tasks.list_tasks(task_state)
    return {
        "tasks": [task_to_dict(t) for t in tasks],
        "count": len(tasks),
    }


async def handle_tasks_cancel(params: dict) -> dict:
    """Cancel a task"""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("task_id required")

    task = await state.tasks.get(task_id)
    if not task:
        raise ValueError(f"Task not found: {task_id}")

    await state.tasks.update(task_id, state=TaskState.CANCELED)
    task = await state.tasks.get(task_id)

    return {"task": task_to_dict(task)}


# =============================================================================
# Helpers
# =============================================================================

def parse_message(data: dict) -> Message:
    """Parse message from request data"""
    role = data.get("role", "user")

    # Handle parts or simple content
    if "parts" in data:
        parts = [Part(**p) for p in data["parts"]]
    elif "content" in data:
        parts = [Part(type=PartType.TEXT, text=data["content"])]
    else:
        parts = []

    return Message(role=role, parts=parts, metadata=data.get("metadata"))


def build_ollama_messages(messages: list[Message]) -> list[dict]:
    """Convert A2A messages to Ollama format"""
    ollama_messages = []

    for msg in messages:
        # Extract text content from parts
        content = ""
        for part in msg.parts:
            if part.type == PartType.TEXT and part.text:
                content += part.text

        ollama_messages.append({
            "role": msg.role if msg.role != "agent" else "assistant",
            "content": content,
        })

    return ollama_messages


def task_to_dict(task: Task) -> dict:
    """Serialize task for JSON response"""
    return {
        "id": task.id,
        "state": task.state.value,
        "messages": [m.model_dump() for m in task.messages],
        "artifacts": task.artifacts,
        "metadata": task.metadata,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


# =============================================================================
# MCP Endpoint (streamable-http transport)
# =============================================================================

# MCP session storage
mcp_sessions: dict[str, dict] = {}


def get_mcp_tools() -> list[dict]:
    """
    Convert agent skills to MCP tool format.
    Each skill becomes an MCP tool that can be called by other agents.

    If STATIC_TOOLS env var is set, only return those tools (for curated context).
    Otherwise return all tools (full JIT discovery mode).
    """
    # Define all available tools
    # These map to internal A2A task execution
    # All tools run async by default - they return a task_id immediately
    # and the caller should use get_task_result to retrieve results
    all_tools = {
        "invoke": {
            "name": "invoke",
            "description": f"Invoke {AGENT_NAME} ({AGENT_DESCRIPTION}) to perform a task. Returns a task_id immediately - use get_task_result to retrieve the result when ready. Continue talking to the user while the task runs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task or question for the agent to handle"
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional additional context or background information"
                    }
                },
                "required": ["task"]
            }
        },
        "generate_code": {
            "name": "generate_code",
            "description": "Generate code based on a description. Returns a task_id immediately - use get_task_result to retrieve the generated code. Tell the user you've started working on it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What the code should do"
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language (e.g., python, javascript, go)"
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional existing code or context"
                    }
                },
                "required": ["description"]
            }
        },
        "analyze_code": {
            "name": "analyze_code",
            "description": "Analyze code for bugs, improvements, or explanations. Returns a task_id immediately - use get_task_result to retrieve the analysis.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The code to analyze"
                    },
                    "analysis_type": {
                        "type": "string",
                        "description": "Type of analysis: 'review', 'explain', 'debug', 'optimize'",
                        "enum": ["review", "explain", "debug", "optimize"]
                    }
                },
                "required": ["code"]
            }
        },
        "answer_question": {
            "name": "answer_question",
            "description": "Answer a question or provide information. Returns a task_id immediately - use get_task_result to retrieve the answer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to answer"
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context or background for the question"
                    }
                },
                "required": ["question"]
            }
        },
        "get_task_result": {
            "name": "get_task_result",
            "description": "Get the result of a pending task. Call this after invoking another tool to retrieve the result. If the task is still running, you'll be told to wait.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID returned from a previous tool call"
                    }
                },
                "required": ["task_id"]
            }
        }
    }

    # Filter to static tools if configured, otherwise return all
    if STATIC_TOOLS_LIST:
        tools = [all_tools[name] for name in STATIC_TOOLS_LIST if name in all_tools]
        logger.info(f"Exposing {len(tools)} static tools: {STATIC_TOOLS_LIST}")
    else:
        tools = list(all_tools.values())

    return tools


def build_prompt_from_tool(tool_name: str, arguments: dict) -> Optional[str]:
    """Build a prompt from tool name and arguments."""
    if tool_name == "invoke":
        prompt = arguments.get("task", "")
        if context := arguments.get("context"):
            prompt = f"Context: {context}\n\nTask: {prompt}"
        return prompt

    elif tool_name == "generate_code":
        desc = arguments.get("description", "")
        lang = arguments.get("language", "")
        context = arguments.get("context", "")
        prompt = f"Generate {lang + ' ' if lang else ''}code: {desc}"
        if context:
            prompt += f"\n\nExisting context:\n{context}"
        return prompt

    elif tool_name == "analyze_code":
        code = arguments.get("code", "")
        analysis_type = arguments.get("analysis_type", "review")
        return f"Please {analysis_type} the following code:\n\n```\n{code}\n```"

    elif tool_name == "answer_question":
        question = arguments.get("question", "")
        context = arguments.get("context", "")
        prompt = question
        if context:
            prompt = f"Context: {context}\n\nQuestion: {question}"
        return prompt

    elif tool_name == "get_task_result":
        # Special tool - handled separately
        return None

    return None


# Tool definition for worker agents to send structured responses
RESPOND_TO_MANAGER_TOOL = {
    "type": "function",
    "function": {
        "name": "respond_to_manager",
        "description": "Send your final response to the manager agent. Use this tool ONCE when you have completed the task. Include only the essential result - no reasoning or intermediate steps.",
        "parameters": {
            "type": "object",
            "properties": {
                "response": {
                    "type": "string",
                    "description": "Your concise final response to the manager. Include the result, any code, or answer requested."
                },
                "success": {
                    "type": "boolean",
                    "description": "Whether the task was completed successfully",
                    "default": True
                }
            },
            "required": ["response"]
        }
    }
}

# System prompt for worker agents
WORKER_SYSTEM_PROMPT = """You are a worker agent executing a task for a manager agent.

IMPORTANT: When you have completed the task, you MUST call the `respond_to_manager` tool with your final response.
- Include ONLY the essential result (code, answer, etc.)
- Do NOT include your reasoning or thought process
- Keep the response concise and directly usable by the manager
- Call the tool exactly ONCE when done"""

# Extended system prompt when worker has MCP tools available
WORKER_MCP_SYSTEM_PROMPT = """You are a worker agent executing a task for a manager agent.
You have access to external tools via MCP servers.

Steps:
1. Call mcp_discover with pattern "*" to see ALL available tools
2. Use the discovered tools to complete the task
3. After getting results from tools, respond with ONLY the essential data

CRITICAL RULES:
- Always use pattern "*" with mcp_discover (not a specific keyword)
- After discovering tools, call them to get real data — do NOT make up answers
- Your final text response must contain ONLY the result data, no reasoning or filler
- Be concise — the manager will present your results to the user"""


async def execute_task_background(
    task_id: str,
    prompt: str,
    tool_name: str = "invoke",
    callback_url: Optional[str] = None,
    session_id: Optional[str] = None,
):
    """
    Execute a task in the background and store result.

    Uses the respond_to_manager tool to get structured, concise responses
    from the worker agent instead of full message content.

    If callback_url is provided, POSTs result to that URL when complete.
    """
    logger.info(f"Background task started: {task_id} session={session_id or 'none'} (callback={callback_url is not None})")

    try:
        # Call Ollama directly with respond_to_manager tool
        # This bypasses the A2A message/send flow for efficiency
        system_prompt = WORKER_MCP_SYSTEM_PROMPT if WORKER_MCP_SERVERS else WORKER_SYSTEM_PROMPT

        ollama_request = {
            "model": DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }

        # Give worker access to remote MCP servers for JIT tool discovery
        if WORKER_MCP_SERVERS:
            # MCP mode: Ollama handles all tool calls via MCP, so we can't
            # mix in native tools like respond_to_manager. The worker will
            # return its answer as message content instead.
            ollama_request["mcp_servers"] = WORKER_MCP_SERVERS
            ollama_request["max_tool_rounds"] = DEFAULT_MAX_TOOL_ROUNDS
            ollama_request["jit_max_tools"] = 20  # Show all remote tools
            logger.info(f"Worker has {len(WORKER_MCP_SERVERS)} MCP server(s): "
                       f"{[s.get('name', '?') for s in WORKER_MCP_SERVERS]}")
        else:
            # No MCP servers: use respond_to_manager tool for structured output
            ollama_request["tools"] = [RESPOND_TO_MANAGER_TOOL]

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json=ollama_request,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                raise ValueError(f"Ollama error: {response.text}")

            data = response.json()
            msg = data.get("message", {})

            # Try to extract response from tool call first
            content = ""
            success = True
            tool_calls = msg.get("tool_calls", [])

            for tc in tool_calls:
                func = tc.get("function", {})
                if func.get("name") == "respond_to_manager":
                    args = func.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except:
                            args = {"response": args}
                    content = args.get("response", "")
                    success = args.get("success", True)
                    logger.info(f"Worker used respond_to_manager tool (success={success})")
                    break

            # Fallback to message content if no tool call
            if not content:
                content = msg.get("content", "")
                if content:
                    logger.info(f"Worker response (message content): {content[:500]}")

        # Update task state
        await state.tasks.update(task_id, state=TaskState.COMPLETED)

        # Store result in task messages for get_task_result
        agent_message = Message(
            role="agent",
            parts=[Part(type=PartType.TEXT, text=content)]
        )
        await state.tasks.add_message(task_id, agent_message)

        logger.info(f"Background task completed: {task_id} (len={len(content)})")

        # Emit push notification (subscription-based)
        await emit_notification(
            task_id,
            "task.completed" if success else "task.failed",
            result={"content": content}
        )

        # POST to callback URL if provided (orchestrator integration)
        if callback_url:
            await _post_callback(
                callback_url,
                task_id=task_id,
                tool_name=tool_name,
                content=content,
                success=success,
                session_id=session_id,
            )

    except Exception as e:
        logger.error(f"Background task failed: {task_id} - {e}", exc_info=True)
        await state.tasks.update(task_id, state=TaskState.FAILED)

        # Emit failure notification (subscription-based)
        await emit_notification(
            task_id,
            "task.failed",
            result={"error": str(e)}
        )

        # POST to callback URL if provided
        if callback_url:
            await _post_callback(
                callback_url,
                task_id=task_id,
                tool_name=tool_name,
                content=str(e),
                success=False,
                session_id=session_id,
            )


async def handle_mcp_tool_call(
    tool_name: str,
    arguments: dict,
    async_mode: bool = False,
    callback_url: Optional[str] = None,
    external_task_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Execute an MCP tool call by creating an internal A2A task.

    Args:
        tool_name: Name of the tool to execute
        arguments: Tool arguments
        async_mode: If True, return immediately with task_id
        callback_url: URL to POST result when task completes (orchestrator integration)
        external_task_id: Task ID provided by orchestrator for tracking
        session_id: Orchestrator session ID for webhook routing

    Returns:
        Tuple of (result_text, task_id). task_id is set for async calls.
    """
    # Handle get_task_result specially
    if tool_name == "get_task_result":
        task_id = arguments.get("task_id", "")
        if not task_id:
            return "Error: task_id is required", None

        task = await state.tasks.get(task_id)
        if not task:
            return f"Error: Task {task_id} not found", None

        if task.state == TaskState.WORKING:
            return f"Task {task_id} is still running. Please wait and check again.", None
        elif task.state == TaskState.FAILED:
            return f"Task {task_id} failed.", None
        elif task.state == TaskState.COMPLETED:
            # Extract result from last agent message
            for msg in reversed(task.messages):
                if msg.role == "agent":
                    content = ""
                    for part in msg.parts:
                        if part.type == PartType.TEXT and part.text:
                            content += part.text
                    return content if content else "Task completed with no output.", None
            return "Task completed but no result found.", None
        else:
            return f"Task {task_id} status: {task.state.value}", None

    # Build prompt from tool
    prompt = build_prompt_from_tool(tool_name, arguments)
    if prompt is None:
        return f"Unknown tool: {tool_name}", None

    if async_mode:
        # Use external task ID if provided (from orchestrator), otherwise generate
        task = await state.tasks.create(task_id=external_task_id)
        task_id = task.id

        # Start background task with callback URL for orchestrator integration
        asyncio.create_task(execute_task_background(
            task_id=task_id,
            prompt=prompt,
            tool_name=tool_name,
            callback_url=callback_url,
            session_id=session_id,
        ))

        return f"Task {task_id} is now running in the background. Continue talking to the user. Call get_task_result(task_id=\"{task_id}\") when ready to retrieve the result.", task_id

    else:
        # Synchronous execution (original behavior)
        # Note: Don't use tools_path here to avoid nested MCP calls
        # which can timeout. The worker should answer directly.
        params = {
            "message": {"role": "user", "content": prompt},
            "model": DEFAULT_MODEL,
            "tools_path": None,  # Explicitly None to avoid nested MCP
        }

        try:
            result = await handle_message_send(params)

            # Extract text content from response
            message = result.get("message", {})
            content = ""
            for part in message.get("parts", []):
                if part.get("type") == "text":
                    content += part.get("text", "")

            return content if content else "Task completed with no output.", None

        except Exception as e:
            logger.error(f"MCP tool call failed: {e}", exc_info=True)
            return f"Error executing tool: {e}", None


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """
    MCP streamable-http endpoint.

    Handles MCP JSON-RPC protocol for tool discovery and execution.
    This allows other agents to discover and invoke this agent's capabilities.

    Query params (for orchestrator integration):
        session_id: Orchestrator session ID for webhook routing
        webhook: Webhook URL to POST results to

    Methods:
        initialize          - Initialize MCP session
        notifications/initialized - Client initialized notification
        tools/list          - List available tools
        tools/call          - Execute a tool
    """
    # Get MCP session ID from header
    mcp_session_id = request.headers.get("mcp-session-id", "")

    # Get orchestrator integration params from query string
    orch_session_id = request.query_params.get("session_id")
    orch_webhook = request.query_params.get("webhook")

    try:
        body = await request.json()
    except Exception as e:
        return mcp_error_response(None, -32700, f"Parse error: {e}")

    # Handle JSON-RPC request
    jsonrpc = body.get("jsonrpc", "")
    if jsonrpc != "2.0":
        return mcp_error_response(body.get("id"), -32600, "Invalid JSON-RPC version")

    method = body.get("method", "")
    params = body.get("params", {})
    request_id = body.get("id")  # None for notifications

    logger.debug(f"MCP request: method={method} id={request_id} mcp_session={mcp_session_id[:8] if mcp_session_id else 'none'} orch_session={orch_session_id or 'none'}")

    # Route to handler
    if method == "initialize":
        # Create new session
        new_session_id = str(uuid.uuid4())
        mcp_sessions[new_session_id] = {
            "created": datetime.now().isoformat(),
            "initialized": False,
        }

        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": AGENT_NAME,
                "version": "2.0.0",
            }
        }

        response = mcp_success_response(request_id, result)
        response.headers["mcp-session-id"] = new_session_id
        return response

    elif method == "notifications/initialized":
        # Mark session as initialized (notification, no response)
        if mcp_session_id and mcp_session_id in mcp_sessions:
            mcp_sessions[mcp_session_id]["initialized"] = True
        # Notifications don't get responses
        return JSONResponse(content={}, status_code=202)

    elif method == "tools/list":
        tools = get_mcp_tools()
        return mcp_success_response(request_id, {"tools": tools})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        # Extract orchestrator integration params (from arguments or query params)
        # Query params are set by orchestrator, arguments by direct MCP calls
        callback_url = arguments.pop("_webhook", None) or orch_webhook
        external_task_id = arguments.pop("_task_id", None)

        # All tools are async by default (except get_task_result which is instant)
        # This allows the manager to continue generation while tasks run
        # The _async param can be set to False to force sync (not recommended)
        if tool_name == "get_task_result":
            async_mode = False  # get_task_result is always sync (it's just a status check)
        else:
            async_mode = not arguments.pop("_sync", False)  # Default async, opt-out with _sync

        logger.info(f"MCP tool call: {tool_name} async={async_mode} callback={callback_url is not None} session={orch_session_id or 'none'}")

        # Execute the tool
        result_text, task_id = await handle_mcp_tool_call(
            tool_name,
            arguments,
            async_mode=async_mode,
            callback_url=callback_url,
            external_task_id=external_task_id,
            session_id=orch_session_id,
        )

        # Return MCP tool result format
        result = {
            "content": [
                {"type": "text", "text": result_text}
            ],
            "isError": False,
        }

        # Include task_id for async calls
        if task_id:
            result["metadata"] = {"task_id": task_id, "async": True}

        return mcp_success_response(request_id, result)

    elif method == "tasks/get":
        # MCP method to get task status (alternative to get_task_result tool)
        task_id = params.get("task_id", "")
        if not task_id:
            return mcp_error_response(request_id, -32602, "task_id is required")

        task = await state.tasks.get(task_id)
        if not task:
            return mcp_error_response(request_id, -32001, f"Task {task_id} not found")

        return mcp_success_response(request_id, {"task": task_to_dict(task)})

    else:
        return mcp_error_response(request_id, -32601, f"Method not found: {method}")


def mcp_success_response(request_id: Optional[str | int], result: Any) -> JSONResponse:
    """Create MCP JSON-RPC success response"""
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        },
        headers={"Content-Type": "application/json"}
    )


def mcp_error_response(request_id: Optional[str | int], code: int, message: str) -> JSONResponse:
    """Create MCP JSON-RPC error response"""
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        headers={"Content-Type": "application/json"}
    )


# =============================================================================
# Legacy Compatibility Endpoints
# =============================================================================

@app.post("/v1/chat")
async def legacy_chat(request: Request):
    """
    Legacy /v1/chat endpoint for backwards compatibility
    Translates to message/send or message/stream
    """
    body = await request.json()

    # Convert to A2A format
    messages = body.get("messages", [])
    if messages:
        last_message = messages[-1]
        a2a_message = {
            "role": last_message.get("role", "user"),
            "content": last_message.get("content", ""),
        }
    else:
        a2a_message = {"role": "user", "content": ""}

    params = {
        "message": a2a_message,
        "model": body.get("model", DEFAULT_MODEL),
        "tools_path": body.get("tools_path", DEFAULT_TOOLS_PATH),
        "mcp_servers": body.get("mcp_servers"),
    }

    if body.get("stream", True):
        return await handle_message_stream(params)
    else:
        result = await handle_message_send(params)

        # Convert back to legacy format
        task = result.get("task", {})
        message = result.get("message", {})
        content = ""
        for part in message.get("parts", []):
            if part.get("type") == "text":
                content += part.get("text", "")

        return JSONResponse(content={
            "content": content,
            "task_id": task.get("id"),
            "task_status": task.get("state"),
            "done": task.get("state") in ["completed", "failed"],
        })


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=BRIDGE_HOST,
        port=BRIDGE_PORT,
        log_level="debug" if os.getenv("DEBUG") else "info"
    )
