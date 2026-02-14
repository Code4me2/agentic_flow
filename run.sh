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
    AGENT_NAME="LocalCoder" \
    AGENT_DESCRIPTION="Code generation agent with filesystem access via native MCP" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python a2a_ollama_bridge.py
}

# Start the A2A-Ollama bridge for worker agent
start_worker_bridge() {
    log_info "Starting Worker Bridge (nemotron-3-nano) on port 8001..."
    activate_venv
    BRIDGE_PORT=8001 \
    BRIDGE_MODEL="nemotron-3-nano:30b" \
    AGENT_NAME="LocalWorker" \
    AGENT_DESCRIPTION="General purpose reasoning and task execution agent" \
    python a2a_ollama_bridge.py
}

# Start the manager CLI
start_manager() {
    log_info "Starting Manager Agent..."
    activate_venv
    WORKER_URLS="http://localhost:8001,http://localhost:8002" \
    MANAGER_MODEL="llama3.3:70b" \
    TOOLS_PATH="/home/velvetm/Desktop" \
    python manager_agent.py
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
    AGENT_NAME="LocalWorker" \
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
    AGENT_NAME="LocalCoder" \
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
    echo "  all         Start all components (worker, coder, manager)"
    echo "  worker      Start Worker Bridge only (port 8001)"
    echo "  coder       Start Coder Bridge only (port 8002)"
    echo "  manager     Start Manager Agent only (CLI)"
    echo "  status      Check status of all components"
    echo "  discover    Test A2A agent discovery"
    echo "  test        Test A2A JSON-RPC endpoint"
    echo "  test-async  Run async delegation test"
    echo "  help        Show this help message"
    echo ""
    echo "Architecture (ADK-Free):"
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  Manager (Ollama + Webhook Server :8001)            │"
    echo "  │    │                                                │"
    echo "  │    ├──→ Worker Bridge (:8001)                       │"
    echo "  │    │      └──→ Ollama/nemotron-3-nano:30b           │"
    echo "  │    │                                                │"
    echo "  │    └──→ Coder Bridge (:8002)                        │"
    echo "  │           └──→ Ollama/qwen3-coder:30b               │"
    echo "  │                 └──→ Native MCP (filesystem, etc)   │"
    echo "  │                                                     │"
    echo "  │  Push notifications: HMAC-signed webhooks           │"
    echo "  │  Async delegation with turn-boundary injection      │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
    echo "Endpoints:"
    echo "  GET  /.well-known/agent.json   - Agent discovery"
    echo "  POST /a2a                      - JSON-RPC (message/send, tasks/get)"
    echo "  POST /mcp                      - MCP tools endpoint"
    echo "  POST /notifications/register   - Webhook registration"
    echo "  GET  /health                   - Health check"
    echo ""
    echo "Prerequisites:"
    echo "  1. Ollama running: systemctl start ollama"
    echo "  2. Models: llama3.3:70b, nemotron-3-nano:30b, qwen3-coder:30b-a3b-q8_0"
}

# Main command dispatcher
case "${1:-help}" in
    all)
        start_all
        ;;
    worker)
        check_prereqs && start_worker_bridge
        ;;
    coder)
        check_prereqs && start_coder_bridge
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
    help|*)
        show_help
        ;;
esac
