from __future__ import annotations

import os
from typing import Any, Callable

from . import server as core
from .continuation import (
    normalize_checkpoint_payload,
    read_checkpoint,
    write_checkpoint,
)
from .errors import ToolFailure
from .managed_worktree import (
    attach_existing_branch_worktree,
    cleanup_managed_worktree,
    registered_managed_worktrees,
    registered_worktrees,
)
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
    state_fingerprint_complete,
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

    def _on_context_mutation_workspace_bound(self, state: Any) -> None:
        owner = str(getattr(state, "context_id", "") or "")
        actual = self._state_snapshot()
        branch = str(actual.get("branch") or "")
        if not owner or not branch:
            raise ToolFailure(
                "INVALID_STATE",
                "Managed worktree binding requires a named branch and logical context.",
                category="conflict",
            )
        previous = read_authoritative_state_checkpoint(
            self.canonical_project_root, owner
        )
        write_state_checkpoint(
            self.canonical_project_root,
            branch,
            snapshot=actual,
            phase="baseline",
            operation="managed_worktree_bind",
            owner=owner,
            logical_task=self._state_task(),
            outcome="baseline",
            previous_checkpoint_id=str((previous or {}).get("checkpoint_id") or "")
            or None,
            authority_owner=owner,
        )

    def _context_mutation_workspace_base_revision(self, state: Any) -> str:
        owner = str(getattr(state, "context_id", "") or "")
        checkpoint = (
            read_authoritative_state_checkpoint(self.canonical_project_root, owner)
            if owner
            else None
        )
        snapshot = (
            checkpoint.get("snapshot")
            if isinstance(checkpoint, dict)
            and isinstance(checkpoint.get("snapshot"), dict)
            else None
        )
        local_head = str((snapshot or {}).get("local_head") or "").strip()
        return local_head or "HEAD"

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
        if (
            self.transport == "http"
            and timeout_ms > core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS
        ):
            raise ToolFailure(
                "INVALID_ARGUMENT",
                (
                    "state_effect=selected_repo execution is synchronous, and HTTP "
                    "cannot safely wait for the requested timeout. Maximum safe HTTP "
                    f"timeout is {core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS} ms; the process "
                    "was not started. Use state_effect=none only for long non-mutating "
                    "checks/builds/tests; split or reduce genuinely mutating commands."
                ),
                category="validation",
                details={
                    "state_effect": self._EXEC_STATE_EFFECT_SELECTED_REPO,
                    "requested_timeout_ms": timeout_ms,
                    "max_http_timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS,
                    "process_started": False,
                },
            )
        managed_args["yield_time_ms"] = timeout_ms
        managed_args["_force_foreground_wait"] = True

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

    def _state_snapshot_for_workspace(self, workspace: Any) -> dict[str, Any]:
        return collect_state_snapshot(
            workspace,
            project_id=str(self.active_project.get("id") or "") or None,
            installed_service_version=core.__version__,
            installed_service_git_sha=self._installed_runtime_sha(),
            protocol_version=self.protocol_version,
            writer_owner=None,
            logical_task=None,
            git_env=self._git_env(),
            timestamp=now_iso(),
        )

    @staticmethod
    def _resume_failure(
        code: str,
        message: str,
        reason: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> ToolFailure:
        return ToolFailure(
            code,
            message,
            category="conflict"
            if code in {"STATE_DRIFT", "INVALID_STATE"}
            else "not_found",
            retryable=retryable,
            details={"reason": reason, **(details or {})},
        )

    def _validate_resume_snapshot(
        self,
        payload: dict[str, Any],
        workspace: Any,
    ) -> dict[str, Any]:
        actual = self._state_snapshot_for_workspace(workspace)
        expected_branch = str(payload.get("branch") or "")
        expected_head = str(payload.get("head") or "")
        expected_fingerprint = str(payload.get("state_fingerprint") or "")
        actual_fingerprint = state_fingerprint(actual)
        if (
            actual.get("branch") != expected_branch
            or actual.get("local_head") != expected_head
            or actual_fingerprint != expected_fingerprint
            or not state_fingerprint_complete(actual)
        ):
            raise self._resume_failure(
                "STATE_DRIFT",
                "Saved continuation state no longer exactly matches the resume target.",
                "resume_state_drift",
                retryable=True,
                details={
                    "expected_branch": expected_branch,
                    "expected_head": expected_head,
                    "expected_state_fingerprint": expected_fingerprint,
                    "actual_branch": actual.get("branch"),
                    "actual_head": actual.get("local_head"),
                    "actual_state_fingerprint": actual_fingerprint,
                },
            )
        expected_dirty = bool(payload.get("workspace_dirty"))
        actual_dirty = bool(
            actual.get("dirty_paths")
            or actual.get("staged_paths")
            or actual.get("untracked_paths")
        )
        if expected_dirty != actual_dirty:
            raise self._resume_failure(
                "STATE_DRIFT",
                "Saved continuation dirty-state classification changed.",
                "resume_state_drift",
                retryable=True,
                details={
                    "expected_dirty": expected_dirty,
                    "actual_dirty": actual_dirty,
                },
            )
        return actual

    def _resume_active_resources(self, context_id: str) -> bool:
        with self.sessions_lock:
            active_commands = self.starting_sessions or any(
                session.process.poll() is None for session in self.sessions.values()
            )
        with self.sandbox_lock:
            active_sandbox = self.sandbox_users > 0
        active_job = bool(
            self.shared_job_registry is not None
            and self.shared_job_registry.has_running_jobs(context_id)
        )
        return bool(active_commands or active_sandbox or active_job)

    def _resume_target(
        self,
        *,
        payload: dict[str, Any],
        context_id: str,
    ) -> tuple[Any, bool]:
        canonical = self.canonical_project_root.resolve(strict=True)
        branch = str(payload["branch"])
        head = str(payload["head"])
        workspace_kind = str(payload["workspace_kind"])
        dirty = bool(payload["workspace_dirty"])
        all_worktrees = registered_worktrees(canonical)
        managed = [
            item
            for item in registered_managed_worktrees(canonical)
            if item.branch == branch
        ]

        if workspace_kind == "managed" and dirty:
            if len(managed) != 1:
                reason = (
                    "ambiguous_target" if len(managed) > 1 else "resume_target_missing"
                )
                code = "INVALID_STATE" if len(managed) > 1 else "NOT_FOUND"
                raise self._resume_failure(
                    code,
                    "Dirty managed continuation does not have one exact preserved worktree.",
                    reason,
                    details={"candidate_count": len(managed), "branch": branch},
                )
            if managed[0].head != head:
                raise self._resume_failure(
                    "STATE_DRIFT",
                    "Preserved managed worktree HEAD changed after checkpoint.",
                    "resume_state_drift",
                    retryable=True,
                )
            return managed[0].path, False

        if workspace_kind == "canonical":
            try:
                canonical_actual = self._validate_resume_snapshot(payload, canonical)
            except ToolFailure as exact_error:
                if dirty:
                    raise exact_error
            else:
                del canonical_actual
                return canonical, False

        exact_managed = [
            item for item in managed if item.head == head and item.path.is_dir()
        ]
        if len(exact_managed) == 1:
            return exact_managed[0].path, False
        if len(exact_managed) > 1:
            raise self._resume_failure(
                "INVALID_STATE",
                "Multiple managed worktrees match the saved continuation branch.",
                "ambiguous_target",
            )

        branch_ref = git_text(
            canonical, ["rev-parse", f"refs/heads/{branch}"], env=self._git_env()
        )
        if branch_ref != head:
            raise self._resume_failure(
                "INVALID_STATE",
                "Saved continuation branch is missing or no longer points at saved HEAD.",
                "resume_target_missing",
                details={
                    "branch": branch,
                    "saved_head": head,
                    "current_ref": branch_ref,
                },
            )
        occupied = [item for item in all_worktrees if item.branch == branch]
        if occupied:
            raise self._resume_failure(
                "INVALID_STATE",
                "Saved clean continuation branch is already checked out in another worktree.",
                "workspace_already_owned",
                details={"paths": [str(item.path) for item in occupied]},
            )
        return attach_existing_branch_worktree(canonical, context_id, branch), True

    def _resume_continuation(self, args: dict[str, Any]) -> dict[str, Any]:
        logical_task = str(args.get("logical_task") or "").strip()
        branch_scope = str(args.get("branch") or "").strip()
        if not logical_task and not branch_scope:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "action=resume requires explicit logical_task or branch scope.",
                category="validation",
                details={"reason": "resume_scope_required"},
            )
        scope = self._continuation_scope(args)
        record = read_checkpoint(self.canonical_project_root, scope)
        if record is None:
            raise self._resume_failure(
                "NOT_FOUND",
                "Continuation checkpoint was not found.",
                "continuation_not_found",
            )
        raw_payload = record.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        required_strings = ("branch", "head", "checkpoint_id", "state_fingerprint")
        if (
            record.get("version") != 2
            or any(
                not isinstance(payload.get(key), str) or not payload.get(key)
                for key in required_strings
            )
            or payload.get("workspace_kind") not in {"canonical", "managed"}
            or not isinstance(payload.get("workspace_dirty"), bool)
        ):
            raise self._resume_failure(
                "INVALID_STATE",
                "Continuation checkpoint lacks trusted v2 resume metadata.",
                "resume_metadata_insufficient",
            )
        if payload.get("state_fingerprint_complete") is not True:
            raise self._resume_failure(
                "INVALID_STATE",
                "Continuation checkpoint does not contain a complete state fingerprint.",
                "fingerprint_incomplete",
            )
        registry = self.logical_context_registry
        if registry is None:
            raise ToolFailure(
                "CONTEXT_NOT_FOUND",
                "Resume requires logical-context support.",
                category="not_found",
                retryable=True,
            )
        context_id = self._ensure_logical_context()
        if context_id is None:
            raise ToolFailure(
                "CONTEXT_NOT_FOUND",
                "Resume requires a live logical context.",
                category="not_found",
                retryable=True,
            )
        state = registry.get(context_id)
        if state is None:
            raise ToolFailure(
                "CONTEXT_NOT_FOUND",
                "Resume requires a live logical context.",
                category="not_found",
                retryable=True,
            )
        if state.canonical_project_root.resolve(
            strict=True
        ) != self.canonical_project_root.resolve(strict=True):
            raise self._resume_failure(
                "INVALID_STATE",
                "Current logical context is bound to a different canonical project.",
                "context_workspace_conflict",
            )
        if self._resume_active_resources(context_id):
            raise self._resume_failure(
                "INVALID_STATE",
                "Cannot change continuation routing while command or job resources are active.",
                "active_command_resources",
            )

        try:
            if state.mutation_workspace_claimed:
                self._validate_resume_snapshot(payload, state.effective_workspace_root)
                return {
                    "action": "resume",
                    "scope": scope,
                    "status": "already_resumed",
                    "checkpoint": record,
                }
        except ToolFailure:
            if state.effective_workspace_root != state.canonical_project_root:
                raise self._resume_failure(
                    "INVALID_STATE",
                    "Current context already owns a conflicting mutation workspace.",
                    "context_workspace_conflict",
                )

        saved_branch = str(payload["branch"])
        self._acquire_state_leases([saved_branch], str(payload["checkpoint_id"]))
        created_target = False
        claimed = False
        target: Any = None
        previous: tuple[Any, Any, bool] | None = None
        try:
            target, created_target = self._resume_target(
                payload=payload,
                context_id=context_id,
            )
            self._validate_resume_snapshot(payload, target)
            mapped_cwd = self._map_logical_context_default_cwd(state, target)
            try:
                previous = registry.claim_existing_workspace(
                    state,
                    target_workspace=target,
                    default_cwd=mapped_cwd,
                )
            except RuntimeError as exc:
                raise ToolFailure(
                    "CONTEXT_NOT_FOUND",
                    "Logical context disappeared during continuation resume.",
                    category="not_found",
                    retryable=True,
                ) from exc
            except ValueError as exc:
                raise self._resume_failure(
                    "INVALID_STATE",
                    "Current context already owns another mutation workspace.",
                    "context_workspace_conflict",
                ) from exc
            except FileExistsError as exc:
                raise self._resume_failure(
                    "INVALID_STATE",
                    "Resume target is owned by another live logical context.",
                    "workspace_already_owned",
                ) from exc
            claimed = True
            core.Runtime._apply_logical_context_state(self, state)
            actual = self._validate_resume_snapshot(
                payload, self.effective_workspace_root
            )
            new_state_checkpoint = write_state_checkpoint(
                self.canonical_project_root,
                saved_branch,
                snapshot=actual,
                phase="baseline",
                operation="continuation_resume",
                owner=context_id,
                logical_task=self._state_task(),
                outcome="baseline",
                previous_checkpoint_id=str(payload["checkpoint_id"]),
                authority_owner=context_id,
            )
            refreshed_payload = dict(payload)
            refreshed_payload.update(
                {
                    "branch": actual.get("branch"),
                    "head": actual.get("local_head"),
                    "checkpoint_id": new_state_checkpoint["checkpoint_id"],
                    "state_fingerprint": state_fingerprint(actual),
                    "state_fingerprint_complete": state_fingerprint_complete(actual),
                    "workspace_kind": (
                        "canonical"
                        if self.effective_workspace_root == self.canonical_project_root
                        else "managed"
                    ),
                    "workspace_dirty": bool(
                        actual.get("dirty_paths")
                        or actual.get("staged_paths")
                        or actual.get("untracked_paths")
                    ),
                }
            )
            refreshed = write_checkpoint(
                self.canonical_project_root,
                scope,
                refreshed_payload,
            )
            return {
                "action": "resume",
                "scope": scope,
                "status": "resumed",
                "checkpoint": refreshed,
                "state_checkpoint": new_state_checkpoint,
                "workspace": str(self.effective_workspace_root),
            }
        except BaseException:
            if claimed and previous is not None and target is not None:
                registry.rollback_existing_workspace_claim(
                    state,
                    expected_workspace=target,
                    previous=previous,
                )
                try:
                    core.Runtime._apply_logical_context_state(self, state)
                except BaseException:
                    pass
            if created_target and target is not None:
                cleanup_managed_worktree(self.canonical_project_root, target)
            raise
        finally:
            self._release_state_owner_leases()

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
        action = str(args.get("action") or "").strip().lower()
        if action == "resume":
            return self._resume_continuation(args)
        if action != "write":
            result = super().continuation_checkpoint(args)
            if action not in {"read", "list"}:
                return result
            actual = self._state_snapshot()
            actual_head = str(actual.get("local_head") or "")
            actual_fingerprint = state_fingerprint(actual)

            def downgrade_verification(payload: dict[str, Any]) -> None:
                if payload.get("verification_status") != "passed":
                    return
                if (
                    payload.get("verified_head") != actual_head
                    or payload.get("verified_state_fingerprint") != actual_fingerprint
                ):
                    payload["verification_status"] = "stale"

            if action == "read":
                checkpoint = result.get("checkpoint")
                if isinstance(checkpoint, dict):
                    raw_payload = checkpoint.get("payload")
                    if isinstance(raw_payload, dict):
                        checkpoint["payload"] = dict(raw_payload)
                        downgrade_verification(checkpoint["payload"])
                return result
            checkpoints = result.get("checkpoints")
            if isinstance(checkpoints, list):
                for item in checkpoints:
                    if isinstance(item, dict):
                        downgrade_verification(item)
            return result
        if "payload" not in args:
            return super().continuation_checkpoint(args)
        payload = normalize_checkpoint_payload(args.get("payload"))
        requested_checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
        requested_branch = str(payload.get("branch") or "").strip()
        requested_head = str(payload.get("head") or "").strip()
        requested_fingerprint = str(payload.get("state_fingerprint") or "").strip()

        self._ensure_state_baseline()
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
            expected_snapshot = (
                previous.get("snapshot")
                if isinstance(previous, dict)
                and isinstance(previous.get("snapshot"), dict)
                else None
            )
            drift = (
                compare_snapshots(expected_snapshot, actual)
                if expected_snapshot is not None
                else {}
            )
            reconciliation: dict[str, Any] = {"status": "not_needed"}
            requested_evidence = (
                requested_checkpoint_id,
                requested_branch,
                requested_head,
                requested_fingerprint,
            )
            has_complete_requested_evidence = all(requested_evidence)
            if has_complete_requested_evidence:
                if requested_checkpoint_id != current_checkpoint_id:
                    return {
                        "action": action,
                        "scope": self._continuation_scope(args),
                        "checkpoint": None,
                        "state_reconciliation": {
                            "status": "not_applied",
                            "reason": "checkpoint_identity_mismatch",
                            "requested_checkpoint_id": requested_checkpoint_id,
                            "actual_checkpoint_id": current_checkpoint_id or None,
                        },
                    }
                if requested_branch != actual_branch or requested_head != actual_head:
                    return {
                        "action": action,
                        "scope": self._continuation_scope(args),
                        "checkpoint": None,
                        "state_reconciliation": {
                            "status": "not_applied",
                            "reason": "payload_branch_head_mismatch",
                            "actual_branch": actual_branch,
                            "actual_head": actual_head,
                        },
                    }
                if requested_fingerprint != actual_fingerprint:
                    return {
                        "action": action,
                        "scope": self._continuation_scope(args),
                        "checkpoint": None,
                        "state_reconciliation": {
                            "status": "not_applied",
                            "reason": "state_fingerprint_mismatch",
                            "requested_fingerprint": requested_fingerprint,
                            "actual_fingerprint": actual_fingerprint,
                        },
                    }
            if drift:
                if not has_complete_requested_evidence:
                    return {
                        "action": action,
                        "scope": self._continuation_scope(args),
                        "checkpoint": None,
                        "state_reconciliation": {
                            "status": "not_applied",
                            "reason": "missing_reconciliation_evidence",
                        },
                    }
                previous = write_state_checkpoint(
                    self.canonical_project_root,
                    actual_branch,
                    snapshot=actual,
                    phase="after",
                    operation="continuation_checkpoint_reconcile",
                    owner=self._state_owner(),
                    logical_task=self._state_task(),
                    outcome="reconciled",
                    previous_checkpoint_id=current_checkpoint_id or None,
                    authority_owner=authority_owner,
                )
                current_checkpoint_id = str(previous["checkpoint_id"])
                reconciliation = {
                    "status": "reconciled",
                    "checkpoint_id": current_checkpoint_id,
                }

            verified_status = str(payload.get("verification_status") or "not_run")
            verified_head = str(payload.get("verified_head") or "").strip()
            verified_fingerprint = str(
                payload.get("verified_state_fingerprint") or ""
            ).strip()
            if verified_status == "passed":
                if not verified_head or not verified_fingerprint:
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        "passed verification requires verified_head and verified_state_fingerprint.",
                        category="validation",
                    )
                if (
                    verified_head != actual_head
                    or verified_fingerprint != actual_fingerprint
                ):
                    verified_status = "stale"

            trusted_payload = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "branch",
                    "head",
                    "checkpoint_id",
                    "state_fingerprint",
                    "state_fingerprint_complete",
                    "workspace_kind",
                    "workspace_dirty",
                    "verification_status",
                }
            }
            trusted_payload.update(
                {
                    "branch": actual_branch,
                    "head": actual_head,
                    "checkpoint_id": current_checkpoint_id,
                    "state_fingerprint": actual_fingerprint,
                    "state_fingerprint_complete": state_fingerprint_complete(actual),
                    "workspace_kind": (
                        "canonical"
                        if self.effective_workspace_root == self.canonical_project_root
                        else "managed"
                    ),
                    "workspace_dirty": bool(
                        actual.get("dirty_paths")
                        or actual.get("staged_paths")
                        or actual.get("untracked_paths")
                    ),
                    "verification_status": verified_status,
                }
            )
            scope = self._continuation_scope(args)
            record = write_checkpoint(
                self.canonical_project_root,
                scope,
                trusted_payload,
            )
            return {
                "action": action,
                "scope": scope,
                "checkpoint": record,
                "state_reconciliation": reconciliation,
            }
        finally:
            self._release_state_owner_leases()


def main(argv: list[str] | None = None) -> int:
    original_runtime = core.Runtime
    setattr(core, "Runtime", StateManagedRuntime)
    try:
        return core.main(argv)
    finally:
        setattr(core, "Runtime", original_runtime)
