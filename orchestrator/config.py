"""Orchestrator configuration."""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    """Configuration loaded from environment variables."""

    # Server
    host: str = field(default_factory=lambda: os.getenv("ORCH_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("ORCH_PORT", "8000")))

    # Ollama
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "ministral-3:14b"))
    tools_path: str = field(default_factory=lambda: os.getenv("TOOLS_PATH", ""))

    # Agents
    agent_urls: List[str] = field(default_factory=lambda:
        [u.strip() for u in os.getenv("AGENT_URLS", "").split(",") if u.strip()])
    agent_timeout: float = field(default_factory=lambda: float(os.getenv("AGENT_TIMEOUT", "300")))

    # Sessions
    session_ttl: int = field(default_factory=lambda: int(os.getenv("SESSION_TTL", "1800")))

    # Webhook
    webhook_host: str = field(default_factory=lambda: os.getenv("WEBHOOK_HOST", "localhost"))
    webhook_port: int = field(default_factory=lambda: int(os.getenv("ORCH_PORT", "8000")))

    @property
    def webhook_url(self) -> str:
        """URL where this orchestrator receives webhooks."""
        return f"http://{self.webhook_host}:{self.webhook_port}/webhook"

    def __post_init__(self):
        """Log configuration on startup."""
        pass


# Global config instance
config = Config()
