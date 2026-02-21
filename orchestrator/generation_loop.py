"""
Core generation loop with native Ollama MCP support.

Tools are served directly by agent MCP endpoints:
- Ollama handles tool discovery via mcp_discover
- Ollama executes tools via MCP HTTP transport
- Agents can filter exposed tools via STATIC_TOOLS env var

For async results (via webhooks), results are injected at
generation boundaries.
"""

import logging
from typing import Any, AsyncIterator, Dict, List

from .state import Session, LoopState, sessions
from .agent_client import agents, AgentResult
from .ollama_client import ollama
from .config import config

logger = logging.getLogger(__name__)


async def run_generation_loop(
    session: Session,
    initial_messages: List[Dict[str, Any]],
) -> AsyncIterator[Dict[str, Any]]:
    """
    Run the generation loop until no more results to process.

    Yields chunks for streaming to client:
    - {"type": "content", "content": "..."} - text to stream
    - {"type": "tool_call", "tool_call": {...}} - tool call info
    - {"type": "tool_result", "result": {...}} - tool result
    - {"type": "injection", "count": N, "agents": [...]} - async result injection
    - {"type": "done"} - loop complete

    The loop continues if:
    - Static tool calls need processing
    - Async results arrive at generation boundary
    """
    session.state = LoopState.GENERATING
    messages = initial_messages.copy()

    try:
        while True:
            session.generation_count += 1
            gen_num = session.generation_count

            logger.info(f"Starting generation {gen_num} for session {session.session_id}")

            # Run one generation (may loop internally for static tool calls)
            continue_loop = False
            accumulated_content = []  # Track assistant response content
            accumulated_tool_calls = []  # Track tool calls
            accumulated_tool_results = []  # Track tool results

            async for chunk in _run_single_generation(session, messages, gen_num):
                if chunk.get("type") == "_continue":
                    # Internal signal: static tool results injected, continue
                    continue_loop = True
                    messages = chunk["messages"]
                elif chunk.get("type") == "content":
                    # Accumulate content for session persistence
                    accumulated_content.append(chunk["content"])
                    yield chunk
                elif chunk.get("type") == "tool_call":
                    # Accumulate tool calls for session persistence
                    tc = chunk["tool_call"]
                    accumulated_tool_calls.append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": tc.get("function", {}),
                    })
                    yield chunk
                elif chunk.get("type") == "tool_result":
                    # Accumulate tool results for session persistence
                    result = chunk["result"]
                    accumulated_tool_results.append(result)
                    yield chunk
                else:
                    yield chunk

            # Persist assistant message (with content and/or tool calls)
            if accumulated_content or accumulated_tool_calls:
                assistant_msg = {"role": "assistant"}
                if accumulated_content:
                    assistant_msg["content"] = "".join(accumulated_content)
                if accumulated_tool_calls:
                    assistant_msg["tool_calls"] = accumulated_tool_calls
                messages.append(assistant_msg)
                logger.debug(f"Persisted assistant message (content={len(accumulated_content)} chunks, tool_calls={len(accumulated_tool_calls)})")

            # Persist tool results as separate messages
            for i, result in enumerate(accumulated_tool_results):
                tool_call_id = accumulated_tool_calls[i]["id"] if i < len(accumulated_tool_calls) else ""
                # Format result content
                if isinstance(result, dict):
                    content = result.get("content", str(result))
                    if isinstance(content, list):
                        # MCP format: [{"type": "text", "text": "..."}]
                        content = "\n".join(
                            item.get("text", str(item))
                            for item in content
                            if isinstance(item, dict)
                        )
                else:
                    content = str(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content,
                })
            if accumulated_tool_results:
                logger.debug(f"Persisted {len(accumulated_tool_results)} tool results")

            if continue_loop:
                # Static tool results were injected, continue to next generation
                continue

            # Generation complete - check for async results
            results = sessions.drain_results(session)

            if not results:
                # No async results, end loop
                logger.info(f"Generation loop complete for session {session.session_id} after {gen_num} generations")
                yield {"type": "done"}
                break

            # Inject async results and continue
            logger.info(f"Injecting {len(results)} async results, continuing to generation {gen_num + 1}")

            injection = _format_results_injection(results)
            messages.append({"role": "system", "content": injection})

            # Yield injection marker (for client visibility)
            yield {
                "type": "injection",
                "count": len(results),
                "agents": [r.agent_name for r in results],
            }

            # Loop continues with new generation

    finally:
        session.state = LoopState.IDLE
        session.messages = messages  # Save conversation state
        logger.debug(f"Session {session.session_id} state -> IDLE")


