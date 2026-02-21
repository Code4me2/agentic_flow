#!/bin/bash
# Launch script for the Local A2A + MCP Agent Stack
#
# Architecture (ADK-Free):
#   Manager (Ollama) → A2A Protocol → Bridge(s) → Ollama (native MCP)
#
# Features:
#   - Async task delegation with push notifications
#   - Turn-boundary result injection
#   - HMAC-signed webhooks

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check prerequisites
check_prereqs() {
    # Check Ollama
    if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        log_error "Ollama is not running. Start with: ollama serve"
        return 1
    fi
    log_success "Ollama is running"

    # Check venv
    if [ ! -d ".venv" ]; then
        log_warn "Virtual environment not found. Creating..."
        python -m venv .venv
        source .venv/bin/activate
        pip install httpx fastapi uvicorn pydantic python-dotenv
    fi
    log_success "Virtual environment ready"

    return 0
}

# Activate virtual environment
activate_venv() {
    source .venv/bin/activate
}

# Start the A2A-Ollama bridge for coder agent
start_coder_bridge() {
    log_info "Starting Coder Bridge (qwen3-coder) on port 8002..."
    activate_venv
    BRIDGE_PORT=8002 \
    BRIDGE_MODEL="qwen3-coder:30b-a3b-q8_0" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/coder.json" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python a2a_ollama_bridge.py
}

# Start the A2A-Ollama bridge for worker agent
start_worker_bridge() {
    log_info "Starting Worker Bridge (nemotron-3-nano) on port 8001..."
    activate_venv
    BRIDGE_PORT=8001 \
    BRIDGE_MODEL="nemotron-3-nano:30b" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/worker.json" \
    python a2a_ollama_bridge.py
}

# Start the A2A-Ollama bridge for calendar agent
start_calendar_bridge() {
    log_info "Starting Calendar Bridge on port 8003..."
    activate_venv
    BRIDGE_PORT=8003 \
    BRIDGE_MODEL="${CALENDAR_MODEL:-ministral-3:14b}" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/calendar.json" \
    STATIC_TOOLS="invoke,get_task_result" \
    WORKER_MCP_SERVERS='[{"name":"vikunja","transport":"http","url":"http://100.119.170.128:8085/mcp"}]' \
    python a2a_ollama_bridge.py
}

# Start the manager CLI (legacy)
start_manager() {
    log_info "Starting Manager Agent..."
    activate_venv
    WORKER_URLS="http://localhost:8001,http://localhost:8002" \
    MANAGER_MODEL="llama3.3:70b" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python manager_agent.py
}

# Start the orchestrator (OpenAI-compatible API)
start_orchestrator() {
    log_info "Starting Orchestrator on port ${ORCH_PORT:-8000}..."
    activate_venv
    ORCH_PORT="${ORCH_PORT:-8000}" \
    OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}" \
    OLLAMA_MODEL="${OLLAMA_MODEL:-ministral-3:14b}" \
    AGENT_URLS="${AGENT_URLS:-http://localhost:8001,http://localhost:8002}" \
    WEBHOOK_HOST="${WEBHOOK_HOST:-localhost}" \
    TOOLS_PATH="${TOOLS_PATH:-/home/velvetm/Desktop}" \
    python -m orchestrator.main
}

# Test worker discovery
test_discovery() {
    log_info "Testing A2A worker discovery..."

    echo ""
    log_info "Checking Worker Bridge (8001)..."
    curl -s http://localhost:8001/.well-known/agent.json | python -m json.tool 2>/dev/null || log_warn "Worker not available"

    echo ""
    log_info "Checking Coder Bridge (8002)..."
    curl -s http://localhost:8002/.well-known/agent.json | python -m json.tool 2>/dev/null || log_warn "Coder not available"

    echo ""
    log_info "Checking Calendar Bridge (8003)..."
    curl -s http://localhost:8003/.well-known/agent.json | python -m json.tool 2>/dev/null || log_warn "Calendar not available"
}

