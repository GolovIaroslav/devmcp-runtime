from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from coding_tools_mcp import server as core
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.state_identity import BuildIdentityMixin
from coding_tools_mcp.state_mutations import StateMutationMixin
from coding_tools_mcp.state_snapshot import (
    collect_state_snapshot,
    compare_snapshots,
    filter_ci_runs_for_sha,
    handoff_text,
    read_build_identity,
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


class StateManagementTests(TestCase):
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

    def test_explicit_continuation_reconcile_requires_actual_branch_and_head(
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

                    wrong = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": {"branch": "main", "head": "0" * 40},
                        }
                    )
                    self.assertEqual(
                        wrong["state_reconciliation"]["status"], "not_applied"
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    with self.assertRaises(ToolFailure) as still_drift:
                        runtime.git_create_branch({"name": "still-blocked"})
                    self.assertEqual(still_drift.exception.code, "STATE_DRIFT")
                    self.assertIsNone(inspect_writer_lease(repo, "main"))

                    reconciled = runtime.continuation_checkpoint(
                        {
                            "action": "write",
                            "payload": {"branch": "main", "head": head},
                        }
                    )
                    self.assertEqual(
                        reconciled["state_reconciliation"]["status"], "reconciled"
                    )
                    self.assertIsNone(inspect_writer_lease(repo, "main"))
                    created = runtime.git_create_branch({"name": "after-reconcile"})
                    self.assertEqual(created["branch"], "after-reconcile")
                    self.assertIsNone(inspect_writer_lease(repo, "after-reconcile"))
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
