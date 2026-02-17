"""Session state and result queue management."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .models import LoopState
from .agent_client import PendingTask, AgentResult
from .config import config

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """
    Session state for a conversation.

    Tracks:
    - Generation loop state (IDLE vs GENERATING)
    - Conversation messages
    - Pending async tasks
    - Result queue for injection
    """
    session_id: str
    state: LoopState = LoopState.IDLE
    messages: list = field(default_factory=list)
    pending_tasks: Dict[str, PendingTask] = field(default_factory=dict)
    result_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    generation_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    last_access: datetime = field(default_factory=datetime.now)

    # Event to signal IDLE state that results are available
    results_available: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        # Ensure queue and event are proper instances
        if not isinstance(self.result_queue, asyncio.Queue):
            self.result_queue = asyncio.Queue()
        if not isinstance(self.results_available, asyncio.Event):
            self.results_available = asyncio.Event()


class SessionManager:
    """
    Manages session state across requests.

    Handles:
    - Session creation and retrieval
    - Result delivery from webhooks to sessions
    - Session TTL cleanup
    """

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._task_to_session: Dict[str, str] = {}  # task_id -> session_id mapping
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start background cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_expired())
        logger.info("Session manager started")

    async def stop(self):
        """Stop cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Session manager stopped")

    async def get_or_create(self, session_id: str) -> Session:
        """Get existing session or create new one."""
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = Session(session_id=session_id)
                logger.info(f"Created new session: {session_id}")

            session = self._sessions[session_id]
            session.last_access = datetime.now()
            return session

    async def add_pending_task(self, session_id: str, task: PendingTask):
        """Add a pending task to session."""
        async with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].pending_tasks[task.task_id] = task
                self._task_to_session[task.task_id] = session_id
                logger.debug(f"Added pending task {task.task_id} to session {session_id}")

    async def deliver_result(
        self,
        task_id: str,
        result: AgentResult,
        session_id: Optional[str] = None,
    ) -> bool:
        """
        Deliver result to appropriate session's queue.

        Called by webhook handler when push notification arrives.

        If session_id is provided (from callback payload), use it directly.
        Otherwise fall back to task_id → session_id mapping.
        """
        async with self._lock:
            # Use provided session_id or look up by task_id
            if not session_id:
                session_id = self._task_to_session.get(task_id)

            if not session_id or session_id not in self._sessions:
                logger.warning(f"No session found for task {task_id} (session_id={session_id})")
                return False

            session = self._sessions[session_id]

            # Add to queue
            await session.result_queue.put(result)

            # Remove from pending
            if task_id in session.pending_tasks:
                del session.pending_tasks[task_id]
            if task_id in self._task_to_session:
                del self._task_to_session[task_id]

            logger.info(f"Delivered result for task {task_id} to session {session_id}")

            # Signal if in IDLE state
            if session.state == LoopState.IDLE:
                session.results_available.set()
                logger.debug(f"Signaled results available for IDLE session {session_id}")

            return True

    def drain_results(self, session: Session) -> List[AgentResult]:
        """Drain all results from queue (non-blocking)."""
        results = []
        while True:
            try:
                results.append(session.result_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if results:
            logger.info(f"Drained {len(results)} results from session {session.session_id}")

        return results

    async def _cleanup_expired(self):
        """Remove expired sessions periodically."""
        while True:
            await asyncio.sleep(60)

            async with self._lock:
                now = datetime.now()
                ttl = timedelta(seconds=config.session_ttl)

                expired = [
                    sid for sid, session in self._sessions.items()
                    if now - session.last_access > ttl
                ]

                for sid in expired:
                    # Clean up task mappings
                    session = self._sessions[sid]
                    for task_id in session.pending_tasks:
                        if task_id in self._task_to_session:
                            del self._task_to_session[task_id]

                    del self._sessions[sid]
                    logger.info(f"Expired session: {sid}")

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)


# Global instance
sessions = SessionManager()
