"""
Agentic Orchestrator - Main Entry Point

OpenAI-compatible API server with native Ollama MCP support.
Agents are discovered via /.well-known/agent.json and registered
as MCP servers for Ollama to handle tool discovery and execution.

Usage:
    python -m orchestrator.main

Environment Variables:
    ORCH_HOST       - Server host (default: 0.0.0.0)
    ORCH_PORT       - Server port (default: 8000)
    OLLAMA_URL      - Ollama API URL (default: http://localhost:11434)
    OLLAMA_MODEL    - Default model (default: ministral-3:14b)
    AGENT_URLS      - Comma-separated agent URLs (MCP endpoints)
    WEBHOOK_HOST    - Hostname for webhook callbacks
    SESSION_TTL     - Session TTL in seconds (default: 1800)
"""

import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import config
from .state import sessions
from .ollama_client import ollama
from .agent_client import agents
from .api import router as api_router
from .webhooks import router as webhook_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""

    # Startup
    logger.info("=" * 60)
    logger.info("Agentic Orchestrator Starting")
    logger.info("=" * 60)

    # Start session manager
    await sessions.start()

    # Discover agents via MCP
    if config.agent_urls:
        logger.info(f"Discovering agents from: {config.agent_urls}")
        await agents.discover()

        if agents.agents:
            logger.info(f"Discovered {len(agents.agents)} agents:")
            for name, agent in agents.agents.items():
                logger.info(f"  - {name}: {len(agent.tools)} MCP tools at {agent.url}")
        else:
            logger.warning("No agents discovered")
    else:
        logger.warning("No AGENT_URLS configured - agent delegation disabled")

    # Log configuration
    logger.info(f"Ollama URL: {config.ollama_url}")
    logger.info(f"Ollama Model: {config.ollama_model}")
    logger.info(f"Webhook URL: {config.webhook_url}")
    logger.info(f"Session TTL: {config.session_ttl}s")

    logger.info("-" * 60)
    logger.info(f"Server ready at http://{config.host}:{config.port}")
    logger.info(f"OpenAI endpoint: http://{config.host}:{config.port}/v1/chat/completions")
    logger.info(f"Webhook endpoint: http://{config.host}:{config.port}/webhook")
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("Shutting down...")
    await sessions.stop()
    await ollama.close()
    await agents.close()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Agentic Orchestrator",
    description=(
        "OpenAI-compatible API with native Ollama MCP support. "
        "Agents are discovered and registered as MCP servers. "
        "Ollama handles tool discovery (mcp_discover) and execution. "
        "Results are injected at generation boundaries."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(api_router)
app.include_router(webhook_router)


@app.get("/health")
async def health():
    """Health check endpoint."""
    agent_info = {}
    for name, agent in agents.agents.items():
        agent_info[name] = {
            "url": agent.url,
            "mcp_tools": len(agent.tools),
        }

    return {
        "status": "healthy",
        "agents": agent_info,
        "session_count": sessions.session_count,
        "ollama_url": config.ollama_url,
        "model": config.ollama_model,
    }


@app.get("/")
async def root():
    """Root endpoint with basic info."""
    return {
        "name": "Agentic Orchestrator",
        "version": "0.3.0",
        "protocol": "Native Ollama MCP",
        "endpoints": {
            "chat": "/v1/chat/completions",
            "models": "/v1/models",
            "events": "/v1/events",
            "webhook": "/webhook",
            "health": "/health",
        },
    }


def main():
    """Run the orchestrator server."""
    uvicorn.run(
        "orchestrator.main:app",
        host=config.host,
        port=config.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
