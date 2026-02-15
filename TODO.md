# A2A + Remote MCP - Feature Roadmap

## Completed Features

### Orchestrator (OpenAI-Compatible API)
- [x] OpenAI-compatible `/v1/chat/completions` endpoint
- [x] `/v1/models` endpoint listing Ollama models
- [x] Agent discovery via `/.well-known/agent.json`
- [x] Agent registration as MCP servers for Ollama
- [x] MCP-based tool discovery (JIT via `mcp_discover`)
- [x] Streaming responses with `tool_call` and `tool_result` events
- [x] Session management for multi-turn conversations
- [x] Generation-boundary result injection

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
│              (unmute, curl, OpenAI-compatible apps)                  │
└─────────────────────────────────────────────────────────────────────┘
                                │
                      POST /v1/chat/completions
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Orchestrator (localhost:8000)                     │
│                      OpenAI-Compatible API                           │
│                                                                      │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Agent Discovery │  │ MCP Server Mgmt  │  │ Result Injection   │  │
│  │ - agent.json    │  │ - mcp_servers    │  │ - Generation loop  │  │
│  │ - Registration  │  │ - JIT discovery  │  │ - Stream events    │  │
│  └─────────────────┘  └──────────────────┘  └────────────────────┘  │
│                                                                      │
│  Registers agents as MCP servers → Ollama handles tool execution     │
└─────────────────────────────────────────────────────────────────────┘
                                │
                    mcp_servers: [{url, transport}]
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Ollama (MCP Fork, localhost:11434)                │
│                                                                      │
│  1. Receives mcp_servers config from orchestrator                    │
│  2. Model calls mcp_discover → finds agent tools                     │
│  3. Model calls tools → Ollama POSTs to agent /mcp                   │
│  4. Results returned to model for next generation                    │
└─────────────────────────────────────────────────────────────────────┘
                                │
                       MCP HTTP Transport
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
┌────────────────────────┐         ┌────────────────────────────────┐
│  A2A Bridge (8001)     │         │  Remote Agents                 │
│  ministral-3:14b       │         │                                │
│                        │         │  ┌──────────────────────────┐  │
│  Endpoints:            │         │  │ CalendarAgent            │  │
│  - /mcp (MCP tools)    │         │  │ MCP HTTP Transport       │  │
│  - /a2a (A2A tasks)    │         │  │ Skills: calendar, etc    │  │
│  - /notifications/*    │         │  └──────────────────────────┘  │
│                        │         │                                │
│  MCP Tools:            │         │                                │
│  - invoke              │         │                                │
│  - generate_code       │         │                                │
│  - analyze_code        │         │                                │
│  - answer_question     │         │                                │
│  - get_task_result     │         │                                │
│                        │         │                                │
│  Push: HMAC-signed     │         │                                │
│  webhooks on complete  │         │                                │
└────────────────────────┘         └────────────────────────────────┘
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

### Orchestrator (OpenAI-Compatible)
```bash
# Start the stack
./run.sh orch

# Chat with agent tools
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ministral-3:14b",
    "messages": [{"role": "user", "content": "Calculate 10 factorial"}],
    "stream": true,
    "agent_urls": ["http://localhost:8001"]
  }'
```

### Direct Ollama with Remote MCP
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

### MCP Tool Flow
```
1. Client → POST /v1/chat/completions with agent_urls
2. Orchestrator discovers agents, registers as mcp_servers
3. Ollama receives mcp_servers config
4. Model calls mcp_discover → finds OllamaAgent:* tools
5. Model calls OllamaAgent:answer_question
6. Ollama POSTs to agent /mcp endpoint
7. Agent executes, returns result
8. Model incorporates result, generates response
```

### Configuration
```bash
# Orchestrator
ORCHESTRATOR_PORT=8000
OLLAMA_API_BASE=http://localhost:11434

# Agent Bridge
BRIDGE_PORT=8001
BRIDGE_MODEL=ministral-3:14b
AGENT_NAME=OllamaAgent
NOTIFICATION_MAX_RETRIES=5
```

---

## Next Steps

1. **Unmute Integration** - Voice interface with orchestrator
   - Configure unmute to use orchestrator as OpenAI endpoint
   - STT → Orchestrator → Agent delegation → TTS
   - Handle streaming tool_call/tool_result events

2. **A2A Artifacts** - File/data outputs from tasks
   - Artifact storage and retrieval
   - Streaming for large files

3. **Agent Authentication** - Secure agent-to-agent communication
   - API keys or mTLS
   - Request signing

4. **Larger Model Support** - Handle longer inference times
   - Increase Ollama MCP timeout (currently 30s)
   - Async tool execution with polling
   - Progress notifications for long-running tasks