# Start all components
start_all() {
    check_prereqs || exit 1

    echo ""
    echo "=========================================="
    echo "  Local A2A Agent Stack (ADK-Free)"
    echo "=========================================="
    echo ""

    # Start worker bridge in background
    log_info "Starting Worker Bridge in background..."
    activate_venv
    BRIDGE_PORT=8001 \
    BRIDGE_MODEL="nemotron-3-nano:30b" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/worker.json" \
    python a2a_ollama_bridge.py &
    WORKER_PID=$!
    sleep 2

    if kill -0 $WORKER_PID 2>/dev/null; then
        log_success "Worker Bridge started (PID: $WORKER_PID) on :8001"
    else
        log_error "Worker Bridge failed to start"
        exit 1
    fi

    # Start coder bridge in background
    log_info "Starting Coder Bridge in background..."
    BRIDGE_PORT=8002 \
    BRIDGE_MODEL="qwen3-coder:30b-a3b-q8_0" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/coder.json" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python a2a_ollama_bridge.py &
    CODER_PID=$!
    sleep 2

    if kill -0 $CODER_PID 2>/dev/null; then
        log_success "Coder Bridge started (PID: $CODER_PID) on :8002"
    else
        log_error "Coder Bridge failed to start"
        kill $WORKER_PID 2>/dev/null
        exit 1
    fi

    echo ""
    log_info "A2A Discovery URLs:"
    echo "  Worker: http://localhost:8001/.well-known/agent.json"
    echo "  Coder:  http://localhost:8002/.well-known/agent.json"
    echo ""
    log_info "JSON-RPC Endpoints:"
    echo "  Worker: http://localhost:8001/a2a"
    echo "  Coder:  http://localhost:8002/a2a"
    echo ""
    log_info "Push Notification Endpoints:"
    echo "  Worker: http://localhost:8001/notifications/register"
    echo "  Coder:  http://localhost:8002/notifications/register"
    echo ""

    # Cleanup function
    cleanup() {
        echo ""
        log_info "Shutting down..."
        kill $WORKER_PID $CODER_PID 2>/dev/null
        wait $WORKER_PID $CODER_PID 2>/dev/null
        log_success "All bridges stopped"
    }
    trap cleanup EXIT INT TERM

    # Start manager (foreground)
    log_info "Starting Manager Agent..."
    echo ""
    WORKER_URLS="http://localhost:8001,http://localhost:8002" \
    MANAGER_MODEL="llama3.3:70b" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python manager_agent.py
}

# Show status of all components
status() {
    echo "Component Status:"
    echo ""

    # Check Ollama
    if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        log_success "Ollama: running"
        MODELS=$(curl -s http://localhost:11434/api/tags | python -c "import sys,json; print(', '.join([m['name'] for m in json.load(sys.stdin).get('models',[])]))" 2>/dev/null)
        echo "         Models: $MODELS"
    else
        log_error "Ollama: not running"
    fi

    echo ""

    # Check Worker Bridge
    if curl -s http://localhost:8001/health > /dev/null 2>&1; then
        log_success "Worker Bridge (8001): running"
        curl -s http://localhost:8001/health | python -c "import sys,json; d=json.load(sys.stdin); print(f\"         Tasks: {d.get('active_tasks',0)}\")" 2>/dev/null
    else
        log_warn "Worker Bridge (8001): not running"
    fi

    # Check Coder Bridge
    if curl -s http://localhost:8002/health > /dev/null 2>&1; then
        log_success "Coder Bridge (8002): running"
        curl -s http://localhost:8002/health | python -c "import sys,json; d=json.load(sys.stdin); print(f\"         Tasks: {d.get('active_tasks',0)}\")" 2>/dev/null
    else
        log_warn "Coder Bridge (8002): not running"
    fi

    # Check Calendar Bridge
    if curl -s http://localhost:8003/health > /dev/null 2>&1; then
        log_success "Calendar Bridge (8003): running"
        curl -s http://localhost:8003/health | python -c "import sys,json; d=json.load(sys.stdin); print(f\"         Tasks: {d.get('active_tasks',0)}\")" 2>/dev/null
    else
        log_warn "Calendar Bridge (8003): not running"
    fi
}

# Test A2A JSON-RPC
test_a2a() {
    log_info "Testing A2A JSON-RPC..."

    echo ""
    log_info "Sending message to Coder Bridge..."

    curl -s -X POST http://localhost:8002/a2a \
        -H "Content-Type: application/json" \
        -d '{
            "jsonrpc": "2.0",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "content": "Hello, what can you do?"
                }
            },
            "id": "test-1"
        }' | python -m json.tool 2>/dev/null || log_error "Test failed"
}

