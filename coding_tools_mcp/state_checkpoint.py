from __future__ import annotations

from pathlib import Path
from typing import Any

from .state_store import (
    checkpoint_path,
    context_checkpoint_path,
    new_checkpoint_id,
    now_iso,
    project_lock,
    read_json,
    write_json,
)


def read_state_checkpoint(project: Path, branch: str) -> dict[str, Any] | None:
    with project_lock(project):
        return read_json(checkpoint_path(project, branch))


def read_authoritative_state_checkpoint(
    project: Path, owner: str
) -> dict[str, Any] | None:
    with project_lock(project):
        return read_json(context_checkpoint_path(project, owner))


def write_state_checkpoint(
    project: Path,
    branch: str,
    *,
    snapshot: dict[str, Any],
    phase: str,
    operation: str,
    owner: str | None,
    logical_task: str | None,
    outcome: str,
    previous_checkpoint_id: str | None = None,
    authority_owner: str | None = None,
) -> dict[str, Any]:
    record = {
        "version": 2,
        "checkpoint_id": new_checkpoint_id(),
        "repo": snapshot.get("repo"),
        "project_id": snapshot.get("project_id"),
        "branch": branch,
        "phase": phase,
        "operation": operation,
        "outcome": outcome,
        "writer_owner": owner,
        "logical_task": logical_task,
        "previous_checkpoint_id": previous_checkpoint_id,
        "timestamp": now_iso(),
        "snapshot": snapshot,
    }
    with project_lock(project):
        write_json(checkpoint_path(project, branch, before=phase == "before"), record)
        if authority_owner is not None and phase != "before":
            write_json(context_checkpoint_path(project, authority_owner), record)
    return record
