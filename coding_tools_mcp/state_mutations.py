from __future__ import annotations

from typing import Any

from .errors import ToolFailure
from .state_remote import verify_remote_branch_head


class StateMutationMixin:
    workspace: Any

    def _git_env(self) -> dict[str, str]:
        return super()._git_env()  # type: ignore[misc,attr-defined]

    def _state_preflight(
        self, operation: str, *, extra_branches: list[str] | None = None
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        raise NotImplementedError

    def _state_after(
        self,
        operation: str,
        branch: str,
        previous: dict[str, Any] | None,
        *,
        outcome: str,
        push_verified: bool | None = None,
        remote_head: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _guarded(
        self,
        operation: str,
        callback: Any,
        *,
        extra_branches: list[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raise NotImplementedError

    def _release_state_owner_leases(self) -> list[str]:
        raise NotImplementedError

    def git_switch_branch(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        target = str(args.get("name") or "").strip()
        result, checkpoint = self._guarded(
            "git_switch_branch",
            lambda: parent.git_switch_branch(args),  # type: ignore[attr-defined]
            extra_branches=[target] if target else None,
        )
        result["state_checkpoint"] = checkpoint
        return result

    def git_create_branch(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        target = str(args.get("name") or "").strip()
        result, checkpoint = self._guarded(
            "git_create_branch",
            lambda: parent.git_create_branch(args),  # type: ignore[attr-defined]
            extra_branches=[target] if target else None,
        )
        result["state_checkpoint"] = checkpoint
        return result

    def git_fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        result, checkpoint = self._guarded(
            "git_fetch",
            lambda: parent.git_fetch(args),  # type: ignore[attr-defined]
        )
        result["state_checkpoint"] = checkpoint
        return result

    def git_pull(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        result, checkpoint = self._guarded(
            "git_pull",
            lambda: parent.git_pull(args),  # type: ignore[attr-defined]
        )
        result["state_checkpoint"] = checkpoint
        return result

    def git_merge_remote_branch(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        result, checkpoint = self._guarded(
            "git_merge_remote_branch",
            lambda: parent.git_merge_remote_branch(args),  # type: ignore[attr-defined]
        )
        result["state_checkpoint"] = checkpoint
        return result

    def git_push(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        branch, previous, _before = self._state_preflight("git_push")
        try:
            try:
                result = parent.git_push(args)  # type: ignore[attr-defined]
            except BaseException:
                self._state_after("git_push", branch, previous, outcome="error")
                raise

            pushed_branch = str(result.get("branch") or branch)
            remote = str(result.get("remote") or args.get("remote") or "origin")
            verified, local_head, remote_head = verify_remote_branch_head(
                self.workspace.root,
                pushed_branch,
                remote,
                git_env=self._git_env(),
            )
            if not verified:
                checkpoint = self._state_after(
                    "git_push",
                    branch,
                    previous,
                    outcome="error",
                    push_verified=False,
                    remote_head=remote_head,
                )
                raise ToolFailure(
                    "REMOTE_HEAD_MISMATCH",
                    "Push completed but the authoritative remote branch head does not match local HEAD.",
                    category="conflict",
                    details={
                        "branch": pushed_branch,
                        "remote": remote,
                        "local_head": local_head,
                        "remote_head": remote_head,
                        "state_checkpoint": checkpoint,
                    },
                )

            checkpoint = self._state_after(
                "git_push",
                branch,
                previous,
                outcome="success",
                push_verified=True,
                remote_head=remote_head,
            )
            result["remote_verification"] = {
                "verified": True,
                "local_head": local_head,
                "remote_head": remote_head,
            }
            result["state_checkpoint"] = checkpoint
            return result
        finally:
            self._release_state_owner_leases()

    def service_update(self, args: dict[str, Any]) -> dict[str, Any]:
        parent = super()
        result, checkpoint = self._guarded(
            "service_update",
            lambda: parent.service_update(args),  # type: ignore[attr-defined]
        )
        result["state_checkpoint"] = checkpoint
        return result
