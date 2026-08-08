import json
import uuid
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any

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

class ApprovalEngine:
    def __init__(self):
        self.config_dir = Path.home() / ".config" / "chatgpt-dev-runtime"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.config_dir / "approvals.json"
        self._load()

    def _load(self):
        if not self.state_file.exists():
            self.requests = {}
            return
        try:
            data = json.loads(self.state_file.read_text())
            self.requests = {req["id"]: ApprovalRequest(**req) for req in data}
        except Exception:
            self.requests = {}

    def _save(self):
        data = [asdict(req) for req in self.requests.values()]
        self.state_file.write_text(json.dumps(data, indent=2))

    def request_approval(self, action: str, cwd: str, reason: str, risk: str, network: bool) -> dict[str, Any]:
        req = ApprovalRequest(
            id=uuid.uuid4().hex,
            status="pending",
            command_or_action=action,
            working_directory=cwd,
            reason=reason,
            risk_class=risk,
            requested_network=network,
            timestamp=time.time(),
            expires_at=time.time() + 3600
        )
        self.requests[req.id] = req
        self._save()
        return {
            "status": "approval_required",
            "approval_id": req.id,
            "command_or_action": req.command_or_action,
            "working_directory": req.working_directory,
            "reason": req.reason,
            "risk_class": req.risk_class,
            "requested_network": req.requested_network,
            "timestamp": req.timestamp,
            "expires_at": req.expires_at,
        }

    def get_status(self, id: str) -> str:
        self._load()
        req = self.requests.get(id)
        if not req:
            return "not_found"
        if req.status == "pending" and time.time() > req.expires_at:
            req.status = "expired"
            self._save()
        return req.status

    def list_pending(self) -> list[dict[str, Any]]:
        self._load()
        return [asdict(req) for req in self.requests.values() if req.status == "pending" and time.time() <= req.expires_at]
        
    def approve(self, id: str):
        self._load()
        if id in self.requests:
            self.requests[id].status = "approved"
            self._save()

    def deny(self, id: str):
        self._load()
        if id in self.requests:
            self.requests[id].status = "denied"
            self._save()

    def evaluate_command(self, cmd: str) -> str:
        """Returns ALLOW, ASK, or DENY based on command text."""
        # Unconditionally denied
        denied_commands = ["sudo", "doas", "su", "mount", "umount", "docker", "rm -rf /"]
        for d in denied_commands:
            if cmd.startswith(d) or f" {d} " in f" {cmd} ":
                return "DENY"
        
        # Allowed defaults
        allowed_prefixes = ["ls ", "cat ", "grep ", "git ", "npm test", "pytest", "ruff ", "mypy "]
        for a in allowed_prefixes:
            if cmd.startswith(a):
                return "ALLOW"
                
        # Ask for everything else
        return "ASK"
