"""
A2A-to-Ollama Bridge

Exposes Ollama models with native MCP support as A2A-compliant agents.
This allows ADK's Manager agent to discover and delegate to Ollama workers
while tool execution happens natively in Ollama (bypassing ADK's MCP layer).

Architecture:
    ADK Manager → A2A Protocol → This Bridge → Ollama API → Native MCP
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import AsyncGenerator, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
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
logger = logging.getLogger("a2a_bridge")

# Configuration
OLLAMA_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8002"))
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
DEFAULT_MODEL = os.getenv("BRIDGE_MODEL", "qwen3-coder-tools:latest")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))

# MCP Configuration
DEFAULT_TOOLS_PATH = os.getenv("TOOLS_PATH", "/home/velvetm/Desktop/hestia-OS")
DEFAULT_MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "15"))
DEFAULT_TOOL_TIMEOUT = int(os.getenv("TOOL_TIMEOUT", "30000"))
ENABLE_MCP = os.getenv("ENABLE_MCP", "true").lower() == "true"


# --- Pydantic Models ---

class Message(BaseModel):
    role: str
    content: str
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None


class MCPServerConfig(BaseModel):
    """MCP Server configuration for explicit server setup"""
    name: str
    command: str
    args: list[str] = []
    env: dict = {}


class ChatRequest(BaseModel):
    messages: list[Message]
    model: Optional[str] = None
    stream: bool = True
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # MCP options (modded Ollama API)
    tools_path: Optional[str] = None  # Auto-configure filesystem MCP for this path
    mcp_servers: Optional[list[MCPServerConfig]] = None  # Explicit MCP server configs
    max_tool_rounds: Optional[int] = None  # Max tool execution rounds
    tool_timeout: Optional[int] = None  # Timeout per tool in ms
    enable_mcp: Optional[bool] = None  # Override default MCP enablement
    # Standard tools array (no MCP execution, returns tool_calls)
    tools: Optional[list] = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class ChatResponseChunk(BaseModel):
    content: str = ""
    tool_calls: Optional[list[ToolCall]] = None
    done: bool = False
    error: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[str] = None


class AgentCapabilities(BaseModel):
    tools: bool = True
    streaming: bool = True
    multi_turn: bool = True


class AgentEndpoints(BaseModel):
    chat: str = "/v1/chat"
    health: str = "/health"


class AgentCard(BaseModel):
    """A2A Agent Card specification"""
    name: str
    description: str
    version: str
    capabilities: AgentCapabilities
    endpoints: AgentEndpoints
    models: list[str]
    mcp_tools: Optional[list[str]] = None


# --- Bridge State ---

class BridgeState:
    """Maintains bridge state and Ollama connection health"""

    def __init__(self):
        self.healthy = False
        self.last_health_check: Optional[datetime] = None
        self.available_models: list[str] = []
        self.mcp_tools: list[str] = []

    async def check_health(self) -> bool:
        """Check Ollama connectivity and update state"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Check Ollama is running
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


# --- FastAPI App ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle"""
    logger.info(f"Starting A2A-Ollama Bridge on {BRIDGE_HOST}:{BRIDGE_PORT}")
    logger.info(f"Ollama endpoint: {OLLAMA_BASE}")
    logger.info(f"Default model: {DEFAULT_MODEL}")

    # Initial health check
    if await state.check_health():
        logger.info(f"Connected to Ollama. Available models: {state.available_models}")
    else:
        logger.warning("Could not connect to Ollama on startup")

    yield

    logger.info("Shutting down A2A-Ollama Bridge")


app = FastAPI(
    title="A2A-Ollama Bridge",
    description="Bridges A2A protocol to Ollama with native MCP support",
    version="1.0.0",
    lifespan=lifespan
)


# --- A2A Discovery Endpoints ---

@app.get("/.well-known/agent.json", response_model=AgentCard)
async def agent_card():
    """A2A Agent Card for discovery"""
    return AgentCard(
        name="LocalCoder",
        description="Code generation agent with filesystem access via native Ollama MCP",
        version="1.0.0",
        capabilities=AgentCapabilities(
            tools=True,
            streaming=True,
            multi_turn=True
        ),
        endpoints=AgentEndpoints(
            chat="/v1/chat",
            health="/health"
        ),
        models=[DEFAULT_MODEL] + [m for m in state.available_models if m != DEFAULT_MODEL],
        mcp_tools=state.mcp_tools
    )


