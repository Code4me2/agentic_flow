"""
ADK-Free Manager Agent

Orchestrates tasks across multiple A2A-compatible agents without Google dependencies.
Uses pure Python with httpx for agent discovery and communication.

Features:
    - A2A agent discovery via /.well-known/agent.json
    - Task delegation based on agent skills
    - Multi-agent coordination
    - Streaming response aggregation
"""

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, AsyncGenerator

import httpx
from dotenv import load_dotenv

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
        """Get task status"""
        payload = {
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": task_id},
            "id": "1",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/a2a", json=payload)
            return response.json()


# =============================================================================
# Manager Agent
# =============================================================================

class ManagerAgent:
    """
    Orchestrator agent that delegates tasks to A2A workers.

    Uses local Ollama for reasoning about task delegation,
    then communicates with workers via A2A protocol.
    """

    def __init__(self, model: str = MANAGER_MODEL):
        self.model = model
        self.registry = AgentRegistry()
        self.conversation_history: list[dict] = []

    async def setup(self, worker_urls: list[str]):
        """Discover and register worker agents"""
        await self.registry.discover_many(worker_urls)

    def _build_system_prompt(self) -> str:
        """Build system prompt with agent capabilities"""
        return f"""You are an orchestrator agent. Your role is to:
1. Understand user requests
2. Decide which worker agent(s) can best handle the task
3. Delegate tasks and coordinate responses

{self.registry.get_capabilities_prompt()}

When delegating, respond with a JSON block:
```json
{{"delegate": {{"agent": "AgentName", "task": "specific task description"}}}}
```

If you can answer directly without delegation, just respond normally.
If no suitable agent exists, explain what's needed.
"""

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
        """Process user input - reason and delegate as needed"""

        # Add to conversation
        self.conversation_history.append({"role": "user", "content": user_input})

        # Build messages with system prompt
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
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

                logger.info(f"Delegating to {agent_name}: {task}")

                # Find agent
                agent_info = self.registry.get(agent_name)
                if not agent_info:
                    return f"Error: Agent '{agent_name}' not found"

                # Delegate via A2A
                client = A2AClient(agent_info)
                result = await client.send_message(task, tools_path=tools_path)

                # Extract response
                if "result" in result:
                    task_data = result["result"].get("task", {})
                    messages = task_data.get("messages", [])

                    # Get agent's response
                    for msg in reversed(messages):
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
    """Interactive CLI for manager agent"""

    # Default worker URLs - can be overridden via env
    worker_urls = os.getenv("WORKER_URLS", "http://localhost:8002").split(",")

    print("Manager Agent v2 - ADK-Free Multi-Agent Orchestrator")
    print("=" * 50)

    manager = ManagerAgent()
    await manager.setup(worker_urls)

    print(f"\nDiscovered {len(manager.registry.list_agents())} agent(s)")
    for agent in manager.registry.list_agents():
        print(f"  - {agent.name}: {agent.description}")

    print("\nEnter tasks (Ctrl+C to quit):\n")

    tools_path = os.getenv("TOOLS_PATH", "")

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue

            response = await manager.process(user_input, tools_path=tools_path)
            print(f"\n{response}\n")

        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
