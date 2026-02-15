# Local A2A Agent Stack

## Overview

ADK-free multi-agent system using the A2A (Agent-to-Agent) protocol for orchestration.
All agents run locally via Ollama with native MCP tool support.

**Features:**
- OpenAI-compatible API (drop-in replacement for chat completions)
- Native MCP tool discovery and execution via Ollama
- Async task delegation with push notifications
- Turn-boundary result injection
- HMAC-signed webhook notifications

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Client (unmute, curl, etc)                    │
│                              │                                   │
│                    POST /v1/chat/completions                     │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │              Orchestrator (:8000)                          │  │
│  │         OpenAI-compatible API + MCP routing                │  │
│  │                                                            │  │
│  │  - Agent discovery via /.well-known/agent.json             │  │
│  │  - Registers agents as MCP servers for Ollama              │  │
│  │  - Streams tool_call and tool_result events                │  │
│  │  - Generation-boundary result injection                    │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│              mcp_servers: [{url: "http://agent/mcp"}]           │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    Ollama (MCP Fork)                       │  │
│  │              Native MCP with JIT Discovery                 │  │
│  │                                                            │  │
│  │  1. Model calls mcp_discover to find tools                 │  │
│  │  2. Model calls agent tools (e.g., OllamaAgent:invoke)     │  │
│  │  3. Ollama executes via HTTP to agent's /mcp endpoint      │  │
│  │  4. Results injected back to model                         │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                     MCP HTTP Transport                           │
│                              │                                   │
│           ┌──────────────────┴──────────────────┐               │
│           ▼                                      ▼               │
│  ┌─────────────────────┐              ┌─────────────────────┐   │
│  │  A2A Bridge (:8001) │              │  Remote Agents      │   │
│  │  ministral-3:14b    │              │  (calendar, etc)    │   │
│  │                     │              │                     │   │
│  │  Tools:             │              │                     │   │
│  │  - invoke           │              │                     │   │
│  │  - generate_code    │              │                     │   │
│  │  - analyze_code     │              │                     │   │
│  │  - answer_question  │              │                     │   │
│  │  - get_task_result  │              │                     │   │
│  └─────────────────────┘              └─────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Requirements

- **Ollama** with custom MCP fork (autonomous-ollama)
- **Python 3.11+**
- **Models**: ministral-3:14b (default), or other Ollama models

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
# Start the orchestrator stack (recommended)
./run.sh orch

# This starts:
#   - A2A Bridge on :8001 (worker agent)
#   - Orchestrator on :8000 (OpenAI-compatible API)

# Test the orchestrator
./run.sh test-orch

# Or start components individually:
./run.sh bridge      # A2A Bridge on :8001
./run.sh orchestrator # Orchestrator on :8000
```

## Using with OpenAI-Compatible Clients

The orchestrator exposes an OpenAI-compatible API:

```bash
# Simple chat
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ministral-3:14b",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'

# With agent discovery (MCP tools)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ministral-3:14b",
    "messages": [{"role": "user", "content": "Use mcp_discover to find tools, then answer my question."}],
    "stream": true,
    "agent_urls": ["http://localhost:8001"]
  }'
```

## Endpoints

### Orchestrator (:8000)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | OpenAI-compatible chat API |
| `/v1/models` | GET | List available models |
| `/health` | GET | Health check |

### A2A Bridge (:8001)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/.well-known/agent.json` | GET | Agent Card for discovery |
| `/a2a` | POST | A2A JSON-RPC endpoint |
| `/mcp` | POST | MCP tools endpoint |
| `/notifications/register` | POST | Webhook registration |
| `/health` | GET | Health check |

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
```

## MCP Tool Calls

Invoke agent tools via MCP endpoint:

```bash
# Async tool call (returns immediately with task_id)
curl -X POST http://localhost:8002/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "generate_code",
      "arguments": {
        "description": "Write a hello world function",
        "language": "python",
        "_async": true
      }
    },
    "id": "1"
  }'
```

## Push Notifications

Register a webhook to receive task completion notifications:

```bash
# Register webhook
curl -X POST http://localhost:8002/notifications/register \
  -H "Content-Type: application/json" \
  -d '{
    "webhook_url": "http://localhost:8001/webhook",
    "task_id": "abc123",
    "events": ["task.completed", "task.failed"]
  }'

# Response includes HMAC secret for verification
# {
#   "subscription_id": "...",
#   "secret": "...",
#   "events": ["task.completed", "task.failed"]
# }
```

Webhook payloads are signed with HMAC-SHA256:
- Header: `X-Signature: sha256=<signature>`
- Header: `X-Subscription-Id: <id>`
- Header: `X-Event: task.completed`

## Files

| File | Description |
|------|-------------|
| `orchestrator/` | OpenAI-compatible API service |
| `orchestrator/api.py` | FastAPI endpoints for /v1/chat/completions |
| `orchestrator/generation_loop.py` | MCP-aware generation with result injection |
| `orchestrator/agent_client.py` | Agent discovery and MCP server config |
| `orchestrator/ollama_client.py` | Ollama API client with MCP support |
| `a2a_ollama_bridge.py` | A2A-compliant bridge with MCP tools |
| `run.sh` | Launch script |
| `test_orchestrator.py` | Orchestrator test script |

## Environment Variables

```bash
# Orchestrator configuration
ORCHESTRATOR_PORT=8000
ORCHESTRATOR_HOST=0.0.0.0
OLLAMA_API_BASE=http://localhost:11434

# Bridge configuration
BRIDGE_PORT=8001
BRIDGE_MODEL=ministral-3:14b
AGENT_NAME=OllamaAgent
TOOLS_PATH=/home/user/project

# Notification settings
NOTIFICATION_MAX_RETRIES=5
NOTIFICATION_TIMEOUT=10
```

## MCP Tool Flow

When a client sends a request with `agent_urls`, the orchestrator:

1. Discovers agents via `/.well-known/agent.json`
2. Registers them as MCP servers for Ollama
3. Model calls `mcp_discover` to find available tools
4. Model calls tools like `OllamaAgent:answer_question`
5. Ollama executes via HTTP POST to agent's `/mcp` endpoint
6. Results are streamed back as `mcp.tool_result` events