@app.get("/health")
async def health():
    """Health check endpoint"""
    is_healthy = await state.check_health()
    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "ollama_connected": is_healthy,
        "last_check": state.last_health_check.isoformat() if state.last_health_check else None,
        "available_models": state.available_models
    }


# --- Chat Endpoints ---

@app.post("/v1/chat")
async def chat(request: ChatRequest):
    """
    A2A-compliant chat endpoint.
    Translates to Ollama /api/chat and streams responses.
    """
    model = request.model or DEFAULT_MODEL

    # Build Ollama request
    ollama_messages = []
    for msg in request.messages:
        ollama_msg = {
            "role": msg.role,
            "content": msg.content
        }
        # Handle tool results being sent back
        if msg.tool_call_id:
            ollama_msg["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls:
            ollama_msg["tool_calls"] = msg.tool_calls
        ollama_messages.append(ollama_msg)

    ollama_request = {
        "model": model,
        "messages": ollama_messages,
        "stream": request.stream,
    }

    # Add optional parameters
    if request.temperature is not None:
        ollama_request["options"] = ollama_request.get("options", {})
        ollama_request["options"]["temperature"] = request.temperature

    if request.max_tokens is not None:
        ollama_request["options"] = ollama_request.get("options", {})
        ollama_request["options"]["num_predict"] = request.max_tokens

    # MCP Configuration - determine if MCP should be enabled
    use_mcp = request.enable_mcp if request.enable_mcp is not None else ENABLE_MCP

    if use_mcp:
        # Option 1: Explicit MCP servers
        if request.mcp_servers:
            ollama_request["mcp_servers"] = [s.model_dump() for s in request.mcp_servers]
        # Option 2: tools_path for auto-configured filesystem MCP
        elif request.tools_path:
            ollama_request["tools_path"] = request.tools_path
        # Option 3: Use default tools_path
        elif DEFAULT_TOOLS_PATH:
            ollama_request["tools_path"] = DEFAULT_TOOLS_PATH

        # MCP execution options
        ollama_request["max_tool_rounds"] = request.max_tool_rounds or DEFAULT_MAX_TOOL_ROUNDS
        ollama_request["tool_timeout"] = request.tool_timeout or DEFAULT_TOOL_TIMEOUT

    # Standard tools array (passed through, no MCP execution)
    if request.tools:
        ollama_request["tools"] = request.tools

    logger.debug(f"Ollama request: {json.dumps(ollama_request, indent=2)}")

    if request.stream:
        return StreamingResponse(
            stream_ollama_response(ollama_request),
            media_type="application/x-ndjson"
        )
    else:
        return await non_streaming_response(ollama_request)


async def stream_ollama_response(ollama_request: dict) -> AsyncGenerator[str, None]:
    """Stream responses from Ollama, translating to A2A format"""

    accumulated_content = ""
    accumulated_tool_calls = []

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
                    logger.error(f"Ollama error: {error_text}")
                    yield json.dumps(ChatResponseChunk(
                        error=f"Ollama error: {error_text.decode()}",
                        done=True
                    ).model_dump()) + "\n"
                    return

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse chunk: {line[:100]}... - {e}")
                        continue

                    # Extract message content
                    message = chunk.get("message", {})
                    content = message.get("content", "")
                    tool_calls_raw = message.get("tool_calls", [])
                    done = chunk.get("done", False)

                    # Accumulate content
                    if content:
                        accumulated_content += content

                    # Process tool calls from Ollama's native MCP
                    tool_calls = []
                    for tc in tool_calls_raw:
                        tool_call = ToolCall(
                            id=tc.get("id", f"call_{len(accumulated_tool_calls)}"),
                            name=tc.get("function", {}).get("name", tc.get("name", "")),
                            arguments=tc.get("function", {}).get("arguments", tc.get("arguments", {}))
                        )
                        tool_calls.append(tool_call)
                        accumulated_tool_calls.append(tool_call)

                    # Build response chunk
                    response_chunk = ChatResponseChunk(
                        content=content,
                        tool_calls=tool_calls if tool_calls else None,
                        done=done,
                        model=chunk.get("model"),
                        created_at=chunk.get("created_at")
                    )

                    yield json.dumps(response_chunk.model_dump(exclude_none=True)) + "\n"

                    # Log tool execution
                    if tool_calls:
                        for tc in tool_calls:
                            logger.info(f"Tool call: {tc.name}({json.dumps(tc.arguments)})")

                    if done:
                        logger.debug(f"Stream complete. Total content length: {len(accumulated_content)}")
                        logger.debug(f"Total tool calls: {len(accumulated_tool_calls)}")

    except httpx.TimeoutException:
        logger.error("Request to Ollama timed out")
        yield json.dumps(ChatResponseChunk(
            error="Request timed out",
            done=True
        ).model_dump()) + "\n"

    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to Ollama: {e}")
        yield json.dumps(ChatResponseChunk(
            error=f"Failed to connect to Ollama: {e}",
            done=True
        ).model_dump()) + "\n"

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        yield json.dumps(ChatResponseChunk(
            error=f"Unexpected error: {str(e)}",
            done=True
        ).model_dump()) + "\n"


async def non_streaming_response(ollama_request: dict) -> JSONResponse:
    """Handle non-streaming requests"""

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json=ollama_request,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Ollama error: {response.text}"
                )

            data = response.json()
            message = data.get("message", {})

            # Process tool calls
            tool_calls = []
            for tc in message.get("tool_calls", []):
                tool_calls.append(ToolCall(
                    id=tc.get("id", f"call_{len(tool_calls)}"),
                    name=tc.get("function", {}).get("name", tc.get("name", "")),
                    arguments=tc.get("function", {}).get("arguments", tc.get("arguments", {}))
                ))

            return JSONResponse(content=ChatResponseChunk(
                content=message.get("content", ""),
                tool_calls=tool_calls if tool_calls else None,
                done=True,
                model=data.get("model"),
                created_at=data.get("created_at")
            ).model_dump(exclude_none=True))

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to Ollama timed out")
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail=f"Failed to connect to Ollama: {e}")