# Run async test
test_async() {
    log_info "Running async delegation test..."
    activate_venv
    MANAGER_MODEL="${MANAGER_MODEL:-ministral-3:14b}" python test_async.py "$@"
}

# Help text
show_help() {
    echo "Usage: ./run.sh [command]"
    echo ""
    echo "Commands:"
    echo "  orch        Start Orchestrator stack (worker, coder, orchestrator)"
    echo "  orchestrator Start Orchestrator only (OpenAI-compatible API)"
    echo "  all         Start legacy stack (worker, coder, manager CLI)"
    echo "  worker      Start Worker Bridge only (port 8001)"
    echo "  coder       Start Coder Bridge only (port 8002)"
    echo "  calendar    Start Calendar Bridge only (port 8003)"
    echo "  manager     Start Manager Agent CLI (legacy)"
    echo "  status      Check status of all components"
    echo "  discover    Test A2A agent discovery"
    echo "  test        Test A2A JSON-RPC endpoint"
    echo "  test-async  Run async delegation test (legacy manager)"
    echo "  test-orch   Test orchestrator (add --with-agents for full test)"
    echo "  help        Show this help message"
    echo ""
    echo "Architecture (Orchestrator):"
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  Client (unmute, curl, etc)                         │"
    echo "  │    │                                                │"
    echo "  │    └──→ Orchestrator (:8000)                        │"
    echo "  │           │  OpenAI-compatible API                  │"
    echo "  │           │  Generation loop + result injection     │"
    echo "  │           │                                         │"
    echo "  │           ├──→ Ollama (:11434)                      │"
    echo "  │           │      └──→ Inference + sync MCP tools    │"
    echo "  │           │                                         │"
    echo "  │           ├──→ Worker Bridge (:8001)                │"
    echo "  │           │      └──→ Async agent tasks             │"
    echo "  │           │                                         │"
    echo "  │           ├──→ Coder Bridge (:8002)                 │"
    echo "  │           │      └──→ Async agent tasks             │"
    echo "  │           │                                         │"
    echo "  │           └──→ Calendar Bridge (:8003)              │"
    echo "  │                  └──→ Vikunja MCP (remote)          │"
    echo "  │                                                     │"
    echo "  │  Push notifications → Orchestrator /webhook         │"
    echo "  │  Results injected at generation boundaries          │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
    echo "Orchestrator Endpoints:"
    echo "  POST /v1/chat/completions   - OpenAI-compatible chat"
    echo "  GET  /v1/models             - List models"
    echo "  GET  /v1/events             - SSE for IDLE push updates"
    echo "  POST /webhook               - Push notification receiver"
    echo "  GET  /health                - Health check"
    echo ""
    echo "A2A Bridge Endpoints:"
    echo "  GET  /.well-known/agent.json   - Agent discovery"
    echo "  POST /a2a                      - JSON-RPC (tasks/send, tasks/get)"
    echo "  POST /notifications/register   - Webhook registration"
    echo ""
    echo "Environment Variables:"
    echo "  ORCH_PORT      Orchestrator port (default: 8000)"
    echo "  OLLAMA_URL     Ollama URL (default: http://localhost:11434)"
    echo "  OLLAMA_MODEL   Default model (default: ministral-3:14b)"
    echo "  AGENT_URLS     Comma-separated A2A agent URLs"
    echo "  TOOLS_PATH     MCP tools path"
}

