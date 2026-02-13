# A2A + Remote MCP - Feature Roadmap

## Completed Features

### A2A Bridge v2
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

### Manager Agent v2 (ADK-Free)
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

**Async Flow**:
```
Manager calls: LocalCoder:generate_code({..., _async: true})
  → Returns immediately: "Task {id} submitted"
  → Manager acknowledges: "Working on that..."
  → Background: Task executes
  → Manager polls: LocalCoder:get_task_result({task_id: "..."})
  → Returns: Result content
```

---

## Pending Features

### 1. Client-side Async Handling
**Current State**: Async delegation works at bridge level. Client needs to handle result injection.

**TODO**:
- [ ] Track pending task_ids from async tool results
- [ ] Poll agents for completion
- [ ] Inject results into conversation context
- [ ] Webhook-based push notifications (alternative to polling)

### 2. A2A Push Notifications
**Current State**: `pushNotifications: false` in Agent Card

**TODO**:
- [ ] Webhook registration endpoint
- [ ] Task state change notifications
- [ ] Long-running task progress updates
- [ ] Notification retry with backoff

### 3. A2A Artifacts
**Current State**: `artifacts: []` always empty

**TODO**:
- [ ] File artifact support (code, documents)
- [ ] Structured data artifacts
- [ ] Artifact streaming for large files
- [ ] Artifact references across tasks

### 4. Input-Required State
**Current State**: Not implemented

**TODO**:
- [ ] Detect when agent needs user input
- [ ] Pause task in `input-required` state
- [ ] Resume with additional input
- [ ] Timeout for input requests

### 5. Multi-Agent Conversations
**Current State**: Manager delegates to one agent at a time

**TODO**:
- [ ] Agent-to-agent direct communication
- [ ] Shared context between agents
- [ ] Conversation threading
- [ ] Agent handoff protocol

### 6. Security Enhancements
**TODO**:
- [ ] Agent authentication (mTLS, API keys)
- [ ] Request signing
- [ ] Rate limiting
- [ ] Audit logging

---

## Remote MCP Usage

### Target: mcp-calendar.service on solar (Vikunja)

**Test from Ollama directly**:
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

**Available tools** (via JIT discovery):
- `calendar:list_tasks` - List tasks with filtering
- `calendar:get_task` - Get task details by ID
- `calendar:create_task` - Create new task
- `calendar:update_task` - Update existing task
- `calendar:complete_task` - Mark task as done

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User / Client                                │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Manager Agent v2 (localhost)                      │
│                    Ollama/llama3.3:70b                               │
│                                                                      │
│  Discovers agents → Routes tasks → Aggregates results                │
└─────────────────────────────────────────────────────────────────────┘
                    │                       │
         A2A JSON-RPC                A2A JSON-RPC
                    │                       │
                    ▼                       ▼
┌────────────────────────┐    ┌────────────────────────────────────────┐
│  LocalCoder (8002)     │    │  Remote Agents                         │
│  Ollama/ministral-3    │    │                                        │
│                        │    │  ┌──────────────────────────────────┐  │
│  Skills:               │    │  │ CalendarAgent (solar:8085)       │  │
│  - Code generation     │    │  │ MCP HTTP Transport               │  │
│  - File operations     │    │  │ Skills: calendar, scheduling     │  │
│  - Code analysis       │    │  └──────────────────────────────────┘  │
│                        │    │                                        │
│  MCP: filesystem       │    │  ┌──────────────────────────────────┐  │
└────────────────────────┘    │  │ Future: Other remote agents      │  │
                              │  └──────────────────────────────────┘  │
                              └────────────────────────────────────────┘
```

---

## Next Steps

1. Enhance manager delegation robustness
2. Add push notifications for long-running tasks
3. Implement A2A artifacts support
4. Add agent authentication
