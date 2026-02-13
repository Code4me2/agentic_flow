# Local A2A Agent Stack v2

## Overview

ADK-free multi-agent system using the A2A (Agent-to-Agent) protocol for orchestration.
All agents run locally via Ollama with native MCP tool support.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Manager Agent v2 (Ollama/llama3.3:70b)                     │
│    │                                                        │
│    │  A2A Protocol (JSON-RPC)                               │
│    │                                                        │
│    ├──→ Worker Bridge v2 (:8001)                            │
│    │      └──→ Ollama/nemotron-3-nano:30b                   │
│    │            └──→ General reasoning tasks                │
│    │                                                        │
│    └──→ Coder Bridge v2 (:8002)                             │
│           └──→ Ollama/qwen3-coder:30b                       │
│                 └──→ Native MCP (filesystem, git, etc)      │
└─────────────────────────────────────────────────────────────┘
```

## Requirements

- **Ollama** with custom MCP fork (autonomous-ollama)
- **Python 3.11+**
- **Models**: llama3.3:70b, nemotron-3-nano:30b, qwen3-coder:30b-a3b-q8_0

## Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install minimal dependencies (no Google ADK)
pip install httpx fastapi uvicorn pydantic python-dotenv
```

## Quick Start

```bash
# Start all components
./run.sh all

# Or start individually:
./run.sh worker   # Port 8001
./run.sh coder    # Port 8002
./run.sh manager  # CLI interface
```

## A2A Endpoints

Each bridge exposes standard A2A endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/.well-known/agent.json` | GET | Agent Card for discovery |
| `/a2a` | POST | JSON-RPC endpoint |
| `/health` | GET | Health check |
| `/v1/chat` | POST | Legacy chat (backwards compatible) |

## JSON-RPC Methods

```bash
# Send message (create/continue task)
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "message/send",
    "params": {
      "message": {"role": "user", "content": "List files in /tmp"}
    },
    "id": "1"
  }'

# Stream response
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "message/stream",
    "params": {
      "message": {"role": "user", "content": "Hello"}
    },
    "id": "1"
  }'

# Get task status
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tasks/get",
    "params": {"task_id": "abc123"},
    "id": "1"
  }'

# List tasks
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tasks/list",
    "params": {},
    "id": "1"
  }'
```

## Agent Discovery

The Manager automatically discovers workers via their Agent Cards:

```bash
# Discover agent capabilities
curl http://localhost:8002/.well-known/agent.json
```

Response includes skills that the manager uses for task delegation:
```json
{
  "name": "LocalCoder",
  "skills": [
    {"id": "code-generation", "name": "Code Generation", "tags": ["coding"]},
    {"id": "file-operations", "name": "File Operations", "tags": ["filesystem"]}
  ]
}
```

## Task States

A2A task lifecycle:
- `submitted` - Task created
- `working` - Agent is processing
- `input-required` - Waiting for user input
- `completed` - Successfully finished
- `failed` - Error occurred
- `canceled` - Manually canceled

## MCP Integration

Enable MCP tools via request parameters:

```json
{
  "message": {"role": "user", "content": "Read config.json"},
  "tools_path": "/home/user/project",
  "max_tool_rounds": 15
}
```

Or explicit MCP servers:
```json
{
  "mcp_servers": [
    {"name": "fs", "command": "npx", "args": ["-y", "@anthropic/mcp-server-filesystem", "/path"]}
  ]
}
```

## Remote MCP via WebSocket

Connect to remote MCP servers (e.g., over Tailscale):

```json
{
  "mcp_servers": [
    {
      "name": "remote-tools",
      "transport": "websocket",
      "url": "ws://server.tailnet.ts.net:8080/mcp",
      "headers": {"Authorization": "Bearer token"}
    }
  ]
}
```

## Files

| File | Description |
|------|-------------|
| `a2a_ollama_bridge_v2.py` | A2A-compliant bridge with JSON-RPC |
| `manager_agent_v2.py` | ADK-free orchestrator agent |
| `run.sh` | Launch script |
| `.env` | Configuration |
| `a2a_ollama_bridge.py` | Legacy v1 bridge |

## Comparison: v1 vs v2

| Feature | v1 | v2 |
|---------|----|----|
| Google ADK dependency | Required | None |
| A2A JSON-RPC | No | Yes |
| Task management | No | Yes |
| Skills-based routing | No | Yes |
| Agent Card | Basic | Full spec |
| Streaming | SSE | SSE with events |