# Start orchestrator with bridges
start_orch_stack() {
    check_prereqs || exit 1

    echo ""
    echo "=========================================="
    echo "  Orchestrator Stack (OpenAI-compatible)"
    echo "=========================================="
    echo ""

    # Start worker bridge in background
    log_info "Starting Worker Bridge in background..."
    activate_venv
    BRIDGE_PORT=8001 \
    BRIDGE_MODEL="nemotron-3-nano:30b" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/worker.json" \
    python a2a_ollama_bridge.py &
    WORKER_PID=$!
    sleep 2

    if kill -0 $WORKER_PID 2>/dev/null; then
        log_success "Worker Bridge started (PID: $WORKER_PID) on :8001"
    else
        log_error "Worker Bridge failed to start"
        exit 1
    fi

    # Start coder bridge in background
    log_info "Starting Coder Bridge in background..."
    BRIDGE_PORT=8002 \
    BRIDGE_MODEL="qwen3-coder:30b-a3b-q8_0" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/coder.json" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python a2a_ollama_bridge.py &
    CODER_PID=$!
    sleep 2

    if kill -0 $CODER_PID 2>/dev/null; then
        log_success "Coder Bridge started (PID: $CODER_PID) on :8002"
    else
        log_error "Coder Bridge failed to start"
        kill $WORKER_PID 2>/dev/null
        exit 1
    fi

    # Start calendar bridge in background
    log_info "Starting Calendar Bridge in background..."
    BRIDGE_PORT=8003 \
    BRIDGE_MODEL="${CALENDAR_MODEL:-ministral-3:14b}" \
    AGENT_CONFIG="$SCRIPT_DIR/agents/calendar.json" \
    STATIC_TOOLS="invoke,get_task_result" \
    WORKER_MCP_SERVERS='[{"name":"vikunja","transport":"http","url":"http://100.119.170.128:8085/mcp"}]' \
    python a2a_ollama_bridge.py &
    CALENDAR_PID=$!
    sleep 2

    if kill -0 $CALENDAR_PID 2>/dev/null; then
        log_success "Calendar Bridge started (PID: $CALENDAR_PID) on :8003"
    else
        log_error "Calendar Bridge failed to start"
        kill $WORKER_PID $CODER_PID 2>/dev/null
        exit 1
    fi

    echo ""
    log_info "A2A Agents:"
    echo "  Worker:   http://localhost:8001"
    echo "  Coder:    http://localhost:8002"
    echo "  Calendar: http://localhost:8003"
    echo ""

    # Cleanup function
    cleanup() {
        echo ""
        log_info "Shutting down..."
        kill $WORKER_PID $CODER_PID $CALENDAR_PID 2>/dev/null
        wait $WORKER_PID $CODER_PID $CALENDAR_PID 2>/dev/null
        log_success "All components stopped"
    }
    trap cleanup EXIT INT TERM

    # Start orchestrator (foreground)
    log_info "Starting Orchestrator..."
    echo ""
    ORCH_PORT=8000 \
    OLLAMA_URL="http://localhost:11434" \
    OLLAMA_MODEL="${OLLAMA_MODEL:-ministral-3:14b}" \
    AGENT_URLS="http://localhost:8001,http://localhost:8002,http://localhost:8003" \
    WEBHOOK_HOST="localhost" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python -m orchestrator.main
}

# Main command dispatcher
case "${1:-help}" in
    all)
        start_all
        ;;
    orch)
        start_orch_stack
        ;;
    orchestrator)
        check_prereqs && start_orchestrator
        ;;
    worker)
        check_prereqs && start_worker_bridge
        ;;
    coder)
        check_prereqs && start_coder_bridge
        ;;
    calendar)
        check_prereqs && start_calendar_bridge
        ;;
    manager)
        check_prereqs && start_manager
        ;;
    status)
        status
        ;;
    discover)
        test_discovery
        ;;
    test)
        test_a2a
        ;;
    test-async)
        shift
        check_prereqs && test_async "$@"
        ;;
    test-orch)
        shift
        activate_venv
        python test_orchestrator.py "$@"
        ;;
    help|*)
        show_help
        ;;
esac
