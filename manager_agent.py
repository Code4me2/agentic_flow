"""
Manager Agent - Orchestrates tasks and delegates to Worker Agents via A2A Protocol.
Uses local Ollama model (ministral-3:14b) for orchestration.

Architecture (Option A):
    Manager (ADK/LiteLLM) → A2A Protocol → Bridge → Ollama (native MCP)

Workers are discovered via A2A agent cards:
    - LocalCoder: http://localhost:8002/.well-known/agent.json
    - LocalWorker: http://localhost:8001/.well-known/agent.json
"""

import os
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import Agent
from google.adk.models import LiteLlm

# A2A Worker Registry
WORKERS = {
    "LocalCoder": {
        "url": "http://localhost:8002",
        "description": "Code generation with filesystem MCP tools",
        "model": "qwen3-coder-tools:latest"
    },
    "LocalWorker": {
        "url": "http://localhost:8001",
        "description": "General task execution with nemotron-3-nano",
        "model": "nemotron-3-nano:30b"
    }
}


async def discover_workers() -> dict:
    """Fetch agent cards from all registered workers"""
    discovered = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, info in WORKERS.items():
            try:
                response = await client.get(f"{info['url']}/.well-known/agent.json")
                if response.status_code == 200:
                    discovered[name] = {
                        **info,
                        "agent_card": response.json(),
                        "status": "online"
                    }
                else:
                    discovered[name] = {**info, "status": "error", "error": response.text}
            except Exception as e:
                discovered[name] = {**info, "status": "offline", "error": str(e)}
    return discovered


async def delegate_to_worker(worker_name: str, task: str, context: dict = None) -> dict:
    """Delegate a task to a specific worker via A2A"""
    if worker_name not in WORKERS:
        return {"error": f"Unknown worker: {worker_name}"}

    worker = WORKERS[worker_name]

    async with httpx.AsyncClient(timeout=300) as client:
        try:
            response = await client.post(
                f"{worker['url']}/v1/delegate",
                json={
                    "task": task,
                    "context": context or {},
                    "model": worker["model"],
                    "max_turns": 10
                }
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


def build_worker_instruction() -> str:
    """Build instruction string with available workers"""
    worker_list = "\n".join([
        f"  - {name}: {info['description']} (model: {info['model']})"
        for name, info in WORKERS.items()
    ])

    return f"""You are an orchestration agent that manages and delegates tasks to specialized workers.

Available Workers (via A2A Protocol):
{worker_list}

When given a task:
1. Analyze what type of work is needed
2. Decide which worker is best suited
3. Formulate a clear, specific instruction for the worker
4. Delegate using the delegate_task function
5. Review the result and either:
   - Return the final answer to the user
   - Delegate additional work if needed
   - Ask clarifying questions if the task is ambiguous

For coding tasks, prefer LocalCoder (has filesystem access via MCP).
For general reasoning or analysis, prefer LocalWorker.

Be concise in your orchestration. Don't over-explain your delegation choices."""


# Manager agent - pure orchestration, no MCP tools
manager = Agent(
    name="Orchestrator",
    model=LiteLlm(model="ollama_chat/ministral-3:14b"),
    instruction=build_worker_instruction(),
    tools=[]  # No tools - orchestration only
)


async def main():
    """Test the manager agent and worker discovery"""
    print("=" * 60)
    print("Manager Agent - A2A Orchestration Mode")
    print("=" * 60)

    print("\nDiscovering workers...")
    workers = await discover_workers()

    for name, info in workers.items():
        status = info.get("status", "unknown")
        if status == "online":
            card = info.get("agent_card", {})
            print(f"  ✓ {name}: {card.get('description', 'No description')}")
        else:
            print(f"  ✗ {name}: {status} - {info.get('error', 'Unknown error')}")

    print("\n" + "=" * 60)
    print("Manager ready. Run 'adk web --port 8000' for web interface.")
    print("Or use the delegation API directly.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
