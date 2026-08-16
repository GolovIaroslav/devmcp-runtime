from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from coding_tools_mcp import server as core
from coding_tools_mcp.continuation import (
    checkpoint_path as continuation_checkpoint_path,
)
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.managed_worktree import (
    cleanup_managed_worktree,
    create_managed_worktree,
    registered_worktrees,
)
from coding_tools_mcp.processes import ExecSession
from coding_tools_mcp.session_state import LogicalContextRegistry, SharedJobRegistry
from coding_tools_mcp.state_checkpoint import (
    read_authoritative_state_checkpoint,
    write_state_checkpoint,
)
from coding_tools_mcp.state_identity import BuildIdentityMixin
from coding_tools_mcp.state_mutations import StateMutationMixin
from coding_tools_mcp.state_snapshot import (
    MAX_HASH_BYTES,
    collect_state_snapshot,
    compare_snapshots,
    filter_ci_runs_for_sha,
    handoff_text,
    read_build_identity,
    state_fingerprint,
    state_fingerprint_complete,
)
from coding_tools_mcp.stateful_server import StateManagedRuntime
from coding_tools_mcp.writer_lease import (
    acquire_writer_leases,
    inspect_writer_lease,
    release_writer_lease,
)


def init_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "State Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    return repo, head


def bind_test_managed_workspace(
    runtime: StateManagedRuntime,
    registry: LogicalContextRegistry,
) -> tuple[Path, str]:
    context_id = runtime._ensure_logical_context()
    assert context_id is not None
    state = registry.get(context_id)
    assert state is not None
    worktree, branch = create_managed_worktree(
        runtime.canonical_project_root, context_id, base_revision="HEAD"
    )
    registry.claim_existing_workspace(
        state, target_workspace=worktree, default_cwd=worktree
    )
    core.Runtime._apply_logical_context_state(runtime, state)
    runtime._on_context_mutation_workspace_bound(state)
    return worktree, branch


def write_test_continuation(
    runtime: StateManagedRuntime,
    logical_task: str,
) -> dict[str, object]:
    actual = runtime._state_snapshot()
    branch = str(actual["branch"])
    owner = runtime._state_owner()
    previous = read_authoritative_state_checkpoint(
        runtime.canonical_project_root, owner
    )
    write_state_checkpoint(
        runtime.canonical_project_root,
        branch,
        snapshot=actual,
        phase="after",
        operation="test_resume_setup",
        owner=owner,
        logical_task=logical_task,
        outcome="success",
        previous_checkpoint_id=str((previous or {}).get("checkpoint_id") or "") or None,
        authority_owner=owner,
    )
    return runtime.continuation_checkpoint(
        {
            "action": "write",
            "logical_task": logical_task,
            "payload": {"objective": "resume exact saved state"},
        }
    )


def new_context_runtime(
    repo: Path,
    registry: LogicalContextRegistry,
    *,
    shared_jobs: SharedJobRegistry | None = None,
) -> tuple[StateManagedRuntime, str]:
    runtime = StateManagedRuntime(
        workspace=repo,
        sandbox_backend="unsafe",
        logical_context_registry=registry,
        shared_job_registry=shared_jobs,
        persist_project_selection=False,
    )
    context_id = runtime._ensure_logical_context()
    assert context_id is not None
    return runtime, context_id


