from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .processes import HARD_KILL_SIGNAL, ExecSession, terminate_process_group


LOGICAL_CONTEXT_TTL_SECONDS = 60 * 60
COMPLETED_JOB_TTL_SECONDS = 5 * 60
MAX_LOGICAL_CONTEXTS = 512
MAX_SHARED_JOBS = 256
MAX_CAPABILITY_LEASES = 1024
DEFAULT_CAPABILITY_LEASE_TTL_SECONDS = 15 * 60


@dataclass
class LogicalContextState:
    context_id: str
    canonical_project_root: Path
    effective_workspace_root: Path
    default_cwd: Path
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    leases: int = 0
    mutation_workspace_claimed: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class LogicalContextRegistry:
    """Server-owned logical contexts that survive individual MCP HTTP sessions."""

    def __init__(
        self,
        *,
        ttl_seconds: int = LOGICAL_CONTEXT_TTL_SECONDS,
        on_expire: Callable[[LogicalContextState], None] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._on_expire = on_expire
        self._contexts: dict[str, LogicalContextState] = {}
        self._lock = threading.Lock()

    def set_expire_callback(
        self, callback: Callable[[LogicalContextState], None] | None
    ) -> None:
        self._on_expire = callback

    def _notify_expired(self, states: list[LogicalContextState]) -> None:
        callback = self._on_expire
        if callback is None:
            return
        for state in states:
            try:
                callback(state)
            except Exception:
                pass

    def create(
        self,
        canonical_project_root: Path,
        effective_workspace_root: Path,
        default_cwd: Path,
    ) -> LogicalContextState:
        self.prune()
        evicted: LogicalContextState | None = None
        with self._lock:
            if len(self._contexts) >= MAX_LOGICAL_CONTEXTS:
                evictable = [
                    context_id
                    for context_id, state in self._contexts.items()
                    if state.leases == 0
                ]
                if not evictable:
                    raise RuntimeError("maximum leased logical-context count reached")
                oldest_id = min(
                    evictable,
                    key=lambda item: self._contexts[item].last_seen,
                )
                evicted = self._contexts.pop(oldest_id, None)
            context_id = "ctx_" + secrets.token_urlsafe(24)
            state = LogicalContextState(
                context_id=context_id,
                canonical_project_root=canonical_project_root.resolve(strict=True),
                effective_workspace_root=effective_workspace_root.resolve(strict=True),
                default_cwd=default_cwd.resolve(strict=True),
            )
            self._contexts[context_id] = state
        if evicted is not None:
            self._notify_expired([evicted])
        return state

    def get(self, context_id: str) -> LogicalContextState | None:
        self.prune()
        with self._lock:
            state = self._contexts.get(context_id)
            if state is None:
                return None
            state.last_seen = time.monotonic()
            return state

    def update(
        self,
        state: LogicalContextState,
        *,
        canonical_project_root: Path,
        effective_workspace_root: Path,
        default_cwd: Path,
    ) -> None:
        canonical_project_root = canonical_project_root.resolve(strict=True)
        effective_workspace_root = effective_workspace_root.resolve(strict=True)
        default_cwd = default_cwd.resolve(strict=True)
        abandoned: LogicalContextState | None = None
        with self._lock:
            current = self._contexts.get(state.context_id)
            if current is not state:
                return
            if state.canonical_project_root != canonical_project_root:
                if state.effective_workspace_root != state.canonical_project_root:
                    abandoned = LogicalContextState(
                        context_id=state.context_id,
                        canonical_project_root=state.canonical_project_root,
                        effective_workspace_root=state.effective_workspace_root,
                        default_cwd=state.default_cwd,
                        mutation_workspace_claimed=True,
                    )
                state.mutation_workspace_claimed = False
            state.canonical_project_root = canonical_project_root
            state.effective_workspace_root = effective_workspace_root
            state.default_cwd = default_cwd
            state.last_seen = time.monotonic()
        if abandoned is not None:
            self._notify_expired([abandoned])

    def claim_mutation_workspace(self, state: LogicalContextState) -> bool:
        """Claim mutation ownership and report whether this context must isolate."""
        self.prune()
        with self._lock:
            current = self._contexts.get(state.context_id)
            if current is not state:
                raise RuntimeError("logical context is no longer registered")
            if state.mutation_workspace_claimed:
                return state.effective_workspace_root != state.canonical_project_root
            contended = any(
                other is not state
                and other.mutation_workspace_claimed
                and other.canonical_project_root == state.canonical_project_root
                for other in self._contexts.values()
            )
            state.mutation_workspace_claimed = True
            state.last_seen = time.monotonic()
            return contended

    def rollback_mutation_workspace_claim(self, state: LogicalContextState) -> None:
        with self._lock:
            current = self._contexts.get(state.context_id)
            if (
                current is state
                and state.effective_workspace_root == state.canonical_project_root
            ):
                state.mutation_workspace_claimed = False
                state.last_seen = time.monotonic()

    def retain(self, context_id: str) -> LogicalContextState | None:
        self.prune()
        with self._lock:
            state = self._contexts.get(context_id)
            if state is None:
                return None
            state.leases += 1
            state.last_seen = time.monotonic()
            return state

    def release(self, context_id: str) -> None:
        with self._lock:
            state = self._contexts.get(context_id)
            if state is None:
                return
            state.leases = max(0, state.leases - 1)
            state.last_seen = time.monotonic()

    def prune(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        with self._lock:
            expired = [
                context_id
                for context_id, state in self._contexts.items()
                if state.leases == 0 and state.last_seen < cutoff
            ]
            states = [self._contexts.pop(context_id) for context_id in expired]
        self._notify_expired(states)

    def close(self) -> None:
        with self._lock:
            states = list(self._contexts.values())
            self._contexts.clear()
        self._notify_expired(states)

    def stats(self) -> dict[str, int]:
        self.prune()
        with self._lock:
            return {
                "total": len(self._contexts),
                "claimed": sum(
                    1
                    for state in self._contexts.values()
                    if state.mutation_workspace_claimed
                ),
                "isolated": sum(
                    1
                    for state in self._contexts.values()
                    if state.effective_workspace_root != state.canonical_project_root
                ),
                "capacity": MAX_LOGICAL_CONTEXTS,
                "ttl_seconds": self.ttl_seconds,
            }


@dataclass
class SharedJobRecord:
    session: ExecSession
    owner_context_id: str
    owner_runtime: Any
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)


class SharedJobRegistry:
    """Server-owned job handles with logical-context ownership checks."""

    def __init__(
        self,
        *,
        completed_ttl_seconds: int = COMPLETED_JOB_TTL_SECONDS,
        context_registry: LogicalContextRegistry | None = None,
    ) -> None:
        self.completed_ttl_seconds = completed_ttl_seconds
        self.context_registry = context_registry
        self._jobs: dict[str, SharedJobRecord] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._stop_event = threading.Event()
        self._janitor = threading.Thread(
            target=self._janitor_loop,
            name="devmcp-shared-job-janitor",
            daemon=True,
        )
        self._janitor.start()

    def _janitor_loop(self) -> None:
        interval = max(1.0, min(30.0, self.completed_ttl_seconds / 2.0))
        while not self._stop_event.wait(interval):
            self.prune()
            if self.context_registry is not None:
                self.context_registry.prune()

    def new_handle(self) -> str:
        return "job_" + secrets.token_urlsafe(24)

    def register(
        self, session: ExecSession, *, owner_context_id: str, owner_runtime: Any
    ) -> None:
        self.prune()
        with self._lock:
            if self._closed:
                raise RuntimeError("shared job registry is closed")
            if len(self._jobs) >= MAX_SHARED_JOBS:
                raise RuntimeError("maximum shared job count reached")
            if session.session_id in self._jobs:
                raise RuntimeError("duplicate shared job handle")
            self._jobs[session.session_id] = SharedJobRecord(
                session=session,
                owner_context_id=owner_context_id,
                owner_runtime=owner_runtime,
            )
            context_registry = self.context_registry
        if context_registry is not None:
            retained = context_registry.retain(owner_context_id)
            if retained is None:
                with self._lock:
                    self._jobs.pop(session.session_id, None)
                raise RuntimeError(
                    "logical context expired while registering shared job"
                )

    def lookup(
        self, handle: str, *, owner_context_id: str
    ) -> tuple[str, ExecSession | None]:
        """Return (found|forbidden|missing, session)."""

        self.prune()
        with self._lock:
            record = self._jobs.get(handle)
            if record is None:
                return "missing", None
            if not secrets.compare_digest(record.owner_context_id, owner_context_id):
                return "forbidden", None
            record.last_seen = time.monotonic()
            return "found", record.session

    def contains(self, handle: str) -> bool:
        with self._lock:
            return handle in self._jobs

    def has_running_jobs(self, owner_context_id: str) -> bool:
        self.prune()
        with self._lock:
            return any(
                secrets.compare_digest(record.owner_context_id, owner_context_id)
                and record.session.process.poll() is None
                for record in self._jobs.values()
            )

    def touch(self, handle: str) -> None:
        with self._lock:
            record = self._jobs.get(handle)
            if record is not None:
                record.last_seen = time.monotonic()

    def remove(self, handle: str, *, release_resources: bool = False) -> bool:
        with self._lock:
            record = self._jobs.pop(handle, None)
        if record is None:
            return False
        if self.context_registry is not None:
            self.context_registry.release(record.owner_context_id)
        if release_resources:
            record.session.release_owned_resources()
        return True

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            for record in self._jobs.values():
                record.session.refresh_status()
            expired = [
                handle
                for handle, record in self._jobs.items()
                if record.session.process.poll() is not None
                and record.session.completed_at is not None
                and now - record.session.completed_at > self.completed_ttl_seconds
            ]
            records = [self._jobs.pop(handle) for handle in expired]
        for record in records:
            if self.context_registry is not None:
                self.context_registry.release(record.owner_context_id)
            record.session.close_process_streams()
            record.session.release_owned_resources()

    def stats(self) -> dict[str, int]:
        self.prune()
        with self._lock:
            records = list(self._jobs.values())
            return {
                "total": len(records),
                "running": sum(
                    1 for record in records if record.session.process.poll() is None
                ),
                "capacity": MAX_SHARED_JOBS,
                "completed_ttl_seconds": self.completed_ttl_seconds,
            }

    def close(self) -> None:
        self._stop_event.set()
        if threading.current_thread() is not self._janitor:
            self._janitor.join(timeout=1)
        with self._lock:
            self._closed = True
            records = list(self._jobs.values())
            self._jobs.clear()
        for record in records:
            if self.context_registry is not None:
                self.context_registry.release(record.owner_context_id)
            session = record.session
            if session.process.poll() is None:
                session.terminating = True
                terminate_process_group(session.process, HARD_KILL_SIGNAL, force=True)
                session.refresh_status()
            session.close_process_streams()
            session.release_owned_resources()
