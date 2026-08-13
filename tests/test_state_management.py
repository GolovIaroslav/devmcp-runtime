from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.state_snapshot import (
    collect_state_snapshot,
    compare_snapshots,
    filter_ci_runs_for_sha,
    handoff_text,
    read_build_identity,
)
from coding_tools_mcp.writer_lease import acquire_writer_leases, release_writer_lease


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
