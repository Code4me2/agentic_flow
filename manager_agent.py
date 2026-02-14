"""
ADK-Free Manager Agent

Orchestrates tasks across multiple A2A-compatible agents without Google dependencies.
Uses pure Python with httpx for agent discovery and communication.

Features:
    - A2A agent discovery via /.well-known/agent.json
    - Task delegation based on agent skills
    - Multi-agent coordination
    - Streaming response aggregation
    - Async task delegation with turn-boundary result injection
    - Push notifications via webhooks (replaces polling)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, AsyncGenerator, Callable

import httpx
from dotenv import load_dotenv

# Optional FastAPI for webhook server
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("manager_agent")

# Configuration
OLLAMA_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
MANAGER_MODEL = os.getenv("MANAGER_MODEL", "llama3.3:70b")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))
ASYNC_POLL_INTERVAL = float(os.getenv("ASYNC_POLL_INTERVAL", "2.0"))  # seconds

# Webhook server configuration (for receiving push notifications)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8001"))
WEBHOOK_PUBLIC_URL = os.getenv("WEBHOOK_PUBLIC_URL", f"http://localhost:{WEBHOOK_PORT}")
USE_PUSH_NOTIFICATIONS = os.getenv("USE_PUSH_NOTIFICATIONS", "true").lower() == "true"


# =============================================================================
# Async Task Tracking
# =============================================================================

class AsyncTaskState(str, Enum):
    """State of an async delegated task"""
    PENDING = "pending"      # Submitted, waiting for completion
    COMPLETED = "completed"  # Result available
    FAILED = "failed"        # Task failed
    INJECTED = "injected"    # Result has been injected into conversation


@dataclass
class AsyncTaskInfo:
    """Tracks an async task delegated to an agent"""
    task_id: str
    agent_name: str
    agent_url: str
    original_request: str
    state: AsyncTaskState = AsyncTaskState.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    submitted_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    # Webhook subscription info
    subscription_id: Optional[str] = None
    subscription_secret: Optional[str] = None


# =============================================================================
# Agent Registry
# =============================================================================

@dataclass
class AgentInfo:
    """Discovered A2A agent information"""
    name: str
    url: str
    description: str
    skills: list[dict] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    healthy: bool = True
    last_check: Optional[datetime] = None


class AgentRegistry:
    """Registry of discovered A2A agents"""

    def __init__(self):
        self._agents: dict[str, AgentInfo] = {}

    async def discover(self, url: str) -> Optional[AgentInfo]:
        """Discover agent at URL via A2A agent card"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Fetch agent card
                card_url = f"{url.rstrip('/')}/.well-known/agent.json"
                response = await client.get(card_url)

                if response.status_code != 200:
                    logger.warning(f"Failed to discover agent at {url}: {response.status_code}")
                    return None

                card = response.json()

                agent = AgentInfo(
                    name=card.get("name", "Unknown"),
                    url=url,
                    description=card.get("description", ""),
                    skills=card.get("skills", []),
                    capabilities=card.get("capabilities", {}),
                    healthy=True,
                    last_check=datetime.now(),
                )

                self._agents[agent.name] = agent
                logger.info(f"Discovered agent: {agent.name} at {url}")
                logger.debug(f"  Skills: {[s.get('name') for s in agent.skills]}")

                return agent

        except Exception as e:
            logger.error(f"Discovery failed for {url}: {e}")
            return None

    async def discover_many(self, urls: list[str]) -> list[AgentInfo]:
        """Discover multiple agents concurrently"""
        tasks = [self.discover(url) for url in urls]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    def get(self, name: str) -> Optional[AgentInfo]:
        """Get agent by name"""
        return self._agents.get(name)

    def list_agents(self) -> list[AgentInfo]:
        """List all registered agents"""
        return list(self._agents.values())

    def find_by_skill(self, skill_tag: str) -> list[AgentInfo]:
        """Find agents with matching skill tag"""
        matching = []
        for agent in self._agents.values():
            for skill in agent.skills:
                if skill_tag.lower() in [t.lower() for t in skill.get("tags", [])]:
                    matching.append(agent)
                    break
        return matching

    def get_capabilities_prompt(self) -> str:
        """Generate prompt describing available agents"""
        if not self._agents:
            return "No agents available for delegation."

        lines = ["Available agents for task delegation:\n"]

        for agent in self._agents.values():
            lines.append(f"## {agent.name}")
            lines.append(f"URL: {agent.url}")
            lines.append(f"Description: {agent.description}")

            if agent.skills:
                lines.append("Skills:")
                for skill in agent.skills:
                    lines.append(f"  - {skill.get('name')}: {skill.get('description', '')}")

            lines.append("")

        return "\n".join(lines)


