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
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 64
MAX_COMPLETED_ITEMS = 64
MAX_COMPLETED_ITEM_CHARS = 256
MAX_REMAINING_ITEMS = 64
MAX_TEXT_CHARS = 4096
VERIFICATION_STATUSES = {"not_run", "partial", "passed", "failed", "stale"}
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
    "objective",
    "remaining_items",
    "verification_status",
    "verified_head",
    "verified_state_fingerprint",
    "state_fingerprint_complete",
    "workspace_kind",
    "workspace_dirty",
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


def checkpoint_root(project: Path) -> Path:
    project_id, _ = _scope_id(project, "")
    config_root = ensure_dirs(config_paths()).root.resolve()
    project_root = project.resolve()
    if config_root == project_root or config_root.is_relative_to(project_root):
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "continuation checkpoint storage must be outside the selected project.",
            category="runtime",
        )
    return config_root / "continuation-checkpoints" / project_id


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
        if key in {"completed_acceptance_items", "remaining_items"}:
            max_items = (
                MAX_COMPLETED_ITEMS
                if key == "completed_acceptance_items"
                else MAX_REMAINING_ITEMS
            )
            if not isinstance(value, list) or len(value) > max_items:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} must contain at most {max_items} strings.",
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
        elif key in {"state_fingerprint_complete", "workspace_dirty"}:
            if not isinstance(value, bool):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"{key} must be a boolean.",
                    category="validation",
                )
            normalized[key] = value
        elif key == "verification_status":
            if value not in VERIFICATION_STATUSES:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "verification_status is not supported.",
                    category="validation",
                    details={"allowed": sorted(VERIFICATION_STATUSES)},
                )
            normalized[key] = value
        elif key == "workspace_kind":
            if value not in {"canonical", "managed"}:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "workspace_kind must be canonical or managed.",
                    category="validation",
                )
            normalized[key] = value
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
        "version": 2,
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


def checkpoint_summary(record: dict[str, Any]) -> dict[str, Any]:
    version = record.get("version")
    raw_payload = record.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    has_trusted_state = (
        all(
            isinstance(payload.get(field), str) and bool(payload.get(field))
            for field in ("branch", "head", "checkpoint_id", "state_fingerprint")
        )
        and payload.get("workspace_kind") in {"canonical", "managed"}
        and isinstance(payload.get("workspace_dirty"), bool)
    )
    resumable = (
        version == 2
        and has_trusted_state
        and payload.get("state_fingerprint_complete") is True
    )
    blocker: str | None = None
    if version == 1:
        resumable = False
        blocker = "legacy_v1"
    elif version != 2:
        resumable = False
        blocker = "unsupported_version"
    elif not has_trusted_state:
        blocker = "missing_state_authority"
    elif payload.get("state_fingerprint_complete") is not True:
        blocker = "state_fingerprint_incomplete"
    return {
        "version": version,
        "scope": record.get("scope"),
        "updated_at": record.get("updated_at"),
        "active_task": payload.get("active_task"),
        "active_slice": payload.get("active_slice"),
        "objective": payload.get("objective"),
        "branch": payload.get("branch"),
        "head": payload.get("head"),
        "workspace_kind": payload.get("workspace_kind"),
        "workspace_dirty": payload.get("workspace_dirty"),
        "verification_status": payload.get("verification_status"),
        "verified_head": payload.get("verified_head"),
        "verified_state_fingerprint": payload.get("verified_state_fingerprint"),
        "next_action": payload.get("next_action"),
        "resumable": resumable,
        "resume_blocker": blocker,
    }


def list_checkpoints(project: Path, limit: int = DEFAULT_LIST_LIMIT) -> dict[str, Any]:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_LIST_LIMIT
    ):
        raise ToolFailure(
            "INVALID_ARGUMENT",
            f"limit must be an integer from 1 to {MAX_LIST_LIMIT}.",
            category="validation",
        )
    root = checkpoint_root(project)
    if not root.is_dir():
        return {"checkpoints": [], "invalid_count": 0}
    valid: list[dict[str, Any]] = []
    invalid_count = 0
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_CHECKPOINT_BYTES + 8192:
                raise ValueError("oversized")
            record = json.loads(raw)
            if (
                not isinstance(record, dict)
                or record.get("project") != str(project.resolve())
                or not isinstance(record.get("scope"), str)
                or not str(record.get("scope")).startswith(("task:", "branch:"))
                or not isinstance(record.get("updated_at"), str)
                or not isinstance(record.get("payload"), dict)
            ):
                raise ValueError("malformed")
            valid.append(checkpoint_summary(record))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            invalid_count += 1
    valid.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("scope") or ""),
        ),
        reverse=True,
    )
    return {"checkpoints": valid[:limit], "invalid_count": invalid_count}


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
