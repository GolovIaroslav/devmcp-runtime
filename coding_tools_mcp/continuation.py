from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ensure_dirs, paths as config_paths
from .errors import ToolFailure

MAX_CHECKPOINT_BYTES = 16_384
MAX_COMPLETED_ITEMS = 64
MAX_COMPLETED_ITEM_CHARS = 256
MAX_TEXT_CHARS = 4096
CHECKPOINT_FIELDS = {
    "active_task",
    "active_slice",
    "branch",
    "head",
    "checkpoint_id",
    "state_fingerprint",
    "pr_number",
    "workflow_run_id",
    "dirty_state_summary",
    "completed_acceptance_items",
    "next_action",
    "blocker_type",
    "timestamp",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*\S{8,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{12,}", re.IGNORECASE),
)


def _scope_id(project: Path, scope: str) -> tuple[str, str]:
    return (
        hashlib.sha256(str(project.resolve()).encode()).hexdigest(),
        hashlib.sha256(scope.encode()).hexdigest(),
    )


def checkpoint_path(project: Path, scope: str) -> Path:
    project_id, scope_id = _scope_id(project, scope)
    config_root = ensure_dirs(config_paths()).root.resolve()
    project_root = project.resolve()
    if config_root == project_root or config_root.is_relative_to(project_root):
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "continuation checkpoint storage must be outside the selected project.",
            category="runtime",
        )
    root = config_root / "continuation-checkpoints" / project_id
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"{scope_id}.json"


def normalize_checkpoint_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "checkpoint payload must be an object.",
            category="validation",
        )
    unknown = sorted(set(payload) - CHECKPOINT_FIELDS)
    if unknown:
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "checkpoint payload contains unsupported fields.",
            category="validation",
            details={"fields": unknown},
        )
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "completed_acceptance_items":
            if not isinstance(value, list) or len(value) > MAX_COMPLETED_ITEMS:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} must contain at most {MAX_COMPLETED_ITEMS} strings.",
                    category="validation",
                )
            if any(
                not isinstance(item, str) or len(item) > MAX_COMPLETED_ITEM_CHARS
                for item in value
            ):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} items must be strings up to {MAX_COMPLETED_ITEM_CHARS} characters.",
                    category="validation",
                )
            normalized[key] = list(value)
        elif key in {"pr_number", "workflow_run_id"}:
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} must be a positive integer or null.",
                    category="validation",
                )
            normalized[key] = value
        else:
            if value is not None and not isinstance(value, str):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} must be a string or null.",
                    category="validation",
                )
            if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} exceeds {MAX_TEXT_CHARS} characters.",
                    category="validation",
                )
            if isinstance(value, str) and any(
                pattern.search(value) for pattern in SECRET_VALUE_PATTERNS
            ):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} appears to contain secret material and cannot be checkpointed.",
                    category="validation",
                )
            normalized[key] = value
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise ToolFailure(
            "OUTPUT_TOO_LARGE",
            f"checkpoint payload exceeds {MAX_CHECKPOINT_BYTES} bytes.",
            category="validation",
            details={"bytes": len(encoded), "limit": MAX_CHECKPOINT_BYTES},
        )
    return normalized


def write_checkpoint(project: Path, scope: str, payload: Any) -> dict[str, Any]:
    record = {
        "version": 1,
        "project": str(project.resolve()),
        "scope": scope,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "payload": normalize_checkpoint_payload(payload),
    }
    raw = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
    target = checkpoint_path(project, scope)
    fd, temp_name = tempfile.mkstemp(prefix=".checkpoint-", dir=target.parent)
    temporary = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def read_checkpoint(project: Path, scope: str) -> dict[str, Any] | None:
    target = checkpoint_path(project, scope)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "continuation checkpoint could not be read.",
            category="runtime",
        ) from exc
    if len(raw) > MAX_CHECKPOINT_BYTES + 8192:
        raise ToolFailure(
            "INVALID_STATE",
            "stored continuation checkpoint exceeds the supported bound.",
            category="runtime",
        )
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolFailure(
            "INVALID_STATE",
            "stored continuation checkpoint is invalid.",
            category="runtime",
        ) from exc
    if (
        not isinstance(record, dict)
        or record.get("project") != str(project.resolve())
        or record.get("scope") != scope
    ):
        raise ToolFailure(
            "INVALID_STATE",
            "stored continuation checkpoint scope does not match the selected project.",
            category="runtime",
        )
    return record


def clear_checkpoint(project: Path, scope: str) -> bool:
    target = checkpoint_path(project, scope)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "continuation checkpoint could not be cleared.",
            category="runtime",
        ) from exc
    try:
        target.parent.rmdir()
    except OSError:
        pass
    return True