# =============================================================================
# A2A Client
# =============================================================================

class A2AClient:
    """Client for communicating with A2A agents"""

    def __init__(self, agent: AgentInfo):
        self.agent = agent
        self.base_url = agent.url.rstrip("/")

    async def send_message(
        self,
        content: str,
        task_id: Optional[str] = None,
        tools_path: Optional[str] = None,
        stream: bool = False,
    ) -> dict:
        """Send message to agent via JSON-RPC"""

        method = "message/stream" if stream else "message/send"

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {
                "message": {
                    "role": "user",
                    "content": content,
                },
                "tools_path": tools_path,
            },
            "id": task_id or "1",
        }

        if task_id:
            payload["params"]["task_id"] = task_id

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url}/a2a",
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                raise Exception(f"A2A request failed: {response.text}")

            return response.json()

    async def stream_message(
        self,
        content: str,
        task_id: Optional[str] = None,
        tools_path: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream message response via SSE"""

        payload = {
            "jsonrpc": "2.0",
            "method": "message/stream",
            "params": {
                "message": {
                    "role": "user",
                    "content": content,
                },
                "tools_path": tools_path,
            },
            "id": task_id or "1",
        }

        if task_id:
            payload["params"]["task_id"] = task_id

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/a2a",
                json=payload,
                timeout=REQUEST_TIMEOUT
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        yield data

    async def get_task(self, task_id: str) -> dict:
        """Get task status via A2A"""
        payload = {
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": task_id},
            "id": "1",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/a2a", json=payload)
            return response.json()

    async def mcp_call_tool(
        self,
        tool_name: str,
        arguments: dict,
        async_mode: bool = False,
        session_id: Optional[str] = None,
    ) -> dict:
        """
        Call an MCP tool on this agent.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments
            async_mode: If True, request async execution
            session_id: Optional MCP session ID

        Returns:
            MCP tool result dict with 'content', optionally 'metadata' with task_id
        """
        if async_mode:
            arguments = {**arguments, "_async": True}

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
            "id": "1",
        }

        headers = {"Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url}/mcp",
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                raise Exception(f"MCP tool call failed: {response.text}")

            data = response.json()

            if "error" in data:
                raise Exception(f"MCP error: {data['error']}")

            return data.get("result", {})

    async def mcp_get_task(self, task_id: str, session_id: Optional[str] = None) -> dict:
        """Get task status via MCP endpoint"""
        payload = {
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": task_id},
            "id": "1",
        }

        headers = {"Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/mcp",
                json=payload,
                headers=headers
            )
            return response.json()

    async def register_webhook(
        self,
        webhook_url: str,
        task_id: Optional[str] = None,
        events: Optional[list[str]] = None,
    ) -> dict:
        """
        Register a webhook for push notifications.

        Args:
            webhook_url: URL to receive notifications
            task_id: Optional task ID to subscribe to (None = all tasks)
            events: Event types to subscribe to

        Returns:
            Dict with subscription_id and secret
        """
        payload = {
            "webhook_url": webhook_url,
            "task_id": task_id,
            "events": events or ["task.completed", "task.failed"],
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/notifications/register",
                json=payload,
            )

            if response.status_code != 200:
                raise Exception(f"Webhook registration failed: {response.text}")

            return response.json()


# =============================================================================
# Manager Agent
# =============================================================================

class ManagerAgent:
    """
    Orchestrator agent that delegates tasks to A2A workers.

    Uses local Ollama for reasoning about task delegation,
    then communicates with workers via A2A protocol.

    Features:
        - Async task delegation with push notifications or polling
        - Turn-boundary result injection
        - Proactive result announcement
        - Webhook server for receiving push notifications
    """

    def __init__(self, model: str = MANAGER_MODEL, use_push: bool = USE_PUSH_NOTIFICATIONS):
        self.model = model
        self.registry = AgentRegistry()
        self.conversation_history: list[dict] = []

        # Async task tracking
        self._pending_tasks: dict[str, AsyncTaskInfo] = {}
        self._completed_results: asyncio.Queue[AsyncTaskInfo] = asyncio.Queue()
        self._polling_task: Optional[asyncio.Task] = None
        self._webhook_server_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._use_push = use_push and HAS_FASTAPI

        # Webhook server (if using push notifications)
        self._webhook_app: Optional[FastAPI] = None

        # Callback for result notifications (e.g., TTS announcement)
        self.on_result_ready: Optional[Callable[[AsyncTaskInfo], None]] = None

    async def setup(self, worker_urls: list[str]):
        """Discover and register worker agents"""
        await self.registry.discover_many(worker_urls)

        if self._use_push:
            # Start webhook server for push notifications
            await self._start_webhook_server()
            logger.info(f"Started webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
        else:
            # Fallback to polling
            self._polling_task = asyncio.create_task(self._poll_pending_tasks())
            logger.info("Started async task polling (push notifications disabled)")

    async def _start_webhook_server(self):
        """Start the webhook server for receiving push notifications"""
        if not HAS_FASTAPI:
            logger.warning("FastAPI not available, falling back to polling")
            self._use_push = False
            self._polling_task = asyncio.create_task(self._poll_pending_tasks())
            return

        self._webhook_app = FastAPI(title="Manager Webhook Server")

        @self._webhook_app.post("/webhook")
        async def handle_webhook(request: Request):
            """Handle incoming push notifications from agents"""
            # Get headers
            signature = request.headers.get("X-Signature", "")
            subscription_id = request.headers.get("X-Subscription-Id", "")
            event = request.headers.get("X-Event", "")

            body = await request.body()
            payload = json.loads(body)

            task_id = payload.get("task_id", "")
            logger.info(f"Received webhook: event={event} task={task_id}")

            # Find the task
            task_info = self._pending_tasks.get(task_id)
            if not task_info:
                logger.warning(f"Unknown task in webhook: {task_id}")
                return JSONResponse({"status": "ignored", "reason": "unknown task"})

            # Verify signature
            if task_info.subscription_secret:
                expected_sig = hmac.new(
                    task_info.subscription_secret.encode(),
                    json.dumps(payload, sort_keys=True).encode(),
                    hashlib.sha256
                ).hexdigest()

                if signature != f"sha256={expected_sig}":
                    logger.warning(f"Invalid signature for task {task_id}")
                    return JSONResponse(
                        {"status": "error", "reason": "invalid signature"},
                        status_code=403
                    )

            # Process the notification
            if event == "task.completed":
                result = payload.get("result", {})
                content = result.get("content", "")

                task_info.state = AsyncTaskState.COMPLETED
                task_info.result = content
                task_info.completed_at = datetime.now()

                await self._completed_results.put(task_info)
                logger.info(f"Task {task_id} completed via push notification")

                if self.on_result_ready:
                    self.on_result_ready(task_info)

            elif event == "task.failed":
                result = payload.get("result", {})
                error = result.get("error", "Unknown error")

                task_info.state = AsyncTaskState.FAILED
                task_info.error = error
                task_info.completed_at = datetime.now()

                await self._completed_results.put(task_info)
                logger.info(f"Task {task_id} failed via push notification")

            return JSONResponse({"status": "received"})

        @self._webhook_app.get("/health")
        async def webhook_health():
            return {"status": "healthy", "pending_tasks": len(self._pending_tasks)}

        # Start server in background
        config = uvicorn.Config(
            self._webhook_app,
            host=WEBHOOK_HOST,
            port=WEBHOOK_PORT,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._webhook_server_task = asyncio.create_task(server.serve())

    async def shutdown(self):
        """Clean shutdown of background tasks"""
        self._shutdown = True

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

        if self._webhook_server_task:
            self._webhook_server_task.cancel()
            try:
                await self._webhook_server_task
            except asyncio.CancelledError:
                pass

        logger.info("Manager shutdown complete")

    async def _poll_pending_tasks(self):
        """Background task to poll for async task completion"""
        while not self._shutdown:
            try:
                await asyncio.sleep(ASYNC_POLL_INTERVAL)

                # Poll each pending task
                for task_id, task_info in list(self._pending_tasks.items()):
                    if task_info.state != AsyncTaskState.PENDING:
                        continue

                    try:
                        # Get agent client
                        agent_info = self.registry.get(task_info.agent_name)
                        if not agent_info:
                            logger.warning(f"Agent {task_info.agent_name} not found for task {task_id}")
                            continue

                        client = A2AClient(agent_info)

                        # Poll via MCP get_task_result tool
                        result = await client.mcp_call_tool(
                            "get_task_result",
                            {"task_id": task_id}
                        )

                        # Parse result
                        content = ""
                        for part in result.get("content", []):
                            if part.get("type") == "text":
                                content += part.get("text", "")

                        # Check if still running
                        if "still running" in content.lower():
                            logger.debug(f"Task {task_id} still running")
                            continue

                        # Check for failure
                        if "failed" in content.lower():
                            task_info.state = AsyncTaskState.FAILED
                            task_info.error = content
                            task_info.completed_at = datetime.now()
                            await self._completed_results.put(task_info)
                            logger.info(f"Task {task_id} failed")
                            continue

                        # Task completed
                        task_info.state = AsyncTaskState.COMPLETED
                        task_info.result = content
                        task_info.completed_at = datetime.now()
                        await self._completed_results.put(task_info)
                        logger.info(f"Task {task_id} completed")

                        # Trigger callback if registered
                        if self.on_result_ready:
                            self.on_result_ready(task_info)

                    except Exception as e:
                        logger.error(f"Error polling task {task_id}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling loop error: {e}")

    def _build_system_prompt(self, pending_count: int = 0) -> str:
        """Build system prompt with agent capabilities and async state"""
        base_prompt = f"""You are an orchestrator agent. Your role is to:
1. Understand user requests
2. Decide which worker agent(s) can best handle the task
3. Delegate tasks and coordinate responses

{self.registry.get_capabilities_prompt()}

## Task Delegation

You can delegate tasks in two modes:

### Synchronous (blocking)
```json
{{"delegate": {{"agent": "AgentName", "task": "specific task description"}}}}
```

### Asynchronous (non-blocking)
For longer tasks, use async mode. You'll be notified when the result is ready.
```json
{{"delegate": {{"agent": "AgentName", "task": "specific task description", "async": true}}}}
```

When you delegate asynchronously:
- Tell the user the task has been submitted
- Continue the conversation normally
- The result will be delivered to you automatically when ready
- Do NOT repeatedly ask about task status - you will be informed

If you can answer directly without delegation, just respond normally.
If no suitable agent exists, explain what's needed.
"""
        # Add pending task info if any
        if pending_count > 0:
            base_prompt += f"""
## Pending Tasks
You have {pending_count} async task(s) in progress. Results will be injected when ready.
"""
        return base_prompt

    def _build_result_injection_prompt(self, results: list[AsyncTaskInfo]) -> str:
        """Build prompt to inject completed async results"""
        lines = ["## Async Task Results\n"]
        lines.append("The following delegated tasks have completed:\n")

        for task_info in results:
            lines.append(f"### {task_info.agent_name} - {task_info.original_request[:50]}...")
            if task_info.state == AsyncTaskState.COMPLETED:
                lines.append(f"**Result:**\n{task_info.result}\n")
            else:
                lines.append(f"**Failed:** {task_info.error}\n")

        lines.append("\nPlease inform the user about these results.")
        return "\n".join(lines)

    async def _query_ollama(self, messages: list[dict], stream: bool = False) -> str:
        """Query local Ollama for reasoning"""
        request = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json=request,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                raise Exception(f"Ollama error: {response.text}")

            data = response.json()
            return data.get("message", {}).get("content", "")

    async def process(self, user_input: str, tools_path: Optional[str] = None) -> str:
        """
        Process user input - reason and delegate as needed.

        After processing, automatically checks for completed async results
        and generates follow-up announcements (turn-boundary injection).
        """
        # Add to conversation
        self.conversation_history.append({"role": "user", "content": user_input})

        # Count pending async tasks
        pending_count = sum(
            1 for t in self._pending_tasks.values()
            if t.state == AsyncTaskState.PENDING
        )

        # Build messages with system prompt
        messages = [
            {"role": "system", "content": self._build_system_prompt(pending_count)},
            *self.conversation_history
        ]

        # Get manager's response
        response = await self._query_ollama(messages)
        logger.debug(f"Manager response: {response[:200]}...")

        # Check for delegation
        if "```json" in response and '"delegate"' in response:
            try:
                # Extract JSON
                json_start = response.find("```json") + 7
                json_end = response.find("```", json_start)
                delegation = json.loads(response[json_start:json_end])

                agent_name = delegation["delegate"]["agent"]
                task = delegation["delegate"]["task"]
                is_async = delegation["delegate"].get("async", False)

                logger.info(f"Delegating to {agent_name}: {task} (async={is_async})")

                # Find agent
                agent_info = self.registry.get(agent_name)
                if not agent_info:
                    return f"Error: Agent '{agent_name}' not found"

                client = A2AClient(agent_info)

                if is_async:
                    # Async delegation via MCP
                    result = await client.mcp_call_tool(
                        "invoke",
                        {"task": task},
                        async_mode=True
                    )

                    # Extract task_id from response
                    content = ""
                    for part in result.get("content", []):
                        if part.get("type") == "text":
                            content += part.get("text", "")

                    task_id = result.get("metadata", {}).get("task_id")

                    if task_id:
                        # Register webhook for push notifications (if enabled)
                        subscription_id = None
                        subscription_secret = None

                        if self._use_push:
                            try:
                                webhook_url = f"{WEBHOOK_PUBLIC_URL}/webhook"
                                reg = await client.register_webhook(
                                    webhook_url=webhook_url,
                                    task_id=task_id,
                                    events=["task.completed", "task.failed"],
                                )
                                subscription_id = reg.get("subscription_id")
                                subscription_secret = reg.get("secret")
                                logger.info(f"Registered webhook for task {task_id}")
                            except Exception as e:
                                logger.warning(f"Failed to register webhook: {e}, falling back to polling")

                        # Track the async task
                        self._pending_tasks[task_id] = AsyncTaskInfo(
                            task_id=task_id,
                            agent_name=agent_name,
                            agent_url=agent_info.url,
                            original_request=task,
                            subscription_id=subscription_id,
                            subscription_secret=subscription_secret,
                        )
                        logger.info(f"Async task submitted: {task_id} (push={subscription_id is not None})")

                    # Generate acknowledgment response
                    ack_response = f"I've delegated that to {agent_name}. {content}"
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": ack_response
                    })

                    return ack_response

                else:
                    # Synchronous delegation via A2A
                    result = await client.send_message(task, tools_path=tools_path)

                    # Extract response
                    if "result" in result:
                        task_data = result["result"].get("task", {})
                        messages_list = task_data.get("messages", [])

                        # Get agent's response
                        for msg in reversed(messages_list):
                            if msg.get("role") == "agent":
                                parts = msg.get("parts", [])
                                for part in parts:
                                    if part.get("type") == "text":
                                        agent_response = part.get("text", "")

                                        # Add to history
                                        self.conversation_history.append({
                                            "role": "assistant",
                                            "content": f"[{agent_name}]: {agent_response}"
                                        })

                                        return f"**{agent_name}**:\n{agent_response}"

                    return f"Delegation completed but no response received"

            except Exception as e:
                logger.error(f"Delegation failed: {e}", exc_info=True)
                return f"Delegation failed: {e}"

        # No delegation - direct response
        self.conversation_history.append({"role": "assistant", "content": response})
        return response

    async def check_and_inject_results(self) -> Optional[str]:
        """
        Check for completed async results and generate follow-up announcement.

        This should be called at turn boundaries (after current response is
        delivered to user, e.g., after TTS finishes speaking).

        Returns:
            Follow-up message announcing results, or None if no results ready.
        """
        # Collect all completed results from queue (non-blocking)
        completed: list[AsyncTaskInfo] = []
        while True:
            try:
                task_info = self._completed_results.get_nowait()
                completed.append(task_info)
                # Mark as injected
                task_info.state = AsyncTaskState.INJECTED
            except asyncio.QueueEmpty:
                break

        if not completed:
            return None

        logger.info(f"Injecting {len(completed)} completed result(s)")

        # Build injection prompt
        injection_prompt = self._build_result_injection_prompt(completed)

        # Add system message with results
        self.conversation_history.append({
            "role": "system",
            "content": injection_prompt
        })

        # Generate follow-up announcement
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            *self.conversation_history
        ]

        announcement = await self._query_ollama(messages)

        # Add to history
        self.conversation_history.append({
            "role": "assistant",
            "content": announcement
        })

        # Clean up completed tasks from pending
        for task_info in completed:
            if task_info.task_id in self._pending_tasks:
                del self._pending_tasks[task_info.task_id]

        return announcement

    def get_pending_task_count(self) -> int:
        """Get count of pending async tasks"""
        return sum(
            1 for t in self._pending_tasks.values()
            if t.state == AsyncTaskState.PENDING
        )

    def get_pending_tasks(self) -> list[AsyncTaskInfo]:
        """Get list of pending async tasks"""
        return [
            t for t in self._pending_tasks.values()
            if t.state == AsyncTaskState.PENDING
        ]

    async def process_stream(
        self,
        user_input: str,
        tools_path: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """Process with streaming output"""
        # For now, use non-streaming and yield result
        # TODO: Implement true streaming delegation
        result = await self.process(user_input, tools_path)
        yield result


# =============================================================================
# CLI Interface
# =============================================================================

async def main():
    """Interactive CLI for manager agent with async task support"""

    # Default worker URLs - can be overridden via env
    worker_urls = os.getenv("WORKER_URLS", "http://localhost:8002").split(",")

    print("Manager Agent v2 - ADK-Free Multi-Agent Orchestrator")
    print("=" * 50)
    print("Features: Async delegation, turn-boundary injection")
    print("")

    manager = ManagerAgent()
    await manager.setup(worker_urls)

    print(f"Discovered {len(manager.registry.list_agents())} agent(s)")
    for agent in manager.registry.list_agents():
        print(f"  - {agent.name}: {agent.description}")

    print("\nEnter tasks (Ctrl+C to quit)")
    print("Tip: Use async delegation for long tasks\n")

    tools_path = os.getenv("TOOLS_PATH", "")

    try:
        while True:
            try:
                # Show pending task count if any
                pending = manager.get_pending_task_count()
                prompt = f"You [{pending} pending]: " if pending else "You: "

                user_input = input(prompt).strip()
                if not user_input:
                    # Empty input - good opportunity to check for results
                    announcement = await manager.check_and_inject_results()
                    if announcement:
                        print(f"\n[Async Result]\n{announcement}\n")
                    continue

                # Process user input
                response = await manager.process(user_input, tools_path=tools_path)
                print(f"\n{response}\n")

                # Turn boundary: Check for completed async results
                # In a real system, this would be after TTS finishes speaking
                announcement = await manager.check_and_inject_results()
                if announcement:
                    print(f"\n[Async Result]\n{announcement}\n")

            except KeyboardInterrupt:
                print("\nGoodbye!")
                break
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                print(f"Error: {e}")
    finally:
        await manager.shutdown()


async def demo_async_flow():
    """Demo showing async delegation and result injection"""
    print("=== Async Flow Demo ===\n")

    worker_urls = os.getenv("WORKER_URLS", "http://localhost:8002").split(",")

    manager = ManagerAgent()
    await manager.setup(worker_urls)

    if not manager.registry.list_agents():
        print("No agents discovered. Start a worker agent first.")
        return

    print("Simulating async delegation flow...\n")

    # User asks for something that takes time
    response = await manager.process(
        "Generate a Python REST API with CRUD operations for a todo list. Use async mode."
    )
    print(f"Manager: {response}\n")

    # User continues conversation while task runs in background
    print("--- User continues conversation ---")
    response = await manager.process("What's 2 + 2?")
    print(f"Manager: {response}\n")

    # Simulate waiting for task completion
    print("--- Waiting for async task to complete ---")
    for i in range(30):  # Wait up to 60 seconds
        await asyncio.sleep(2)
        announcement = await manager.check_and_inject_results()
        if announcement:
            print(f"\n[Async Result]\n{announcement}\n")
            break
        print(f"  Polling... ({(i+1)*2}s)")

    await manager.shutdown()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        asyncio.run(demo_async_flow())
    else:
        asyncio.run(main())
