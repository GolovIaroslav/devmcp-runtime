from __future__ import annotations

import os
from typing import Any, Callable

from . import server as core
from .errors import ToolFailure
from .state_checkpoint import read_state_checkpoint, write_state_checkpoint
from .state_identity import BuildIdentityMixin
from .state_mutations import StateMutationMixin
from .state_snapshot import (
    collect_state_snapshot,
    compare_snapshots,
    git_text,
    read_build_identity,
)
from .state_store import now_iso
from .writer_lease import (
    acquire_writer_leases,
    inspect_writer_lease,
    release_owner_leases,
)


class StateManagedRuntime(StateMutationMixin, BuildIdentityMixin, core.Runtime):
    """Add cheap branch-level concurrency control around existing DevMCP tools."""

    def _state_owner(self) -> str:
        return self._active_context_id() or f"runtime:{self.server_instance_id}"

    def _state_task(self) -> str | None:
        return self._task_scope_id()

    def _state_branch(self) -> str:
        branch = git_text(
            self.workspace.root, ["branch", "--show-current"], env=self._git_env()
        )
        if not branch:
            raise ToolFailure(
                "INVALID_STATE",
                "State-managed mutation requires a named local branch.",
                category="conflict",
            )
        return branch

    def _build_identity(self) -> dict[str, Any]:
        return read_build_identity(
            config_path=os.environ.get("DEVMCP_POLICY_CONFIG_FILE"),
            package_version=core.__version__,
            protocol_version=self.protocol_version,
            env_sha=os.environ.get("DEVMCP_INSTALLED_RUNTIME_SHA"),
        )

    def _state_snapshot(
        self,
        *,
        push_verified: bool | None = None,
        remote_head: str | None = None,
    ) -> dict[str, Any]:
        branch = git_text(
            self.workspace.root, ["branch", "--show-current"], env=self._git_env()
        )
        lease = inspect_writer_lease(self.workspace.root, branch) if branch else None
        return collect_state_snapshot(
            self.workspace.root,
            project_id=str(self.active_project.get("id") or "") or None,
            installed_service_version=core.__version__,
            installed_service_git_sha=self._installed_runtime_sha(),
            protocol_version=self.protocol_version,
            writer_owner=str((lease or {}).get("owner") or "") or None,
            logical_task=str((lease or {}).get("logical_task") or "") or None,
            git_env=self._git_env(),
            push_verified=push_verified,
            authoritative_remote_head=remote_head,
            timestamp=now_iso(),
        )

    def _acquire_state_leases(
        self, branches: list[str], checkpoint_id: str | None
    ) -> None:
        acquire_writer_leases(
            self.workspace.root,
            branches,
            owner=self._state_owner(),
            logical_task=self._state_task(),
            checkpoint_id=checkpoint_id,
        )

    def _release_state_owner_leases(self) -> list[str]:
        return release_owner_leases(
            self.workspace.root,
            owner=self._state_owner(),
        )

    def _state_preflight(
        self, operation: str, *, extra_branches: list[str] | None = None
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        branch = self._state_branch()
        expected_record = read_state_checkpoint(self.workspace.root, branch)
        expected_snapshot = (
            expected_record.get("snapshot")
            if isinstance(expected_record, dict)
            and isinstance(expected_record.get("snapshot"), dict)
            else None
        )
        self._acquire_state_leases(
            [branch, *(extra_branches or [])],
            str((expected_record or {}).get("checkpoint_id") or "") or None,
        )
        try:
            actual = self._state_snapshot()
            if expected_snapshot is not None:
                drift = compare_snapshots(expected_snapshot, actual)
                if drift:
                    raise ToolFailure(
                        "STATE_DRIFT",
                        "Repository state changed since the last automatic checkpoint.",
                        category="conflict",
                        retryable=True,
                        details={
                            "expected": expected_snapshot,
                            "actual": actual,
                            "changed_fields": drift,
                            "reconcile": (
                                "Inspect the changed fields, then explicitly reconcile with "
                                "continuation_checkpoint(action=write, payload={branch: <actual branch>, "
                                "head: <actual local_head>})."
                            ),
                        },
                    )
            write_state_checkpoint(
                self.workspace.root,
                branch,
                snapshot=actual,
                phase="before",
                operation=operation,
                owner=self._state_owner(),
                logical_task=self._state_task(),
                outcome="started",
                previous_checkpoint_id=str(
                    (expected_record or {}).get("checkpoint_id") or ""
                )
                or None,
            )
            return branch, expected_record, actual
        except BaseException:
            self._release_state_owner_leases()
            raise

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
        actual = self._state_snapshot(
            push_verified=push_verified,
            remote_head=remote_head,
        )
        current_branch = str(actual.get("branch") or branch)
        return write_state_checkpoint(
            self.workspace.root,
            current_branch,
            snapshot=actual,
            phase="after",
            operation=operation,
            owner=self._state_owner(),
            logical_task=self._state_task(),
            outcome=outcome,
            previous_checkpoint_id=str((previous or {}).get("checkpoint_id") or "")
            or None,
        )

    def _guarded(
        self,
        operation: str,
        callback: Callable[[], dict[str, Any]],
        *,
        extra_branches: list[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        branch, previous, _before = self._state_preflight(
            operation, extra_branches=extra_branches
        )
        try:
            try:
                result = callback()
            except BaseException:
                self._state_after(operation, branch, previous, outcome="error")
                raise
            checkpoint = self._state_after(
                operation, branch, previous, outcome="success"
            )
            return result, checkpoint
        finally:
            self._release_state_owner_leases()

    def apply_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        result, checkpoint = self._guarded(
            "apply_patch", lambda: super(StateManagedRuntime, self).apply_patch(args)
        )
        result["state_checkpoint"] = checkpoint
        return result

    def git_commit(self, args: dict[str, Any]) -> dict[str, Any]:
        result, checkpoint = self._guarded(
            "git_commit", lambda: super(StateManagedRuntime, self).git_commit(args)
        )
        result["state_checkpoint"] = checkpoint
        return result

    def continuation_checkpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        result = super().continuation_checkpoint(args)
        action = str(args.get("action") or "").strip().lower()
        payload = args.get("payload")
        if action != "write" or not isinstance(payload, dict):
            return result

        requested_branch = str(payload.get("branch") or "").strip()
        requested_head = str(payload.get("head") or "").strip()
        if not requested_branch or not requested_head:
            return result

        actual_branch = self._state_branch()
        previous = read_state_checkpoint(self.workspace.root, actual_branch)
        self._acquire_state_leases(
            [actual_branch],
            str((previous or {}).get("checkpoint_id") or "") or None,
        )
        try:
            actual = self._state_snapshot()
            actual_head = str(actual.get("local_head") or "")
            if requested_branch != actual_branch or requested_head != actual_head:
                result["state_reconciliation"] = {
                    "status": "not_applied",
                    "reason": "payload_branch_head_mismatch",
                    "actual_branch": actual_branch,
                    "actual_head": actual_head,
                }
                return result

            checkpoint = write_state_checkpoint(
                self.workspace.root,
                actual_branch,
                snapshot=actual,
                phase="after",
                operation="continuation_checkpoint_reconcile",
                owner=self._state_owner(),
                logical_task=self._state_task(),
                outcome="reconciled",
                previous_checkpoint_id=str((previous or {}).get("checkpoint_id") or "")
                or None,
            )
            result["state_reconciliation"] = {
                "status": "reconciled",
                "checkpoint_id": checkpoint["checkpoint_id"],
                "branch": actual_branch,
                "head": actual_head,
            }
            return result
        finally:
            self._release_state_owner_leases()


def main(argv: list[str] | None = None) -> int:
    original_runtime = core.Runtime
    setattr(core, "Runtime", StateManagedRuntime)
    try:
        return core.main(argv)
    finally:
        setattr(core, "Runtime", original_runtime)
