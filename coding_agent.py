"""
Coding Agent - Specialized code generation and tool-use agent.
Uses local Ollama model (qwen3-coder-tools) with native MCP support.
Exposes capabilities via A2A agent.json on port 8002.
"""

import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from google.adk.agents import Agent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.models import LiteLlm

# Use qwen3-coder-tools with native tool-use capabilities
model = LiteLlm(model="ollama_chat/qwen3-coder-tools:latest")

coder = Agent(
    name="LocalCoder",
    model=model,
    instruction="""Expert Python developer focusing on high-performance local servers.
You have access to filesystem tools via MCP. Use them to read, write, and navigate code.
When given a coding task:
1. Analyze the existing code structure
2. Plan your changes
3. Implement with clean, efficient code
4. Verify your changes work correctly""",
    tools=["filesystem_mcp"]
)

# Convert to A2A and serve locally on Port 8002
app = to_a2a(coder, port=8002)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
