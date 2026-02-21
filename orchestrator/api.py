"""
OpenAI-compatible API endpoints with async MCP agent support.

Provides /v1/chat/completions and /v1/models endpoints compatible
with OpenAI clients. Agents communicate via MCP protocol with
async task submission and push notification delivery.
"""

import json
import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .config import config
from .state import sessions
from .generation_loop import run_generation_loop, watch_idle_results
from .agent_client import agents
from .ollama_client import ollama

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/v1/models")
async def list_models():
    """
    List available models (OpenAI-compatible).

    Returns models from Ollama in OpenAI format.
    """
    ollama_models = await ollama.list_models()

    models = [
        {
            "id": m.get("name", "unknown"),
            "object": "model",
            "created": int(time.time()),
            "owned_by": "ollama",
        }
        for m in ollama_models
    ]

    return {"object": "list", "data": models}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions with async agent delegation.

    Tool calls to agents are submitted asynchronously via MCP.
    Results are batched and injected at generation boundaries.

    The response stream includes:
    - Regular content chunks (delta.content)
    - Tool call chunks (delta.tool_calls)
    - Custom events: agent.pending, agent.injection
    """
    body = await request.json()

    # Parse request
    session_id = body.get("session_id") or f"sess_{uuid.uuid4().hex[:8]}"
    messages = body.get("messages", [])
    stream = body.get("stream", True)
    model = body.get("model", config.ollama_model)

    # Convert messages to dict format if needed
    messages = [
        m if isinstance(m, dict) else m.dict()
        for m in messages
    ]

    # Ensure agents discovered
    if not agents._discovered:
        agent_urls = body.get("agent_urls") or config.agent_urls
        await agents.discover(agent_urls)

    # Get or create session
    session = await sessions.get_or_create(session_id)

    # Merge client messages with server state
    # Server state includes tool_call/tool_result messages that client doesn't track.
    # Compare USER message counts (both sides agree on user messages) rather than
    # total counts, which diverge when the server has extra tool context.
    if session.messages and len(session.messages) > 0:
        server_user_count = sum(1 for m in session.messages if m.get("role") == "user")
        client_user_count = sum(1 for m in messages if m.get("role") == "user")

        if client_user_count > server_user_count:
            # Client has new user messages - extract and append them
            client_user_msgs = [m for m in messages if m.get("role") == "user"]
            new_user_msgs = client_user_msgs[server_user_count:]
            session.messages.extend(new_user_msgs)
            logger.debug(f"Appended {len(new_user_msgs)} new user messages "
                        f"(server: {server_user_count} user / {len(session.messages)} total)")
        elif client_user_count == server_user_count:
            # No new user messages - server state is current (has tool context)
            logger.debug(f"Server state current ({len(session.messages)} messages, "
                        f"{server_user_count} user msgs)")
        else:
            # Client has fewer user messages - reset to client state
            logger.warning(f"Client has fewer user messages ({client_user_count}) "
                          f"than server ({server_user_count}), resetting")
            session.messages = messages.copy()
    else:
        # New session - use client messages
        session.messages = messages.copy()

    logger.info(f"Chat completion: session={session_id}, messages={len(session.messages)}, stream={stream}")

    # Build system prompt from discovered agents and inject at front
    system_prompt = agents.build_system_prompt()
    gen_messages = session.messages.copy()
    if gen_messages and gen_messages[0].get("role") == "system":
        gen_messages[0] = {"role": "system", "content": system_prompt}
    else:
        gen_messages.insert(0, {"role": "system", "content": system_prompt})

    if stream:
        return StreamingResponse(
            _stream_with_loop(session, gen_messages, model),
            media_type="text/event-stream",
            headers={
                "X-Session-ID": session_id,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    else:
        # Non-streaming: collect all output
        content_parts = []

        async for chunk in _stream_with_loop(session, gen_messages, model):
            # Parse SSE data
            if chunk.startswith("data: "):
                data_str = chunk[6:].strip()
                if data_str and data_str != "[DONE]":
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta:
                            content_parts.append(delta["content"])
                    except:
                        pass

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(content_parts),
                },
                "finish_reason": "stop",
            }],
        }


async def _stream_with_loop(
    session,
    messages: list,
    model: str,
) -> AsyncIterator[str]:
    """
    Stream generation loop output as SSE.

    Converts internal chunk types to OpenAI-compatible SSE format.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    async for chunk in run_generation_loop(session, messages):
        chunk_type = chunk.get("type")

        if chunk_type == "content":
            yield _format_sse_content(completion_id, created, model, chunk["content"])

        elif chunk_type == "tool_result":
            # Suppress mcp.tool_result - it's internal to orchestrator
            # Emitting it crashes OpenAI SDK clients (no choices field)
            pass

        elif chunk_type == "injection":
            # Custom event for result injection
            yield _format_sse_event("agent.injection", {
                "count": chunk["count"],
                "agents": chunk["agents"],
            })

        elif chunk_type == "tool_call":
            yield _format_sse_tool_call(completion_id, created, model, chunk["tool_call"])

        elif chunk_type == "done":
            yield _format_sse_done(completion_id, created, model)

    yield "data: [DONE]\n\n"


