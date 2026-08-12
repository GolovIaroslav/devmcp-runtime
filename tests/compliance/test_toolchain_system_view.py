from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime


def bwrap_execution_available() -> tuple[bool, str]:
    if os.name == "nt":
        return False, "bubblewrap is Linux-only"
    binary = shutil.which("bwrap")
    if binary is None:
        return False, "bubblewrap is unavailable"
    try:
        completed = subprocess.run(
            [
                binary,
                "--unshare-user",
                "--uid",
                "0",
                "--gid",
                "0",
                "--ro-bind",
                "/",
                "/",
                "--",
                "/bin/true",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if completed.returncode != 0:
        return False, completed.stderr.strip() or f"bwrap exit {completed.returncode}"
    return True, ""


class ToolchainSystemViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        available, reason = bwrap_execution_available()
        if not available:
            raise unittest.SkipTest(f"real bwrap execution unavailable: {reason}")

    def _runtime(self, root: Path) -> Runtime:
        return Runtime(root, policy_profile="autonomous", sandbox_backend="bwrap")

    def _workspace_runtime(self, root: Path) -> Runtime:
        return Runtime(root, permission_mode="trusted", sandbox_backend="bwrap")

    def _run(self, runtime: Runtime, argv: list[str], *, timeout_ms: int = 30_000):
        return runtime.exec_argv(
            {
                "argv": argv,
                "transaction_mode": "discard",
                "timeout_ms": timeout_ms,
                "yield_time_ms": timeout_ms,
            }
        )

    def _host_version_works(self, argv: list[str]) -> bool:
        try:
            return (
                subprocess.run(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

    def test_python_venv_and_pip_work_when_host_toolchain_supports_them(self) -> None:
        python = shutil.which("python3") or shutil.which("python")
        if python is None:
            self.skipTest("host Python is unavailable")
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            host_probe = Path(tmp) / "host-venv"
            host_venv = subprocess.run(
                [python, "-m", "venv", str(host_probe)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            if host_venv.returncode != 0:
                self.skipTest(
                    f"host python -m venv unavailable: {host_venv.stderr[-300:]}"
                )
            shutil.rmtree(host_probe, ignore_errors=True)

            runtime = self._workspace_runtime(root)
            try:
                venv = self._run(
                    runtime,
                    [python, "-m", "venv", "venv-smoke"],
                    timeout_ms=60_000,
                )
                self.assertEqual(venv["status"], "success", venv)
                pip = self._run(runtime, [python, "-m", "pip", "--version"])
                self.assertEqual(pip["status"], "success", pip)
                self.assertIn("pip", pip["stdout"].lower())
            finally:
                runtime.close()

    def test_available_language_package_toolchains_can_discover_runtime_metadata(
        self,
    ) -> None:
        candidates = [
            (["uv", "--version"], "uv"),
            (["npm", "--version"], "npm"),
            (["pnpm", "--version"], "pnpm"),
            (["cargo", "--version"], "cargo"),
            (["go", "version"], "go"),
            (["mvn", "-version"], "maven"),
            (["gradle", "--version"], "gradle"),
        ]
        host_supported = [
            (argv, name) for argv, name in candidates if self._host_version_works(argv)
        ]
        if not host_supported:
            self.skipTest(
                "none of the optional package/toolchain commands are installed"
            )

        with TemporaryDirectory() as tmp:
            runtime = self._workspace_runtime(Path(tmp))
            try:
                failures: list[str] = []
                for argv, name in host_supported:
                    result = self._run(runtime, argv, timeout_ms=60_000)
                    if result["status"] != "success":
                        failures.append(
                            f"{name}: exit={result.get('exit_code')} stderr={result.get('stderr', '')[-300:]}"
                        )
                self.assertEqual(failures, [])
            finally:
                runtime.close()

    def test_system_view_hides_shadow_and_runtime_home_credentials(self) -> None:
        python = shutil.which("python3") or shutil.which("python")
        if python is None:
            self.skipTest("host Python is unavailable")
        with TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp))
            try:
                shadow = self._run(
                    runtime,
                    [
                        python,
                        "-c",
                        "p='/'+'etc/'+'shadow'; open(p, 'rb').read(1)",
                    ],
                )
                self.assertEqual(shadow["status"], "failed", shadow)
                home = self._run(
                    runtime,
                    [
                        python,
                        "-c",
                        "import os,pathlib; p=pathlib.Path.home(); print(p); print((p/'.ssh').exists()); print((p/'.aws').exists())",
                    ],
                )
                self.assertEqual(home["status"], "success", home)
                lines = home["stdout"].splitlines()
                self.assertNotEqual(lines[0], str(Path.home()))
                self.assertEqual(lines[-2:], ["False", "False"])
            finally:
                runtime.close()

    def test_transactional_exec_argv_applies_success_discards_failure_and_preserves_concurrent_wip(
        self,
    ) -> None:
        python = shutil.which("python3") or shutil.which("python")
        if python is None:
            self.skipTest("host Python is unavailable")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tracked.txt"
            target.write_text("user-wip\n", encoding="utf-8")
            runtime = self._runtime(root)
            try:
                success = runtime.exec_argv(
                    {
                        "argv": [
                            python,
                            "-c",
                            "from pathlib import Path; Path('tracked.txt').write_text('user-wip\\nagent\\n'); Path('generated.bin').write_bytes(b'\\x00artifact')",
                        ],
                        "transaction_mode": "apply",
                        "timeout_ms": 20_000,
                        "yield_time_ms": 20_000,
                    }
                )
                self.assertEqual(success["status"], "success", success)
                self.assertEqual(success["transaction"]["status"], "applied")
                self.assertTrue(success["transaction"]["preexisting_dirty_preserved"])
                self.assertEqual(
                    target.read_text(encoding="utf-8"), "user-wip\nagent\n"
                )
                self.assertEqual((root / "generated.bin").read_bytes(), b"\x00artifact")

                failed = runtime.exec_argv(
                    {
                        "argv": [
                            python,
                            "-c",
                            "from pathlib import Path; Path('tracked.txt').write_text('must-discard\\n'); raise SystemExit(7)",
                        ],
                        "transaction_mode": "apply",
                        "timeout_ms": 20_000,
                        "yield_time_ms": 20_000,
                    }
                )
                self.assertEqual(failed["status"], "failed", failed)
                self.assertEqual(
                    failed["transaction"]["status"], "discarded_on_command_failure"
                )
                self.assertEqual(
                    target.read_text(encoding="utf-8"), "user-wip\nagent\n"
                )

                def concurrent_user_edit() -> None:
                    time.sleep(0.15)
                    target.write_text("concurrent-user-change\n", encoding="utf-8")

                writer = threading.Thread(target=concurrent_user_edit)
                writer.start()
                try:
                    with self.assertRaises(ToolFailure) as conflict:
                        runtime.exec_argv(
                            {
                                "argv": [
                                    python,
                                    "-c",
                                    "import time; from pathlib import Path; time.sleep(0.4); Path('tracked.txt').write_text('agent-late\\n')",
                                ],
                                "transaction_mode": "apply",
                                "timeout_ms": 20_000,
                                "yield_time_ms": 20_000,
                            }
                        )
                finally:
                    writer.join(timeout=2)
                self.assertEqual(conflict.exception.code, "TRANSACTION_CONFLICT")
                self.assertEqual(
                    target.read_text(encoding="utf-8"), "concurrent-user-change\n"
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
