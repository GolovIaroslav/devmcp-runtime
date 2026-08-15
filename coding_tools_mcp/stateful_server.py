from __future__ import annotations

import os
from typing import Any, Callable

from . import server as core
from .errors import ToolFailure
from .state_checkpoint import (
    read_authoritative_state_checkpoint,
    write_state_checkpoint,
)
from .state_identity import BuildIdentityMixin
from .state_mutations import StateMutationMixin
from .state_snapshot import (
    collect_state_snapshot,
    compare_snapshots,
    git_text,
    read_build_identity,
    state_fingerprint,
)
from .state_store import now_iso
from .writer_lease import (
    acquire_writer_leases,
    inspect_writer_lease,
    release_owner_leases,
)


class StateManagedRuntime(StateMutationMixin, BuildIdentityMixin, core.Runtime):
    """Add cheap branch-level concurrency control around existing DevMCP tools."""

    _EXEC_STATE_EFFECT_NONE = "none"
    _EXEC_STATE_EFFECT_SELECTED_REPO = "selected_repo"

    def _state_owner(self) -> str:
        return self._active_context_id() or f"runtime:{self.server_instance_id}"

    def _state_task(self) -> str | None:
        return self._task_scope_id()

    def _ensure_state_baseline(
        self, *, owner: str | None = None
    ) -> dict[str, Any] | None:
        authority_owner = owner or self._state_owner()
        existing = read_authoritative_state_checkpoint(
            self.canonical_project_root, authority_owner
        )
        if existing is not None:
            return existing
        actual = self._state_snapshot()
        branch = str(actual.get("branch") or "")
        if not branch:
            return None
        return write_state_checkpoint(
            self.canonical_project_root,
            branch,
            snapshot=actual,
            phase="baseline",
            operation="context_baseline",
            owner=authority_owner,
            logical_task=self._state_task(),
            outcome="baseline",
            authority_owner=authority_owner,
        )

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        result = super().initialize(client_info)
        owner = self._active_context_id()
        if owner:
            self._ensure_state_baseline(owner=owner)
        return result

    def _apply_logical_context_state(self, state: Any) -> None:
        super()._apply_logical_context_state(state)
        owner = str(getattr(state, "context_id", "") or "")
        if owner:
            self._ensure_state_baseline(owner=owner)

    def local_state_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_state_baseline()
        return super().local_state_snapshot(args)

    def _profile_exec_command(self, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_state_baseline()
        state_effect = str(
            args.get("state_effect", self._EXEC_STATE_EFFECT_NONE)
        ).strip()
        if state_effect == self._EXEC_STATE_EFFECT_NONE:
            return super()._profile_exec_command(args)
        if state_effect != self._EXEC_STATE_EFFECT_SELECTED_REPO:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "state_effect must be none or selected_repo.",
                category="validation",
            )
        if bool(args.get("tty", False)):
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "state_effect=selected_repo is incompatible with tty=true because the command must finish in the same tool call.",
                category="validation",
            )
        transaction_mode = str(args.get("transaction_mode", "discard")).strip().lower()
        if transaction_mode != "discard":
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "state_effect=selected_repo requires direct non-transactional execution.",
                category="validation",
            )

        managed_args = dict(args)
        timeout_ms = int(managed_args.get("timeout_ms", 30000))
        managed_args["yield_time_ms"] = timeout_ms

        branch, previous, _before = self._state_preflight("exec_command")
        try:
            result = super()._profile_exec_command(managed_args)
            if result.get("status") == "running":
                raise ToolFailure(
                    "INVALID_STATE",
                    "State-managed exec did not reach a terminal state in the tool call.",
                    category="runtime",
                )
            if result.get("command_success") is True:
                current_branch = self._state_branch()
                if current_branch != branch:
                    raise ToolFailure(
                        "INVALID_STATE",
                        "State-managed exec changed the current branch; the result was not accepted as authoritative state.",
                        category="conflict",
                        retryable=True,
                        details={
                            "before_branch": branch,
                            "after_branch": current_branch,
                        },
                    )
                checkpoint = self._state_after(
                    "exec_command", branch, previous, outcome="success"
                )
                result["state_checkpoint"] = checkpoint
            return result
        finally:
            self._release_state_owner_leases()

    def exec_argv(self, args: dict[str, Any]) -> dict[str, Any]:
        state_effect = str(
            args.get("state_effect", self._EXEC_STATE_EFFECT_NONE)
        ).strip()
        if (
            state_effect == self._EXEC_STATE_EFFECT_SELECTED_REPO
            and "transaction_mode" not in args
        ):
            args = {**args, "transaction_mode": "discard"}
        return super().exec_argv(args)

    def _execute_task_argv(
        self, argv: list[str], args: dict[str, Any], capabilities: set[str]
    ) -> dict[str, Any]:
        self._ensure_state_baseline()
        return super()._execute_task_argv(argv, args, capabilities)

    def _state_branch(self) -> str:
        branch = git_text(
            self.effective_workspace_root,
            ["branch", "--show-current"],
            env=self._git_env(),
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
            self.effective_workspace_root,
            ["branch", "--show-current"],
            env=self._git_env(),
        )
        lease = (
            inspect_writer_lease(self.canonical_project_root, branch)
            if branch
            else None
        )
        return collect_state_snapshot(
            self.effective_workspace_root,
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
            self.canonical_project_root,
            branches,
            owner=self._state_owner(),
            logical_task=self._state_task(),
            checkpoint_id=checkpoint_id,
        )

    def _release_state_owner_leases(self) -> list[str]:
        return release_owner_leases(
            self.canonical_project_root,
            owner=self._state_owner(),
        )

    def _state_preflight(
        self, operation: str, *, extra_branches: list[str] | None = None
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        branch = self._state_branch()
        authority_owner = self._state_owner()
        expected_record = read_authoritative_state_checkpoint(
            self.canonical_project_root, authority_owner
        )
        if expected_record is None:
            expected_record = self._ensure_state_baseline(owner=authority_owner)
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
                    evidence = {
                        "checkpoint_id": str(
                            (expected_record or {}).get("checkpoint_id") or ""
                        ),
                        "branch": actual.get("branch"),
                        "head": actual.get("local_head"),
                        "state_fingerprint": state_fingerprint(actual),
                    }
                    raise ToolFailure(
                        "STATE_DRIFT",
                        "Repository state changed since the last automatic checkpoint.",
                        category="conflict",
                        retryable=True,
                        details={
                            "expected": expected_snapshot,
                            "actual": actual,
                            "changed_fields": drift,
                            "reconciliation_evidence": evidence,
                            "reconcile": (
                                "Inspect the changed fields, then pass the returned branch, head, "
                                "checkpoint_id, and state_fingerprint back in the "
                                "continuation_checkpoint write payload."
                            ),
                        },
                    )
            write_state_checkpoint(
                self.canonical_project_root,
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
            self.canonical_project_root,
            current_branch,
            snapshot=actual,
            phase="after",
            operation=operation,
            owner=self._state_owner(),
            logical_task=self._state_task(),
            outcome=outcome,
            previous_checkpoint_id=str((previous or {}).get("checkpoint_id") or "")
            or None,
            authority_owner=self._state_owner(),
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

        requested_checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
        requested_branch = str(payload.get("branch") or "").strip()
        requested_head = str(payload.get("head") or "").strip()
        requested_fingerprint = str(payload.get("state_fingerprint") or "").strip()

        actual_branch = self._state_branch()
        authority_owner = self._state_owner()
        previous = read_authoritative_state_checkpoint(
            self.canonical_project_root, authority_owner
        )
        self._acquire_state_leases(
            [actual_branch],
            str((previous or {}).get("checkpoint_id") or "") or None,
        )
        try:
            actual = self._state_snapshot()
            actual_head = str(actual.get("local_head") or "")
            actual_fingerprint = state_fingerprint(actual)
            current_checkpoint_id = str((previous or {}).get("checkpoint_id") or "")
            if (
                not requested_checkpoint_id
                or not requested_branch
                or not requested_head
                or not requested_fingerprint
            ):
                result["state_reconciliation"] = {
                    "status": "not_applied",
                    "reason": "missing_reconciliation_evidence",
                }
                return result
            if requested_checkpoint_id != current_checkpoint_id:
                result["state_reconciliation"] = {
                    "status": "not_applied",
                    "reason": "checkpoint_identity_mismatch",
                    "requested_checkpoint_id": requested_checkpoint_id,
                    "actual_checkpoint_id": current_checkpoint_id or None,
                }
                return result
            if requested_branch != actual_branch or requested_head != actual_head:
                result["state_reconciliation"] = {
                    "status": "not_applied",
                    "reason": "payload_branch_head_mismatch",
                    "actual_branch": actual_branch,
                    "actual_head": actual_head,
                }
                return result
            if requested_fingerprint != actual_fingerprint:
                result["state_reconciliation"] = {
                    "status": "not_applied",
                    "reason": "state_fingerprint_mismatch",
                    "requested_fingerprint": requested_fingerprint,
                    "actual_fingerprint": actual_fingerprint,
                }
                return result

            checkpoint = write_state_checkpoint(
                self.canonical_project_root,
                actual_branch,
                snapshot=actual,
                phase="after",
                operation="continuation_checkpoint_reconcile",
                owner=self._state_owner(),
                logical_task=self._state_task(),
                outcome="reconciled",
                previous_checkpoint_id=str((previous or {}).get("checkpoint_id") or "")
                or None,
                authority_owner=authority_owner,
            )
            result["state_reconciliation"] = {
                "status": "reconciled",
                "checkpoint_id": checkpoint["checkpoint_id"],
                "branch": actual_branch,
                "head": actual_head,
                "state_fingerprint": actual_fingerprint,
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
