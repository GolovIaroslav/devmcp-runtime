from __future__ import annotations

from typing import Any


class BuildIdentityMixin:
    def _build_identity(self) -> dict[str, Any]:
        raise NotImplementedError

    def server_info_payload(self) -> dict[str, Any]:
        payload = super().server_info_payload()  # type: ignore[misc]
        payload["build_identity"] = self._build_identity()
        return payload

    def service_status(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = super().service_status(args)  # type: ignore[misc]
        payload["build_identity"] = self._build_identity()
        return payload
