from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


MAX_HTTP_SESSIONS = 128
HTTP_SESSION_TTL_SECONDS = 60 * 60


def _close_runtime(runtime: Any) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _runtime_evictable(runtime: Any) -> bool:
    evictable = getattr(runtime, "http_session_evictable", None)
    if callable(evictable):
        try:
            return bool(evictable())
        except BaseException:
            return False
    return True


@dataclass
class HTTPSessionRecord:
    runtime: Any
    last_seen: float
    active_requests: int = 0
    closing: bool = False


class HTTPSessionManager:
    """Own independent Runtime instances for Streamable HTTP sessions."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._sessions: dict[str, HTTPSessionRecord] = {}
        self._lock = threading.Lock()
        self._creating = 0
        self._closed = False

    def create(self) -> Any:
        self.prune()
        evicted: HTTPSessionRecord | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session manager is closed")
            if len(self._sessions) + self._creating >= MAX_HTTP_SESSIONS:
                idle = [
                    (session_id, record)
                    for session_id, record in self._sessions.items()
                    if (
                        record.active_requests == 0
                        and not record.closing
                        and _runtime_evictable(record.runtime)
                    )
                ]
                if not idle:
                    raise RuntimeError("maximum HTTP session count reached")
                session_id, evicted = min(idle, key=lambda item: item[1].last_seen)
                self._sessions.pop(session_id, None)
            self._creating += 1
        runtime: Any | None = None
        installed = False
        try:
            if evicted is not None:
                _close_runtime(evicted.runtime)
            runtime = self._factory()
            record = HTTPSessionRecord(
                runtime=runtime,
                last_seen=time.monotonic(),
                active_requests=1,
            )
            with self._lock:
                if self._closed:
                    raise RuntimeError("HTTP session manager is closed")
                if runtime.http_session_id in self._sessions:
                    raise RuntimeError("duplicate HTTP session identifier")
                self._sessions[runtime.http_session_id] = record
                installed = True
            return runtime
        finally:
            with self._lock:
                self._creating -= 1
            if runtime is not None and not installed:
                _close_runtime(runtime)

    def get(self, session_id: str) -> Any | None:
        self.prune()
        with self._lock:
            if self._closed:
                return None
            record = self._sessions.get(session_id)
            if record is None or record.closing:
                return None
            record.last_seen = time.monotonic()
            record.active_requests += 1
            return record.runtime

    def release(self, session_id: str) -> None:
        close_record: HTTPSessionRecord | None = None
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return
            if record.active_requests > 0:
                record.active_requests -= 1
            record.last_seen = time.monotonic()
            if record.closing and record.active_requests == 0:
                close_record = self._sessions.pop(session_id, None)
        if close_record is not None:
            _close_runtime(close_record.runtime)

    def delete(self, session_id: str) -> bool:
        close_record: HTTPSessionRecord | None = None
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            record.closing = True
            if record.active_requests == 0:
                close_record = self._sessions.pop(session_id, None)
        if close_record is not None:
            _close_runtime(close_record.runtime)
        return True

    def prune(self) -> None:
        cutoff = time.monotonic() - HTTP_SESSION_TTL_SECONDS
        with self._lock:
            expired = [
                session_id
                for session_id, record in self._sessions.items()
                if (
                    record.last_seen < cutoff
                    and record.active_requests == 0
                    and not record.closing
                    and _runtime_evictable(record.runtime)
                )
            ]
            records = [self._sessions.pop(session_id) for session_id in expired]
        for record in records:
            _close_runtime(record.runtime)

    def stats(self) -> dict[str, int]:
        """Return bounded session-capacity telemetry without exposing session IDs."""

        with self._lock:
            records = list(self._sessions.values())
            return {
                "capacity": MAX_HTTP_SESSIONS,
                "total": len(records),
                "creating": self._creating,
                "active_sessions": sum(
                    1 for record in records if record.active_requests > 0
                ),
                "active_requests": sum(record.active_requests for record in records),
                "closing": sum(1 for record in records if record.closing),
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            records = list(self._sessions.values())
            self._sessions.clear()
        for record in records:
            _close_runtime(record.runtime)
