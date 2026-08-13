from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .errors import ToolFailure
from .state_store import DEFAULT_WRITER_TTL_SECONDS, MAX_WRITER_TTL_SECONDS, lease_path, now_iso, project_lock, read_json, write_json


def _active(record: dict[str, Any], now: float) -> bool:
    try:
        return float(record.get("expires_at_epoch", 0.0)) > now
    except (TypeError, ValueError):
        return False


def inspect_writer_lease(project: Path, branch: str, *, now: float | None = None) -> dict[str, Any] | None:
    current = time.time() if now is None else now
    with project_lock(project):
        record = read_json(lease_path(project, branch))
        if record is None:
            return None
        return {**record, "stale": not _active(record, current)}


def acquire_writer_leases(project: Path, branches: list[str], *, owner: str, logical_task: str | None, ttl_seconds: int = DEFAULT_WRITER_TTL_SECONDS, checkpoint_id: str | None = None, now: float | None = None) -> dict[str, dict[str, Any]]:
    if not 1 <= ttl_seconds <= MAX_WRITER_TTL_SECONDS:
        raise ToolFailure("INVALID_ARGUMENT", "writer lease TTL is out of range.", category="validation")
    unique_branches = sorted({branch for branch in branches if branch})
    if not owner or not unique_branches:
        raise ToolFailure("INVALID_STATE", "writer lease requires an owner and named branch.", category="conflict")
    current = time.time() if now is None else now
    acquired: dict[str, dict[str, Any]] = {}
    with project_lock(project):
        existing = {branch: read_json(lease_path(project, branch)) for branch in unique_branches}
        conflicts = []
        for branch, record in existing.items():
            if record is not None and _active(record, current) and record.get("owner") != owner:
                conflicts.append({"branch": branch, "owner": record.get("owner"), "logical_task": record.get("logical_task"), "since": record.get("acquired_at"), "expires_at": record.get("expires_at"), "checkpoint_id": record.get("checkpoint_id")})
        if conflicts:
            raise ToolFailure("WRITER_LEASE_CONFLICT", "Another DevMCP logical context owns the repository branch writer lease.", category="conflict", retryable=True, details={"conflicts": conflicts})
        for branch in unique_branches:
            previous = existing[branch]
            same_owner = previous is not None and _active(previous, current) and previous.get("owner") == owner
            record = {"version": 1, "branch": branch, "owner": owner, "logical_task": logical_task, "acquired_at": previous.get("acquired_at") if same_owner and previous is not None else now_iso(current), "heartbeat_at": now_iso(current), "expires_at": now_iso(current + ttl_seconds), "expires_at_epoch": current + ttl_seconds, "ttl_seconds": ttl_seconds, "checkpoint_id": checkpoint_id, "recovered_stale_owner": None if previous is None or same_owner else {"owner": previous.get("owner"), "logical_task": previous.get("logical_task"), "expires_at": previous.get("expires_at")}}
            write_json(lease_path(project, branch), record)
            acquired[branch] = record
    return acquired


def release_writer_lease(project: Path, branch: str, *, owner: str, logical_task: str | None = None, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    with project_lock(project):
        path = lease_path(project, branch)
        record = read_json(path)
        if record is None:
            return False
        if _active(record, current) and record.get("owner") != owner:
            raise ToolFailure("WRITER_LEASE_CONFLICT", "Cannot release a writer lease owned by another logical context.", category="conflict", retryable=True, details={"branch": branch, "owner": record.get("owner"), "logical_task": record.get("logical_task"), "since": record.get("acquired_at"), "expires_at": record.get("expires_at"), "checkpoint_id": record.get("checkpoint_id")})
        write_json(path, {**record, "released_at": now_iso(current), "expires_at": now_iso(current), "expires_at_epoch": current})
        return True
