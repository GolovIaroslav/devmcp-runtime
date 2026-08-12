from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import ToolFailure


POLICY_VERSION = "v4"
DEFAULT_EXPIRED_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _normalise_cwd(cwd: str | os.PathLike[str]) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(cwd)))


def _normalise_env(env: dict[str, Any] | None) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ToolFailure(
            "INVALID_ARGUMENT", "env must be an object.", category="validation"
        )
    return {
        str(key): str(value)
        for key, value in sorted(env.items(), key=lambda item: str(item[0]))
    }


@dataclass(frozen=True)
class Operation:
    """The immutable capability being approved and later consumed."""

    action: str | tuple[str, ...]
    cwd: str
    env_delta: tuple[tuple[str, str], ...]
    task_id: str
    network: bool
    sandbox_id: str
    capabilities: tuple[str, ...]
    policy_version: str = POLICY_VERSION

    @classmethod
    def create(
        cls,
        action: str | list[str] | tuple[str, ...],
        cwd: str,
        env: dict[str, Any] | None = None,
        *,
        task_id: str = "",
        network: bool = False,
        sandbox_id: str = "",
        capabilities: list[str] | tuple[str, ...] | set[str] | None = None,
        policy_version: str = POLICY_VERSION,
    ) -> "Operation":
        if isinstance(action, (list, tuple)):
            canonical_action: str | tuple[str, ...] = tuple(
                str(item) for item in action
            )
        elif isinstance(action, str):
            canonical_action = action
        else:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "Operation command must be a string or argv array.",
                category="validation",
            )
        env_delta = tuple(_normalise_env(env).items())
        canonical_caps = tuple(
            sorted({str(capability) for capability in (capabilities or ())})
        )
        return cls(
            action=canonical_action,
            cwd=_normalise_cwd(cwd),
            env_delta=env_delta,
            task_id=str(task_id),
            network=bool(network),
            sandbox_id=str(sandbox_id),
            capabilities=canonical_caps,
            policy_version=str(policy_version),
        )

    @property
    def action_kind(self) -> str:
        return "argv" if isinstance(self.action, tuple) else "shell"

    def canonical_json(self) -> str:
        payload = {
            "action": list(self.action)
            if isinstance(self.action, tuple)
            else self.action,
            "action_kind": self.action_kind,
            "capabilities": list(self.capabilities),
            "cwd": self.cwd,
            "env_delta": dict(self.env_delta),
            "network": self.network,
            "policy_version": self.policy_version,
            "sandbox_id": self.sandbox_id,
            "task_id": self.task_id,
        }
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def compute_digest(
    action: str | list[str] | tuple[str, ...],
    cwd: str,
    env: dict[str, Any] | None = None,
    task_id: str = "",
    network: bool = False,
    sandbox: bool = True,
    capabilities: list[str] | set[str] | tuple[str, ...] | None = None,
    policy_version: str = POLICY_VERSION,
    sandbox_id: str = "",
) -> str:
    """Compatibility wrapper exposing the canonical operation digest."""
    operation = Operation.create(
        action,
        cwd,
        env,
        task_id=task_id,
        network=network,
        sandbox_id=sandbox_id if sandbox else f"{sandbox_id}:disabled",
        capabilities=capabilities,
        policy_version=policy_version,
    )
    return operation.digest()


@dataclass
class ApprovalRequest:
    id: str
    status: str
    command_or_action: str
    working_directory: str
    reason: str
    risk_class: str
    requested_network: bool
    timestamp: float
    expires_at: float
    digest: str
    env: str
    task_id: str
    capabilities: str


