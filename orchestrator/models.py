"""Data models for orchestrator."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ============================================================================
# OpenAI-Compatible Request/Response Models
# ============================================================================

class ChatMessage(BaseModel):
    """OpenAI chat message format."""
    role: str
    content: str
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI chat completion request."""
    model: str
    messages: List[ChatMessage]
    stream: bool = True
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    # Extensions
    session_id: Optional[str] = None


class ModelInfo(BaseModel):
    """OpenAI model info."""
    id: str
    object: str = "model"
    created: int
    owned_by: str = "ollama"


# ============================================================================
# Internal State Models
# ============================================================================

class LoopState(str, Enum):
    """Generation loop state."""
    IDLE = "idle"
    GENERATING = "generating"
