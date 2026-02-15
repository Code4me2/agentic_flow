"""
Agentic Orchestrator

OpenAI-compatible API layer with async agent delegation and
generation-loop result injection.

Components:
- generation_loop: Core loop with result injection at boundaries
- state: Session state and result queue management
- api: OpenAI-compatible endpoints
- webhooks: Push notification receiver
- agent_client: A2A protocol client
- ollama_client: Ollama API streaming client
"""

from .main import app

__version__ = "0.1.0"