class ApprovalEngine:
    def __init__(self, db_path: Path | None = None):
        if db_path is not None:
            self.db_path = db_path
            self.config_dir = db_path.parent
        else:
            from .config import ensure_dirs, migrate_legacy, paths

            selected = ensure_dirs(paths())
            migrate_legacy(selected)
            self.config_dir = selected.root
            self.db_path = selected.approvals_db
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.mark_expired()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        if not self.db_path.exists():
            self.db_path.touch(mode=0o600)
        else:
            try:
                self.db_path.chmod(0o600)
            except OSError:
                pass

        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    command_or_action TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    risk_class TEXT NOT NULL,
                    requested_network BOOLEAN NOT NULL,
                    timestamp REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    digest TEXT NOT NULL,
                    env TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    action_kind TEXT NOT NULL DEFAULT 'shell',
                    sandbox_id TEXT NOT NULL DEFAULT '',
                    policy_version TEXT NOT NULL DEFAULT 'v4'
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
            for name, definition in (
                ("capabilities", "TEXT NOT NULL DEFAULT '[]'"),
                ("action_kind", "TEXT NOT NULL DEFAULT 'shell'"),
                ("sandbox_id", "TEXT NOT NULL DEFAULT ''"),
                ("policy_version", "TEXT NOT NULL DEFAULT 'v4'"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {definition}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS patterns (pattern TEXT PRIMARY KEY, created_at REAL NOT NULL)"
            )

    def _operation(
        self,
        action: str | list[str] | tuple[str, ...],
        cwd: str,
        env: dict[str, Any] | None,
        *,
        task_id: str,
        network: bool,
        sandbox_id: str,
        capabilities: list[str] | tuple[str, ...] | set[str] | None,
        policy_version: str = POLICY_VERSION,
    ) -> Operation:
        return Operation.create(
            action,
            cwd,
            env,
            task_id=task_id,
            network=network,
            sandbox_id=sandbox_id,
            capabilities=capabilities,
            policy_version=policy_version,
        )

    def request_approval(
        self,
        action: str | list[str] | tuple[str, ...],
        cwd: str,
        reason: str,
        risk: str,
        network: bool,
        env: dict[str, Any] | None = None,
        task_id: str = "",
        sandbox: bool = True,
        sandbox_id: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        requested_caps = set(str(capability) for capability in (capabilities or ()))
        if network:
            requested_caps.add("network")
        operation = self._operation(
            action,
            cwd,
            env,
            task_id=task_id,
            network=network,
            sandbox_id=sandbox_id if sandbox else f"{sandbox_id}:disabled",
            capabilities=requested_caps,
        )
        req_id = uuid.uuid4().hex
        timestamp = time.time()
        expires_at = timestamp + 3600
        action_display = (
            json.dumps(list(operation.action), ensure_ascii=False)
            if operation.action_kind == "argv"
            else str(operation.action)
        )
        env_safe = json.dumps(
            {key: "***" for key, _ in operation.env_delta}, sort_keys=True
        )
        caps_list = list(operation.capabilities)

        with self._connection() as conn:
            patterns = [row[0] for row in conn.execute("SELECT pattern FROM patterns")]
            import fnmatch

            auto_approved = any(
                fnmatch.fnmatch(action_display, pattern) for pattern in patterns
            )
            status = "approved" if auto_approved else "pending"
            conn.execute(
                """
                INSERT INTO requests
                (id, status, command_or_action, working_directory, reason, risk_class,
                 requested_network, timestamp, expires_at, digest, env, task_id,
                 capabilities, action_kind, sandbox_id, policy_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req_id,
                    status,
                    action_display,
                    operation.cwd,
                    reason,
                    risk,
                    operation.network,
                    timestamp,
                    expires_at,
                    operation.digest(),
                    env_safe,
                    operation.task_id,
                    json.dumps(caps_list),
                    operation.action_kind,
                    operation.sandbox_id,
                    operation.policy_version,
                ),
            )

        if auto_approved:
            return {"ok": True, "status": "approved", "approval_id": req_id}
        return {
            "ok": False,
            "status": "approval_required",
            "approval_id": req_id,
            "operation_summary": action_display,
            "command_or_action": action_display,
            "working_directory": operation.cwd,
            "reason": f"Permission required: {reason}",
            "risk_class": risk,
            "requested_network": operation.network,
            "capabilities": caps_list,
            "requested_capabilities": caps_list,
            "timestamp": timestamp,
            "expires_at": expires_at,
        }

    def get_status(self, req_id: str) -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status, expires_at FROM requests WHERE id = ?", (req_id,)
            ).fetchone()
            if not row:
                return "not_found"
            status, expires_at = str(row[0]), float(row[1])
            if status in {"pending", "approved"} and time.time() >= expires_at:
                conn.execute(
                    "UPDATE requests SET status = 'expired' WHERE id = ? AND status IN ('pending', 'approved') AND expires_at < ?",
                    (req_id, time.time()),
                )
                return "expired"
            return status

    def list_pending(self) -> list[dict[str, Any]]:
        self.mark_expired()
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM requests WHERE status = 'pending' AND expires_at >= ?",
                (time.time(),),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    item["capabilities"] = json.loads(item.get("capabilities") or "[]")
                except (TypeError, json.JSONDecodeError):
                    item["capabilities"] = []
                result.append(item)
            return result

    def mark_expired(self) -> int:
        """Transition only expired pending/approved requests; never touch active ones."""

        now = time.time()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE requests SET status = 'expired' WHERE status IN ('pending', 'approved') AND expires_at < ?",
                (now,),
            )
            return int(cursor.rowcount)

    def prune_expired(
        self, *, older_than_seconds: float = DEFAULT_EXPIRED_RETENTION_SECONDS
    ) -> int:
        """Delete only expired history older than the requested retention window."""

        cutoff = time.time() - max(0.0, float(older_than_seconds))
        self.mark_expired()
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM requests WHERE status = 'expired' AND expires_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)

    def clear_expired(self) -> int:
        """Explicitly clear all expired records, leaving active records untouched."""

        self.mark_expired()
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM requests WHERE status = 'expired'")
            return int(cursor.rowcount)

    def approve(self, req_id: str, pattern: Optional[str] = None) -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE requests SET status = 'approved' WHERE id = ? AND status = 'pending' AND expires_at >= ?",
                (req_id, now),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT status, expires_at FROM requests WHERE id = ?", (req_id,)
                ).fetchone()
                if row is None:
                    raise ToolFailure(
                        "NOT_FOUND",
                        f"Approval request {req_id} not found.",
                        category="validation",
                    )
                if row[0] == "pending" and float(row[1]) < now:
                    conn.execute(
                        "UPDATE requests SET status = 'expired' WHERE id = ? AND status = 'pending'",
                        (req_id,),
                    )
                raise ToolFailure(
                    "INVALID_STATE",
                    f"Approval {req_id} cannot be approved from state '{row[0]}'.",
                    category="security",
                )
            if pattern:
                conn.execute(
                    "INSERT OR REPLACE INTO patterns (pattern, created_at) VALUES (?, ?)",
                    (pattern, now),
                )

    def deny(self, req_id: str) -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE requests SET status = 'denied' WHERE id = ? AND status = 'pending' AND expires_at >= ?",
                (req_id, now),
            )
            if cursor.rowcount == 1:
                return
            row = conn.execute(
                "SELECT status FROM requests WHERE id = ?", (req_id,)
            ).fetchone()
            if row is None:
                raise ToolFailure(
                    "NOT_FOUND",
                    f"Approval request {req_id} not found.",
                    category="validation",
                )
            raise ToolFailure(
                "INVALID_STATE",
                f"Approval {req_id} cannot be denied from state '{row[0]}'.",
                category="security",
            )

    def consume(
        self,
        req_id: str,
        action: str | list[str] | tuple[str, ...],
        cwd: str,
        env: dict[str, Any] | None = None,
        task_id: str = "",
        network: bool = False,
        sandbox: bool = True,
        sandbox_id: str = "",
        capabilities: list[str] | None = None,
    ) -> list[str]:
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT status, digest, expires_at, capabilities, action_kind,
                       sandbox_id, policy_version
                FROM requests WHERE id = ?
                """,
                (req_id,),
            ).fetchone()
            if row is None:
                raise ToolFailure(
                    "ACCESS_DENIED",
                    f"Approval request {req_id} not found.",
                    category="security",
                )
            try:
                stored_caps = [
                    str(capability)
                    for capability in json.loads(row["capabilities"] or "[]")
                ]
            except (TypeError, json.JSONDecodeError) as exc:
                raise ToolFailure(
                    "ACCESS_DENIED",
                    "Approval has invalid capability data.",
                    category="security",
                ) from exc
            operation = self._operation(
                action,
                cwd,
                env,
                task_id=task_id,
                network=network,
                sandbox_id=sandbox_id if sandbox else f"{sandbox_id}:disabled",
                capabilities=stored_caps,
                policy_version=str(row["policy_version"] or POLICY_VERSION),
            )
            if operation.action_kind != str(row["action_kind"]):
                raise ToolFailure(
                    "ACCESS_DENIED",
                    "Execution operation kind does not match approval.",
                    category="security",
                )
            now = time.time()
            cursor = conn.execute(
                """
                UPDATE requests SET status = 'consumed'
                WHERE id = ? AND status = 'approved' AND digest = ? AND expires_at >= ?
                """,
                (req_id, operation.digest(), now),
            )
            if cursor.rowcount != 1:
                status = str(row["status"])
                if float(row["expires_at"]) < now:
                    conn.execute(
                        "UPDATE requests SET status = 'expired' WHERE id = ? AND status = 'approved' AND expires_at < ?",
                        (req_id, now),
                    )
                    raise ToolFailure(
                        "ACCESS_DENIED",
                        f"Approval {req_id} has expired.",
                        category="security",
                    )
                if status != "approved":
                    raise ToolFailure(
                        "ACCESS_DENIED",
                        f"Approval {req_id} is in status '{status}', expected 'approved'.",
                        category="security",
                    )
                raise ToolFailure(
                    "ACCESS_DENIED",
                    "Execution parameters do not match approved digest.",
                    category="security",
                )
            return stored_caps

    def evaluate_command(self, cmd: str) -> str:
        """Returns ALLOW, ASK, or DENY based on command text."""
        denied_commands = [
            "sudo",
            "doas",
            "su",
            "mount",
            "umount",
            "docker",
            "rm -rf /",
        ]
        for denied in denied_commands:
            if cmd.startswith(denied) or f" {denied} " in f" {cmd} ":
                return "DENY"

        allowed_prefixes = [
            "ls ",
            "cat ",
            "grep ",
            "git ",
            "npm test",
            "pytest",
            "ruff ",
            "mypy ",
            "env",
            "sleep ",
            "python ",
            "python3 ",
            "printf ",
            "echo ",
            "yes ",
            "false",
            "kill ",
            "pwd",
            "awk ",
        ]
        for prefix in allowed_prefixes:
            if (
                cmd.startswith(prefix)
                or cmd == prefix.strip()
                or (
                    "python" in cmd
                    and (".venv/bin/python" in cmd or ".local/share/uv/python" in cmd)
                )
            ):
                return "ALLOW"
        return "ASK"