class StateManagementTests(TestCase):
    def test_canonical_namespace_with_effective_linked_worktree_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical, canonical_head = init_repo(root)
            effective = root / "effective"
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "effective", str(effective)],
                cwd=canonical,
                check=True,
            )
            (effective / "tracked.txt").write_text("effective\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=effective, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "effective change"], cwd=effective, check=True
            )
            effective_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=effective,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertNotEqual(effective_head, canonical_head)

            restored = core.Runtime(
                canonical,
                project_roots=[root],
                sandbox_backend="unsafe",
            )
            try:
                restored._apply_logical_context_state(
                    SimpleNamespace(
                        canonical_project_root=canonical.resolve(),
                        effective_workspace_root=effective.resolve(),
                        default_cwd=effective.resolve(),
                    )
                )
                self.assertEqual(restored.canonical_project_root, canonical.resolve())
                self.assertEqual(restored.effective_workspace_root, effective.resolve())
                self.assertEqual(restored.default_cwd, effective.resolve())
                self.assertEqual(
                    Path(restored.current_project({})["path"]), canonical.resolve()
                )
            finally:
                restored.close()

            config = root / "config"
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(config)}):
                runtime = StateManagedRuntime(
                    workspace=effective,
                    project_roots=[root],
                    sandbox_backend="unsafe",
                )
                try:
                    runtime.canonical_project_root = canonical.resolve()
                    runtime.active_project = runtime._project_record_for_path(canonical)

                    snapshot = runtime._state_snapshot()
                    self.assertEqual(snapshot["branch"], "effective")
                    self.assertEqual(snapshot["local_head"], effective_head)

                    owner = runtime._state_owner()
                    baseline = runtime._ensure_state_baseline(owner=owner)
                    self.assertIsNotNone(baseline)
                    self.assertIsNotNone(
                        read_authoritative_state_checkpoint(canonical, owner)
                    )
                    self.assertIsNone(
                        read_authoritative_state_checkpoint(effective, owner)
                    )

                    runtime._acquire_state_leases(["effective"], None)
                    self.assertIsNotNone(inspect_writer_lease(canonical, "effective"))
                    self.assertIsNone(inspect_writer_lease(effective, "effective"))
                    runtime._release_state_owner_leases()

                    runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "worktree-foundation",
                            "payload": {"head": effective_head},
                        }
                    )
                    scope = "task:worktree-foundation"
                    self.assertTrue(
                        continuation_checkpoint_path(canonical, scope).is_file()
                    )
                    self.assertFalse(
                        continuation_checkpoint_path(effective, scope).is_file()
                    )
                finally:
                    runtime.close()

    def test_state_managed_runtime_wires_all_required_mutation_guards(self) -> None:
        self.assertTrue(issubclass(StateManagedRuntime, StateMutationMixin))
        self.assertIsNot(StateManagedRuntime.apply_patch, core.Runtime.apply_patch)
        self.assertIsNot(StateManagedRuntime.git_commit, core.Runtime.git_commit)
        for name in (
            "git_create_branch",
            "git_switch_branch",
            "git_fetch",
            "git_pull",
            "git_merge_remote_branch",
            "git_push",
            "service_update",
        ):
            self.assertIs(
                getattr(StateManagedRuntime, name),
                getattr(StateMutationMixin, name),
                name,
            )
        self.assertIs(StateManagedRuntime.service_restart, core.Runtime.service_restart)

    def test_push_guard_verifies_authoritative_remote_head(self) -> None:
        sha = "a" * 40

        class PushBase:
            def git_push(self, args: dict[str, object]) -> dict[str, object]:
                return {
                    "branch": "main",
                    "remote": str(args.get("remote") or "origin"),
                    "result": "pushed",
                }

        class PushHarness(StateMutationMixin, PushBase):
            def __init__(self, root: Path) -> None:
                self.workspace = SimpleNamespace(root=root)
                self.release_calls = 0

            def _git_env(self) -> dict[str, str]:
                return {}

            def _state_preflight(
                self, operation: str, *, extra_branches: list[str] | None = None
            ) -> tuple[str, dict[str, object] | None, dict[str, object]]:
                return "main", None, {}

            def _state_after(
                self,
                operation: str,
                branch: str,
                previous: dict[str, object] | None,
                *,
                outcome: str,
                push_verified: bool | None = None,
                remote_head: str | None = None,
            ) -> dict[str, object]:
                return {
                    "operation": operation,
                    "outcome": outcome,
                    "push_verified": push_verified,
                    "remote_head": remote_head,
                }

            def _release_state_owner_leases(self) -> list[str]:
                self.release_calls += 1
                return ["main"]

        with TemporaryDirectory() as tmp:
            runtime = PushHarness(Path(tmp))
            with patch(
                "coding_tools_mcp.state_mutations.verify_remote_branch_head",
                return_value=(True, sha, sha),
            ):
                result = runtime.git_push({"remote": "origin"})
            self.assertEqual(result["remote_verification"]["remote_head"], sha)
            self.assertTrue(result["state_checkpoint"]["push_verified"])
            self.assertEqual(runtime.release_calls, 1)

            with patch(
                "coding_tools_mcp.state_mutations.verify_remote_branch_head",
                return_value=(False, sha, "b" * 40),
            ):
                with self.assertRaises(ToolFailure) as mismatch:
                    runtime.git_push({"remote": "origin"})
            self.assertEqual(mismatch.exception.code, "REMOTE_HEAD_MISMATCH")
            self.assertEqual(mismatch.exception.details["local_head"], sha)
            self.assertEqual(mismatch.exception.details["remote_head"], "b" * 40)
            self.assertFalse(
                mismatch.exception.details["state_checkpoint"]["push_verified"]
            )
            self.assertEqual(runtime.release_calls, 2)

    def test_build_identity_is_reported_by_server_info_and_service_status(self) -> None:
        sha = "a" * 40

        class IdentityBase:
            def server_info_payload(self) -> dict[str, object]:
                return {"version": "1.2.3"}

            def service_status(self, args: dict[str, object]) -> dict[str, object]:
                return {"stdout": "DevMCP Runtime 1.2.3"}

        class IdentityHarness(BuildIdentityMixin, IdentityBase):
            def _build_identity(self) -> dict[str, object]:
                return {"git_sha": sha, "package_version": "1.2.3"}

        runtime = IdentityHarness()
        self.assertEqual(
            runtime.server_info_payload()["build_identity"]["git_sha"], sha
        )
        self.assertEqual(runtime.service_status({})["build_identity"]["git_sha"], sha)

    def test_writer_is_single_owner_with_release_and_ttl_recovery(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                acquire_writer_leases(
                    repo,
                    ["main"],
                    owner="a",
                    logical_task="task-a",
                    now=100,
                    ttl_seconds=10,
                )
                with self.assertRaises(ToolFailure) as conflict:
                    acquire_writer_leases(
                        repo, ["main"], owner="b", logical_task="task-b", now=101
                    )
                self.assertEqual(conflict.exception.code, "WRITER_LEASE_CONFLICT")
                self.assertTrue(
                    release_writer_lease(
                        repo, "main", owner="a", logical_task="task-a", now=102
                    )
                )
                acquire_writer_leases(
                    repo,
                    ["main"],
                    owner="b",
                    logical_task="task-b",
                    now=103,
                    ttl_seconds=1,
                )
                recovered = acquire_writer_leases(
                    repo, ["main"], owner="c", logical_task="task-c", now=105
                )["main"]
                self.assertEqual(recovered["recovered_stale_owner"]["owner"], "b")

    def test_snapshot_detects_dirty_and_untracked_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            before = collect_state_snapshot(repo, **kwargs)
            (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
            (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
            after = collect_state_snapshot(repo, **kwargs)
            drift = compare_snapshots(before, after)
            self.assertIn("dirty_paths", drift)
            self.assertIn("untracked_paths", drift)
            self.assertIn("content_hashes", drift)

    def test_state_fingerprint_uses_only_canonical_drift_fields(self) -> None:
        base = {
            "branch": "main",
            "local_head": "a" * 40,
            "upstream": None,
            "remote_tracking_head": None,
            "dirty_paths": ["tracked.txt"],
            "staged_paths": [],
            "untracked_paths": [],
            "content_hashes": {"tracked.txt": "b" * 64},
            "timestamp": "first",
            "writer_owner": "one",
        }
        changed_metadata = {**base, "timestamp": "second", "writer_owner": "two"}
        self.assertEqual(state_fingerprint(base), state_fingerprint(changed_metadata))
        changed_content = {
            **base,
            "content_hashes": {"tracked.txt": "c" * 64},
        }
        self.assertNotEqual(state_fingerprint(base), state_fingerprint(changed_content))

        changed_remote_cache = {**base, "remote_tracking_head": "d" * 40}
        self.assertEqual(compare_snapshots(base, changed_remote_cache), {})
        self.assertEqual(
            state_fingerprint(base), state_fingerprint(changed_remote_cache)
        )

    def test_remote_tracking_head_is_not_reported_as_authoritative_remote_head(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            local_only = collect_state_snapshot(repo, **kwargs)
            self.assertEqual(local_only["remote_tracking_head"], head)
            self.assertIsNone(local_only["remote_head"])
            verified = collect_state_snapshot(
                repo,
                **kwargs,
                authoritative_remote_head=head,
                push_verified=True,
            )
            self.assertEqual(verified["remote_head"], head)
            self.assertTrue(verified["push_verified"])

    def test_linked_worktree_fetch_cache_movement_is_informational(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            remote = root / "remote.git"
            worktree_b = root / "worktree-b"
            external = root / "external"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "worker", worktree_b, "main"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "clone", "-q", "-b", "main", str(remote), str(external)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "external@example.com"],
                cwd=external,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "External"], cwd=external, check=True
            )

            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            before = collect_state_snapshot(repo, **kwargs)
            self.assertEqual(before["upstream"], "origin/main")
            self.assertEqual(before["remote_tracking_head"], head)

            (external / "remote.txt").write_text("remote\n", encoding="utf-8")
            subprocess.run(["git", "add", "remote.txt"], cwd=external, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "remote advance"], cwd=external, check=True
            )
            subprocess.run(
                ["git", "push", "-q", "origin", "main"], cwd=external, check=True
            )
            subprocess.run(["git", "fetch", "-q", "origin"], cwd=worktree_b, check=True)

            after = collect_state_snapshot(repo, **kwargs)
            self.assertNotEqual(after["remote_tracking_head"], head)
            self.assertEqual(after["upstream"], "origin/main")
            self.assertEqual(compare_snapshots(before, after), {})
            self.assertEqual(state_fingerprint(before), state_fingerprint(after))

            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    (external / "remote-2.txt").write_text(
                        "remote 2\n", encoding="utf-8"
                    )
                    subprocess.run(
                        ["git", "add", "remote-2.txt"], cwd=external, check=True
                    )
                    subprocess.run(
                        ["git", "commit", "-qm", "remote advance again"],
                        cwd=external,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "push", "-q", "origin", "main"],
                        cwd=external,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "fetch", "-q", "origin"], cwd=worktree_b, check=True
                    )
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; Path('managed.txt').write_text('ok\\n')",
                            ],
                            "state_effect": "selected_repo",
                        }
                    )
                    self.assertTrue(result["command_success"])
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_fetch_prune_preserves_configured_upstream_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            remote = root / "remote.git"
            worktree_b = root / "worktree-b"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "worker", worktree_b, "main"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "receive.denyDeleteCurrent", "ignore"],
                cwd=remote,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-q", "origin", ":main"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "fetch", "-q", "--prune", "origin"], cwd=worktree_b, check=True
            )

            snapshot = collect_state_snapshot(
                repo,
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            self.assertEqual(snapshot["upstream"], "origin/main")
            self.assertIsNone(snapshot["remote_tracking_head"])

    def test_actual_configured_upstream_mutation_causes_state_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )
            subprocess.run(["git", "branch", "other"], cwd=repo, check=True)
            subprocess.run(
                ["git", "push", "-q", "origin", "other"], cwd=repo, check=True
            )
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    subprocess.run(
                        ["git", "branch", "--set-upstream-to=origin/other", "main"],
                        cwd=repo,
                        check=True,
                    )
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.exec_argv(
                            {
                                "argv": ["python3", "-c", "print('blocked')"],
                                "state_effect": "selected_repo",
                            }
                        )
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIn("upstream", drift.exception.details["changed_fields"])
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_reconcile_ignores_fetch_only_remote_cache_movement(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            remote = root / "remote.git"
            external = root / "external"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "clone", "-q", "-b", "main", str(remote), str(external)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "external@example.com"],
                cwd=external,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "External"], cwd=external, check=True
            )
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    (repo / "reviewed.txt").write_text("reviewed\n", encoding="utf-8")
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked"})
                    evidence = drift.exception.details["reconciliation_evidence"]

                    (external / "remote.txt").write_text("remote\n", encoding="utf-8")
                    subprocess.run(
                        ["git", "add", "remote.txt"], cwd=external, check=True
                    )
                    subprocess.run(
                        ["git", "commit", "-qm", "advance"], cwd=external, check=True
                    )
                    subprocess.run(
                        ["git", "push", "-q", "origin", "main"],
                        cwd=external,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "fetch", "-q", "origin"], cwd=repo, check=True
                    )

                    reconciled = runtime.continuation_checkpoint(
                        {"action": "write", "payload": evidence}
                    )
                    self.assertEqual(
                        reconciled["state_reconciliation"]["status"], "reconciled"
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_sensitive_untracked_name_is_not_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
            state = collect_state_snapshot(
                repo,
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            self.assertTrue(state["untracked_paths"][0].startswith("sensitive:"))
            self.assertNotIn(".env", state["content_hashes"])

    def test_fingerprint_distinguishes_staged_index_blob_with_same_worktree(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            (repo / "tracked.txt").write_text("index-a\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("worktree\n", encoding="utf-8")
            first = collect_state_snapshot(repo, **kwargs)

            (repo / "tracked.txt").write_text("index-b\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("worktree\n", encoding="utf-8")
            second = collect_state_snapshot(repo, **kwargs)

            self.assertEqual(first["dirty_paths"], second["dirty_paths"])
            self.assertEqual(first["staged_paths"], second["staged_paths"])
            self.assertEqual(first["content_hashes"], second["content_hashes"])
            self.assertNotEqual(first["index_state_hash"], second["index_state_hash"])
            self.assertNotEqual(state_fingerprint(first), state_fingerprint(second))

    def test_fingerprint_distinguishes_tracked_content_change(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
            first = collect_state_snapshot(repo, **kwargs)
            (repo / "tracked.txt").write_text("three\n", encoding="utf-8")
            second = collect_state_snapshot(repo, **kwargs)
            self.assertNotEqual(state_fingerprint(first), state_fingerprint(second))

    def test_fingerprint_distinguishes_executable_mode_change(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            path = repo / "tracked.txt"
            path.write_text("dirty\n", encoding="utf-8")
            first = collect_state_snapshot(repo, **kwargs)
            path.chmod(path.stat().st_mode | 0o111)
            second = collect_state_snapshot(repo, **kwargs)
            self.assertNotEqual(
                first["worktree_state_hash"], second["worktree_state_hash"]
            )
            self.assertNotEqual(state_fingerprint(first), state_fingerprint(second))

    def test_fingerprint_distinguishes_sensitive_content_without_exposure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            secret = repo / ".env"
            secret.write_text("SECRET=one\n", encoding="utf-8")
            first = collect_state_snapshot(repo, **kwargs)
            secret.write_text("SECRET=two\n", encoding="utf-8")
            second = collect_state_snapshot(repo, **kwargs)
            self.assertNotEqual(
                first["worktree_state_hash"], second["worktree_state_hash"]
            )
            self.assertNotEqual(state_fingerprint(first), state_fingerprint(second))
            self.assertNotIn(".env", json.dumps(second, sort_keys=True))

    def test_fingerprint_distinguishes_symlink_target_change(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            link = repo / "link"
            link.symlink_to("target-a")
            first = collect_state_snapshot(repo, **kwargs)
            link.unlink()
            link.symlink_to("target-b")
            second = collect_state_snapshot(repo, **kwargs)
            self.assertNotEqual(
                first["worktree_state_hash"], second["worktree_state_hash"]
            )
            self.assertNotEqual(state_fingerprint(first), state_fingerprint(second))

    def test_fingerprint_distinguishes_untracked_content_change(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            untracked = repo / "notes.txt"
            untracked.write_text("one\n", encoding="utf-8")
            first = collect_state_snapshot(repo, **kwargs)
            untracked.write_text("two\n", encoding="utf-8")
            second = collect_state_snapshot(repo, **kwargs)
            self.assertNotEqual(state_fingerprint(first), state_fingerprint(second))

    def test_large_dirty_state_marks_fingerprint_incomplete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            (repo / "large.bin").write_bytes(b"x" * (MAX_HASH_BYTES + 1))
            state = collect_state_snapshot(
                repo,
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            self.assertFalse(state["worktree_state_complete"])
            self.assertFalse(state_fingerprint_complete(state))

    def test_ci_identity_rejects_stale_sha(self) -> None:
        current, stale = filter_ci_runs_for_sha(
            "new",
            [
                {"workflow_run_id": 1, "attempt": 1, "job_id": 10, "commit_sha": "old"},
                {"workflow_run_id": 2, "attempt": 2, "job_id": 20, "commit_sha": "new"},
            ],
        )
        self.assertEqual([item["workflow_run_id"] for item in current], [2])
        self.assertEqual([item["workflow_run_id"] for item in stale], [1])

    def test_build_identity_and_generated_handoff_use_exact_sha(self) -> None:
        with TemporaryDirectory() as tmp:
            sha = "a" * 40
            config = Path(tmp) / "config.toml"
            config.write_text(
                f'installed_runtime_sha = "{sha}"\n'
                'installed_runtime_branch = "main"\n'
                'installed_runtime_source_repo = "github.com/example/repo"\n'
                "installed_runtime_dirty_build = false\n",
                encoding="utf-8",
            )
            identity = read_build_identity(
                config_path=str(config), package_version="1.2.3", protocol_version="p"
            )
            self.assertEqual(identity["git_sha"], sha)
            self.assertFalse(identity["dirty_build"])
            current = {
                "repo": "github.com/example/repo",
                "project_id": "p",
                "branch": "main",
                "local_head": sha,
                "upstream": "origin/main",
                "remote_head": sha,
                "dirty_paths": [],
                "staged_paths": [],
                "untracked_paths": [],
                "installed_service_version": "1.2.3",
                "installed_service_git_sha": sha,
                "writer_owner": "a",
                "logical_task": "task-a",
            }
            text = handoff_text(
                checkpoint={"checkpoint_id": "cp"}, current=current, drift={}
            )
            self.assertIn(f"local_head={sha}", text)
            self.assertIn(f"installed_service_git_sha={sha}", text)
            self.assertIn("github_external_state=not_collected_by_devmcp", text)

    def test_untracked_disappearance_is_detected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            probe = repo / "temporary.txt"
            probe.write_text("temporary\n", encoding="utf-8")
            kwargs = dict(
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha=head,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            before = collect_state_snapshot(repo, **kwargs)
            probe.unlink()
            after = collect_state_snapshot(repo, **kwargs)
            self.assertIn("untracked_paths", compare_snapshots(before, after))

    def test_old_service_sha_is_observable_without_git_guessing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            state = collect_state_snapshot(
                repo,
                project_id="fixture",
                installed_service_version="1",
                installed_service_git_sha="b" * 40,
                protocol_version="test",
                writer_owner=None,
                logical_task=None,
            )
            self.assertEqual(state["local_head"], head)
            self.assertNotEqual(state["installed_service_git_sha"], state["local_head"])

    def test_unexpected_external_mutation_still_raises_state_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
            try:
                patch_text = """*** Begin Patch
*** Update File: tracked.txt
@@ -1,1 +1,1 @@
-one
+two
*** End Patch
"""
                runtime.apply_patch({"patch": patch_text})
                self.assertIsNone(inspect_writer_lease(repo, "main"))
                (repo / "external.txt").write_text("outside writer\n", encoding="utf-8")
                with self.assertRaises(ToolFailure) as drift:
                    runtime.git_create_branch({"name": "should-not-create"})
                self.assertEqual(drift.exception.code, "STATE_DRIFT")
                self.assertIsNone(inspect_writer_lease(repo, "main"))
                self.assertIn(
                    "untracked_paths", drift.exception.details["changed_fields"]
                )
            finally:
                runtime.close()

    def test_explicit_continuation_reconcile_requires_exact_observed_state(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.apply_patch(
                        {
                            "patch": """*** Begin Patch
*** Update File: tracked.txt
@@ -1,1 +1,1 @@
-one
+two
*** End Patch
"""
                        }
                    )
                    (repo / "external.txt").write_text(
                        "reviewed external mutation\n", encoding="utf-8"
                    )
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-before-reconcile"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    evidence = drift.exception.details["reconciliation_evidence"]

                    wrong = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": {
                                **evidence,
                                "checkpoint_id": "wrong-checkpoint",
                            },
                        }
                    )
                    self.assertEqual(
                        wrong["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertEqual(
                        wrong["state_reconciliation"]["reason"],
                        "checkpoint_identity_mismatch",
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    with self.assertRaises(ToolFailure) as still_drift:
                        runtime.git_create_branch({"name": "still-blocked"})
                    self.assertEqual(still_drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))

                    reconciled = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": evidence,
                        }
                    )
                    self.assertEqual(
                        reconciled["state_reconciliation"]["status"], "reconciled"
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    created = runtime.git_create_branch({"name": "after-reconcile"})
                    self.assertEqual(created["branch"], "after-reconcile")
                    self.assertIsNone(inspect_writer_lease(repo, "after-reconcile"))

                    old = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": evidence,
                        }
                    )
                    self.assertEqual(
                        old["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertEqual(
                        old["state_reconciliation"]["reason"],
                        "checkpoint_identity_mismatch",
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "after-reconcile"))
                finally:
                    runtime.close()

    def test_continuation_v2_uses_server_derived_state_authority(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    written = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "trusted-v2",
                            "payload": {
                                "objective": "finish 4A",
                                "remaining_items": ["verify"],
                                "branch": "forged-branch",
                                "head": "forged-head",
                                "state_fingerprint": "forged-fingerprint",
                                "workspace_kind": "managed",
                                "workspace_dirty": True,
                                "state_fingerprint_complete": False,
                            },
                        }
                    )
                    payload = written["checkpoint"]["payload"]
                    self.assertEqual(written["checkpoint"]["version"], 2)
                    self.assertEqual(payload["branch"], "main")
                    self.assertEqual(payload["head"], head)
                    self.assertNotEqual(
                        payload["state_fingerprint"], "forged-fingerprint"
                    )
                    self.assertTrue(payload["state_fingerprint_complete"])
                    self.assertEqual(payload["workspace_kind"], "canonical")
                    self.assertFalse(payload["workspace_dirty"])
                    self.assertTrue(payload["checkpoint_id"])
                finally:
                    runtime.close()

    def test_continuation_resume_rebinds_dirty_managed_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    worktree, branch = bind_test_managed_workspace(
                        setup, setup_registry
                    )
                    (worktree / "tracked.txt").write_text(
                        "dirty continuation\n", encoding="utf-8"
                    )
                    written = write_test_continuation(setup, "dirty-managed-resume")
                    payload = written["checkpoint"]["payload"]
                    self.assertEqual(payload["workspace_kind"], "managed")
                    self.assertTrue(payload["workspace_dirty"])
                    self.assertEqual(payload["branch"], branch)
                    self.assertEqual(payload["head"], head)
                finally:
                    setup.close()
                    setup_registry.close()

                resume_registry = LogicalContextRegistry()
                resumed, _resume_context = new_context_runtime(repo, resume_registry)
                try:
                    result = resumed.continuation_checkpoint(
                        {"action": "resume", "logical_task": "dirty-managed-resume"}
                    )
                    self.assertEqual(result["status"], "resumed")
                    self.assertEqual(resumed.effective_workspace_root, worktree)
                    self.assertEqual(resumed.default_cwd, worktree)
                    self.assertEqual(
                        (worktree / "tracked.txt").read_text(encoding="utf-8"),
                        "dirty continuation\n",
                    )
                    self.assertEqual(resumed._state_branch(), branch)
                finally:
                    resumed.close()
                    resume_registry.close()

    def test_continuation_resume_preserves_external_default_cwd(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            outside = root / "outside"
            outside.mkdir()
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    worktree, _branch = bind_test_managed_workspace(
                        setup, setup_registry
                    )
                    (worktree / "tracked.txt").write_text(
                        "dirty continuation\n", encoding="utf-8"
                    )
                    write_test_continuation(setup, "external-cwd-resume")
                finally:
                    setup.close()
                    setup_registry.close()

                resume_registry = LogicalContextRegistry()
                resumed, resume_context = new_context_runtime(repo, resume_registry)
                try:
                    resumed.set_default_cwd({"path": str(outside)})
                    resume_state = resume_registry.get(resume_context)
                    assert resume_state is not None
                    resumed._save_logical_context_state(resume_state)

                    result = resumed.continuation_checkpoint(
                        {"action": "resume", "logical_task": "external-cwd-resume"}
                    )
                    self.assertEqual(result["status"], "resumed")
                    self.assertEqual(resumed.effective_workspace_root, worktree)
                    self.assertEqual(resumed.default_cwd, outside.resolve())
                    self.assertEqual(resume_state.default_cwd, outside.resolve())
                finally:
                    resumed.close()
                    resume_registry.close()

    def test_continuation_resume_recreates_clean_managed_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    original_worktree, branch = bind_test_managed_workspace(
                        setup, setup_registry
                    )
                    written = write_test_continuation(setup, "clean-managed-resume")
                    self.assertFalse(
                        written["checkpoint"]["payload"]["workspace_dirty"]
                    )
                finally:
                    setup.close()
                    setup_registry.close()

                self.assertEqual(
                    cleanup_managed_worktree(repo, original_worktree), "removed_clean"
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
                        cwd=repo,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    ).returncode,
                    0,
                )

                resume_registry = LogicalContextRegistry()
                resumed, _resume_context = new_context_runtime(repo, resume_registry)
                try:
                    result = resumed.continuation_checkpoint(
                        {"action": "resume", "logical_task": "clean-managed-resume"}
                    )
                    recreated = resumed.effective_workspace_root
                    self.assertEqual(result["status"], "resumed")
                    self.assertNotEqual(recreated, original_worktree)
                    self.assertTrue(recreated.is_dir())
                    self.assertEqual(resumed._state_branch(), branch)
                    self.assertEqual(
                        [
                            record.path
                            for record in registered_worktrees(repo)
                            if record.branch == branch
                        ],
                        [recreated],
                    )
                    again = resumed.continuation_checkpoint(
                        {"action": "resume", "logical_task": "clean-managed-resume"}
                    )
                    self.assertEqual(again["status"], "already_resumed")
                    self.assertEqual(resumed.effective_workspace_root, recreated)
                finally:
                    resumed.close()
                    resume_registry.close()

    def test_continuation_resume_claims_canonical_and_preserves_state_drift_guard(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    written = write_test_continuation(setup, "canonical-resume")
                    self.assertEqual(
                        written["checkpoint"]["payload"]["workspace_kind"], "canonical"
                    )
                finally:
                    setup.close()
                    setup_registry.close()

                resume_registry = LogicalContextRegistry()
                resumed, _resume_context = new_context_runtime(repo, resume_registry)
                try:
                    result = resumed.continuation_checkpoint(
                        {"action": "resume", "logical_task": "canonical-resume"}
                    )
                    self.assertEqual(result["status"], "resumed")
                    self.assertEqual(resumed.effective_workspace_root, repo.resolve())
                    self.assertEqual(
                        resumed.continuation_checkpoint(
                            {"action": "resume", "logical_task": "canonical-resume"}
                        )["status"],
                        "already_resumed",
                    )
                    (repo / "tracked.txt").write_text(
                        "drift after resume\n", encoding="utf-8"
                    )
                    with self.assertRaises(ToolFailure) as drift:
                        resumed.git_create_branch({"name": "must-still-drift"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                finally:
                    resumed.close()
                    resume_registry.close()

    def test_continuation_resume_requires_explicit_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                registry = LogicalContextRegistry()
                runtime, _context_id = new_context_runtime(repo, registry)
                try:
                    with self.assertRaises(ToolFailure) as missing_scope:
                        runtime.continuation_checkpoint({"action": "resume"})
                    self.assertEqual(missing_scope.exception.code, "INVALID_ARGUMENT")
                    self.assertEqual(
                        missing_scope.exception.details["reason"],
                        "resume_scope_required",
                    )
                finally:
                    runtime.close()
                    registry.close()

    def test_continuation_resume_requires_active_logical_context(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    write_test_continuation(setup, "active-context-required")
                finally:
                    setup.close()
                    setup_registry.close()

                registry = LogicalContextRegistry()
                resumed = StateManagedRuntime(
                    workspace=repo,
                    sandbox_backend="unsafe",
                    logical_context_registry=registry,
                    persist_project_selection=False,
                )
                try:
                    self.assertIsNone(resumed._active_context_id())
                    with self.assertRaises(ToolFailure) as no_context:
                        resumed.continuation_checkpoint(
                            {
                                "action": "resume",
                                "logical_task": "active-context-required",
                            }
                        )
                    self.assertEqual(no_context.exception.code, "CONTEXT_NOT_FOUND")
                    self.assertEqual(no_context.exception.category, "not_found")
                finally:
                    resumed.close()
                    registry.close()

    def test_continuation_resume_rejects_live_owner_collision(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    write_test_continuation(setup, "live-owner-resume")
                finally:
                    setup.close()
                    setup_registry.close()

                registry = LogicalContextRegistry()
                owner, owner_context = new_context_runtime(repo, registry)
                resumed, _resume_context = new_context_runtime(repo, registry)
                try:
                    owner_state = registry.get(owner_context)
                    assert owner_state is not None
                    registry.claim_existing_workspace(
                        owner_state, target_workspace=repo, default_cwd=repo
                    )
                    with self.assertRaises(ToolFailure) as collision:
                        resumed.continuation_checkpoint(
                            {"action": "resume", "logical_task": "live-owner-resume"}
                        )
                    self.assertEqual(collision.exception.code, "INVALID_STATE")
                    self.assertEqual(
                        collision.exception.details["reason"], "workspace_already_owned"
                    )
                finally:
                    resumed.close()
                    owner.close()
                    registry.close()

    def test_continuation_resume_rejects_active_job_resource(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    write_test_continuation(setup, "active-job-resume")
                finally:
                    setup.close()
                    setup_registry.close()

                registry = LogicalContextRegistry()
                jobs = SharedJobRegistry(context_registry=registry)
                resumed, context_id = new_context_runtime(
                    repo, registry, shared_jobs=jobs
                )
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                session = ExecSession(
                    session_id=jobs.new_handle(),
                    process=process,
                    timeout_at=time.time() + 60,
                )
                jobs.register(
                    session,
                    owner_context_id=context_id,
                    owner_runtime=resumed,
                )
                try:
                    with self.assertRaises(ToolFailure) as active:
                        resumed.continuation_checkpoint(
                            {"action": "resume", "logical_task": "active-job-resume"}
                        )
                    self.assertEqual(active.exception.code, "INVALID_STATE")
                    self.assertEqual(
                        active.exception.details["reason"], "active_command_resources"
                    )
                finally:
                    jobs.close()
                    resumed.close()
                    registry.close()

    def test_continuation_resume_fails_closed_for_legacy_v1(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            scope = "task:legacy-resume"
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    write_test_continuation(setup, "legacy-resume")
                finally:
                    setup.close()
                    setup_registry.close()
                path = continuation_checkpoint_path(repo, scope)
                record = json.loads(path.read_text(encoding="utf-8"))
                record["version"] = 1
                path.write_text(json.dumps(record), encoding="utf-8")

                registry = LogicalContextRegistry()
                resumed, _context_id = new_context_runtime(repo, registry)
                try:
                    with self.assertRaises(ToolFailure) as legacy:
                        resumed.continuation_checkpoint(
                            {"action": "resume", "logical_task": "legacy-resume"}
                        )
                    self.assertEqual(legacy.exception.code, "INVALID_STATE")
                    self.assertEqual(
                        legacy.exception.details["reason"],
                        "resume_metadata_insufficient",
                    )
                finally:
                    resumed.close()
                    registry.close()

    def test_continuation_resume_revalidates_after_claim_before_baseline(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            scope = "task:resume-double-validation"
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                setup_registry = LogicalContextRegistry()
                setup, _setup_context = new_context_runtime(repo, setup_registry)
                try:
                    worktree, _branch = bind_test_managed_workspace(
                        setup, setup_registry
                    )
                    (worktree / "tracked.txt").write_text(
                        "saved dirty state\n", encoding="utf-8"
                    )
                    write_test_continuation(setup, "resume-double-validation")
                finally:
                    setup.close()
                    setup_registry.close()
                before = continuation_checkpoint_path(repo, scope).read_bytes()

                registry = LogicalContextRegistry()
                resumed, context_id = new_context_runtime(repo, registry)
                original_claim = registry.claim_existing_workspace

                def claim_then_tamper(*args: object, **kwargs: object) -> object:
                    previous = original_claim(*args, **kwargs)
                    (worktree / "tracked.txt").write_text(
                        "tampered after first validation\n", encoding="utf-8"
                    )
                    return previous

                try:
                    with patch.object(
                        registry,
                        "claim_existing_workspace",
                        side_effect=claim_then_tamper,
                    ):
                        with self.assertRaises(ToolFailure) as drift:
                            resumed.continuation_checkpoint(
                                {
                                    "action": "resume",
                                    "logical_task": "resume-double-validation",
                                }
                            )
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertEqual(
                        drift.exception.details["reason"], "resume_state_drift"
                    )
                    self.assertIsNone(
                        read_authoritative_state_checkpoint(repo, context_id)
                    )
                    self.assertEqual(
                        continuation_checkpoint_path(repo, scope).read_bytes(), before
                    )
                    self.assertEqual(resumed.effective_workspace_root, repo.resolve())
                finally:
                    resumed.close()
                    registry.close()

    def test_continuation_validation_happens_before_persistence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "validate-first",
                            "payload": {"objective": "trusted-before-drift"},
                        }
                    )
                    scope = "task:validate-first"
                    before = continuation_checkpoint_path(repo, scope).read_bytes()
                    (repo / "external.txt").write_text("drift\n", encoding="utf-8")
                    rejected = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "validate-first",
                            "payload": {
                                "objective": "must-not-persist",
                                "branch": "main",
                                "head": "wrong",
                                "checkpoint_id": "wrong",
                                "state_fingerprint": "wrong",
                            },
                        }
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertEqual(
                        continuation_checkpoint_path(repo, scope).read_bytes(), before
                    )
                finally:
                    runtime.close()

    def test_passed_verification_is_downgraded_when_state_does_not_match(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    fingerprint = state_fingerprint(runtime._state_snapshot())
                    passed = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "verification",
                            "payload": {
                                "verification_status": "passed",
                                "verified_head": head,
                                "verified_state_fingerprint": fingerprint,
                            },
                        }
                    )
                    self.assertEqual(
                        passed["checkpoint"]["payload"]["verification_status"],
                        "passed",
                    )
                    (repo / "external.txt").write_text("changed\n", encoding="utf-8")
                    observed = runtime.continuation_checkpoint(
                        {"action": "read", "logical_task": "verification"}
                    )
                    self.assertEqual(
                        observed["checkpoint"]["payload"]["verification_status"],
                        "stale",
                    )
                    listed = runtime.continuation_checkpoint({"action": "list"})
                    summary = next(
                        item
                        for item in listed["checkpoints"]
                        if item["scope"] == "task:verification"
                    )
                    self.assertEqual(summary["verification_status"], "stale")
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-verification"})
                    evidence = drift.exception.details["reconciliation_evidence"]
                    stale = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "verification",
                            "payload": {
                                **evidence,
                                "verification_status": "passed",
                                "verified_head": head,
                                "verified_state_fingerprint": fingerprint,
                            },
                        }
                    )
                    self.assertEqual(
                        stale["checkpoint"]["payload"]["verification_status"],
                        "stale",
                    )
                finally:
                    runtime.close()

    def test_reconcile_rejects_same_branch_head_after_untracked_toctou(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    (repo / "reviewed.txt").write_text("B\n", encoding="utf-8")
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked"})
                    evidence = drift.exception.details["reconciliation_evidence"]
                    (repo / "later.txt").write_text("C\n", encoding="utf-8")
                    rejected = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": evidence,
                        }
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["reason"],
                        "state_fingerprint_mismatch",
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    with self.assertRaises(ToolFailure) as still_drift:
                        runtime.git_create_branch({"name": "still-blocked"})
                    self.assertEqual(still_drift.exception.code, "STATE_DRIFT")
                finally:
                    runtime.close()

    def test_reconcile_rejects_same_branch_head_after_staged_toctou(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    (repo / "reviewed.txt").write_text("B\n", encoding="utf-8")
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked"})
                    evidence = drift.exception.details["reconciliation_evidence"]
                    subprocess.run(["git", "add", "reviewed.txt"], cwd=repo, check=True)
                    rejected = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": evidence,
                        }
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["reason"],
                        "state_fingerprint_mismatch",
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_reconcile_rejects_same_branch_head_after_content_toctou(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    (repo / "tracked.txt").write_text("B\n", encoding="utf-8")
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked"})
                    evidence = drift.exception.details["reconciliation_evidence"]
                    (repo / "tracked.txt").write_text("C\n", encoding="utf-8")
                    rejected = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": evidence,
                        }
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertEqual(
                        rejected["state_reconciliation"]["reason"],
                        "state_fingerprint_mismatch",
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_initial_dirty_repo_can_be_established_as_context_baseline(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            (repo / "tracked.txt").write_text("existing WIP\n", encoding="utf-8")
            (repo / "existing.txt").write_text("untracked WIP\n", encoding="utf-8")
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    baseline = runtime.local_state_snapshot({})
                    self.assertIn("tracked.txt", baseline["dirty_paths"])
                    created = runtime.git_create_branch({"name": "from-dirty-baseline"})
                    self.assertEqual(created["branch"], "from-dirty-baseline")
                    self.assertIsNone(inspect_writer_lease(repo, "from-dirty-baseline"))
                finally:
                    runtime.close()

    def test_mutation_after_initial_baseline_raises_state_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    (repo / "after-baseline.txt").write_text(
                        "changed\n", encoding="utf-8"
                    )
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_plain_exec_establishes_baseline_before_first_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; Path('via-exec.txt').write_text('changed\\n')",
                            ],
                            "cwd": ".",
                        }
                    )
                    self.assertEqual(result["exit_code"], 0)
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-after-exec"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIn(
                        "untracked_paths", drift.exception.details["changed_fields"]
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_plain_exec_successful_tracked_mutation_still_causes_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_command(
                        {"cmd": "printf 'two\\n' > tracked.txt", "yield_time_ms": 5000}
                    )
                    self.assertTrue(result["command_success"])
                    self.assertNotIn("state_checkpoint", result)
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-after-plain-exec"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_managed_exec_successful_tracked_mutation_advances_checkpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_command(
                        {
                            "cmd": "printf 'two\\n' > tracked.txt",
                            "state_effect": "selected_repo",
                            "yield_time_ms": 1,
                        }
                    )
                    self.assertTrue(result["command_success"])
                    self.assertEqual(
                        result["state_checkpoint"]["snapshot"]["dirty_paths"],
                        ["tracked.txt"],
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    runtime.git_create_branch({"name": "after-managed-exec"})
                finally:
                    runtime.close()

    def test_managed_exec_argv_accepts_untracked_creation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; Path('created.txt').write_text('new\\n')",
                            ],
                            "state_effect": "selected_repo",
                            "yield_time_ms": 1,
                        }
                    )
                    self.assertEqual(
                        result["state_checkpoint"]["snapshot"]["untracked_paths"],
                        ["created.txt"],
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    runtime.git_create_branch({"name": "after-untracked"})
                finally:
                    runtime.close()

    def test_managed_exec_argv_uses_direct_mode_on_bwrap_backend(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="bwrap")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; Path('created.txt').write_text('new\\n')",
                            ],
                            "state_effect": "selected_repo",
                        }
                    )
                    self.assertTrue(result["command_success"])
                    self.assertEqual(result["transaction"]["mode"], "direct")
                    self.assertIn("state_checkpoint", result)
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_managed_exec_argv_accepts_staging(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": ["git", "add", "tracked.txt"],
                            "state_effect": "selected_repo",
                        }
                    )
                    self.assertEqual(
                        result["state_checkpoint"]["snapshot"]["staged_paths"],
                        ["tracked.txt"],
                    )
                    runtime.git_create_branch({"name": "after-stage"})
                finally:
                    runtime.close()

    def test_managed_exec_argv_accepts_git_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = init_repo(root)
            (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": ["git", "commit", "-am", "managed exec commit"],
                            "state_effect": "selected_repo",
                        }
                    )
                    new_head = result["state_checkpoint"]["snapshot"]["local_head"]
                    self.assertNotEqual(new_head, head)
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    runtime.git_create_branch({"name": "after-commit"})
                finally:
                    runtime.close()

    def test_managed_exec_nonzero_without_mutation_keeps_previous_baseline(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": ["python3", "-c", "raise SystemExit(7)"],
                            "state_effect": "selected_repo",
                        }
                    )
                    self.assertEqual(result["exit_code"], 7)
                    self.assertNotIn("state_checkpoint", result)
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    runtime.git_create_branch({"name": "after-clean-failure"})
                finally:
                    runtime.close()

    def test_managed_exec_nonzero_after_partial_mutation_leaves_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; Path('partial.txt').write_text('partial\\n'); raise SystemExit(9)",
                            ],
                            "state_effect": "selected_repo",
                        }
                    )
                    self.assertEqual(result["exit_code"], 9)
                    self.assertNotIn("state_checkpoint", result)
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-after-partial"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_managed_exec_timeout_after_mutation_leaves_drift_and_no_writer(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; import time; Path('partial.txt').write_text('partial\\n'); time.sleep(5); Path('late.txt').write_text('late\\n')",
                            ],
                            "state_effect": "selected_repo",
                            "timeout_ms": 80,
                            "yield_time_ms": 1,
                        }
                    )
                    self.assertTrue(result["timed_out"])
                    self.assertNotIn("state_checkpoint", result)
                    self.assertFalse((repo / "late.txt").exists())
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-after-timeout"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_managed_exec_forces_foreground_despite_short_yield(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; import time; time.sleep(0.08); Path('foreground.txt').write_text('done\\n')",
                            ],
                            "state_effect": "selected_repo",
                            "timeout_ms": 1000,
                            "yield_time_ms": 1,
                        }
                    )
                    self.assertEqual(result["status"], "success")
                    self.assertTrue((repo / "foreground.txt").exists())
                    self.assertIn("state_checkpoint", result)
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_http_managed_exec_rejects_unsafe_timeout_before_spawn_or_lease(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            marker = repo / "must-not-start.txt"
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(
                    workspace=repo, sandbox_backend="unsafe", transport="http"
                )
                try:
                    with patch.object(runtime, "_state_preflight") as state_preflight:
                        with self.assertRaises(ToolFailure) as rejected:
                            runtime.exec_command(
                                {
                                    "argv": [
                                        "python3",
                                        "-c",
                                        "from pathlib import Path; Path('must-not-start.txt').write_text('started\\n')",
                                    ],
                                    "state_effect": "selected_repo",
                                    "timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS
                                    + 1,
                                }
                            )
                        state_preflight.assert_not_called()
                    error = rejected.exception
                    self.assertEqual(error.code, "INVALID_ARGUMENT")
                    self.assertEqual(error.category, "validation")
                    self.assertFalse(marker.exists())
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    self.assertEqual(
                        error.details,
                        {
                            "state_effect": "selected_repo",
                            "requested_timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS
                            + 1,
                            "max_http_timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS,
                            "process_started": False,
                        },
                    )
                    json.dumps(
                        {
                            "code": error.code,
                            "message": error.message,
                            "category": error.category,
                            "retryable": error.retryable,
                            "details": error.details,
                        }
                    )
                finally:
                    runtime.close()

    def test_http_managed_exec_boundary_default_and_exec_argv_are_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(
                    workspace=repo, sandbox_backend="unsafe", transport="http"
                )
                try:
                    boundary = runtime.exec_command(
                        {
                            "cmd": "true",
                            "state_effect": "selected_repo",
                            "timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS,
                        }
                    )
                    self.assertTrue(boundary["command_success"])
                    default_timeout = runtime.exec_command(
                        {"cmd": "true", "state_effect": "selected_repo"}
                    )
                    self.assertTrue(default_timeout["command_success"])
                    with self.assertRaises(ToolFailure) as argv_rejected:
                        runtime.exec_argv(
                            {
                                "argv": ["python3", "-c", "raise SystemExit(0)"],
                                "state_effect": "selected_repo",
                                "timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS + 1,
                            }
                        )
                    self.assertEqual(argv_rejected.exception.code, "INVALID_ARGUMENT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_stdio_managed_exec_allows_timeout_above_http_ceiling(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(
                    workspace=repo, sandbox_backend="unsafe", transport="stdio"
                )
                try:
                    result = runtime.exec_argv(
                        {
                            "argv": ["python3", "-c", "raise SystemExit(0)"],
                            "state_effect": "selected_repo",
                            "timeout_ms": core.HTTP_SAFE_BLOCKING_WAIT_MAX_MS + 1,
                        }
                    )
                    self.assertTrue(result["command_success"])
                    self.assertIn("state_checkpoint", result)
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_external_mutation_after_successful_managed_exec_still_drifts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    managed = runtime.exec_argv(
                        {
                            "argv": [
                                "python3",
                                "-c",
                                "from pathlib import Path; Path('managed.txt').write_text('managed\\n')",
                            ],
                            "state_effect": "selected_repo",
                        }
                    )
                    self.assertIn("state_checkpoint", managed)
                    (repo / "external.txt").write_text("external\n", encoding="utf-8")
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.git_create_branch({"name": "blocked-after-external"})
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_managed_exec_rejects_tty_and_transactional_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    with self.assertRaises(ToolFailure) as tty_error:
                        runtime.exec_command(
                            {
                                "cmd": "true",
                                "tty": True,
                                "state_effect": "selected_repo",
                            }
                        )
                    self.assertEqual(tty_error.exception.code, "INVALID_ARGUMENT")
                    with self.assertRaises(ToolFailure) as transaction_error:
                        runtime.exec_argv(
                            {
                                "argv": ["true"],
                                "transaction_mode": "apply",
                                "state_effect": "selected_repo",
                            }
                        )
                    self.assertEqual(
                        transaction_error.exception.code, "INVALID_ARGUMENT"
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_raw_branch_switch_is_detected_against_context_anchor(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    subprocess.run(
                        ["git", "switch", "-q", "-c", "raw-branch"],
                        cwd=repo,
                        check=True,
                    )
                    with self.assertRaises(ToolFailure) as drift:
                        runtime.apply_patch(
                            {
                                "patch": """*** Begin Patch
*** Update File: tracked.txt
@@ -1,1 +1,1 @@
-one
+two
*** End Patch
"""
                            }
                        )
                    self.assertEqual(drift.exception.code, "STATE_DRIFT")
                    self.assertIn("branch", drift.exception.details["changed_fields"])
                    self.assertIsNone(inspect_writer_lease(repo, "raw-branch"))
                finally:
                    runtime.close()

    def test_managed_branch_create_and_switch_refresh_context_anchor(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            with patch.dict("os.environ", {"DEVMCP_CONFIG_DIR": str(root / "config")}):
                runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
                try:
                    runtime.local_state_snapshot({})
                    created = runtime.git_create_branch({"name": "managed"})
                    self.assertEqual(created["branch"], "managed")
                    self.assertIsNone(inspect_writer_lease(repo, "managed"))
                    switched = runtime.git_switch_branch({"name": "main"})
                    self.assertEqual(switched["branch"], "main")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    runtime.apply_patch(
                        {
                            "patch": """*** Begin Patch
*** Update File: tracked.txt
@@ -1,1 +1,1 @@
-one
+two
*** End Patch
"""
                        }
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                finally:
                    runtime.close()

    def test_completed_state_mutation_does_not_block_another_runtime_owner(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )

            first = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
            second = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")
            try:
                first.git_fetch({"remote": "origin"})
                self.assertIsNone(inspect_writer_lease(repo, "main"))
                second.git_fetch({"remote": "origin"})
                self.assertIsNone(inspect_writer_lease(repo, "main"))
            finally:
                first.close()
                second.close()

    def test_real_state_managed_runtime_mro_and_git_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _head = init_repo(root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", "main"], cwd=repo, check=True
            )

            runtime = StateManagedRuntime(workspace=repo, sandbox_backend="unsafe")

            # 1. _git_env returns env dict without MRO NotImplementedError
            git_env = runtime._git_env()
            self.assertIsInstance(git_env, dict)
            self.assertIn("GIT_CONFIG_KEY_0", git_env)

            # 2. local_state_snapshot works without error
            snapshot = runtime.local_state_snapshot({})
            self.assertEqual(snapshot["branch"], "main")

            # 3. git_status works without error
            status = runtime.git_status({})
            self.assertTrue(status["is_repo"])
            self.assertEqual(status["branch"], "main")

            # 4. guarded apply_patch does not crash due to MRO or recursion
            patch_text = """*** Begin Patch
*** Update File: tracked.txt
@@ -1,1 +1,1 @@
-one
+two
*** End Patch
"""
            res_patch = runtime.apply_patch({"patch": patch_text})
            self.assertIn("state_checkpoint", res_patch)

            # 5. guarded git_commit does not crash due to MRO or recursion
            res_commit = runtime.git_commit(
                {"message": "update file", "paths": ["tracked.txt"]}
            )
            self.assertIn("state_checkpoint", res_commit)

            # 6. guarded git_create_branch & git_switch_branch do not crash due to MRO
            res_create = runtime.git_create_branch({"name": "feature-branch"})
            self.assertIn("state_checkpoint", res_create)

            res_switch = runtime.git_switch_branch({"name": "main"})
            self.assertIn("state_checkpoint", res_switch)

            # 7. fetch/pull refresh automatic checkpoints before later mutations
            res_fetch = runtime.git_fetch({"remote": "origin"})
            self.assertIn("state_checkpoint", res_fetch)
            res_pull = runtime.git_pull({"remote": "origin"})
            self.assertIn("state_checkpoint", res_pull)

            # 8. guarded git_merge_remote_branch does not crash due to MRO
            res_merge = runtime.git_merge_remote_branch({"branch": "main"})
            self.assertIn("state_checkpoint", res_merge)

            # 9. guarded git_push does not crash due to MRO
            res_push = runtime.git_push({"remote": "origin"})
            self.assertIn("state_checkpoint", res_push)
            self.assertIsNone(inspect_writer_lease(repo, "main"))

            # 10. guarded service_update does not crash due to MRO
            with patch.object(
                core.Runtime, "service_update", return_value={"updated": True}
            ):
                res_service = runtime.service_update({})
                self.assertIn("state_checkpoint", res_service)
                self.assertIsNone(inspect_writer_lease(repo, "main"))

            # 11. service restart starts with no completed-operation writer lease
            self.assertIsNone(inspect_writer_lease(repo, "main"))
            with patch.object(
                core.Runtime, "service_restart", return_value={"scheduled": True}
            ):
                res_restart = runtime.service_restart({})
                self.assertTrue(res_restart["scheduled"])
                self.assertIsNone(inspect_writer_lease(repo, "main"))
