from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    workspace: Path
    default_cwd: Path
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    leases: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class LogicalContextRegistry:
    """Server-owned logical contexts that survive individual MCP HTTP sessions."""

    def __init__(self, *, ttl_seconds: int = LOGICAL_CONTEXT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._contexts: dict[str, LogicalContextState] = {}
        self._lock = threading.Lock()

    def create(self, workspace: Path, default_cwd: Path) -> LogicalContextState:
        self.prune()
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
                self._contexts.pop(oldest_id, None)
            context_id = "ctx_" + secrets.token_urlsafe(24)
            state = LogicalContextState(
                context_id=context_id,
                workspace=workspace.resolve(strict=True),
                default_cwd=default_cwd.resolve(strict=True),
            )
            self._contexts[context_id] = state
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
        self, state: LogicalContextState, *, workspace: Path, default_cwd: Path
    ) -> None:
        workspace = workspace.resolve(strict=True)
        default_cwd = default_cwd.resolve(strict=True)
        with self._lock:
            current = self._contexts.get(state.context_id)
            if current is not state:
                return
            state.workspace = workspace
            state.default_cwd = default_cwd
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
            for context_id in expired:
                self._contexts.pop(context_id, None)

    def stats(self) -> dict[str, int]:
        self.prune()
        with self._lock:
            return {
                "total": len(self._contexts),
                "capacity": MAX_LOGICAL_CONTEXTS,
                "ttl_seconds": self.ttl_seconds,
            }


@dataclass
class CapabilityLeaseRecord:
    lease_id: str
    owner_context_id: str
    capability: str
    target: str
    scope: str
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = 0.0
    task_scope_id: str | None = None


class CapabilityLeaseRegistry:
    """Ephemeral, owner-scoped capability grants.

    Leases are memory-only and therefore disappear on restart.  ``once``
    leases are consumed by the first matching operation; ``task`` leases also
    require the matching task scope; ``session`` leases live until expiry or
    explicit revocation.
    """

    def __init__(self) -> None:
        self._leases: dict[str, CapabilityLeaseRecord] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        expired = [
            lease_id
            for lease_id, record in self._leases.items()
            if record.expires_at <= now
        ]
        for lease_id in expired:
            self._leases.pop(lease_id, None)

    def create(
        self,
        *,
        owner_context_id: str,
        capability: str,
        target: str,
        scope: str,
        ttl_seconds: int = DEFAULT_CAPABILITY_LEASE_TTL_SECONDS,
        task_scope_id: str | None = None,
    ) -> CapabilityLeaseRecord:
        if scope not in {"once", "task", "session"}:
            raise ValueError("capability lease scope must be once, task, or session")
        if scope == "task" and not task_scope_id:
            raise ValueError("task-scoped capability lease requires task_scope_id")
        ttl = max(1, min(int(ttl_seconds), 24 * 60 * 60))
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            if len(self._leases) >= MAX_CAPABILITY_LEASES:
                raise RuntimeError("maximum capability lease count reached")
            record = CapabilityLeaseRecord(
                lease_id="lease_" + secrets.token_urlsafe(24),
                owner_context_id=owner_context_id,
                capability=capability,
                target=target,
                scope=scope,
                expires_at=now + ttl,
                task_scope_id=task_scope_id,
            )
            self._leases[record.lease_id] = record
            return record

    def _owner_records_locked(
        self, owner_context_id: str, *, task_scope_id: str | None = None
    ) -> list[CapabilityLeaseRecord]:
        self._prune_locked(time.monotonic())
        return [
            record
            for record in self._leases.values()
            if secrets.compare_digest(record.owner_context_id, owner_context_id)
            and (
                record.scope != "task"
                or (
                    task_scope_id is not None
                    and record.task_scope_id is not None
                    and secrets.compare_digest(record.task_scope_id, task_scope_id)
                )
            )
        ]

    def root_paths(
        self,
        owner_context_id: str,
        *,
        write: bool,
        task_scope_id: str | None = None,
    ) -> list[Path]:
        with self._lock:
            records = self._owner_records_locked(
                owner_context_id, task_scope_id=task_scope_id
            )
            result: list[Path] = []
            for record in records:
                if write and record.capability != "workspace.additional_write_root":
                    continue
                if not write and record.capability not in {
                    "workspace.additional_read_root",
                    "workspace.additional_write_root",
                }:
                    continue
                try:
                    path = Path(record.target).resolve(strict=True)
                except OSError:
                    continue
                if path.is_dir() and path not in result:
                    result.append(path)
            return result

    def consume_root_match(
        self,
        owner_context_id: str,
        path: Path,
        *,
        write: bool,
        task_scope_id: str | None = None,
    ) -> str | None:
        resolved = path.resolve(strict=False)
        with self._lock:
            records = self._owner_records_locked(
                owner_context_id, task_scope_id=task_scope_id
            )
            for record in records:
                if write and record.capability != "workspace.additional_write_root":
                    continue
                if not write and record.capability not in {
                    "workspace.additional_read_root",
                    "workspace.additional_write_root",
                }:
                    continue
                try:
                    target = Path(record.target).resolve(strict=True)
                    resolved.relative_to(target)
                except (OSError, ValueError):
                    continue
                return record.lease_id
        return None

    def consume_once(self, lease_id: str, *, owner_context_id: str) -> bool:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None or record.scope != "once":
                return False
            if not secrets.compare_digest(record.owner_context_id, owner_context_id):
                return False
            self._leases.pop(lease_id, None)
            return True

    def list_owner(
        self, owner_context_id: str, *, task_scope_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            records = self._owner_records_locked(
                owner_context_id, task_scope_id=task_scope_id
            )
            now = time.monotonic()
            return [
                {
                    "lease_id": record.lease_id,
                    "capability": record.capability,
                    "target": record.target,
                    "scope": record.scope,
                    "task_scope_id": record.task_scope_id,
                    "expires_in_seconds": max(0, int(record.expires_at - now)),
                }
                for record in records
            ]

    def revoke(self, lease_id: str, *, owner_context_id: str) -> bool:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                return False
            if not secrets.compare_digest(record.owner_context_id, owner_context_id):
                return False
            self._leases.pop(lease_id, None)
            return True

    def clear_owner(self, owner_context_id: str) -> None:
        with self._lock:
            owned = [
                lease_id
                for lease_id, record in self._leases.items()
                if secrets.compare_digest(record.owner_context_id, owner_context_id)
            ]
            for lease_id in owned:
                self._leases.pop(lease_id, None)

    def clear_task(self, owner_context_id: str, task_scope_id: str) -> int:
        with self._lock:
            owned = [
                lease_id
                for lease_id, record in self._leases.items()
                if secrets.compare_digest(record.owner_context_id, owner_context_id)
                and record.scope == "task"
                and record.task_scope_id is not None
                and secrets.compare_digest(record.task_scope_id, task_scope_id)
            ]
            for lease_id in owned:
                self._leases.pop(lease_id, None)
            return len(owned)

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._prune_locked(time.monotonic())
            return {"total": len(self._leases), "capacity": MAX_CAPABILITY_LEASES}


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
