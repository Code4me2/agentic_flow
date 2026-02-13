"""
Worker Agent - Exposes capabilities via A2A agent.json (Agent Card)
Uses local Ollama model (deepseek-r1) for reasoning tasks.
"""

import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from google.adk.agents import Agent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.models import LiteLlm

# Use local Ollama model
model = LiteLlm(model="ollama_chat/nemotron-3-nano:30b")

worker = Agent(
    name="LocalWorker",
    model=model,
    instruction="General-purpose worker agent for task execution and coordination.",
    tools=["filesystem_mcp"]  # Connect your MCP tools here
)

# Convert to A2A and serve locally on Port 8001
app = to_a2a(worker, port=8001)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
