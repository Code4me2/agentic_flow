# A2A + Remote MCP - Feature Roadmap

## Completed Features

### A2A Bridge
- [x] Agent Card at `/.well-known/agent.json` with skills
- [x] JSON-RPC endpoint at `/a2a`
- [x] `message/send` - non-streaming message handling
- [x] `message/stream` - SSE streaming with events
- [x] `tasks/get` - retrieve task by ID
- [x] `tasks/list` - list all tasks with optional state filter
- [x] `tasks/cancel` - cancel running task
- [x] Task state management (submitted → working → completed/failed)
- [x] Task continuation (multi-turn via task_id)
- [x] MCP passthrough via `tools_path`
- [x] Legacy `/v1/chat` compatibility endpoint

### Manager Agent (ADK-Free)
- [x] Agent discovery via A2A cards
- [x] Agent registry with skill-based lookup
- [x] Basic delegation via JSON in responses
- [x] CLI interface for testing

### Remote MCP via HTTP (streamable-http)
- [x] Streamable-http transport in Ollama
- [x] Session ID management (mcp-session-id header)
- [x] Tool name prefix stripping for remote calls
- [x] URL validation for remote transports
- [x] Tested with Vikunja calendar on solar:8085
- [x] JIT discovery works with remote MCP tools

### Agent Delegation via MCP
- [x] MCP endpoint (`/mcp`) on A2A bridge for agent-as-tool
- [x] Agents expose tools: `invoke`, `generate_code`, `analyze_code`, `answer_question`
- [x] Manager discovers agents via `mcp_discover`
- [x] Manager calls agent tools (delegation = tool call)
- [x] Multi-agent delegation tested (LocalCoder + Calendar)
- [x] Async execution with `_async: true` parameter
- [x] Background task execution
- [x] `get_task_result` tool for polling async results
- [x] `tasks/get` MCP method for task status

### Client-side Async Handling (Turn-Boundary Injection)
- [x] `AsyncTaskInfo` dataclass for tracking delegated tasks
- [x] `_pending_tasks` dict in ManagerAgent for state tracking
- [x] `_completed_results` asyncio.Queue for injection buffer
- [x] Background polling task (`_poll_pending_tasks`)
- [x] Turn-boundary injection via `check_and_inject_results()`
- [x] System prompt awareness of pending task count
- [x] Result injection prompt builder
- [x] Proactive announcement generation at turn boundaries

### A2A Push Notifications
- [x] Webhook registration endpoint on agent (`/notifications/register`)
- [x] `pushNotificationConfig` in Agent Card
- [x] Subscription storage with secrets (`WebhookSubscription`, `NotificationStore`)
- [x] Event emission on task state change (`emit_notification()`)
- [x] HMAC signature on webhook payloads (`X-Signature: sha256=...`)
- [x] Retry with exponential backoff (1s, 2s, 4s, 8s, 16s)
- [x] Manager webhook handler endpoint (FastAPI on port 8001)
- [x] Signature verification on incoming webhooks
- [x] Fallback to polling when push unavailable

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User / Client                                │
│                    (CLI, Voice/unmuted, API)                         │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Manager Agent (localhost)                      │
│                    Ollama + Webhook Server (port 8001)               │
│                                                                      │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Agent Registry  │  │ Async Task Mgmt  │  │ Result Injection   │  │
│  │ - Discovery     │  │ - _pending_tasks │  │ - Turn boundary    │  │
│  │ - Skill lookup  │  │ - Push/Poll      │  │ - Announcements    │  │
│  └─────────────────┘  └──────────────────┘  └────────────────────┘  │
│                              ▲                                       │
│                              │ POST /webhook (push notification)     │
│  Discovers agents → Delegates (sync/async) → Injects results         │
└─────────────────────────────────────────────────────────────────────┘
                    │                       │
         MCP / A2A JSON-RPC         MCP / A2A JSON-RPC
            + webhooks                      │
                    │                       │
                    ▼                       ▼
┌────────────────────────┐    ┌────────────────────────────────────────┐
│  LocalCoder (8002)     │    │  Remote Agents                         │
│  Ollama/qwen3-coder    │    │                                        │
│                        │    │  ┌──────────────────────────────────┐  │
│  Endpoints:            │    │  │ CalendarAgent (solar:8085)       │  │
│  - /mcp (MCP tools)    │    │  │ MCP HTTP Transport               │  │
│  - /a2a (A2A tasks)    │    │  │ Skills: calendar, scheduling     │  │
│  - /notifications/*    │    │  └──────────────────────────────────┘  │
│                        │    │                                        │
│  MCP Tools:            │    │                                        │
│  - invoke              │    │                                        │
│  - generate_code       │    │                                        │
│  - analyze_code        │    │                                        │
│  - answer_question     │    │                                        │
│  - get_task_result     │    │                                        │
│                        │    │                                        │
│  Push: HMAC-signed     │    │                                        │
│  webhooks on complete  │    │                                        │
└────────────────────────┘    └────────────────────────────────────────┘
```

---

## Pending Features

### A2A Artifacts
**Current State**: `artifacts: []` always empty

- [ ] File artifact support (code, documents)
- [ ] Structured data artifacts
- [ ] Artifact streaming for large files
- [ ] Artifact references across tasks

### Input-Required State
**Current State**: Not implemented

- [ ] Detect when agent needs user input
- [ ] Pause task in `input-required` state
- [ ] Resume with additional input
- [ ] Timeout for input requests

### Multi-Agent Conversations
**Current State**: Manager delegates to one agent at a time

- [ ] Agent-to-agent direct communication
- [ ] Shared context between agents
- [ ] Conversation threading
- [ ] Agent handoff protocol

### Security Enhancements
- [ ] Agent authentication (mTLS, API keys)
- [ ] Request signing
- [ ] Rate limiting
- [ ] Audit logging

---

## Quick Reference

### Remote MCP (Vikunja Calendar)
```bash
curl http://localhost:11434/api/chat -d '{
  "model": "ministral-3:14b",
  "messages": [{"role": "user", "content": "List my tasks"}],
  "mcp_servers": [{
    "name": "calendar",
    "transport": "http",
    "url": "http://100.119.170.128:8085/mcp"
  }],
  "stream": false
}'
```

### Async Delegation Flow
```
Manager calls: LocalCoder:invoke({task: "...", _async: true})
  → Returns immediately: "Task {id} submitted"
  → Agent sends POST /webhook on completion
  → Manager verifies HMAC signature
  → Result queued for turn-boundary injection
  → Manager announces: "Here's what LocalCoder found..."
```

### Configuration
```bash
# Manager
MANAGER_MODEL=llama3.3:70b
WORKER_URLS=http://localhost:8002
USE_PUSH_NOTIFICATIONS=true
WEBHOOK_PORT=8001

# Agent Bridge
BRIDGE_PORT=8002
BRIDGE_MODEL=qwen3-coder:30b-a3b-q8_0
AGENT_NAME=LocalCoder
NOTIFICATION_MAX_RETRIES=5
```

---

## Next Steps

1. **Integration Testing** - Full voice flow (unmuted)
   - STT → VAD → Manager → Async delegation → TTS → Result announcement

2. **A2A Artifacts** - File/data outputs from tasks
   - Artifact storage and retrieval
   - Streaming for large files

3. **Agent Authentication** - Secure agent-to-agent communication
   - API keys or mTLS
   - Request signing
