"""Ollama API streaming client."""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from .config import config

logger = logging.getLogger(__name__)


class OllamaClient:
    """
    Async client for Ollama API with streaming support.

    Handles:
    - Streaming chat completions
    - Tool call detection (Ollama returns tool_calls, we execute)
    - Model listing
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        self.base_url = config.ollama_url

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models from Ollama."""
        try:
            resp = await self.client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return data.get("models", [])
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream chat completion from Ollama with hybrid tool support.

        Supports two modes of tool access:
        1. Static tools (via `tools` param): Always available, no discovery needed
        2. JIT tools (via `mcp_servers` param): Discovered via mcp_discover

        Both can be used simultaneously - static tools for common operations,
        JIT for specialized/rare tools.

        Yields chunks with structure:
        - {"type": "content", "content": "..."} - text content
        - {"type": "tool_call", "tool_call": {...}} - tool invocation
        - {"type": "tool_result", "result": {...}} - tool execution result
        - {"type": "done", ...} - generation complete

        Args:
            model: Model name
            messages: Conversation messages
            tools: Static tool definitions (Ollama/OpenAI format)
            mcp_servers: MCP server configs for JIT discovery
            temperature: Sampling temperature
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }

        # Add static tools (always available)
        if tools:
            payload["tools"] = tools

        # Add MCP servers for JIT tool discovery
        if mcp_servers:
            payload["mcp_servers"] = mcp_servers

        tools_count = len(tools) if tools else 0
        mcp_count = len(mcp_servers) if mcp_servers else 0
        logger.info(f"Starting chat stream: model={model}, messages={len(messages)}, "
                   f"static_tools={tools_count}, mcp_servers={mcp_count}")

        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse chunk: {line[:100]}")
                        continue

                    # Extract message from chunk
                    message = chunk.get("message", {})

                    # Check for tool calls (MCP tool invocations)
                    tool_calls = message.get("tool_calls", [])
                    if tool_calls:
                        for tc in tool_calls:
                            yield {
                                "type": "tool_call",
                                "tool_call": tc,
                            }

                    # Check for tool results (MCP tool execution results)
                    tool_results = message.get("tool_results", [])
                    if tool_results:
                        for tr in tool_results:
                            yield {
                                "type": "tool_result",
                                "result": tr,
                            }

                    # Check for content
                    content = message.get("content", "")
                    if content:
                        yield {
                            "type": "content",
                            "content": content,
                        }

                    # Check for done
                    if chunk.get("done"):
                        yield {
                            "type": "done",
                            "total_duration": chunk.get("total_duration"),
                            "eval_count": chunk.get("eval_count"),
                        }
                        return

        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama HTTP error: {e.response.status_code}")
            yield {"type": "error", "error": str(e)}
        except Exception as e:
            logger.error(f"Ollama stream error: {e}")
            yield {"type": "error", "error": str(e)}

    async def chat_sync(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """Non-streaming chat completion with MCP support."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }

        if mcp_servers:
            payload["mcp_servers"] = mcp_servers

        try:
            resp = await self.client.post(
                f"{self.base_url}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Ollama sync error: {e}")
            return {"error": str(e)}


# Global instance
ollama = OllamaClient()
