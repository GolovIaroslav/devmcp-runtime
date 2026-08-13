from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime
from tests.compliance.fixtures import init_git


class PytestCollectionTests(unittest.TestCase):
    def test_root_collection_excludes_executable_fixture_projects(self) -> None:
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertNotIn(
            "tests/compliance/fixtures/tiny-python-project/tests/test_math_utils.py",
            completed.stdout,
        )

    def test_p0_multi_project_git_dogfood(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "projects"
            first = library / "first"
            second = library / "nested" / "second"
            remote = root / "remote.git"
            for repo, value in ((first, "first\n"), (second, "second\n")):
                repo.mkdir(parents=True)
                (repo / "tracked.txt").write_text(value, encoding="utf-8")
                if repo == first:
                    (repo / "Makefile").write_text(
                        "test:\n\t@printf 'project-check-ok\\n'\n", encoding="utf-8"
                    )
                init_git(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(
                ["git", "-C", str(first), "remote", "add", "origin", str(remote)],
                check=True,
            )
            with patch.dict(
                "os.environ",
                {"DEVMCP_CONFIG_DIR": str(root / "config")},
                clear=False,
            ):
                runtime = Runtime(
                    first,
                    policy_profile="balanced",
                    project_roots=[library],
                    sandbox_backend="unsafe",
                )
                try:
                    projects = runtime.list_projects({})["projects"]
                    self.assertEqual(
                        {item["relative_path"] for item in projects},
                        {"first", "nested/second"},
                    )
                    runtime.select_project({"project": "first"})
                    self.assertEqual(
                        runtime.read_file({"path": "tracked.txt"})["content"],
                        "first\n",
                    )
                    checks = runtime.project_checks({})["checks"]
                    self.assertEqual([item["id"] for item in checks], ["test"])
                    checked = runtime.run_project_check(
                        {"check_id": "test", "timeout_ms": 5000, "yield_time_ms": 5000}
                    )
                    self.assertEqual(checked.get("exit_code"), 0, checked)
                    self.assertIn("project-check-ok", checked.get("stdout", ""))
                    res = runtime.read_file({"path": "../nested/second/tracked.txt"})
                    self.assertEqual(res.get("content"), "second\n")
                    runtime.apply_patch(
                        {
                            "patch": (
                                "*** Begin Patch\n"
                                "*** Update File: tracked.txt\n"
                                "@@\n"
                                "-first\n"
                                "+changed\n"
                                "*** End Patch"
                            )
                        }
                    )
                    (first / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
                    runtime.git_create_branch({"name": "feature/dogfood"})
                    commit = runtime.git_commit(
                        {"message": "test: dogfood", "paths": ["tracked.txt"]}
                    )
                    self.assertEqual(commit["branch"], "feature/dogfood")
                    self.assertIn(
                        "?? unrelated.txt",
                        subprocess.run(
                            ["git", "-C", str(first), "status", "--porcelain"],
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout,
                    )
                    pushed = runtime.git_push({})
                    self.assertEqual(pushed["result"], "pushed")
                    self.assertEqual(
                        subprocess.run(
                            [
                                "git",
                                "--git-dir",
                                str(remote),
                                "rev-parse",
                                "refs/heads/feature/dogfood",
                            ],
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout.strip(),
                        commit["sha"],
                    )
                    self.assertEqual(
                        (second / "tracked.txt").read_text(encoding="utf-8"),
                        "second\n",
                    )
                finally:
                    runtime.close()

    def test_p0_session_isolation_symlink_and_uv_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "projects"
            first = library / "first"
            second = library / "second"
            for repo in (first, second):
                repo.mkdir(parents=True)
                (repo / "tracked.txt").write_text(repo.name + "\n", encoding="utf-8")
                init_git(repo)
            session_a = Runtime(
                first, policy_profile="balanced", project_roots=[library]
            )
            session_b = Runtime(
                first, policy_profile="balanced", project_roots=[library]
            )
            try:
                session_a.select_project({"project": "first"})
                session_b.select_project({"project": "second"})
                self.assertEqual(
                    session_a.current_project({})["relative_path"], "first"
                )
                self.assertEqual(
                    session_b.current_project({})["relative_path"], "second"
                )
                self.assertEqual(session_a.workspace.root, first.resolve())
                self.assertEqual(session_b.workspace.root, second.resolve())
                if sys.platform != "win32":
                    (first / "escape").symlink_to(second, target_is_directory=True)
                    res = session_a.read_file({"path": "escape/tracked.txt"})
                    self.assertEqual(res.get("content"), "second\n")
            finally:
                session_a.close()
                session_b.close()

            uv_repo = library / "uv-project"
            uv_repo.mkdir()
            (uv_repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(uv_repo)
            (uv_repo / "pyproject.toml").write_text(
                "[project]\nname='fixture'\nversion='0.0.0'\n", encoding="utf-8"
            )
            (uv_repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (uv_repo / "tests").mkdir()
            runtime = Runtime(
                uv_repo, policy_profile="balanced", project_roots=[library]
            )
            try:
                checks = runtime.project_checks({})["checks"]
                test_check = next(item for item in checks if item["id"] == "test")
                self.assertEqual(test_check["environment"], "uv")
                self.assertEqual(
                    test_check["argv"][:5],
                    ["uv", "run", "--offline", "--frozen", "--no-sync"],
                )
                self.assertNotEqual(test_check["argv"][0], "pytest")
                self.assertEqual(runtime.list_tasks({"category": "git"})["tasks"], [])
                with self.assertRaises(ToolFailure):
                    runtime.run_task({"task_id": "git.status"})
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
