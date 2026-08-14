"""Small local audit log with a deliberately closed, non-secret event shape."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ensure_dirs, paths


def append_tool_event(
    tool: str,
    *,
    ok: bool,
    error_code: Any,
    duration_ms: int,
    execution_mode: str = "build",
) -> None:
    selected = ensure_dirs(paths())
    event = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "tool": str(tool),
        "ok": bool(ok),
        "error_code": str(error_code) if error_code else None,
        "duration_ms": int(duration_ms),
        "execution_mode": str(execution_mode),
    }
    try:
        with selected.audit_log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            )
        if os.name != "nt":
            selected.audit_log.chmod(0o600)
    except OSError:
        # Auditing must not make a coding tool unavailable.
        return


def read_recent_events(
    path: Path | None = None, *, limit: int = 100
) -> list[dict[str, Any]]:
    selected = path or paths().audit_log
    try:
        lines = selected.read_text(encoding="utf-8").splitlines()[-max(1, limit) :]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events
