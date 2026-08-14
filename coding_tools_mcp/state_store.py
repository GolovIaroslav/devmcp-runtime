from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ensure_dirs, paths as config_paths
from .errors import ToolFailure

DEFAULT_WRITER_TTL_SECONDS = 900
MAX_WRITER_TTL_SECONDS = 86_400
MAX_STATE_BYTES = 2 * 1024 * 1024


def now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), tz=timezone.utc).isoformat()


def _scope_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def state_root(project: Path) -> Path:
    config_root = ensure_dirs(config_paths()).root.resolve()
    project_root = project.resolve()
    if config_root == project_root or config_root.is_relative_to(project_root):
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "state-management storage must be outside the selected project.",
            category="runtime",
        )
    root = config_root / "state-management" / _scope_id(str(project_root))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


@contextmanager
def project_lock(project: Path) -> Iterator[None]:
    handle = (state_root(project) / ".lock").open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":  # pragma: no cover - Windows CI
            import msvcrt

            getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover - Windows CI
                import msvcrt

                getattr(msvcrt, "locking")(
                    handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1
                )
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "state-management record could not be read.",
            category="runtime",
        ) from exc
    if len(raw) > MAX_STATE_BYTES:
        raise ToolFailure(
            "INVALID_STATE",
            "state-management record exceeds the supported bound.",
            category="runtime",
        )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolFailure(
            "INVALID_STATE",
            "state-management record is invalid JSON.",
            category="runtime",
        ) from exc
    if not isinstance(value, dict):
        raise ToolFailure(
            "INVALID_STATE",
            "state-management record must be an object.",
            category="runtime",
        )
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_STATE_BYTES:
        raise ToolFailure(
            "OUTPUT_TOO_LARGE",
            "state-management record exceeds the supported bound.",
            category="runtime",
        )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def lease_path(project: Path, branch: str) -> Path:
    return state_root(project) / "leases" / f"{_scope_id(branch)}.json"


def checkpoint_path(project: Path, branch: str, *, before: bool = False) -> Path:
    suffix = ".before.json" if before else ".json"
    return state_root(project) / "checkpoints" / f"{_scope_id(branch)}{suffix}"


def context_checkpoint_path(project: Path, owner: str) -> Path:
    return state_root(project) / "contexts" / f"{_scope_id(owner)}.json"


def new_checkpoint_id() -> str:
    return uuid.uuid4().hex
