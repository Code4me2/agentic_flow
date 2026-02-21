"""Agent discovery and async task submission via MCP protocol."""

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from .config import config

logger = logging.getLogger(__name__)

MANAGER_BASE_PROMPT = (
    "You are a helpful voice assistant that coordinates specialized agents.\n"
    "Keep responses conversational and concise — the user is listening, not reading.\n"
    "Do not use markdown formatting, bullet lists, or code blocks in responses.\n"
    "When delegating to an agent, briefly tell the user what you're doing."
)


@dataclass
class AgentCard:
    """Discovered agent metadata."""
    name: str
    url: str
    description: str
    skills: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    push_notifications: bool = True


@dataclass
class PendingTask:
    """Tracking info for a pending async task."""
    task_id: str
    agent_name: str
    agent_url: str
    tool_name: str
    arguments: Dict[str, Any]
    submitted_at: datetime = field(default_factory=datetime.now)


@dataclass
class AgentResult:
    """Result from an async agent task."""
    task_id: str
    agent_name: str
    tool_name: str
    content: str
    success: bool
    received_at: datetime = field(default_factory=datetime.now)


class AgentClient:
    """
    Client for agent communication via MCP protocol.

    Handles:
    - Agent discovery via MCP tools/list
    - Async task submission via MCP tools/call
    - Tool schema generation for Ollama
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.agents: Dict[str, AgentCard] = {}
        self._discovered = False

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    async def discover(self, urls: Optional[List[str]] = None):
        """
        Discover agents and their tools via MCP protocol.

        Connects to each agent's /mcp endpoint and calls tools/list
        to get available tools.
        """
        urls = urls or config.agent_urls

        for url in urls:
            if not url:
                continue

            try:
                # Get agent metadata from A2A agent card
                info = await self._get_agent_info(url)

                # Discover tools via MCP
                tools = await self._mcp_list_tools(url)

                card = AgentCard(
                    name=info["name"],
                    url=url,
                    description=info["description"],
                    skills=info["skills"],
                    tools=tools,
                )

                self.agents[card.name] = card
                logger.info(f"Discovered agent: {card.name} ({card.description}) at {url} with {len(tools)} tools")

            except Exception as e:
                logger.warning(f"Failed to discover agent at {url}: {e}")

        self._discovered = True
        logger.info(f"Agent discovery complete: {len(self.agents)} agents found")

    async def _get_agent_info(self, url: str) -> Dict[str, Any]:
        """Get agent metadata from A2A agent card endpoint."""
        fallback = {
            "name": f"Agent_{url.split(':')[-1]}",
            "description": f"Agent at {url}",
            "skills": [],
        }
        try:
            resp = await self.client.get(f"{url}/.well-known/agent.json")
            resp.raise_for_status()
            data = resp.json()
            return {
                "name": data.get("name", fallback["name"]),
                "description": data.get("description", fallback["description"]),
                "skills": data.get("skills", []),
            }
        except Exception:
            return fallback

    async def _mcp_list_tools(self, url: str) -> List[Dict[str, Any]]:
        """
        Get tool list from agent via MCP protocol.

        Calls tools/list on the agent's /mcp endpoint.
        """
        request = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": f"list_{secrets.token_hex(4)}",
            "params": {}
        }

        try:
            resp = await self.client.post(f"{url}/mcp", json=request)
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                logger.warning(f"MCP error from {url}: {data['error']}")
                return []

            return data.get("result", {}).get("tools", [])
        except Exception as e:
            logger.warning(f"Failed to list tools from {url}: {e}")
            return []

    def build_system_prompt(self) -> str:
        """
        Build system prompt with agent roster for the manager model.

        Returns a prompt containing behavior instructions and a
        dynamically assembled agent roster from discovered agent cards.
        """
        parts = [MANAGER_BASE_PROMPT]

        if self.agents:
            parts.append("\nAvailable agents:")
            for name, agent in self.agents.items():
                parts.append(f"\n- {name}: {agent.description}")
                for skill in agent.skills:
                    s_desc = skill.get("description", "")
                    if s_desc:
                        parts.append(f"  - {skill.get('name', '')}: {s_desc}")
            parts.append(
                "\nUse the appropriate agent's invoke tool based on what the user needs. "
                "Each invoke runs asynchronously — tell the user you're working on it."
            )

        return "\n".join(parts)

    def get_mcp_servers(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get MCP server configurations for Ollama.

        Returns list of MCP server configs using HTTP transport
        that Ollama can use to discover and call tools via MCP protocol.

        If session_id is provided, it's included in the URL so agents
        can include it in webhook callbacks for result routing.
        """
        servers = []

        for agent in self.agents.values():
            # Include session_id in URL for webhook routing
            url = f"{agent.url}/mcp"
            if session_id:
                url = f"{url}?session_id={session_id}&webhook={config.webhook_url}"

            server = {
                "name": agent.name,
                "transport": "http",
                "url": url,
            }
            servers.append(server)

        return servers

    def get_agent_names(self) -> List[str]:
        """Get list of discovered agent names."""
        return list(self.agents.keys())

    def get_agent(self, name: str) -> Optional[AgentCard]:
        """Get agent by name."""
        return self.agents.get(name)

    async def submit_task_async(
        self,
        agent: AgentCard,
        tool_name: str,
        arguments: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> PendingTask:
        """
        Submit task to agent asynchronously via MCP tools/call.

        The task is submitted and returns immediately. Results will
        be delivered via push notification to the webhook endpoint.
        """
        task_id = task_id or f"task_{secrets.token_hex(8)}"

        # MCP tools/call request
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": task_id,
            "params": {
                "name": tool_name,
                "arguments": arguments,
                # Extension: request async execution with webhook callback
                "_async": True,
                "_webhook": config.webhook_url,
                "_task_id": task_id,
            }
        }

        try:
            # Submit async - don't wait for completion
            resp = await self.client.post(
                f"{agent.url}/mcp",
                json=request,
                timeout=5.0,  # Short timeout - just for submission
            )

            # Check if accepted
            if resp.status_code in (200, 202):
                logger.info(f"Submitted async task {task_id} to {agent.name}:{tool_name}")
            else:
                logger.warning(f"Task submission returned {resp.status_code}")

        except httpx.TimeoutException:
            # Timeout is expected for async - task continues in background
            logger.debug(f"Task {task_id} submitted (timeout expected for async)")
        except Exception as e:
            logger.error(f"Failed to submit task to {agent.name}: {e}")
            raise

        return PendingTask(
            task_id=task_id,
            agent_name=agent.name,
            agent_url=agent.url,
            tool_name=tool_name,
            arguments=arguments,
        )


# Global instance
agents = AgentClient()
