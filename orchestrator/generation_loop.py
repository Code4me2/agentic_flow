"""
Core generation loop with MCP-based agent delegation.

Ollama handles MCP tool discovery and execution natively:
1. Orchestrator passes mcp_servers config to Ollama
2. Model gets access to mcp_discover tool
3. Model discovers agent tools via mcp_discover
4. Model calls tools, Ollama executes via MCP
5. Results returned in stream

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
    - {"type": "tool_call", "tool_call": {...}} - tool call info (from Ollama)
    - {"type": "tool_result", "result": {...}} - tool result (from Ollama MCP)
    - {"type": "injection", "count": N, "agents": [...]} - async result injection
    - {"type": "done"} - loop complete

    The loop continues if async results arrive at generation boundary.
    """
    session.state = LoopState.GENERATING
    messages = initial_messages.copy()

    try:
        while True:
            session.generation_count += 1
            gen_num = session.generation_count

            logger.info(f"Starting generation {gen_num} for session {session.session_id}")

            # Run one generation
            async for chunk in _run_single_generation(session, messages, gen_num):
                yield chunk

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
    Run a single generation with MCP servers.

    Ollama handles tool discovery (via mcp_discover) and execution
    (via MCP tools/call) natively. We just stream the results.
    """
    model = config.ollama_model

    # Get MCP server configs for discovered agents
    # Pass session_id so agents can include it in webhook callbacks
    mcp_servers = agents.get_mcp_servers(session.session_id) if agents._discovered else []

    if mcp_servers:
        logger.info(f"Using MCP servers: {[s['name'] for s in mcp_servers]}")
    else:
        logger.warning(f"No MCP servers! agents._discovered={agents._discovered}")

    async for chunk in ollama.chat_stream(
        model=model,
        messages=messages,
        mcp_servers=mcp_servers if mcp_servers else None,
    ):
        chunk_type = chunk.get("type")

        if chunk_type == "error":
            logger.error(f"Ollama error: {chunk.get('error')}")
            yield {"type": "content", "content": f"\n[Error: {chunk.get('error')}]\n"}
            return

        elif chunk_type == "tool_call":
            # Pass through tool call info for visibility
            yield {"type": "tool_call", "tool_call": chunk["tool_call"]}

        elif chunk_type == "tool_result":
            # Pass through tool result for visibility
            yield {"type": "tool_result", "result": chunk["result"]}

        elif chunk_type == "content":
            yield {"type": "content", "content": chunk["content"]}

        elif chunk_type == "done":
            # Don't yield done yet - loop will check for async results first
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
