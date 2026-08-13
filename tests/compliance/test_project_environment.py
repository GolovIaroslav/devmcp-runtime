from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.sandbox import ExecutionSandbox
from coding_tools_mcp.server import Runtime, exec_output_diagnostics
from tests.compliance.fixtures import init_git


class ProjectEnvironmentTests(unittest.TestCase):
    def _repo(self, root: Path, makefile: str = "test:\n\t@true\n") -> Path:
        repo = root / "repo"
        repo.mkdir()
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        (repo / "Makefile").write_text(makefile, encoding="utf-8")
        init_git(repo)
        return repo

    def test_project_path_does_not_prepend_devmcp_runtime_python(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            runtime = Runtime(repo, sandbox_backend="unsafe")
            try:
                with (
                    patch.object(
                        server_module.sys, "executable", "/opt/devmcp/bin/python3"
                    ),
                    patch.object(server_module.sys, "prefix", "/opt/devmcp"),
                    patch.object(server_module.sys, "base_prefix", "/usr"),
                    patch.dict(
                        os.environ,
                        {"PATH": "/opt/devmcp/bin:/usr/bin:/bin"},
                        clear=False,
                    ),
                ):
                    info = runtime._project_environment_info()
                    env = runtime._task_env({})
                self.assertTrue(info["runtime_bin_removed_from_path"])
                self.assertNotIn("/opt/devmcp/bin", env["PATH"].split(os.pathsep))
                self.assertNotEqual(info["interpreter"], "/opt/devmcp/bin/python3")
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fixture uses POSIX venv/bin layout")
    def test_project_venv_has_priority_for_make_python3(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(
                root,
                "test:\n\t@python3 -c 'import sys; print(sys.executable)'\n",
            )
            venv_bin = repo / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            shim = "#!/bin/sh\nprintf '%s\\n' \"$0\"\n"
            for name in ("python", "python3"):
                path = venv_bin / name
                path.write_text(shim, encoding="utf-8")
                path.chmod(0o755)
            (repo / "pyproject.toml").write_text(
                "[project]\nname='fixture'\nversion='0.0.0'\n", encoding="utf-8"
            )
            runtime = Runtime(repo, permission_mode="trusted", sandbox_backend="unsafe")
            try:
                info = runtime.project_checks({})["execution_environment"]
                self.assertEqual(
                    info["interpreter"], str((venv_bin / "python").resolve())
                )
                self.assertEqual(
                    info["path"].split(os.pathsep)[0], str(venv_bin.resolve())
                )
                result = runtime.run_project_check(
                    {"check_id": "test", "yield_time_ms": 5000, "timeout_ms": 10000}
                )
                self.assertEqual(result["status"], "success")
                self.assertTrue(result["command_success"])
                self.assertIn(".venv/bin/python3", result["stdout"])
            finally:
                runtime.close()

    def test_missing_project_check_executable_has_environment_diagnostic(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            runtime = Runtime(repo, sandbox_backend="unsafe")
            try:
                with patch.dict(os.environ, {"PATH": str(root / "empty")}, clear=False):
                    with self.assertRaises(ToolFailure) as missing:
                        runtime.run_project_check({"check_id": "test"})
                self.assertEqual(missing.exception.code, "PROJECT_ENVIRONMENT_ERROR")
                self.assertIn("execution_environment", missing.exception.details)
            finally:
                runtime.close()

    def test_command_result_distinguishes_success_and_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(
                root,
                "test:\n\t@sh -c 'printf failed-check\\n; exit 7'\n",
            )
            runtime = Runtime(repo, permission_mode="trusted", sandbox_backend="unsafe")
            try:
                succeeded = runtime.exec_command(
                    {"cmd": "true", "yield_time_ms": 5000, "timeout_ms": 10000}
                )
                failed = runtime.exec_command(
                    {"cmd": "false", "yield_time_ms": 5000, "timeout_ms": 10000}
                )
                check = runtime.run_project_check(
                    {"check_id": "test", "yield_time_ms": 5000, "timeout_ms": 10000}
                )
                task = runtime.run_task({"task_id": "test.echo", "yield_time_ms": 5000})
                self.assertEqual(succeeded["status"], "success")
                self.assertTrue(succeeded["command_success"])
                self.assertEqual(failed["status"], "failed")
                self.assertFalse(failed["command_success"])
                self.assertEqual(check["status"], "failed")
                self.assertNotEqual(check["exit_code"], 0)
                self.assertFalse(check["command_success"])
                self.assertEqual(task["status"], "success")
                self.assertTrue(task["command_success"])
            finally:
                runtime.close()

    def test_missing_dependency_output_is_classified(self) -> None:
        diagnostics = exec_output_diagnostics(
            {
                "status": "failed",
                "exit_code": 1,
                "stdout": "",
                "stderr": "python3: No module named 'ruff'",
            }
        )
        codes = {item["code"] for item in diagnostics}
        self.assertIn("PROJECT_DEPENDENCY_MISSING", codes)

    def test_exec_command_structured_argv_bypasses_shell_interpretation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            runtime = Runtime(repo, permission_mode="trusted", sandbox_backend="unsafe")
            try:
                result = runtime.exec_command(
                    {
                        "argv": ["printf", "%s", "literal; echo not-a-shell"],
                        "yield_time_ms": 5000,
                        "timeout_ms": 10000,
                    }
                )
                self.assertEqual(result["status"], "success")
                self.assertTrue(result["command_success"])
                self.assertEqual(result["stdout"], "literal; echo not-a-shell")
                with self.assertRaises(ToolFailure):
                    runtime.exec_command({"cmd": "true", "argv": ["true"]})
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fixture uses POSIX venv symlinks")
    def test_execution_snapshot_preserves_safe_venv_links_and_rewrites_shebang(
        self,
    ) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("system python fixture target is unavailable")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            venv_bin = repo / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python3").symlink_to(system_python)
            (venv_bin / "python").symlink_to("python3")
            console = venv_bin / "pytest"
            console.write_text(
                f"#!{repo}/.venv/bin/python\nprint('ok')\n",
                encoding="utf-8",
            )
            console.chmod(0o755)
            (venv_bin / "private.pem").write_text("not-for-sandbox\n", encoding="utf-8")

            sandbox = ExecutionSandbox.create(repo, owner_root=root / "sandbox-owner")
            try:
                copied_python = sandbox.sandbox_dir / ".venv" / "bin" / "python"
                copied_python3 = sandbox.sandbox_dir / ".venv" / "bin" / "python3"
                copied_console = sandbox.sandbox_dir / ".venv" / "bin" / "pytest"
                self.assertTrue(copied_python.is_symlink())
                self.assertTrue(copied_python3.is_symlink())
                self.assertEqual(copied_python3.resolve(), system_python.resolve())
                first_line = copied_console.read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual(
                    first_line,
                    f"#!{sandbox.sandbox_dir}/.venv/bin/python",
                )
                self.assertFalse(
                    (sandbox.sandbox_dir / ".venv" / "bin" / "private.pem").exists()
                )
            finally:
                sandbox.cleanup()


if __name__ == "__main__":
    unittest.main()
