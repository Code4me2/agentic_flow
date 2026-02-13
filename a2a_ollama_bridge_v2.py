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
import json
import logging
import os
import sys
import uuid
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
DEFAULT_MODEL = os.getenv("BRIDGE_MODEL", "qwen3-coder:30b-a3b-q8_0")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))
AGENT_NAME = os.getenv("AGENT_NAME", "OllamaAgent")
AGENT_DESCRIPTION = os.getenv("AGENT_DESCRIPTION", "Local AI agent with native MCP tool support")

# MCP Configuration
DEFAULT_TOOLS_PATH = os.getenv("TOOLS_PATH", "")
DEFAULT_MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "15"))
DEFAULT_TOOL_TIMEOUT = int(os.getenv("TOOL_TIMEOUT", "30000"))


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

    return AgentCard(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        url=base_url,
        version="2.0.0",
        capabilities={
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        skills=[
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
        ],
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
    """
    # Define tools based on agent capabilities
    # These map to internal A2A task execution
    tools = [
        {
            "name": "invoke",
            "description": f"Invoke {AGENT_NAME} to perform a task. Send a natural language request and receive the result. Set _async=true to run in background and get a task_id for later retrieval.",
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
                    },
                    "_async": {
                        "type": "boolean",
                        "description": "If true, run task in background and return task_id immediately"
                    }
                },
                "required": ["task"]
            }
        },
        {
            "name": "generate_code",
            "description": "Generate code based on a description. Set _async=true to run in background.",
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
                    },
                    "_async": {
                        "type": "boolean",
                        "description": "If true, run in background and return task_id"
                    }
                },
                "required": ["description"]
            }
        },
        {
            "name": "analyze_code",
            "description": "Analyze code for bugs, improvements, or explanations. Set _async=true to run in background.",
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
                    },
                    "_async": {
                        "type": "boolean",
                        "description": "If true, run in background and return task_id"
                    }
                },
                "required": ["code"]
            }
        },
        {
            "name": "answer_question",
            "description": "Answer a question or provide information on a topic. Set _async=true to run in background.",
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
                    },
                    "_async": {
                        "type": "boolean",
                        "description": "If true, run in background and return task_id"
                    }
                },
                "required": ["question"]
            }
        },
        {
            "name": "get_task_result",
            "description": "Get the result of an async task. Use this to poll for results after starting an async task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID returned from an async tool call"
                    }
                },
                "required": ["task_id"]
            }
        }
    ]
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


async def execute_task_background(task_id: str, prompt: str):
    """Execute a task in the background and store result."""
    logger.info(f"Background task started: {task_id}")

    params = {
        "task_id": task_id,
        "message": {"role": "user", "content": prompt},
        "model": DEFAULT_MODEL,
        "tools_path": DEFAULT_TOOLS_PATH,
    }

    try:
        result = await handle_message_send(params)

        # Extract text content from response
        message = result.get("message", {})
        content = ""
        for part in message.get("parts", []):
            if part.get("type") == "text":
                content += part.get("text", "")

        # Task is already updated by handle_message_send
        logger.info(f"Background task completed: {task_id}")

    except Exception as e:
        logger.error(f"Background task failed: {task_id} - {e}", exc_info=True)
        await state.tasks.update(task_id, state=TaskState.FAILED)


async def handle_mcp_tool_call(tool_name: str, arguments: dict, async_mode: bool = False) -> tuple[str, Optional[str]]:
    """
    Execute an MCP tool call by creating an internal A2A task.

    Args:
        tool_name: Name of the tool to execute
        arguments: Tool arguments
        async_mode: If True, return immediately with task_id

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
        # Create task and start background execution
        task = await state.tasks.create()
        task_id = task.id

        # Start background task
        asyncio.create_task(execute_task_background(task_id, prompt))

        return f"Task {task_id} submitted. Use get_task_result with this task_id to retrieve the result when complete.", task_id

    else:
        # Synchronous execution (original behavior)
        params = {
            "message": {"role": "user", "content": prompt},
            "model": DEFAULT_MODEL,
            "tools_path": DEFAULT_TOOLS_PATH,
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

    Methods:
        initialize          - Initialize MCP session
        notifications/initialized - Client initialized notification
        tools/list          - List available tools
        tools/call          - Execute a tool
    """
    # Get or create session ID
    session_id = request.headers.get("mcp-session-id", "")

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

    logger.debug(f"MCP request: method={method} id={request_id} session={session_id[:8] if session_id else 'none'}...")

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
        if session_id and session_id in mcp_sessions:
            mcp_sessions[session_id]["initialized"] = True
        # Notifications don't get responses
        return JSONResponse(content={}, status_code=202)

    elif method == "tools/list":
        tools = get_mcp_tools()
        return mcp_success_response(request_id, {"tools": tools})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        # Check for async mode - can be in arguments or params
        async_mode = arguments.pop("_async", False) or params.get("async", False)

        logger.info(f"MCP tool call: {tool_name} async={async_mode} args={list(arguments.keys())}")

        # Execute the tool
        result_text, task_id = await handle_mcp_tool_call(tool_name, arguments, async_mode=async_mode)

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
