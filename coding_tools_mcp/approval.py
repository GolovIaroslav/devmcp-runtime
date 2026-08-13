from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


POLICY_VERSION = "v4"
DEFAULT_EXPIRED_RETENTION_SECONDS = 7 * 24 * 60 * 60


class ApprovalEngine:
    """Compatibility stub. Live approval gating has been deleted from DevMCP execution model."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path

    def request_approval(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status": "approved", "approval_id": "auto"}

    def get_status(self, req_id: str) -> str:
        return "approved"

    def list_pending(self) -> list[dict[str, Any]]:
        return []

    def mark_expired(self) -> int:
        return 0

    def prune_expired(
        self, *, older_than_seconds: float = DEFAULT_EXPIRED_RETENTION_SECONDS
    ) -> int:
        return 0

    def clear_expired(self) -> int:
        return 0

    def approve(self, req_id: str, pattern: Optional[str] = None) -> None:
        pass

    def deny(self, req_id: str) -> None:
        pass

    def consume(self, *args: Any, **kwargs: Any) -> list[str]:
        return []