# --- Multi-Agent Coordination ---

@app.post("/v1/delegate")
async def delegate(request: Request):
    """
    Delegation endpoint for autonomous multi-turn tool execution.

    Ollama's native MCP handles the full tool execution cycle internally.
    This endpoint just needs to send the request with MCP enabled.
    """
    body = await request.json()
    task = body.get("task", "")
    context = body.get("context", {})
    tools_path = body.get("tools_path", DEFAULT_TOOLS_PATH)
    max_tool_rounds = body.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)
    tool_timeout = body.get("tool_timeout", DEFAULT_TOOL_TIMEOUT)

    messages = [
        {"role": "system", "content": context.get("system_prompt", "You are a helpful coding assistant with filesystem access.")},
        {"role": "user", "content": task}
    ]

    # Build Ollama request with MCP enabled
    ollama_request = {
        "model": body.get("model", DEFAULT_MODEL),
        "messages": messages,
        "stream": False,
        "tools_path": tools_path,
        "max_tool_rounds": max_tool_rounds,
        "tool_timeout": tool_timeout
    }

    # Optional: explicit MCP servers
    if body.get("mcp_servers"):
        ollama_request["mcp_servers"] = body["mcp_servers"]
        del ollama_request["tools_path"]  # Use explicit servers instead

    logger.info(f"Delegation request: tools_path={tools_path}, max_rounds={max_tool_rounds}")
    logger.debug(f"Ollama request: {json.dumps(ollama_request, indent=2)}")

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json=ollama_request,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                return JSONResponse(
                    status_code=response.status_code,
                    content={"error": f"Ollama error: {response.text}"}
                )

            data = response.json()
            message = data.get("message", {})
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            # Log tool execution info
            if tool_calls:
                logger.info(f"Tools executed: {[tc.get('function', {}).get('name', tc.get('name')) for tc in tool_calls]}")

            return JSONResponse(content={
                "status": "complete",
                "content": content,
                "tool_calls": tool_calls,
                "model": data.get("model"),
                "total_duration": data.get("total_duration"),
                "eval_count": data.get("eval_count")
            })

    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"error": "Request to Ollama timed out"}
        )
    except httpx.ConnectError as e:
        return JSONResponse(
            status_code=503,
            content={"error": f"Failed to connect to Ollama: {e}"}
        )


# --- Utility Endpoints ---

@app.get("/v1/models")
async def list_models():
    """List available Ollama models"""
    await state.check_health()
    return {"models": state.available_models}


@app.get("/v1/tools")
async def list_tools():
    """
    List available MCP tools.
    Note: This requires querying Ollama's MCP manager.
    """
    # TODO: Implement when Ollama exposes MCP tool listing API
    return {
        "tools": state.mcp_tools,
        "note": "Tool list populated from mcp-servers.json"
    }


# --- Main ---

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=BRIDGE_HOST,
        port=BRIDGE_PORT,
        log_level="debug" if os.getenv("DEBUG") else "info"
    )