@router.get("/v1/events")
async def event_stream(session_id: str):
    """
    SSE endpoint for receiving async results during IDLE state.

    Connect to this endpoint after a generation completes to receive
    push notifications when agent results arrive. This enables
    proactive announcement of results without waiting for user input.

    The stream will:
    1. Wait for results to arrive
    2. Inject results and start new generation
    3. Stream the generation output
    4. Return to waiting

    This is optional - results will also be injected at the start
    of the next /v1/chat/completions request.
    """
    session = await sessions.get_or_create(session_id)

    logger.info(f"Event stream connected for session {session_id}")

    return StreamingResponse(
        _idle_event_stream(session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def _idle_event_stream(session) -> AsyncIterator[str]:
    """Stream results that arrive during IDLE state."""
    model = config.ollama_model

    async for chunk in watch_idle_results(session):
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        created = int(time.time())

        chunk_type = chunk.get("type")

        if chunk_type == "content":
            yield _format_sse_content(completion_id, created, model, chunk["content"])

        elif chunk_type == "injection":
            yield _format_sse_event("agent.injection", {
                "count": chunk["count"],
                "agents": chunk["agents"],
            })

        elif chunk_type == "tool_result":
            # Suppress mcp.tool_result - it's internal to orchestrator
            pass

        elif chunk_type == "done":
            yield _format_sse_done(completion_id, created, model)
            yield "data: [DONE]\n\n"


# =============================================================================
# SSE Formatting Helpers
# =============================================================================

def _format_sse_content(id: str, created: int, model: str, content: str) -> str:
    """Format content chunk as SSE."""
    chunk = {
        "id": id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": None,
        }]
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _format_sse_done(id: str, created: int, model: str) -> str:
    """Format final chunk as SSE."""
    chunk = {
        "id": id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }]
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _format_sse_tool_call(id: str, created: int, model: str, tool_call: dict) -> str:
    """
    Format tool call as SSE (OpenAI-compatible).

    Transforms Ollama's tool_call format to OpenAI spec:
    - arguments must be a JSON string, not a dict
    - index belongs on tool_call, not inside function
    """
    # Extract function info
    func = tool_call.get("function", {})

    # Get index from function (Ollama puts it there) or default to 0
    index = func.pop("index", 0) if isinstance(func, dict) else 0

    # Ensure arguments is a JSON string, not a dict
    arguments = func.get("arguments", {})
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)

    # Build OpenAI-compatible tool_call structure
    formatted_tool_call = {
        "index": index,  # index at tool_call level per OpenAI spec
        "id": tool_call.get("id", f"call_{id[:8]}"),
        "type": "function",
        "function": {
            "name": func.get("name", ""),
            "arguments": arguments,  # JSON string
        }
    }

    chunk = {
        "id": id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [formatted_tool_call]},
            "finish_reason": None,
        }]
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _format_sse_event(event_type: str, data: dict) -> str:
    """Format custom event as SSE."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