async def _run_single_generation(
    session: Session,
    messages: List[Dict[str, Any]],
    gen_num: int,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Run a single generation with native Ollama MCP support.

    Agent MCP endpoints are passed directly to Ollama:
    - Ollama calls mcp_discover to find available tools
    - Ollama executes tools via HTTP to agent /mcp endpoints
    - Agents control which tools are exposed via STATIC_TOOLS config
    """
    model = config.ollama_model

    # Get MCP servers for all discovered agents
    mcp_servers = agents.get_mcp_servers(session.session_id) if agents._discovered else []

    if mcp_servers:
        server_names = [s['name'] for s in mcp_servers]
        logger.info(f"Using MCP servers: {server_names}")
    else:
        logger.warning("No MCP servers available")

    # Track state for tool call handling
    seen_tool_call_ids: set = set()  # Dedupe duplicate tool call chunks

    async for chunk in ollama.chat_stream(
        model=model,
        messages=messages,
        mcp_servers=mcp_servers if mcp_servers else None,
        session_id=session.session_id,
    ):
        chunk_type = chunk.get("type")

        if chunk_type == "error":
            logger.error(f"Ollama error: {chunk.get('error')}")
            yield {"type": "content", "content": f"\n[Error: {chunk.get('error')}]\n"}
            return

        elif chunk_type == "tool_call":
            tool_call = chunk["tool_call"]
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            tool_call_id = tool_call.get("id", "")

            # Dedupe duplicate tool call chunks (Ollama quirk)
            if tool_call_id and tool_call_id in seen_tool_call_ids:
                continue
            if tool_call_id:
                seen_tool_call_ids.add(tool_call_id)

            logger.info(f"Tool call: {tool_name}")

            # Pass through to client for visibility
            # Ollama will execute via MCP and return tool_result
            yield {"type": "tool_call", "tool_call": tool_call}

        elif chunk_type == "tool_result":
            # Tool result from Ollama MCP execution
            result = chunk["result"]
            logger.debug(f"Tool result: {str(result)[:100]}")
            yield {"type": "tool_result", "result": result}

        elif chunk_type == "content":
            yield {"type": "content", "content": chunk["content"]}

        elif chunk_type == "done":
            # Generation complete with no static tool calls
            logger.debug(f"Generation {gen_num} complete, checking for async results")
            return


async def watch_idle_results(session: Session) -> AsyncIterator[Dict[str, Any]]:
    """
    Watch for async results arriving during IDLE state.

    When results arrive while no generation is active, this triggers
    a new generation loop with the results injected.

    This is used for the /v1/events SSE endpoint.
    """
    while True:
        # Wait for results signal
        await session.results_available.wait()
        session.results_available.clear()

        if session.state != LoopState.IDLE:
            # Generation already running, results will be handled there
            logger.debug("Results arrived but generation is active, skipping")
            continue

        # Drain results
        results = sessions.drain_results(session)
        if not results:
            continue

        logger.info(f"Results arrived during IDLE for session {session.session_id}")

        # Inject and start generation
        injection = _format_results_injection(results)
        messages = session.messages + [{"role": "system", "content": injection}]

        # Yield injection marker
        yield {
            "type": "injection",
            "count": len(results),
            "agents": [r.agent_name for r in results],
        }

        # Run generation loop
        async for chunk in run_generation_loop(session, messages):
            yield chunk


def _format_results_injection(results: List[AgentResult]) -> str:
    """
    Format async results for injection into conversation context.

    All results are batched together so the model can see
    related results as a unit.
    """
    lines = [
        "[Agent Results - The following async tasks have completed. "
        "Process these results and respond to the user.]\n"
    ]

    for r in results:
        status = "completed successfully" if r.success else "failed"
        lines.append(f"\n### {r.agent_name}:{r.tool_name} ({status})\n")

        if r.success:
            lines.append(r.content)
        else:
            lines.append(f"Error: {r.content}")

        lines.append("\n")

    return "".join(lines)
