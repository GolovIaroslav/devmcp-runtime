import sqlite3
import uuid
import time
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any, Optional

@dataclass
class ApprovalRequest:
    id: str
    status: str # "pending", "approved", "denied", "expired"
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

class ApprovalEngine:
    def __init__(self):
        self.config_dir = Path.home() / ".config" / "chatgpt-dev-runtime"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config_dir / "approvals.db"
        self._init_db()

    def _init_db(self):
        # Enforce 0600 permissions
        if not self.db_path.exists():
            self.db_path.touch(mode=0o600)
        else:
            self.db_path.chmod(0o600)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    command_or_action TEXT,
                    working_directory TEXT,
                    reason TEXT,
                    risk_class TEXT,
                    requested_network BOOLEAN,
                    timestamp REAL,
                    expires_at REAL,
                    digest TEXT,
                    env TEXT,
                    task_id TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS patterns (
                    pattern TEXT PRIMARY KEY,
                    created_at REAL
                )
            ''')
            conn.commit()

    def request_approval(self, action: str, cwd: str, reason: str, risk: str, network: bool, env: dict = None, task_id: str = "") -> dict[str, Any]:
        req_id = uuid.uuid4().hex
        timestamp = time.time()
        expires_at = timestamp + 3600
        env_raw = json.dumps(env or {})
        env_safe = json.dumps({k: "***" for k in (env or {})})
        
        digest_input = f"{action}:{cwd}:{env_raw}:{task_id}"
        digest = hashlib.sha256(digest_input.encode()).hexdigest()

        with sqlite3.connect(self.db_path) as conn:
            # Check pattern auto-approval
            cursor = conn.cursor()
            cursor.execute("SELECT pattern FROM patterns")
            patterns = [row[0] for row in cursor.fetchall()]
            
            # Simple wildcard matching
            import fnmatch
            auto_approved = False
            for p in patterns:
                if fnmatch.fnmatch(action, p):
                    auto_approved = True
                    break

            status = "approved" if auto_approved else "pending"

            conn.execute('''
                INSERT INTO requests (id, status, command_or_action, working_directory, reason, risk_class, requested_network, timestamp, expires_at, digest, env, task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (req_id, status, action, cwd, reason, risk, network, timestamp, expires_at, digest, env_safe, task_id))
            conn.commit()

        if auto_approved:
            return {"status": "approved", "approval_id": req_id}

        return {
            "ok": False,
            "status": "approval_required",
            "approval_id": req_id,
            "command_or_action": action,
            "working_directory": cwd,
            "reason": f"Permission required: {reason}",
            "risk_class": risk,
            "requested_network": network,
            "timestamp": timestamp,
            "expires_at": expires_at,
        }

    def get_status(self, req_id: str) -> str:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT status, expires_at FROM requests WHERE id = ?", (req_id,)).fetchone()
            if not row:
                return "not_found"
            status, expires_at = row['status'], row['expires_at']
            if status == "pending" and time.time() > expires_at:
                conn.execute("UPDATE requests SET status = 'expired' WHERE id = ?", (req_id,))
                conn.commit()
                return "expired"
            return status

    def list_pending(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM requests WHERE status = 'pending' AND expires_at >= ?", (time.time(),)).fetchall()
            return [dict(row) for row in rows]
            
    def approve(self, req_id: str, pattern: Optional[str] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (req_id,))
            if pattern:
                conn.execute("INSERT OR REPLACE INTO patterns (pattern, created_at) VALUES (?, ?)", (pattern, time.time()))
            conn.commit()

    def deny(self, req_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE requests SET status = 'denied' WHERE id = ?", (req_id,))
            conn.commit()

    def consume(self, req_id: str, action: str, cwd: str, env: dict = None, task_id: str = ""):
        env_raw = json.dumps(env or {})
        digest_input = f"{action}:{cwd}:{env_raw}:{task_id}"
        digest = hashlib.sha256(digest_input.encode()).hexdigest()
        
        from .errors import ToolFailure
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT status, digest FROM requests WHERE id = ?", (req_id,)).fetchone()
            if not row:
                raise ToolFailure("ACCESS_DENIED", "Approval request not found.", category="security")
            
            if row['status'] != "approved":
                raise ToolFailure("ACCESS_DENIED", f"Approval {req_id} is not in approved status (found: {row['status']}).", category="security")
                
            if row['digest'] != digest:
                raise ToolFailure("ACCESS_DENIED", f"Execution parameters do not match approved digest for {req_id}.", category="security")
                
            conn.execute("UPDATE requests SET status = 'consumed' WHERE id = ?", (req_id,))
            conn.commit()

    def evaluate_command(self, cmd: str) -> str:
        """Returns ALLOW, ASK, or DENY based on command text."""
        denied_commands = ["sudo", "doas", "su", "mount", "umount", "docker", "rm -rf /"]
        for d in denied_commands:
            if cmd.startswith(d) or f" {d} " in f" {cmd} ":
                return "DENY"
        
        allowed_prefixes = ["ls ", "cat ", "grep ", "git ", "npm test", "pytest", "ruff ", "mypy ", "env", "sleep ", "python ", "printf ", "echo ", "yes ", "false", "kill ", "pwd", "awk "]
        for a in allowed_prefixes:
            if cmd.startswith(a) or cmd == a.strip() or ("python" in cmd and ".venv/bin/python" in cmd) or ("python" in cmd and ".local/share/uv/python" in cmd):
                return "ALLOW"
                
        return "ASK"
