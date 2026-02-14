# Local A2A Agent Stack

## Overview

ADK-free multi-agent system using the A2A (Agent-to-Agent) protocol for orchestration.
All agents run locally via Ollama with native MCP tool support.

**Features:**
- Async task delegation with push notifications
- Turn-boundary result injection
- HMAC-signed webhook notifications
- MCP tools via native Ollama integration

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Manager Agent (Ollama + Webhook Server :8001)              │
│    │                                                        │
│    │  A2A Protocol (JSON-RPC) + Push Notifications          │
│    │                                                        │
│    ├──→ Worker Bridge (:8001)                               │
│    │      └──→ Ollama/nemotron-3-nano:30b                   │
│    │            └──→ General reasoning tasks                │
│    │                                                        │
│    └──→ Coder Bridge (:8002)                                │
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

# Run async test
./run.sh test-async
```

## Endpoints

Each bridge exposes:

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
| `a2a_ollama_bridge.py` | A2A-compliant bridge with JSON-RPC and push notifications |
| `manager_agent.py` | ADK-free orchestrator with async delegation |
| `test_async.py` | Async delegation test script |
| `run.sh` | Launch script |
| `TODO.md` | Feature roadmap |

## Environment Variables

```bash
# Bridge configuration
BRIDGE_PORT=8002
BRIDGE_MODEL=qwen3-coder:30b-a3b-q8_0
AGENT_NAME=LocalCoder
TOOLS_PATH=/home/user/project

# Manager configuration
MANAGER_MODEL=llama3.3:70b
WORKER_URLS=http://localhost:8001,http://localhost:8002
USE_PUSH_NOTIFICATIONS=true
WEBHOOK_PORT=8001

# Notification settings
NOTIFICATION_MAX_RETRIES=5
NOTIFICATION_TIMEOUT=10
```
