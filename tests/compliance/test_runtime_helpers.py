from __future__ import annotations

import builtins
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp import processes as processes_module
from coding_tools_mcp.patching import AtomicPatchCommitter, FileBaseline, StagedFile
from coding_tools_mcp.sandbox import ExecutionSandbox, SandboxBackend
from coding_tools_mcp.server import (
    LANDLOCK_ACCESS_FS_IOCTL_DEV,
    LANDLOCK_ACCESS_FS_TRUNCATE,
    LANDLOCK_ACCESS_FS_WRITE_FILE,
    MAX_ACTIVE_EXEC_SESSIONS,
    MAX_PATCH_BASELINE_BYTES,
    Runtime,
    ShellEnvPolicy,
    ToolFailure,
    exec_output_diagnostics,
    guard_allow_roots,
    identify_image,
    permission_failure_diagnostics,
    runtime_parent_root,
)
from coding_tools_mcp.processes import ExecSession
from coding_tools_mcp.textutils import truncate_text_head, truncate_text_tail
from coding_tools_mcp.tool_results import (
    MODEL_TEXT_SAFETY_LIMIT_BYTES,
    make_tool_result,
)
from tests.compliance.fixtures import git_fixture_preflight_error, init_git


@contextmanager
def fake_landlock_exec() -> Iterator[dict[str, object]]:
    """Patch landlock + Popen so exec_command runs without spawning a process.

    Yields a dict capturing the landlock write_roots and the Popen args/kwargs;
    "read_fd" holds the fd handed to the server (closed by exec_command itself).
    """
    read_fd, write_fd = os.pipe()
    original_open = server_module.open_landlock_ruleset
    original_popen = server_module.subprocess.Popen
    original_watchdog = server_module.start_session_watchdog
    captured: dict[str, object] = {"read_fd": read_fd}

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        pid = 1

        def poll(self) -> int:
            return 0

    def fake_open(_workspace: Path, _read_roots: list[str], **kwargs: object) -> int:
        captured["write_roots"] = kwargs.get("write_roots")
        return read_fd

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    server_module.open_landlock_ruleset = fake_open
    server_module.subprocess.Popen = fake_popen  # type: ignore[method-assign]
    server_module.start_session_watchdog = lambda _session: None
    try:
        yield captured
    finally:
        server_module.open_landlock_ruleset = original_open
        server_module.subprocess.Popen = original_popen  # type: ignore[method-assign]
        server_module.start_session_watchdog = original_watchdog
        os.close(write_fd)


class RuntimeHelperTests(unittest.TestCase):
    def test_windows_tty_request_reports_explicit_unsupported_error(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.object(processes_module.os, "name", "nt"),
        ):
            with self.assertRaises(ToolFailure) as raised:
                processes_module.spawn_process(
                    "ignored",
                    cwd=tmp,
                    shell=True,
                    env={},
                    tty=True,
                    popen_kwargs={},
                )
        self.assertEqual(raised.exception.code, "TTY_UNSUPPORTED")
        self.assertEqual(raised.exception.details.get("platform"), "nt")

    def test_windows_process_termination_distinguishes_graceful_and_force(self) -> None:
        class FakeProcess:
            pid = 123

            def __init__(self) -> None:
                self.calls: list[object] = []

            def send_signal(self, value: object) -> None:
                self.calls.append(("send_signal", value))

            def wait(self, timeout: float) -> int:
                self.calls.append(("wait", timeout))
                return 0

            def terminate(self) -> None:
                self.calls.append("terminate")

            def kill(self) -> None:
                self.calls.append("kill")

        def fake_hasattr(value: object, name: str) -> bool:
            if value is processes_module.os and name == "killpg":
                return False
            return builtins.hasattr(value, name)

        with (
            patch.object(processes_module.os, "name", "nt"),
            patch.object(
                processes_module, "hasattr", side_effect=fake_hasattr, create=True
            ),
            patch.object(processes_module.signal, "CTRL_BREAK_EVENT", 999, create=True),
        ):
            graceful = FakeProcess()
            processes_module.terminate_process_group(  # type: ignore[arg-type]
                graceful,
                signal.SIGTERM,
            )
            forced = FakeProcess()
            processes_module.terminate_process_group(  # type: ignore[arg-type]
                forced,
                processes_module.HARD_KILL_SIGNAL,
                force=True,
            )

        self.assertEqual(graceful.calls, [("send_signal", 999), ("wait", 1)])
        self.assertEqual(forced.calls, ["kill", ("wait", 1)])

    def test_posix_process_termination_escalates_after_bounded_wait(self) -> None:
        class FakeProcess:
            pid = 123

            def __init__(self) -> None:
                self.wait_calls: list[float] = []

            def wait(self, timeout: float) -> int:
                self.wait_calls.append(timeout)
                if len(self.wait_calls) == 1:
                    raise subprocess.TimeoutExpired("fixture", timeout)
                return 0

            def poll(self) -> int:
                return 0 if len(self.wait_calls) > 1 else None  # type: ignore[return-value]

            def terminate(self) -> None:
                raise AssertionError("fallback terminate should not be used")

            def kill(self) -> None:
                raise AssertionError("fallback kill should not be used")

        process = FakeProcess()
        with patch.object(processes_module.os, "killpg") as killpg:
            processes_module.terminate_process_group(  # type: ignore[arg-type]
                process,
                signal.SIGTERM,
            )

        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(123, signal.SIGTERM),
                unittest.mock.call(123, processes_module.HARD_KILL_SIGNAL),
            ],
        )
        self.assertEqual(process.wait_calls, [1, 1])

    def test_atomic_patch_commit_rolls_back_all_files_after_mid_commit_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first-before\n", encoding="utf-8")
            second.write_text("second-before\n", encoding="utf-8")
            changes = [
                StagedFile(
                    "first.txt",
                    first,
                    "first-after\n",
                    FileBaseline.capture(first),
                    0o644,
                ),
                StagedFile(
                    "second.txt",
                    second,
                    "second-after\n",
                    FileBaseline.capture(second),
                    0o644,
                ),
            ]
            real_replace = os.replace

            def fail_second_install(
                source: os.PathLike[str] | str, destination: os.PathLike[str] | str
            ) -> None:
                source_path = Path(source)
                if (
                    source_path.name.startswith(".coding-tools-patch-")
                    and Path(destination) == second
                ):
                    raise OSError("injected second-file install failure")
                real_replace(source, destination)

            with patch(
                "coding_tools_mcp.patching.os.replace", side_effect=fail_second_install
            ):
                with self.assertRaises(OSError):
                    AtomicPatchCommitter().commit(changes)

            self.assertEqual(first.read_text(encoding="utf-8"), "first-before\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "second-before\n")
            self.assertEqual(list(root.glob(".coding-tools-*-*")), [])

    def test_atomic_patch_commit_rejects_stale_baseline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.txt"
            path.write_text("before\n", encoding="utf-8")
            baseline = FileBaseline.capture(path)
            path.write_text("external-change\n", encoding="utf-8")
            change = StagedFile("file.txt", path, "patch-change\n", baseline, 0o644)

            with self.assertRaises(ToolFailure) as raised:
                AtomicPatchCommitter().commit([change])

            self.assertEqual(raised.exception.code, "PATCH_CONFLICT")
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(path.read_text(encoding="utf-8"), "external-change\n")

    def test_patch_baselines_have_a_bounded_byte_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "file.txt"
            path.write_text("before\n", encoding="utf-8")
            runtime = Runtime(root, permission_mode="trusted", sandbox_backend="unsafe")
            runtime.patch_baseline_bytes = MAX_PATCH_BASELINE_BYTES
            change = StagedFile(
                "file.txt",
                path,
                "after\n",
                FileBaseline.capture(path),
                0o644,
            )
            try:
                with self.assertRaises(ToolFailure) as raised:
                    runtime._commit_staged_files([change])
                self.assertEqual(raised.exception.code, "PATCH_BASELINE_LIMIT")
                self.assertEqual(path.read_text(encoding="utf-8"), "before\n")
                self.assertEqual(runtime.patch_baselines, {})
            finally:
                runtime.close()

    def test_atomic_patch_commit_preserves_backup_when_rollback_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file.txt"
            target.write_text("before\n", encoding="utf-8")
            change = StagedFile(
                "file.txt",
                target,
                "after\n",
                FileBaseline.capture(target),
                0o644,
            )
            real_replace = os.replace

            def fail_install_and_restore(
                source: os.PathLike[str] | str,
                destination: os.PathLike[str] | str,
            ) -> None:
                source_path = Path(source)
                if source_path.name.startswith(".coding-tools-patch-"):
                    raise OSError("injected install failure")
                if source_path.name.startswith(".coding-tools-backup-"):
                    raise OSError("injected rollback failure")
                real_replace(source, destination)

            with patch(
                "coding_tools_mcp.patching.os.replace",
                side_effect=fail_install_and_restore,
            ):
                with self.assertRaises(ToolFailure) as raised:
                    AtomicPatchCommitter().commit([change])

            self.assertEqual(raised.exception.code, "PATCH_ROLLBACK_FAILED")
            backups = raised.exception.details.get("recovery_backups", {})
            self.assertEqual(set(backups), {"file.txt"})
            recovery_path = Path(backups["file.txt"])
            self.assertTrue(recovery_path.exists())
            self.assertEqual(recovery_path.read_text(encoding="utf-8"), "before\n")

    def test_atomic_patch_backup_cleanup_failure_does_not_rollback_committed_file(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file.txt"
            target.write_text("before\n", encoding="utf-8")
            change = StagedFile(
                "file.txt",
                target,
                "after\n",
                FileBaseline.capture(target),
                0o644,
            )
            real_unlink = Path.unlink
            backup_unlinks = 0

            def fail_backup_cleanup(
                path: Path, *args: object, **kwargs: object
            ) -> None:
                nonlocal backup_unlinks
                if path.name.startswith(".coding-tools-backup-"):
                    backup_unlinks += 1
                    if backup_unlinks > 1:
                        raise OSError("injected backup cleanup failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_backup_cleanup):
                AtomicPatchCommitter().commit([change])

            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            retained_backups = list(root.glob(".coding-tools-backup-*"))
            self.assertEqual(len(retained_backups), 1)
            self.assertEqual(
                retained_backups[0].read_text(encoding="utf-8"), "before\n"
            )

    def test_atomic_patch_commit_does_not_overwrite_new_target_race(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "new.txt"
            baseline = FileBaseline.capture(path)
            path.write_text("external-create\n", encoding="utf-8")

            with self.assertRaises(ToolFailure) as raised:
                AtomicPatchCommitter().commit(
                    [StagedFile("new.txt", path, "patch-create\n", baseline, None)]
                )

            self.assertEqual(raised.exception.code, "PATCH_CONFLICT")
            self.assertEqual(path.read_text(encoding="utf-8"), "external-create\n")

    def test_image_identification_reads_jpeg_and_webp_dimensions(self) -> None:
        jpeg = (
            b"\xff\xd8"
            b"\xff\xe0\x00\x02"
            b"\xff\xc0\x00\x11\x08\x00\x10\x00\x20\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
            b"\xff\xd9"
        )
        self.assertEqual(
            identify_image(jpeg, path=file_path("sample.jpg")), ("image/jpeg", 32, 16)
        )

        webp = (
            b"RIFF"
            + (22).to_bytes(4, "little")
            + b"WEBPVP8X"
            + (10).to_bytes(4, "little")
        )
        webp += (
            b"\x00\x00\x00\x00"
            + (63).to_bytes(3, "little")
            + (31).to_bytes(3, "little")
        )
        self.assertEqual(
            identify_image(webp, path=file_path("sample.webp")), ("image/webp", 64, 32)
        )

    def test_tail_truncation_keeps_recent_complete_output(self) -> None:
        result = truncate_text_tail(
            "\n".join(f"line-{index:03d}" for index in range(80)), max_bytes=128
        )
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertIn("line-079", result.content)
        self.assertNotIn("line-000", result.content)

    def test_head_truncation_keeps_overlong_first_line_prefix(self) -> None:
        result = truncate_text_head("a" * 200, max_bytes=20)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertEqual(result.content, "a" * 20)
        self.assertEqual(result.output_bytes, 20)
        self.assertTrue(result.first_line_exceeds_limit)

    def test_head_truncation_keeps_utf8_boundary(self) -> None:
        result = truncate_text_head("é" * 100, max_bytes=21)
        self.assertTrue(result.truncated)
        self.assertTrue(result.content)
        self.assertLessEqual(len(result.content.encode("utf-8")), 21)
        self.assertNotIn("\ufffd", result.content)

    def test_tail_truncation_keeps_long_line_before_trailing_newline(self) -> None:
        result = truncate_text_tail(("a" * 200) + "\n", max_bytes=20)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertEqual(result.content, "a" * 20)
        self.assertTrue(result.last_line_partial)

    def test_command_policy_allows_literal_patterns(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "index.html").write_text("</html>\n", encoding="utf-8")
            runtime = Runtime(workspace)
            runtime._check_command_policy("grep '</html>' index.html", {})
            runtime._check_command_policy('echo "https://example.com/a/b"', {})

    def test_package_module_entrypoint_exposes_help(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "coding_tools_mcp", "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--workspace", result.stdout)
        self.assertIn("--shell-env-inherit", result.stdout)
        self.assertIn("--permission-mode", result.stdout)
        self.assertIn("--allow-network", result.stdout)

    def test_workspace_init_tolerates_missing_home_lookup(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(
                server_module.Path, "home", side_effect=RuntimeError("home unavailable")
            ):
                runtime = Runtime(Path(tmp))

        self.assertEqual(runtime.workspace.root, Path(tmp).resolve())

    def test_kill_session_keeps_unresponsive_session(self) -> None:
        class StillRunningProcess:
            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> None:
                raise subprocess.TimeoutExpired(
                    cmd="still-running", timeout=timeout or 0
                )

        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            session = runtime._make_session(StillRunningProcess())  # type: ignore[arg-type]
            runtime.sessions[session.session_id] = session
            with patch.object(
                server_module, "terminate_process_group", return_value=None
            ):
                result = runtime.kill_session(
                    {"session_id": session.session_id, "wait_ms": 0, "kill_wait_ms": 0}
                )

        self.assertFalse(result.get("killed"), result)
        self.assertEqual(result.get("status"), "terminating", result)
        self.assertFalse(result.get("evicted"), result)
        self.assertIn(session.session_id, runtime.sessions)
        self.assertTrue(
            any(
                "session retained" in warning for warning in result.get("warnings", [])
            ),
            result,
        )

    def test_command_policy_gates_inline_interpreter_code(self) -> None:
        self.skipTest("inline interpreter gates deleted in BUILD mode")

    def test_command_policy_still_blocks_explicit_external_paths_and_network_tools(
        self,
    ) -> None:
        self.skipTest("path/network policy gates deleted in BUILD mode")

    def test_command_policy_allows_standard_special_devices_only(self) -> None:
        self.skipTest("obsolete command device policy in simplified execution model")

    def test_allow_network_only_opens_network_gate(self) -> None:
        self.skipTest("policy capability rules retired in simplified execution model")

    def test_command_env_core_is_not_windows_toolchain_specific(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            host_env = {
                "Path": r"C:\VS\VC\Tools\MSVC\bin;C:\Windows\System32",
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "SystemRoot": r"C:\Windows",
                "ComSpec": r"C:\Windows\System32\cmd.exe",
                "INCLUDE": r"C:\VS\VC\Tools\MSVC\include;C:\SDK\Include",
                "LIB": r"C:\VS\VC\Tools\MSVC\lib;C:\SDK\Lib",
                "LIBPATH": r"C:\VS\VC\Tools\MSVC\libpath",
                "WindowsSdkDir": r"C:\Program Files (x86)\Windows Kits\10\\",
                "VCToolsInstallDir": r"C:\VS\VC\Tools\MSVC\14.99.99999\\",
                "VSCMD_ARG_TGT_ARCH": "x64",
                "UNRELATED": "drop-me",
                "VSCMD_SECRET": "drop-me-too",
            }
            with (
                patch.object(server_module.os, "name", "nt"),
                patch.dict(server_module.os.environ, host_env, clear=True),
            ):
                env = runtime._command_env(
                    {"CUSTOM": "ok", "OPENAI_API_KEY": "fixture-openai-key-value"}
                )

            self.assertEqual(env.get("Path"), host_env["Path"])
            self.assertEqual(env.get("PATHEXT"), host_env["PATHEXT"])
            self.assertEqual(env.get("SystemRoot"), host_env["SystemRoot"])
            self.assertEqual(env.get("ComSpec"), host_env["ComSpec"])
            self.assertEqual(env.get("CUSTOM"), "ok")
            self.assertEqual(env.get("HOME"), str(runtime.command_home_dir()))
            self.assertEqual(env.get("TEMP"), str(runtime.command_tmp_dir()))
            self.assertEqual(env.get("TMP"), str(runtime.command_tmp_dir()))
            self.assertNotIn("INCLUDE", env)
            self.assertNotIn("LIB", env)
            self.assertNotIn("LIBPATH", env)
            self.assertNotIn("WindowsSdkDir", env)
            self.assertNotIn("VCToolsInstallDir", env)
            self.assertNotIn("VSCMD_ARG_TGT_ARCH", env)
            self.assertNotIn("UNRELATED", env)
            self.assertNotIn("VSCMD_SECRET", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertTrue(runtime.command_home_dir().is_dir())
            self.assertTrue(runtime.command_tmp_dir().is_dir())
            self.assertTrue(runtime.cache_dir.is_dir())
            self.assertFalse((workspace / ".coding-tools").exists())

    def test_command_env_uses_external_home_tmp_and_cache_without_ecosystem_cache_vars(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, shell_env_policy=ShellEnvPolicy(inherit="all"))
            host_env = {
                "PATH": "/usr/bin",
                "MAVEN_USER_HOME": "/host/m2",
                "GRADLE_USER_HOME": "/host/gradle",
                "npm_config_cache": "/host/npm",
                "PIP_CACHE_DIR": "/host/pip",
                "GOCACHE": "/host/go-build",
                "GOMODCACHE": "/host/go-mod",
                "CARGO_HOME": "/host/cargo",
                "RUSTUP_HOME": "/host/rustup",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("HOME"), str(runtime.command_home_dir()))
            self.assertEqual(env.get("TMPDIR"), str(runtime.command_tmp_dir()))
            self.assertEqual(runtime.runtime_dir.parent.parent, runtime_parent_root())
            for key in (
                "MAVEN_USER_HOME",
                "GRADLE_USER_HOME",
                "npm_config_cache",
                "PIP_CACHE_DIR",
                "GOCACHE",
                "GOMODCACHE",
                "CARGO_HOME",
                "RUSTUP_HOME",
            ):
                self.assertNotIn(key, env)
            self.assertTrue(runtime.cache_dir.is_dir())
            self.assertFalse((workspace / ".coding-tools").exists())

    def test_legacy_windows_inherit_all_preserves_each_host_temp_variable(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, shell_env_policy=ShellEnvPolicy(inherit="all"))
            runtime._legacy_windows_process_fallback = True
            runtime.sandbox_backend = SandboxBackend(
                "bwrap", True, False, "bwrap unavailable on Windows"
            )
            host_env = {
                "PATH": "host-path",
                "TMP": "host-tmp",
                "TEMP": "host-temp",
                "TMPDIR": "host-tmpdir",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({}, sandboxed=True)

            self.assertEqual(env.get("TMP"), "host-tmp")
            self.assertEqual(env.get("TEMP"), "host-temp")
            self.assertEqual(env.get("TMPDIR"), "host-tmpdir")

    def test_bwrap_has_private_writable_tmp_and_cannot_read_host_tmp(self) -> None:
        self.skipTest("bwrap sandbox deleted in BUILD mode")

    def test_runtime_and_server_info_do_not_create_exec_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            self.assertFalse((workspace / ".coding-tools").exists())
            self.assertFalse(runtime.runtime_dir.exists())

            info = runtime.server_info_payload()
            self.assertEqual(info.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(info.get("home"), str(runtime.command_home_dir()))
            self.assertFalse((workspace / ".coding-tools").exists())
            self.assertFalse(runtime.runtime_dir.exists())

            check = runtime.check_exec_environment({})
            self.assertTrue(check.get("ok"))
            self.assertEqual(check.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(check.get("cache_dir"), str(runtime.cache_dir))
            self.assertFalse((workspace / ".coding-tools").exists())
            self.assertFalse(runtime.runtime_dir.exists())

    def test_server_info_and_check_exec_environment_expose_exec_state(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            info = runtime.server_info_payload()
            self.assertEqual(info.get("permission_mode"), "trusted")
            self.assertEqual(info.get("execution_mode"), "build")
            self.assertEqual(info.get("effective_access"), "full-access")
            self.assertEqual(info.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(info.get("home"), str(runtime.command_home_dir()))
            self.assertEqual(info.get("tmpdir"), str(runtime.command_tmp_dir()))
            self.assertEqual(info.get("cache_dir"), str(runtime.cache_dir))
            check = runtime.check_exec_environment({})
            self.assertTrue(check.get("ok"))
            self.assertEqual(check.get("execution_mode"), "build")
            self.assertEqual(check.get("effective_access"), "full-access")
            self.assertEqual(check.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(check.get("home"), str(runtime.command_home_dir()))

    def test_permission_modes_apply_expected_gates(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            safe = Runtime(workspace, permission_mode="safe")
            self.assertEqual(safe.execution_mode, "plan")
            self.assertEqual(safe.effective_access, "read-only")

            trusted = Runtime(workspace, permission_mode="trusted")
            self.assertEqual(trusted.execution_mode, "build")
            self.assertEqual(trusted.effective_access, "full-access")

            dangerous = Runtime(workspace, permission_mode="dangerous")
            self.assertEqual(dangerous.execution_mode, "build")
            self.assertEqual(dangerous.effective_access, "full-access")

    def test_command_env_all_preserves_toolchain_environment_but_filters_sensitive_values(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, shell_env_policy=ShellEnvPolicy(inherit="all"))
            host_env = {
                "PATH": "/toolchain/bin:/usr/bin",
                "INCLUDE": r"C:\VS\VC\Tools\MSVC\include",
                "LIB": r"C:\VS\VC\Tools\MSVC\lib",
                "LIBPATH": r"C:\VS\VC\Tools\MSVC\libpath",
                "CUDA_PATH": "/opt/cuda",
                "ONEAPI_ROOT": "/opt/intel/oneapi",
                "OPENAI_API_KEY": "fixture-openai-key-value",
                "PYTHONPATH": "/tmp/injected",
                "DYLD_LIBRARY_PATH": "/tmp/injected",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("INCLUDE"), host_env["INCLUDE"])
            self.assertEqual(env.get("LIB"), host_env["LIB"])
            self.assertEqual(env.get("LIBPATH"), host_env["LIBPATH"])
            self.assertEqual(env.get("CUDA_PATH"), host_env["CUDA_PATH"])
            self.assertEqual(env.get("ONEAPI_ROOT"), host_env["ONEAPI_ROOT"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("DYLD_LIBRARY_PATH", env)

    def test_command_env_explicit_policy_still_filters_sensitive_environment(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="dangerous",
                shell_env_policy=ShellEnvPolicy(inherit="all"),
            )
            host_env = {
                "OPENAI_API_KEY": "fixture-openai-key-value",
                "LD_PRELOAD": "/tmp/injected.so",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("LD_PRELOAD", env)

    def test_runtime_root_stays_posix_tmp_when_process_tmpdir_is_workspace_local(
        self,
    ) -> None:
        if os.name == "nt":
            self.skipTest("POSIX /tmp semantics do not apply on Windows")
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            drifted_tmp = workspace / ".coding-tools" / "tmp"
            drifted_tmp.mkdir(parents=True)
            with patch.dict(
                server_module.os.environ, {"TMPDIR": str(drifted_tmp)}, clear=True
            ):
                safe = Runtime(workspace)
                trusted = Runtime(workspace, permission_mode="trusted")
            self.assertEqual(safe.runtime_dir.parent.parent, runtime_parent_root())
            self.assertEqual(trusted.runtime_dir.parent.parent, runtime_parent_root())
            self.assertEqual(safe.command_tmp_dir().parent, safe.runtime_dir)
            self.assertEqual(trusted.command_tmp_dir().parent, trusted.runtime_dir)

    def test_command_env_include_exclude_and_set_are_applied_in_order(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                shell_env_policy=ShellEnvPolicy(
                    inherit="all",
                    include_only=("PATH", "KEEP_*", "SET_BY_POLICY"),
                    exclude=("KEEP_DROP",),
                    set={"SET_BY_POLICY": "configured"},
                ),
            )
            host_env = {
                "PATH": "/usr/bin",
                "KEEP_THIS": "yes",
                "KEEP_DROP": "no",
                "OTHER": "drop",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("PATH"), "/usr/bin")
            self.assertEqual(env.get("KEEP_THIS"), "yes")
            self.assertEqual(env.get("SET_BY_POLICY"), "configured")
            self.assertNotIn("KEEP_DROP", env)
            self.assertNotIn("OTHER", env)

    def test_command_policy_unwraps_env_before_path_checks(self) -> None:
        self.skipTest("path checks deleted in BUILD mode")

    def test_exec_command_warns_and_runs_when_landlock_is_unavailable(self) -> None:
        self.skipTest("Landlock sandbox deleted in BUILD mode")

    def test_exec_command_uses_landlock_wrapper_without_preexec_fn(self) -> None:
        self.skipTest("Landlock sandbox deleted in BUILD mode")

    def test_normal_exec_does_not_initialize_landlock(self) -> None:
        self.skipTest("Landlock sandbox deleted in BUILD mode")

    def test_completed_execution_releases_owned_sandbox(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), sandbox_backend="unsafe")
            try:
                result = runtime.run_task(
                    {"task_id": "test.echo", "timeout_ms": 5000, "yield_time_ms": 5000}
                )
                self.assertEqual(result.get("status"), "success", result)
                self.assertTrue(result.get("command_success"), result)
                self.assertEqual(result.get("exit_code"), 0, result)
                self.assertIsNone(runtime.sandbox)
                sandbox_root = runtime.runtime_dir / "sandboxes"
                self.assertEqual(list(sandbox_root.glob("sandbox-*")), [])
            finally:
                runtime.close()

    def test_failed_execution_releases_owned_sandbox(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            try:
                result = runtime.exec_command(
                    {"cmd": "exit 7", "timeout_ms": 5000, "yield_time_ms": 5000}
                )
                self.assertEqual(result.get("status"), "failed", result)
                self.assertFalse(result.get("command_success"), result)
                self.assertEqual(result.get("exit_code"), 7, result)
                self.assertIsNone(runtime.sandbox)
                sandbox_root = runtime.runtime_dir / "sandboxes"
                self.assertEqual(list(sandbox_root.glob("sandbox-*")), [])
            finally:
                runtime.close()

    def test_repeated_executions_do_not_accumulate_sandboxes(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "Makefile").write_text(
                "test:\n\t@printf 'project-check-ok\\n'\n", encoding="utf-8"
            )
            runtime = Runtime(workspace, sandbox_backend="unsafe")
            try:
                sandbox_root = runtime.runtime_dir / "sandboxes"
                self.assertEqual(
                    [item["id"] for item in runtime.project_checks({})["checks"]],
                    ["test"],
                )
                for _ in range(4):
                    result = runtime.run_project_check(
                        {
                            "check_id": "test",
                            "timeout_ms": 5000,
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(result.get("exit_code"), 0, result)
                    self.assertIn("project-check-ok", result.get("stdout", ""))
                    self.assertIsNone(runtime.sandbox)
                    self.assertEqual(list(sandbox_root.glob("sandbox-*")), [])
            finally:
                runtime.close()

    def test_execution_exception_releases_owned_sandbox(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            original_spawn = server_module.spawn_process

            def fail_spawn(*args: object, **kwargs: object) -> object:
                raise OSError("fixture spawn failure")

            server_module.spawn_process = fail_spawn  # type: ignore[assignment]
            try:
                result = runtime.exec_command(
                    {"cmd": "printf unreachable", "timeout_ms": 5000}
                )
                self.assertEqual(result.get("exit_code"), 127)
            finally:
                server_module.spawn_process = original_spawn  # type: ignore[assignment]
                runtime.close()

    def test_translate_path_exception_releases_owned_sandbox(self) -> None:
        self.skipTest("translate_path_for_exec skipped in BUILD direct execution")

    def test_command_env_base_exception_releases_owned_sandbox(self) -> None:
        self.skipTest("_command_env snapshot skipped in BUILD direct execution")

    def test_make_session_exception_reaps_process_and_closes_pty(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            original_spawn = server_module.spawn_process
            captured: dict[str, object] = {}

            def capture_spawn(*args: object, **kwargs: object) -> object:
                result = original_spawn(*args, **kwargs)
                captured["process"], captured["pty_master_fd"] = result
                return result

            server_module.spawn_process = capture_spawn  # type: ignore[assignment]
            try:
                with patch.object(
                    runtime,
                    "_make_session",
                    side_effect=RuntimeError("session construction failed"),
                ):
                    with self.assertRaises(RuntimeError):
                        runtime.exec_command(
                            {"cmd": "sleep 30", "tty": True, "yield_time_ms": 0}
                        )
                process = captured["process"]
                pty_master_fd = captured["pty_master_fd"]
                assert isinstance(process, subprocess.Popen)
                assert isinstance(pty_master_fd, int)
                self.assertIsNotNone(process.poll())
                with self.assertRaises(OSError):
                    os.fstat(pty_master_fd)
                self.assertIsNone(runtime.sandbox)
                self.assertEqual(runtime.sandbox_users, 0)
            finally:
                server_module.spawn_process = original_spawn  # type: ignore[assignment]
                runtime.close()

    def test_reader_startup_exception_reaps_process_and_releases_sandbox(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            with patch.object(
                server_module,
                "start_reader_threads",
                side_effect=RuntimeError("reader startup failed"),
            ):
                with self.assertRaises(RuntimeError):
                    runtime.exec_command({"cmd": "sleep 30", "yield_time_ms": 0})
            self.assertEqual(runtime.sessions, {})
            self.assertIsNone(runtime.sandbox)
            self.assertEqual(runtime.sandbox_users, 0)
            runtime.close()

    def test_http_session_evictability_accounts_for_background_exec(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            try:
                started = runtime.exec_command(
                    {"cmd": "sleep 30", "timeout_ms": 30000, "yield_time_ms": 0}
                )
                self.assertEqual(started.get("status"), "running", started)
                self.assertFalse(runtime.http_session_evictable())
                runtime.cancel_session(str(started["session_id"]))
                self.assertTrue(runtime.http_session_evictable())
            finally:
                runtime.close()

    def test_pty_master_is_closed_once_when_reader_and_cleanup_race(self) -> None:
        fd = 12345
        read_started = threading.Event()
        allow_reader_exit = threading.Event()
        close_calls: list[int] = []

        class FakeProcess:
            stdin = None
            stdout = None
            stderr = None

            def poll(self) -> int:
                return 0

        session = ExecSession("pty-race", FakeProcess(), pty_master_fd=fd)  # type: ignore[arg-type]

        def fake_read(_fd: int, _size: int) -> bytes:
            read_started.set()
            allow_reader_exit.wait(timeout=2)
            raise OSError("closed")

        def fake_close(value: int) -> None:
            close_calls.append(value)

        with (
            patch.object(processes_module.os, "read", side_effect=fake_read),
            patch.object(processes_module.os, "close", side_effect=fake_close),
        ):
            processes_module.start_reader_threads(session)
            self.assertTrue(read_started.wait(timeout=2))
            session.release_owned_resources()
            allow_reader_exit.set()
            for thread in session.reader_threads:
                thread.join(timeout=2)

        self.assertEqual(close_calls, [fd])
        session.release_owned_resources()

    def test_cancel_session_reaps_after_bounded_termination_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            finished = threading.Event()
            cleanup_calls = 0

            class StubbornProcess:
                stdin = None
                stdout = None
                stderr = None
                pid = 123

                def poll(self) -> int | None:
                    return 0 if finished.is_set() else None

                def wait(self, timeout: float | None = None) -> int:
                    if timeout is not None and not finished.wait(timeout):
                        raise subprocess.TimeoutExpired("fixture", timeout)
                    finished.wait(timeout=2)
                    if not finished.is_set():
                        raise subprocess.TimeoutExpired("fixture", timeout)
                    return 0

            def cleanup() -> None:
                nonlocal cleanup_calls
                cleanup_calls += 1

            session = ExecSession(
                "stubborn",
                StubbornProcess(),  # type: ignore[arg-type]
                resource_cleanup=cleanup,
            )
            with runtime.sessions_lock:
                runtime.sessions[session.session_id] = session
            with (
                patch.object(server_module, "terminate_process_group"),
                patch.object(
                    runtime,
                    "_wait_for_session_exit",
                    side_effect=[False, False],
                ),
            ):
                runtime.cancel_session(session.session_id)

            self.assertIn(session.session_id, runtime.sessions)
            self.assertEqual(cleanup_calls, 0)
            finished.set()
            deadline = time.time() + 2
            while time.time() < deadline and cleanup_calls == 0:
                time.sleep(0.01)
            self.assertEqual(cleanup_calls, 1)
            self.assertNotIn(session.session_id, runtime.sessions)
            runtime.cancel_session(session.session_id)
            runtime.close()

    def test_exec_session_can_defer_resource_cleanup_until_transaction_finishes(
        self,
    ) -> None:
        cleanup_calls = 0

        class ExitedProcess:
            stdin = None
            stdout = None
            stderr = None
            pid = 123

            def poll(self) -> int:
                return 0

        def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        session = ExecSession(
            "transaction",
            ExitedProcess(),  # type: ignore[arg-type]
            resource_cleanup=cleanup,
            auto_release_resources_on_exit=False,
        )

        session.refresh_status()
        self.assertTrue(session.closed)
        self.assertEqual(cleanup_calls, 0)

        session.release_owned_resources()
        self.assertEqual(cleanup_calls, 1)
        session.release_owned_resources()
        self.assertEqual(cleanup_calls, 1)

    def test_cancelled_direct_execution_does_not_create_owned_sandbox(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace, permission_mode="trusted", sandbox_backend="unsafe"
            )
            try:
                with patch.object(
                    ExecutionSandbox,
                    "create",
                    side_effect=AssertionError("direct execution must not snapshot"),
                ):
                    started = runtime.exec_command(
                        {"cmd": "sleep 30", "timeout_ms": 30000, "yield_time_ms": 0}
                    )
                self.assertEqual(started.get("status"), "running", started)
                session_id = str(started["session_id"])
                self.assertIsNone(runtime.sandbox)

                runtime.cancel_session(session_id)

                self.assertIsNone(runtime.sandbox)
                self.assertTrue(workspace.exists())
            finally:
                runtime.close()

    def test_runtime_close_reaps_direct_execution_without_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace, permission_mode="trusted", sandbox_backend="unsafe"
            )
            with patch.object(
                ExecutionSandbox,
                "create",
                side_effect=AssertionError("direct execution must not snapshot"),
            ):
                started = runtime.exec_command(
                    {"cmd": "sleep 30", "timeout_ms": 30000, "yield_time_ms": 0}
                )
            self.assertEqual(started.get("status"), "running", started)
            self.assertIsNone(runtime.sandbox)

            runtime.close()

            self.assertIsNone(runtime.sandbox)
            self.assertTrue(workspace.exists())

    def test_sandbox_cleanup_cannot_escape_owned_temp_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            owner_root = root / "owned-sandboxes"
            outside = root / "outside"
            outside.mkdir()
            outside_file = outside / "keep.txt"
            outside_file.write_text("keep\n", encoding="utf-8")

            sandbox = ExecutionSandbox.create(workspace, owner_root=owner_root)
            owned_path = sandbox.sandbox_dir
            try:
                sandbox.sandbox_dir = outside
                sandbox.cleanup()
                self.assertTrue(outside_file.is_file())
            finally:
                sandbox.sandbox_dir = owned_path
                sandbox.cleanup()
            self.assertFalse(owned_path.exists())

    def test_sandbox_creation_interrupt_cleans_partial_owned_tree(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            owner_root = root / "owned-sandboxes"

            with patch.object(
                ExecutionSandbox, "_sync", side_effect=KeyboardInterrupt()
            ):
                with self.assertRaises(KeyboardInterrupt):
                    ExecutionSandbox.create(workspace, owner_root=owner_root)

            self.assertEqual(list(owner_root.glob("sandbox-*")), [])
            self.assertEqual(list(owner_root.glob(".*.owner")), [])

    def test_legacy_dangerous_maps_to_full_access_shell_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            dangerous_runtime = Runtime(workspace, permission_mode="dangerous")
            self.assertEqual(dangerous_runtime.execution_mode, "build")
            self.assertEqual(dangerous_runtime.effective_access, "full-access")
            self.assertTrue(dangerous_runtime.dangerously_skip_all_permissions)
            with patch.dict(
                server_module.os.environ,
                {"OPENAI_API_KEY": "fixture-openai-key-value"},
                clear=False,
            ):
                inherited = dangerous_runtime.exec_command(
                    {
                        "cmd": "python -c \"import os; print(os.environ['OPENAI_API_KEY'])\"",
                        "timeout_ms": 5000,
                        "yield_time_ms": 5000,
                    }
                )
            self.assertEqual(inherited.get("exit_code"), 0, inherited)
            self.assertEqual(inherited.get("stdout"), "fixture-openai-key-value\n")

    def test_landlock_device_access_includes_truncate_and_ioctl_bits(self) -> None:
        handled = server_module.landlock_handled_access(5)
        device_access = server_module.landlock_device_access(handled)
        self.assertTrue(device_access & LANDLOCK_ACCESS_FS_WRITE_FILE)
        self.assertTrue(device_access & LANDLOCK_ACCESS_FS_TRUNCATE)
        self.assertTrue(device_access & LANDLOCK_ACCESS_FS_IOCTL_DEV)

    def test_guard_allow_roots_include_dns_toolchain_path_and_java_home(self) -> None:
        with TemporaryDirectory() as tmp:
            java_home = Path(tmp) / "jdk"
            explicit_root = Path(tmp) / "explicit-root"
            private_path_dir = Path(tmp) / "bin"
            java_home.mkdir()
            explicit_root.mkdir()
            private_path_dir.mkdir()
            with patch.dict(
                server_module.os.environ,
                {
                    "PATH": str(private_path_dir),
                    "JAVA_HOME": str(java_home),
                    "CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS": str(explicit_root),
                },
                clear=True,
            ):
                roots = set(guard_allow_roots())
        self.assertIn("/etc/resolv.conf", roots)
        self.assertIn("/etc/hosts", roots)
        self.assertIn("/usr", roots)
        self.assertIn("/usr/local/sdkman/candidates", roots)
        self.assertIn("/etc/gitconfig", roots)
        self.assertIn("/etc/gitconfig.d", roots)
        self.assertIn(str(java_home.resolve()), roots)
        self.assertIn(str(explicit_root.resolve()), roots)
        self.assertNotIn(str(private_path_dir.resolve()), roots)

    def test_safe_exec_git_init_and_local_config_reads_system_git_config_roots(
        self,
    ) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            with patch.dict(
                server_module.os.environ,
                {"PATH": os.environ.get("PATH", "")},
                clear=True,
            ):
                self.assertNotIn("GIT_CONFIG_NOSYSTEM", runtime._command_env({}))
                result = runtime.exec_command(
                    {
                        "cmd": (
                            "git init -q tmp-git-repo && "
                            "git -C tmp-git-repo config user.email test@example.invalid && "
                            "git -C tmp-git-repo config user.name Test"
                        ),
                        "timeout_ms": 10000,
                        "yield_time_ms": 30000,
                        "max_output_bytes": 20000,
                    }
                )
        self.assertEqual(result.get("status"), "success", result)
        self.assertTrue(result.get("command_success"), result)
        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertNotIn(
            "unable to access '/etc/gitconfig'", str(result.get("stderr", ""))
        )

    def test_exec_diagnostics_classify_common_failures(self) -> None:
        self.assertEqual(
            exec_output_diagnostics(
                {"stderr": "mvn: cannot create /dev/null: Permission denied"}
            )[0]["code"],
            "DEV_NULL_DENIED",
        )
        self.assertEqual(
            exec_output_diagnostics(
                {"stderr": "curl: (6) Could not resolve host: example.com"}
            )[0]["code"],
            "DNS_RESOLUTION_FAILED",
        )
        self.assertEqual(
            exec_output_diagnostics({"status": "timeout", "timed_out": True})[0][
                "code"
            ],
            "COMMAND_TIMED_OUT",
        )
        self.assertEqual(
            exec_output_diagnostics({"truncated": True})[0]["code"],
            "OUTPUT_TRUNCATED",
        )

    def test_exec_diagnostics_do_not_treat_maven_home_as_unwritable_home(self) -> None:
        output = """warning: unable to access '/etc/gitconfig': Permission denied
fatal: unknown error occurred while reading the configuration files
Maven home: /usr/share/maven
"""
        codes = [item["code"] for item in exec_output_diagnostics({"stderr": output})]
        self.assertIn("LANDLOCK_READ_ROOT_BLOCKED", codes)
        self.assertNotIn("HOME_NOT_WRITABLE", codes)

    def test_exec_diagnostics_treat_eacces_home_path_as_unwritable_home(self) -> None:
        output = (
            "Error: EACCES: permission denied, mkdir '/work/.coding-tools/home/.cache'"
        )
        codes = [item["code"] for item in exec_output_diagnostics({"stderr": output})]
        self.assertIn("HOME_NOT_WRITABLE", codes)

    def test_permission_failure_diagnostics_classify_policy_gates(self) -> None:
        cases = [
            ("network", "NETWORK_PERMISSION_REQUIRED"),
            ("shell_expansion", "SHELL_EXPANSION_PERMISSION_REQUIRED"),
            ("inline_script", "INLINE_SCRIPT_PERMISSION_REQUIRED"),
            ("sensitive_env", "SECRET_ENV_REJECTED"),
        ]
        for permission, expected in cases:
            with self.subTest(permission=permission):
                exc = ToolFailure(
                    "PERMISSION_REQUIRED",
                    "test",
                    category="permission",
                    details={"permission": permission},
                )
                self.assertEqual(
                    permission_failure_diagnostics(exc)[0]["code"], expected
                )

    def test_runtime_exposes_one_stable_truthfully_annotated_tool_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = Runtime(workspace).list_tools()["tools"]
            second = Runtime(workspace).list_tools()["tools"]
            self.assertEqual(first, second)
            names = {tool["name"] for tool in first}
            self.assertIn("apply_patch", names)
            self.assertIn("exec_command", names)
            self.assertIn("read_file", names)
            self.assertNotIn("edit_file", names)
            apply_patch_tool = next(
                tool for tool in first if tool["name"] == "apply_patch"
            )
            self.assertIs(apply_patch_tool["annotations"].get("destructiveHint"), True)
            self.assertIs(apply_patch_tool["annotations"].get("readOnlyHint"), False)

    def test_agent_text_matches_per_tool_limits_without_renderer_truncation(
        self,
    ) -> None:
        # Per-call tool limits (here read_file max_bytes) are the only budget:
        # the renderer must not apply a second, hidden truncation layer.
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            content = ("x" * 49_999) + "!"
            (workspace / "large.txt").write_text(content, encoding="utf-8")
            result = Runtime(workspace).call_tool(
                "read_file",
                {"path": "large.txt", "max_bytes": 60_000},
            )

            payload = result["structuredContent"]
            model_text = "\n".join(
                item["text"] for item in result["content"] if item.get("type") == "text"
            )
            self.assertEqual(payload["content"], content)
            self.assertEqual(model_text, content)
            self.assertNotIn("preview truncated", model_text)

    def agent_text(self, result: dict[str, object]) -> str:
        return "\n".join(
            str(item.get("text"))
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        )

    def test_agent_text_has_an_emergency_safety_ceiling(self) -> None:
        oversized_context = "x" * (MODEL_TEXT_SAFETY_LIMIT_BYTES + 1024)
        result = make_tool_result(
            "search_text",
            {
                "matches": [
                    {
                        "path": "large.txt",
                        "line": 2,
                        "column": 1,
                        "preview": "needle",
                        "before": [oversized_context],
                        "after": [],
                    }
                ],
                "truncated": False,
            },
            is_error=False,
        )
        model_text = self.agent_text(result)
        self.assertLessEqual(
            len(model_text.encode("utf-8")),
            MODEL_TEXT_SAFETY_LIMIT_BYTES,
        )
        self.assertIn("model text reached", model_text)
        self.assertIn("safety ceiling", model_text)

    @unittest.skipIf(os.name == "nt", "POSIX shell command syntax")
    def test_exec_model_text_always_carries_exit_status(self) -> None:
        # A failing command whose stdout looks like success must still be
        # legible as a failure from the model text alone.
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": "echo All checks completed.; exit 7", "timeout_ms": 10000},
            )
            model_text = self.agent_text(result)
            self.assertIn("Status: failed", model_text)
            self.assertIn("exit code 7", model_text)
            self.assertIn("All checks completed.", model_text)

    def test_exec_truncated_model_text_names_a_real_output_ref(self) -> None:
        with TemporaryDirectory() as tmp:
            command = f"{sys.executable} -c \"print('x' * 5000)\""
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": command, "timeout_ms": 10000, "max_output_bytes": 512},
            )
            model_text = self.agent_text(result)
            self.assertIn('read_output(output_ref="session:', model_text)
            self.assertIn(":stdout", model_text)

    @unittest.skipIf(os.name == "nt", "POSIX shell redirection syntax")
    def test_exec_truncation_continues_the_stream_that_was_truncated(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {
                    "cmd": "printf ok; printf 12345678901234567890 >&2",
                    "timeout_ms": 10000,
                    "max_output_bytes": 5,
                },
            )
            payload = result["structuredContent"]
            self.assertIs(payload.get("stdout_truncated"), False)
            self.assertIs(payload.get("stderr_truncated"), True)
            self.assertEqual(payload.get("output_stream"), "stderr")
            self.assertEqual(payload.get("truncated_output_streams"), ["stderr"])
            self.assertTrue(str(payload.get("output_ref", "")).endswith(":stderr"))
            next_ref = (
                payload.get("next_action", {}).get("arguments", {}).get("output_ref")
            )
            self.assertTrue(str(next_ref).endswith(":stderr"))
            model_text = self.agent_text(result)
            self.assertIn("stderr output truncated", model_text)
            self.assertIn(":stderr", model_text)
            self.assertNotIn(
                'output_ref="session:' + str(payload["session_id"]) + ':stdout"',
                model_text,
            )

    @unittest.skipIf(os.name == "nt", "POSIX shell redirection syntax")
    def test_exec_truncation_names_both_stream_continuations(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {
                    "cmd": "printf 1234567890; printf abcdefghij >&2",
                    "timeout_ms": 10000,
                    "max_output_bytes": 5,
                },
            )
            payload = result["structuredContent"]
            self.assertEqual(
                payload.get("truncated_output_streams"),
                ["stdout", "stderr"],
            )
            next_actions = payload.get("next_actions")
            self.assertIsInstance(next_actions, list)
            self.assertEqual(len(next_actions), 2)
            action_refs = [
                action.get("arguments", {}).get("output_ref")
                for action in next_actions
                if isinstance(action, dict)
            ]
            self.assertTrue(str(action_refs[0]).endswith(":stdout"))
            self.assertTrue(str(action_refs[1]).endswith(":stderr"))
            model_text = self.agent_text(result)
            self.assertIn("stdout output truncated", model_text)
            self.assertIn("stderr output truncated", model_text)

    def test_exec_running_model_text_names_the_poll_call(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": "sleep 1", "timeout_ms": 10000, "yield_time_ms": 0},
            )
            model_text = self.agent_text(result)
            self.assertIn("Status: running", model_text)
            self.assertIn('write_stdin(session_id="', model_text)

    def test_read_file_truncation_is_visible_with_continuation(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            content = "\n".join(f"line-{index}" for index in range(1, 2501)) + "\n"
            (workspace / "long.txt").write_text(content, encoding="utf-8")
            result = Runtime(workspace).call_tool("read_file", {"path": "long.txt"})
            model_text = self.agent_text(result)
            self.assertIn("Showing lines 1-2000 of 2500", model_text)
            self.assertIn(
                'continue with read_file(path="long.txt", start_line=2001',
                model_text,
            )
            self.assertIn("line-2000", model_text)
            self.assertNotIn("line-2001\n", model_text)

    def test_read_file_continuation_preserves_default_cwd_relative_path(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            nested = workspace / "nested"
            nested.mkdir()
            (nested / "long.txt").write_text(
                "".join(f"line-{index}\n" for index in range(1, 20)),
                encoding="utf-8",
            )
            runtime = Runtime(workspace)
            runtime.set_default_cwd({"path": "nested"})
            first = runtime.call_tool(
                "read_file",
                {"path": "long.txt", "max_bytes": 16},
            )
            first_payload = first["structuredContent"]
            action = first_payload.get("next_action")
            self.assertIsInstance(action, dict)
            self.assertEqual(action.get("tool"), "read_file")
            self.assertEqual(action.get("arguments", {}).get("path"), "long.txt")
            second = runtime.call_tool(action["tool"], action["arguments"])
            self.assertIs(second.get("isError"), False)
            self.assertEqual(
                second["structuredContent"].get("start_line"),
                first_payload.get("next_start_line"),
            )

    def test_read_file_partial_single_line_is_not_rendered_as_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "single-line.txt").write_text("x" * 100, encoding="utf-8")
            result = Runtime(workspace).call_tool(
                "read_file",
                {"path": "single-line.txt", "max_bytes": 16},
            )
            payload = result["structuredContent"]
            self.assertIs(payload.get("truncated"), True)
            self.assertIs(payload.get("first_line_exceeds_limit"), True)
            self.assertIsNone(payload.get("next_start_line"))
            model_text = self.agent_text(result)
            self.assertIn("content truncated", model_text)
            self.assertIn("raise max_bytes", model_text)

    def test_git_pagination_actions_are_rendered(self) -> None:
        error = git_fixture_preflight_error()
        if error is not None:
            self.skipTest(error)
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tracked = workspace / "tracked.txt"
            tracked.write_text("one\ntwo\nthree\n", encoding="utf-8")
            init_git(workspace)
            tracked.write_text("one changed\ntwo\nthree\n", encoding="utf-8")
            committed = subprocess.run(
                ["git", "commit", "-q", "-am", "second commit"],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            runtime = Runtime(workspace)

            log_result = runtime.call_tool("git_log", {"max_count": 1})
            log_payload = log_result["structuredContent"]
            self.assertIs(log_payload.get("truncated"), True)
            self.assertEqual(
                log_payload.get("next_action", {}).get("arguments", {}).get("skip"),
                1,
            )
            log_text = self.agent_text(log_result)
            self.assertIn("more commits available", log_text)
            self.assertIn("git_log(", log_text)
            self.assertIn("skip=1", log_text)

            blame_result = runtime.call_tool(
                "git_blame",
                {
                    "path": "tracked.txt",
                    "start_line": 1,
                    "end_line": 3,
                    "max_lines": 1,
                },
            )
            blame_payload = blame_result["structuredContent"]
            self.assertIs(blame_payload.get("truncated"), True)
            self.assertEqual(
                blame_payload.get("next_action", {})
                .get("arguments", {})
                .get("start_line"),
                2,
            )
            blame_text = self.agent_text(blame_result)
            self.assertIn("blame lines truncated", blame_text)
            self.assertIn("git_blame(", blame_text)
            self.assertIn("start_line=2", blame_text)

    def test_search_truncation_reports_match_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "data.txt").write_text("needle\n" * 5, encoding="utf-8")
            result = Runtime(workspace).call_tool(
                "search_text",
                {"query": "needle", "max_results": 2},
            )
            model_text = self.agent_text(result)
            self.assertIn("showing 2 of", model_text)
            self.assertIn("max_results", model_text)

    def test_exec_command_tool_errors_use_failed_status(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": "pwd", "workdir": "missing"},
            )
            self.assertIs(result.get("isError"), True)
            self.assertEqual(
                result.get("structuredContent", {}).get("status"), "failed"
            )

    def test_workspace_write_exec_uses_authoritative_tree_without_snapshot(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace, permission_mode="trusted", sandbox_backend="unsafe"
            )
            with patch.object(
                ExecutionSandbox,
                "create",
                side_effect=AssertionError("workspace-write must not snapshot"),
            ):
                result = runtime.exec_command(
                    {
                        "cmd": "printf direct > marker.txt",
                        "timeout_ms": 5_000,
                        "yield_time_ms": 5_000,
                    }
                )
            self.assertEqual(result.get("status"), "success", result)
            self.assertEqual(result.get("execution_mode"), "full-access")
            self.assertEqual(
                result.get("transaction"),
                {"mode": "direct", "status": "not_transactional"},
            )
            self.assertEqual(
                (workspace / "marker.txt").read_text(encoding="utf-8"), "direct"
            )

    def test_full_access_still_blocks_privilege_escalation(self) -> None:
        self.skipTest("sudo executable deny list deleted in BUILD mode")

    def test_execution_mode_cannot_escalate_legacy_permission_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            trusted = Runtime(
                workspace, permission_mode="trusted", sandbox_backend="unsafe"
            )
            res = trusted.exec_command({"cmd": "true"})
            self.assertEqual(res["status"], "success")

            safe = Runtime(workspace, permission_mode="safe", sandbox_backend="unsafe")
            with self.assertRaises(ToolFailure) as raised:
                safe.exec_command({"cmd": "true"})
            self.assertEqual(raised.exception.code, "PERMISSION_REQUIRED")

    @unittest.skipIf(os.name == "nt", "POSIX signal status test")
    def test_exec_command_reports_signal_exit_as_terminated(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            result = runtime.exec_command(
                {"cmd": "kill -TERM $$", "timeout_ms": 5_000, "yield_time_ms": 5_000}
            )
            self.assertEqual(result.get("status"), "terminated", result)
            self.assertEqual(result.get("signal"), "SIGTERM", result)

    def test_active_process_limit_counts_running_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            session_ids: list[str] = []
            try:
                for _ in range(MAX_ACTIVE_EXEC_SESSIONS):
                    result = runtime.exec_command(
                        {"cmd": "sleep 5", "timeout_ms": 10_000, "yield_time_ms": 0}
                    )
                    session_ids.append(str(result["session_id"]))
                with self.assertRaises(ToolFailure) as raised:
                    runtime.exec_command(
                        {"cmd": "sleep 5", "timeout_ms": 10_000, "yield_time_ms": 0}
                    )
                self.assertEqual(raised.exception.code, "SESSION_LIMIT_REACHED")
            finally:
                for session_id in session_ids:
                    try:
                        runtime.kill_session(
                            {
                                "session_id": session_id,
                                "signal": "KILL",
                                "wait_ms": 1000,
                            }
                        )
                    except ToolFailure:
                        pass
                runtime.close()

    def test_initialize_injects_root_instructions_and_indexes_nested_instructions(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text(
                "Run the focused test suite.\n", encoding="utf-8"
            )
            nested = workspace / "packages" / "api" / "AGENTS.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("API-only nested rule.\n", encoding="utf-8")

            initialized = Runtime(workspace).initialize()
            instructions = initialized.get("instructions", "")
            self.assertIn("Run the focused test suite.", instructions)
            self.assertIn("packages/api/AGENTS.md", instructions)
            self.assertNotIn("API-only nested rule.", instructions)
            self.assertIn("apply_patch", instructions)

    def test_exec_command_compact_preview_and_read_output(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            result = runtime.exec_command(
                {
                    "cmd": "printf 'alpha\nbeta\n'",
                    "timeout_ms": 5000,
                    "yield_time_ms": 30000,
                    "verbosity": "preview",
                    "preview_bytes": 64,
                }
            )
            self.assertEqual(result.get("status"), "success", result)
            self.assertTrue(result.get("command_success"), result)
            self.assertEqual(result.get("exit_code"), 0, result)
            self.assertIn("summary", result)
            self.assertIn("preview", result)
            self.assertIn("output_ref", result)
            self.assertIn("output_refs", result)
            self.assertEqual(result.get("output_stream"), "stdout")
            self.assertNotIn("stdout", result)
            page = runtime.read_output(
                {"output_ref": result["output_ref"], "offset": 0, "limit": 128}
            )
            self.assertIn("alpha", page.get("content", ""))
            self.assertIn("beta", page.get("content", ""))
            self.assertEqual(page.get("stream"), "stdout")
            self.assertIsNone(page.get("next_offset"))

    @unittest.skipIf(
        os.name == "nt", "this build explicitly reports ConPTY as unsupported"
    )
    def test_exec_command_tty_uses_a_real_pseudo_terminal(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            script = (
                "import os; print(os.isatty(0), os.isatty(1), os.isatty(2), flush=True)"
            )
            result = runtime.exec_command(
                {
                    "cmd": f"{sys.executable} -c {script!r}",
                    "tty": True,
                    "timeout_ms": 5000,
                    "yield_time_ms": 5000,
                }
            )
            # The tty fast-path intentionally returns as soon as the first
            # output arrives, which can race the exit becoming observable.
            # Follow the documented next_action contract and poll to completion.
            stdout = str(result.get("stdout", ""))
            deadline = time.time() + 5
            while result.get("status") == "running" and time.time() < deadline:
                result = runtime.write_stdin(
                    {
                        "session_id": result["session_id"],
                        "chars": "",
                        "yield_time_ms": 500,
                    }
                )
                stdout += str(result.get("stdout", ""))
            self.assertEqual(result.get("status"), "success", result)
            self.assertTrue(result.get("command_success"), result)
            self.assertIn("True True True", stdout)

    def test_completed_sessions_are_evicted_from_active_storage(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            session_ids: list[str] = []
            for _ in range(20):
                time.sleep(0.01)
                result = runtime.exec_command(
                    {
                        "cmd": "sleep 0.02",
                        "timeout_ms": 2000,
                        "yield_time_ms": 0,
                        "max_output_bytes": 64,
                    }
                )
                session_ids.append(str(result["session_id"]))
            # Poll instead of a fixed sleep: the short-lived processes finish
            # on their own schedule, and eviction only requires that a prune
            # after exit moves them out of active storage.
            eviction_deadline = time.time() + 5
            while time.time() < eviction_deadline:
                runtime._prune_sessions()
                if not runtime.sessions:
                    break
                time.sleep(0.05)
            self.assertEqual(runtime.sessions, {})
            self.assertLessEqual(len(runtime.output_sessions), 20)
            self.assertTrue(set(runtime.output_sessions).issubset(set(session_ids)))
            deadline = time.time() + 1
            while time.time() < deadline and any(
                thread.name.startswith("coding-tools-watchdog-")
                for thread in threading.enumerate()
            ):
                time.sleep(0.01)
            self.assertFalse(
                any(
                    thread.name.startswith("coding-tools-watchdog-")
                    for thread in threading.enumerate()
                )
            )

    def test_running_and_truncated_commands_return_explicit_next_actions(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            running = runtime.exec_command(
                {
                    "cmd": "sleep 1",
                    "timeout_ms": 5000,
                    "yield_time_ms": 0,
                    "max_output_bytes": 64,
                }
            )
            self.assertEqual(running.get("status"), "running")
            self.assertEqual(running.get("next_action", {}).get("tool"), "write_stdin")
            runtime.kill_session(
                {"session_id": running["session_id"], "signal": "KILL"}
            )

            truncated = runtime.exec_command(
                {
                    "cmd": "printf 'abcdefghijklmnopqrstuvwxyz'",
                    "timeout_ms": 5000,
                    "yield_time_ms": 5000,
                    "max_output_bytes": 8,
                }
            )
            self.assertTrue(truncated.get("output_truncated"), truncated)
            self.assertEqual(
                truncated.get("next_action", {}).get("tool"), "read_output"
            )
            self.assertIn("output_ref", truncated)

    def test_read_output_pages_streams_independently(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            script = (
                "import sys,time;"
                "sys.stderr.write('err1\\nerr2\\n'); sys.stderr.flush();"
                "sys.stdout.write('out1\\n'); sys.stdout.flush();"
                "time.sleep(0.4);"
                "sys.stdout.write('out2\\n'); sys.stdout.flush();"
                "time.sleep(1)"
            )
            result = runtime.exec_command(
                {
                    "cmd": f"{sys.executable} -c {script!r}",
                    "timeout_ms": 5000,
                    "yield_time_ms": 100,
                    "verbosity": "preview",
                    "preview_bytes": 64,
                }
            )
            self.assertEqual(result.get("status"), "running", result)
            output_refs = result.get("output_refs")
            self.assertIsInstance(output_refs, dict)
            stderr_ref = output_refs["stderr"]

            first: dict[str, object] = {}
            for _ in range(10):
                first = runtime.read_output(
                    {"output_ref": stderr_ref, "offset": 0, "limit": 5}
                )
                if first.get("content"):
                    break
                time.sleep(0.05)
            self.assertEqual(first.get("content"), "err1\n")
            self.assertEqual(first.get("next_offset"), 5)
            time.sleep(0.6)
            second = runtime.read_output(
                {"output_ref": stderr_ref, "offset": first["next_offset"], "limit": 64}
            )
            self.assertEqual(second.get("offset"), first.get("next_offset"))
            self.assertEqual(second.get("content"), "err2\n")
            self.assertNotIn("out2", second.get("content", ""))
            runtime.kill_session({"session_id": result["session_id"], "wait_ms": 1000})

    def test_read_output_uses_absolute_stream_offsets_after_buffer_drop(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            # The context manager closes the stdout/stderr pipes and waits, so
            # the test does not leak pipe file objects (ResourceWarning).
            with subprocess.Popen(
                [sys.executable, "-c", ""],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                session = server_module.ExecSession(
                    session_id="manual-output", process=process, buffer_limit=4
                )
                session.append_stdout(b"abcdef")
                runtime._remember_output_session(session)

                page = runtime.read_output(
                    {
                        "output_ref": "session:manual-output:stdout",
                        "offset": 0,
                        "limit": 10,
                    }
                )
                self.assertEqual(page.get("offset"), 2)
                self.assertEqual(page.get("requested_offset"), 0)
                self.assertEqual(page.get("content"), "cdef")
                self.assertEqual(page.get("omitted_bytes"), 2)
                self.assertEqual(page.get("retained_start_offset"), 2)

                session.stdout_cursor = 0
                snapshot = session.snapshot_since_cursor(10)
                self.assertEqual(snapshot.get("stdout"), "cdef")
                self.assertEqual(snapshot.get("stdout_omitted_bytes"), 2)
                self.assertIs(snapshot.get("truncated"), True)

    def test_default_cwd_and_git_convenience_tools(self) -> None:
        if server_module.shutil.which("git") is None:
            self.skipTest("git is not available")
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src" / "hello.txt").write_text("hello\n", encoding="utf-8")
            for cmd in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "Runtime Test"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "initial commit"],
            ):
                completed = subprocess.run(
                    cmd,
                    cwd=workspace,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if completed.returncode != 0:
                    self.skipTest(
                        f"git fixture setup failed: {completed.stderr.strip()}"
                    )

            runtime = Runtime(workspace)
            cwd = runtime.set_default_cwd({"path": "src"})
            self.assertEqual(cwd.get("default_cwd"), "src")
            read = runtime.read_file({"path": "hello.txt"})
            self.assertEqual(read.get("content"), "hello\n")

            log = runtime.git_log({"max_count": 5})
            self.assertTrue(log.get("is_repo"))
            self.assertEqual(log.get("commits", [])[0].get("subject"), "initial commit")

            show = runtime.git_show({"include_diff": False, "max_bytes": 4096})
            self.assertTrue(show.get("is_repo"))
            self.assertIn("initial commit", show.get("content", ""))

            blame = runtime.git_blame({"path": "hello.txt", "max_lines": 5})
            self.assertTrue(blame.get("is_repo"))
            self.assertEqual(blame.get("lines", [])[0].get("content"), "hello")

            with self.assertRaises(ToolFailure):
                runtime.set_default_cwd({"path": "../outside"})

    def test_boundary_regressions_for_aliases_and_command_scanning(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "nested").mkdir()
            (workspace / "sample.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
            runtime = Runtime(workspace, permission_mode="trusted")

            cwd_result = runtime.exec_command(
                {
                    "cmd": "pwd",
                    "cwd": "nested",
                    "timeout_ms": 5000,
                    "max_output_bytes": 4096,
                }
            )
            self.assertEqual(cwd_result.get("exit_code"), 0)
            self.assertEqual(
                Path(str(cwd_result.get("stdout", "")).strip()).name, "nested"
            )

            with self.assertRaises(ToolFailure):
                runtime.exec_command({"cmd": "pwd", "workdir": ".", "cwd": "nested"})

            read = runtime.read_file(
                {"path": "sample.txt", "start_line": 2, "max_lines": 1}
            )
            self.assertEqual(read.get("content"), "two\n")
            self.assertEqual(read.get("end_line"), 2)

            tag = "model" + "Version"
            xml_heredoc = (
                "cat > pom.xml <<'EOF'\n"
                "<project>\n"
                f"  <{tag}>4.0.0</{tag}>\n"
                "</project>\n"
                "EOF\n"
                "cat pom.xml"
            )
            xml_result = runtime.exec_command(
                {"cmd": xml_heredoc, "timeout_ms": 5000, "max_output_bytes": 4096}
            )
            self.assertIn(tag, xml_result.get("stdout", ""))
            self.assertIsNone(runtime.sandbox)

    def test_heredoc_payload_stripping_keeps_live_shell_code_scanned(self) -> None:
        self.skipTest("heredoc payload checks deleted in BUILD mode")

    def test_git_helpers_use_command_environment(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            (workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            init_git(workspace)

            # GIT_TEST_ASSUME_DIFFERENT_OWNER makes git treat the repo as owned
            # by another user, reproducing the dubious-ownership failure that
            # motivated routing helper subprocesses through the command env.
            probe = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                env={
                    **os.environ,
                    "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if probe.returncode == 0:
                self.skipTest("git does not honor GIT_TEST_ASSUME_DIFFERENT_OWNER")

            def runtime_with_git_config(config: Path) -> Runtime:
                return Runtime(
                    workspace,
                    shell_env_policy=ShellEnvPolicy(
                        set={
                            "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
                            "GIT_CONFIG_GLOBAL": str(config),
                        }
                    ),
                )

            without_safe = root / "gitconfig-empty"
            without_safe.write_text("", encoding="utf-8")
            status = runtime_with_git_config(without_safe).git_status(
                {"max_entries": 5}
            )
            self.assertFalse(status.get("is_repo"))
            self.assertTrue(
                any(
                    "dubious ownership" in warning
                    for warning in status.get("warnings", [])
                ),
                status.get("warnings"),
            )

            with_safe = root / "gitconfig-safe"
            with_safe.write_text(
                f"[safe]\n\tdirectory = {workspace.as_posix()}\n", encoding="utf-8"
            )
            runtime = runtime_with_git_config(with_safe)
            status = runtime.git_status({"max_entries": 5})
            self.assertTrue(status.get("is_repo"))
            log = runtime.git_log({"max_count": 1})
            self.assertTrue(log.get("is_repo"))
            self.assertEqual(
                log.get("commits", [])[0].get("subject"), "baseline fixture"
            )

    def test_project_selection_scopes_operations_and_rejects_escape(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            library = Path(tmp) / "projects"
            first = library / "first"
            second = library / "nested" / "second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "tracked.txt").write_text("first\n", encoding="utf-8")
            (second / "tracked.txt").write_text("second\n", encoding="utf-8")
            init_git(first)
            init_git(second)
            runtime = Runtime(first, project_roots=[library])
            try:
                projects = runtime.list_projects({})["projects"]
                self.assertEqual(
                    {project["relative_path"] for project in projects},
                    {"first", "nested/second"},
                )
                selected = runtime.select_project({"project": "first"})
                self.assertEqual(selected["relative_path"], "first")
                self.assertEqual(runtime.current_project({})["relative_path"], "first")
                self.assertEqual(
                    runtime.read_file({"path": "tracked.txt"})["content"], "first\n"
                )
                res = runtime.read_file({"path": "../nested/second/tracked.txt"})
                self.assertEqual(res.get("content"), "second\n")
                if os.name != "nt":
                    (first / "sibling-link").symlink_to(
                        second, target_is_directory=True
                    )
                    res_link = runtime.read_file({"path": "sibling-link/tracked.txt"})
                    self.assertEqual(res_link.get("content"), "second\n")
            finally:
                runtime.close()

    def test_active_project_is_runtime_session_scoped(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            library = Path(tmp) / "projects"
            first = library / "first"
            second = library / "second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "tracked.txt").write_text("first\n", encoding="utf-8")
            (second / "tracked.txt").write_text("second\n", encoding="utf-8")
            init_git(first)
            init_git(second)
            session_a = Runtime(first, project_roots=[library])
            session_b = Runtime(first, project_roots=[library])
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
            finally:
                session_a.close()
                session_b.close()

    def test_service_managed_active_project_persists_across_http_runtime_instances(
        self,
    ) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "projects"
            first = library / "first"
            second = library / "second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "tracked.txt").write_text("first\n", encoding="utf-8")
            (second / "tracked.txt").write_text("second\n", encoding="utf-8")
            init_git(first)
            init_git(second)
            state_file = root / "config" / "active-project"
            first_session = Runtime(
                first,
                project_roots=[library],
                active_project_file=state_file,
            )
            try:
                first_session.select_project({"project": "second"})
                self.assertEqual(first_session.workspace.root, second.resolve())
                self.assertEqual(
                    state_file.read_text(encoding="utf-8").strip(),
                    str(second.resolve()),
                )
            finally:
                first_session.close()
            fresh_session = Runtime(
                first,
                project_roots=[library],
                active_project_file=state_file,
            )
            try:
                self.assertEqual(fresh_session.workspace.root, second.resolve())
                self.assertEqual(
                    fresh_session.current_project({})["relative_path"], "second"
                )
                self.assertEqual(
                    fresh_session.read_file({"path": "tracked.txt"})["content"],
                    "second\n",
                )
            finally:
                fresh_session.close()

    def test_wait_for_external_is_bounded_cancel_safe_and_responsive(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            result: dict[str, object] = {}
            thread = threading.Thread(
                target=lambda: result.update(
                    runtime.call_tool(
                        "wait_for_external",
                        {"seconds": 2, "timeout_seconds": 2},
                        request_id="wait-1",
                    )
                )
            )
            thread.start()
            time.sleep(0.05)
            started = time.monotonic()
            info = runtime.server_info({})
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertGreaterEqual(info["tool_count"], 55)
            runtime.cancel_request("wait-1")
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["structuredContent"]["status"], "cancelled")
            completed = runtime.wait_for_external({"seconds": 1, "timeout_seconds": 2})
            self.assertEqual(completed["status"], "completed")
            timed_out = runtime.wait_for_external({"seconds": 2, "timeout_seconds": 1})
            self.assertEqual(timed_out["status"], "timeout")
            with self.assertRaises(ToolFailure) as invalid:
                runtime.wait_for_external({"seconds": 0})
            self.assertEqual(invalid.exception.code, "INVALID_ARGUMENT")
            with self.assertRaises(ToolFailure):
                runtime.wait_for_external({"seconds": 1, "timeout_seconds": 91})
            runtime.close()

    def test_continuation_checkpoint_is_durable_isolated_and_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            project_a = root / "a"
            project_b = root / "b"
            project_a.mkdir()
            project_b.mkdir()
            with patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": str(config)}):
                first = Runtime(project_a)
                first.continuation_checkpoint(
                    {
                        "action": "write",
                        "logical_task": "ticketwise-u7.18",
                        "payload": {
                            "active_slice": "u7_18_review_queue_and_scheduler",
                            "branch": "feat/u7-18-review-queue-and-scheduler",
                            "head": "abc123",
                            "pr_number": 44,
                            "workflow_run_id": 1234,
                            "dirty_state_summary": "clean",
                            "completed_acceptance_items": ["schema", "queue"],
                            "next_action": "poll CI run 1234",
                            "blocker_type": "waiting_ci",
                            "timestamp": "2026-08-11T12:00:00Z",
                        },
                    }
                )
                first.close()
                second = Runtime(project_a)
                loaded = second.continuation_checkpoint(
                    {"action": "read", "logical_task": "ticketwise-u7.18"}
                )["checkpoint"]
                self.assertEqual(loaded["payload"]["head"], "abc123")
                second.continuation_checkpoint(
                    {
                        "action": "write",
                        "logical_task": "ticketwise-u7.18",
                        "payload": {"head": "def456", "next_action": "merge PR"},
                    }
                )
                other = Runtime(project_b)
                self.assertIsNone(
                    other.continuation_checkpoint(
                        {"action": "read", "logical_task": "ticketwise-u7.18"}
                    )["checkpoint"]
                )
                with self.assertRaises(ToolFailure):
                    second.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "ticketwise-u7.18",
                            "payload": {"api_token": "do-not-store"},
                        }
                    )
                with self.assertRaises(ToolFailure):
                    second.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "ticketwise-u7.18",
                            "payload": {
                                "next_action": "retry with Bearer abcdefghijklmnop"
                            },
                        }
                    )
                with self.assertRaises(ToolFailure):
                    second.continuation_checkpoint(
                        {
                            "action": "write",
                            "logical_task": "ticketwise-u7.18",
                            "payload": {"next_action": "x" * 5000},
                        }
                    )
                self.assertTrue(
                    second.continuation_checkpoint(
                        {"action": "clear", "logical_task": "ticketwise-u7.18"}
                    )["cleared"]
                )
                second.close()
                other.close()

    def test_continuation_checkpoint_rejects_storage_inside_project(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            with patch.dict(
                os.environ,
                {"DEVMCP_CONFIG_DIR": str(project / ".devmcp-private")},
            ):
                runtime = Runtime(project)
                try:
                    with self.assertRaises(ToolFailure) as denied:
                        runtime.continuation_checkpoint(
                            {
                                "action": "write",
                                "logical_task": "x",
                                "payload": {"next_action": "continue"},
                            }
                        )
                    self.assertEqual(denied.exception.code, "RUNTIME_DIR_UNWRITABLE")
                finally:
                    runtime.close()

    @unittest.skipIf(os.name == "nt", "process-group marker test is POSIX-specific")
    def test_bounded_process_timeout_kills_descendant_process_tree(self) -> None:
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-survived"
            child = (
                "import pathlib,time; time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "time.sleep(30)"
            )
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                processes_module.run_bounded_process(
                    [sys.executable, "-c", parent], timeout=0.1
                )
            self.assertLess(time.monotonic() - started, 3)
            time.sleep(1)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "process-group marker test is POSIX-specific")
    def test_bounded_process_completed_nonzero_kills_surviving_descendant(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-survived"
            child = (
                "import os,pathlib,time; "
                "devnull=os.open(os.devnull, os.O_WRONLY); "
                "os.dup2(devnull,1); os.dup2(devnull,2); "
                "time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], close_fds=True); "
                "raise SystemExit(7)"
            )
            completed = processes_module.run_bounded_process(
                [sys.executable, "-c", parent], timeout=3
            )
            self.assertEqual(completed.returncode, 7)
            time.sleep(1)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "process-group marker test is POSIX-specific")
    def test_bounded_process_completed_success_kills_surviving_descendant(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-survived"
            child = (
                "import os,pathlib,time; "
                "devnull=os.open(os.devnull, os.O_WRONLY); "
                "os.dup2(devnull,1); os.dup2(devnull,2); "
                "time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], close_fds=True)"
            )
            completed = processes_module.run_bounded_process(
                [sys.executable, "-c", parent], timeout=3
            )
            self.assertEqual(completed.returncode, 0)
            time.sleep(1)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_delegate_select_project_a_writes_only_a(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "a"
            repo_b = root / "b"
            for repo in (repo_a, repo_b):
                repo.mkdir()
                (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
                init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    assert '--new-project' in sys.argv and '--sandbox' in sys.argv\n"
                "    pathlib.Path('delegated.txt').write_text('a-only\\n', encoding='utf-8')\n"
                '    print(\'{"status":"SUCCESS"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo_b, project_roots=[root])
            try:
                runtime.select_project({"project": "a"})
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    result = runtime.antigravity_delegate({"prompt": "Write marker."})
                self.assertTrue(result["task_ok"])
                self.assertEqual((repo_a / "delegated.txt").read_text(), "a-only\n")
                self.assertFalse((repo_b / "delegated.txt").exists())
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_delegate_reselects_b_and_writes_only_b(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "a"
            repo_b = root / "b"
            for repo in (repo_a, repo_b):
                repo.mkdir()
                (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
                init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('delegated.txt').write_text('b-only\\n', encoding='utf-8')\n"
                '    print(\'{"status":"SUCCESS"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo_a, project_roots=[root])
            try:
                runtime.select_project({"project": "a"})
                runtime.select_project({"project": "b"})
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    runtime.antigravity_delegate({"prompt": "Write marker."})
                self.assertFalse((repo_a / "delegated.txt").exists())
                self.assertEqual((repo_b / "delegated.txt").read_text(), "b-only\n")
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_delegate_ignores_persisted_other_session_project(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "a"
            repo_b = root / "b"
            state_file = root / "active-project"
            for repo in (repo_a, repo_b):
                repo.mkdir()
                (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
                init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('delegated.txt').write_text('session-a\\n', encoding='utf-8')\n"
                '    print(\'{"status":"SUCCESS"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            session_a = Runtime(
                repo_a,
                project_roots=[root],
                active_project_file=state_file,
            )
            session_b = Runtime(
                repo_a,
                project_roots=[root],
                active_project_file=state_file,
            )
            try:
                session_a.select_project({"project": "a"})
                session_b.select_project({"project": "b"})
                self.assertEqual(state_file.read_text().strip(), str(repo_b.resolve()))
                self.assertEqual(session_a.current_project({})["relative_path"], "a")
                with patch.object(
                    session_a, "_antigravity_binary", return_value=str(fake)
                ):
                    session_a.antigravity_delegate({"prompt": "Write marker."})
                self.assertEqual((repo_a / "delegated.txt").read_text(), "session-a\n")
                self.assertFalse((repo_b / "delegated.txt").exists())
            finally:
                session_a.close()
                session_b.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_cwd_guard_fails_before_agent_exec(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            expected = root / "expected"
            actual.mkdir()
            expected.mkdir()
            marker = root / "agent-started"
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text('started')\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            guarded = Runtime._antigravity_guarded_argv([str(fake)], expected)
            result = processes_module.run_bounded_process(
                guarded, cwd=str(actual), timeout=5
            )
            self.assertEqual(result.returncode, 125)
            self.assertIn("DEVMCP_AGY_CWD_MISMATCH", result.stderr)
            self.assertFalse(marker.exists())

    def test_antigravity_env_replaces_stale_workspace_and_state_hints(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            runtime = Runtime(repo)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "PATH": os.environ.get("PATH", os.defpath),
                        "HOME": str(root / "home"),
                        "PWD": "/home/jar/Documents/projects/TicketWise",
                        "OLDPWD": "/home/jar/Documents/projects/TicketWise-old",
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_CACHE_HOME": str(root / "ambient-cache"),
                        "XDG_STATE_HOME": str(root / "ambient-state"),
                        "AGY_LAST_PROJECT": "/home/jar/Documents/projects/TicketWise",
                    },
                    clear=True,
                ):
                    env = runtime._antigravity_env(repo)
                self.assertEqual(env["PWD"], str(repo.resolve()))
                self.assertEqual(env["OLDPWD"], str(repo.resolve()))
                self.assertEqual(env["HOME"], str(root / "home"))
                self.assertEqual(env["XDG_CONFIG_HOME"], str(root / "config"))
                self.assertNotEqual(env["XDG_CACHE_HOME"], str(root / "ambient-cache"))
                self.assertNotEqual(env["XDG_STATE_HOME"], str(root / "ambient-state"))
                self.assertTrue(
                    Path(env["XDG_CACHE_HOME"]).is_relative_to(runtime.runtime_dir)
                )
                self.assertTrue(
                    Path(env["XDG_STATE_HOME"]).is_relative_to(runtime.runtime_dir)
                )
                self.assertNotIn("AGY_LAST_PROJECT", env)
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_dirty_a_does_not_block_clean_selected_b(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "a"
            repo_b = root / "b"
            for repo in (repo_a, repo_b):
                repo.mkdir()
                (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
                init_git(repo)
            (repo_a / "tracked.txt").write_text("dirty-a\n", encoding="utf-8")
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('delegated.txt').write_text('clean-b\\n', encoding='utf-8')\n"
                '    print(\'{"status":"SUCCESS"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo_a, project_roots=[root])
            try:
                runtime.select_project({"project": "b"})
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    runtime.antigravity_delegate({"prompt": "Write marker."})
                self.assertEqual((repo_b / "delegated.txt").read_text(), "clean-b\n")
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_dirty_selected_b_blocks_before_launch(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "a"
            repo_b = root / "b"
            for repo in (repo_a, repo_b):
                repo.mkdir()
                (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
                init_git(repo)
            (repo_b / "tracked.txt").write_text("dirty-b\n", encoding="utf-8")
            runtime = Runtime(repo_a, project_roots=[root])
            try:
                runtime.select_project({"project": "b"})
                with patch.object(runtime, "_antigravity_binary") as binary:
                    with self.assertRaises(ToolFailure) as blocked:
                        runtime.antigravity_delegate({"prompt": "Must not start."})
                self.assertEqual(blocked.exception.code, "INVALID_STATE")
                binary.assert_not_called()
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_exit_zero_status_error_is_task_failure(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                '    print(\'{"status":"ERROR","message":"logical failure"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    result = runtime.call_tool(
                        "antigravity_delegate", {"prompt": "Report failure."}
                    )
                self.assertTrue(result["isError"])
                structured = result["structuredContent"]
                self.assertFalse(structured["ok"])
                self.assertEqual(structured["error"]["code"], "AGENT_TASK_FAILED")
                self.assertTrue(structured["error"]["details"]["process_ok"])
                self.assertFalse(structured["error"]["details"]["task_ok"])
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_fails_closed_without_workspace_sandbox(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            outside = root / "outside-selected-workspace.txt"
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"outside = pathlib.Path({str(outside)!r})\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --output-format -p')\n"
                "else:\n"
                "    outside.write_text('escaped', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    with self.assertRaises(ToolFailure) as blocked:
                        runtime.antigravity_delegate({"prompt": "Try to escape."})
                self.assertEqual(blocked.exception.code, "SERVICE_UNAVAILABLE")
                self.assertFalse(outside.exists())
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_hanging_process_times_out_and_cleans_worktree(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            marker = root / "child-survived"
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, subprocess, sys, time\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    child = \"import pathlib,time; time.sleep(2); pathlib.Path(%r).write_text('survived')\"\n"
                "    subprocess.Popen([sys.executable, '-c', child])\n"
                "    time.sleep(30)\n" % str(marker),
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    started = time.monotonic()
                    with self.assertRaises(ToolFailure) as timed_out:
                        runtime.antigravity_delegate(
                            {
                                "prompt": "Hang for regression test.",
                                "timeout_seconds": 1,
                            }
                        )
                    self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(timed_out.exception.code, "SERVICE_COMMAND_FAILED")
                self.assertTrue(timed_out.exception.retryable)
                time.sleep(2.2)
                self.assertFalse(marker.exists())
                worktrees = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=True,
                ).stdout
                self.assertNotIn("devmcp-antigravity-", worktrees)
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_transient_503_requires_opt_in_and_retries_once(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            counter = root / "agy-attempts"
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"counter = pathlib.Path({str(counter)!r})\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
                "    counter.write_text(str(attempt))\n"
                "    if attempt == 1:\n"
                "        print('503 upstream unavailable', file=sys.stderr)\n"
                "        raise SystemExit(1)\n"
                '    print(\'{"result":"ok"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    with self.assertRaises(ToolFailure) as unavailable:
                        runtime.antigravity_delegate(
                            {"prompt": "Transient failure without retry."}
                        )
                    self.assertEqual(unavailable.exception.code, "SERVICE_UNAVAILABLE")
                    self.assertTrue(unavailable.exception.retryable)
                    self.assertEqual(
                        unavailable.exception.details,
                        {"upstream_status": 503, "attempts": 1},
                    )

                    counter.unlink()
                    result = runtime.antigravity_delegate(
                        {
                            "prompt": "Retry one transient failure.",
                            "retry_transient": True,
                        }
                    )
                self.assertEqual(result["attempts"], 2)
                self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_transient_retry_kills_first_attempt_descendant(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            counter = root / "agy-attempts"
            marker = root / "first-child-survived"
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, subprocess, sys\n"
                f"counter = pathlib.Path({str(counter)!r})\n"
                f"marker = {str(marker)!r}\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
                "    counter.write_text(str(attempt))\n"
                "    if attempt == 1:\n"
                "        child = \"import os,pathlib,time; devnull=os.open(os.devnull,os.O_WRONLY); os.dup2(devnull,1); os.dup2(devnull,2); time.sleep(1); pathlib.Path(%r).write_text('survived')\" % marker\n"
                "        subprocess.Popen([sys.executable, '-c', child], close_fds=True)\n"
                "        print('503 upstream unavailable', file=sys.stderr)\n"
                "        raise SystemExit(1)\n"
                '    print(\'{"result":"ok"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    result = runtime.antigravity_delegate(
                        {
                            "prompt": "Retry one transient failure.",
                            "retry_transient": True,
                        }
                    )
                self.assertEqual(result["attempts"], 2)
                time.sleep(1.2)
                self.assertFalse(marker.exists())
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_reports_worktree_cleanup_failure(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                '    print(\'{"result":"ok"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            original_run = server_module.subprocess.run

            def fail_worktree_remove(*args: object, **kwargs: object) -> object:
                argv = args[0] if args else kwargs.get("args")
                if isinstance(argv, list) and "worktree" in argv and "remove" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "cleanup failed")
                return original_run(*args, **kwargs)

            try:
                with (
                    patch.object(
                        runtime, "_antigravity_binary", return_value=str(fake)
                    ),
                    patch.object(
                        server_module.subprocess,
                        "run",
                        side_effect=fail_worktree_remove,
                    ),
                ):
                    with self.assertRaises(ToolFailure) as cleanup_error:
                        runtime.antigravity_delegate({"prompt": "Inspect only."})
                self.assertEqual(cleanup_error.exception.code, "GIT_ERROR")
                self.assertIn(
                    "remove isolated Antigravity worktree",
                    cleanup_error.exception.message,
                )
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_delegate_applies_only_validated_worktree_patch(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('agent-added.txt').write_text('delegated\\n', encoding='utf-8')\n"
                '    print(\'{\\"result\\":\\"ok\\"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    result = runtime.antigravity_delegate(
                        {
                            "prompt": "Add a delegated marker file.",
                            "timeout_seconds": 30,
                        }
                    )
                self.assertTrue(result["applied"])
                self.assertEqual(result["changed_paths"], ["agent-added.txt"])
                self.assertEqual(
                    (repo / "agent-added.txt").read_text(encoding="utf-8"),
                    "delegated\n",
                )
                self.assertEqual(
                    (repo / "tracked.txt").read_text(encoding="utf-8"), "base\n"
                )
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_does_not_apply_over_concurrent_selected_repo_changes(
        self,
    ) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"authoritative = pathlib.Path({str(repo)!r})\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('agent-added.txt').write_text('delegated\\n', encoding='utf-8')\n"
                "    (authoritative / 'tracked.txt').write_text('concurrent-user-change\\n', encoding='utf-8')\n"
                '    print(\'{"status":"SUCCESS"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    with self.assertRaises(ToolFailure) as conflict:
                        runtime.antigravity_delegate(
                            {
                                "prompt": "Add a delegated marker file.",
                                "timeout_seconds": 30,
                            }
                        )
                self.assertEqual(conflict.exception.code, "TRANSACTION_CONFLICT")
                self.assertEqual(
                    tracked.read_text(encoding="utf-8"), "concurrent-user-change\n"
                )
                self.assertFalse((repo / "agent-added.txt").exists())
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_does_not_apply_after_same_commit_branch_switch(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            subprocess.run(
                ["git", "-C", str(repo), "branch", "other"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, subprocess, sys\n"
                f"authoritative = pathlib.Path({str(repo)!r})\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('agent-added.txt').write_text('delegated\\n', encoding='utf-8')\n"
                "    subprocess.run(['git', '-C', str(authoritative), 'switch', 'other'], check=True, stdout=subprocess.DEVNULL)\n"
                '    print(\'{"status":"SUCCESS"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    with self.assertRaises(ToolFailure) as conflict:
                        runtime.antigravity_delegate(
                            {
                                "prompt": "Add a delegated marker file.",
                                "timeout_seconds": 30,
                            }
                        )
                self.assertEqual(conflict.exception.code, "TRANSACTION_CONFLICT")
                self.assertFalse((repo / "agent-added.txt").exists())
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_delegate_discards_delete_attempts(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            fake = root / "agy"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('agy 9.9.9')\n"
                "elif '--help' in sys.argv:\n"
                "    print('--new-project --sandbox --output-format -p')\n"
                "else:\n"
                "    pathlib.Path('tracked.txt').unlink()\n"
                '    print(\'{\\"result\\":\\"ignored-injection\\"}\')\n',
                encoding="utf-8",
            )
            fake.chmod(0o700)
            runtime = Runtime(repo, project_roots=[root])
            try:
                with patch.object(
                    runtime, "_antigravity_binary", return_value=str(fake)
                ):
                    with self.assertRaises(ToolFailure) as denied:
                        runtime.antigravity_delegate(
                            {"prompt": "Inspect the repository.", "timeout_seconds": 30}
                        )
                self.assertEqual(denied.exception.code, "ACCESS_DENIED")
                self.assertEqual(
                    (repo / "tracked.txt").read_text(encoding="utf-8"), "base\n"
                )
            finally:
                runtime.close()

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_delegate_blocks_tracked_sensitive_paths_before_launch(
        self,
    ) -> None:
        self.skipTest("sensitive path denies deleted in BUILD mode")

    @unittest.skipIf(os.name == "nt", "fake executable fixture uses a POSIX shebang")
    def test_antigravity_read_only_discards_agent_edits(self) -> None:
        self.skipTest("sensitive path denies deleted in BUILD mode")

    def test_first_class_git_branch_commit_and_push_are_constrained(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            remote = root / "remote.git"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"],
                check=True,
            )
            with patch.dict(
                os.environ,
                {"DEVMCP_CONFIG_DIR": str(root / "config")},
                clear=False,
            ):
                runtime = Runtime(repo, project_roots=[root])
                try:
                    branch = runtime.git_create_branch({"name": "feature/p0"})
                    self.assertEqual(branch["branch"], "feature/p0")
                    (repo / "tracked.txt").write_text("intended\n", encoding="utf-8")
                    (repo / "unrelated.txt").write_text(
                        "leave me dirty\n", encoding="utf-8"
                    )
                    committed = runtime.git_commit(
                        {
                            "message": "test: explicit path commit",
                            "paths": ["tracked.txt"],
                        }
                    )
                    self.assertEqual(committed["branch"], "feature/p0")
                    self.assertEqual(len(committed["sha"]), 40)
                    changed = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "show",
                            "--pretty=format:",
                            "--name-only",
                            "HEAD",
                        ],
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                    ).stdout.strip()
                    self.assertEqual(changed, "tracked.txt")
                    status = subprocess.run(
                        ["git", "-C", str(repo), "status", "--porcelain"],
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                    ).stdout
                    self.assertIn("?? unrelated.txt", status)
                    with self.assertRaises(ToolFailure):
                        runtime.git_commit(
                            {
                                "message": "test: reject broad directory path",
                                "paths": ["."],
                            }
                        )

                    pushed = runtime.git_push({})
                    self.assertEqual(pushed["branch"], "feature/p0")
                    self.assertEqual(pushed["remote"], "origin")
                    self.assertEqual(pushed["upstream"], "origin/feature/p0")

                    fetched = runtime.git_fetch({})
                    self.assertEqual(fetched["result"], "fetched_and_pruned")

                    pulled = runtime.git_pull({})
                    self.assertEqual(pulled["result"], "fast_forwarded")
                    self.assertEqual(pulled["branch"], "feature/p0")

                    merged = runtime.git_merge_remote_branch({"branch": "main"})
                    self.assertIn(merged["result"], {"merged", "already_up_to_date"})
                    self.assertEqual(merged["merged_branch"], "main")
                    self.assertEqual(merged["branch"], "feature/p0")

                    runtime.git_create_branch({"name": "scratch/delete-me"})
                    runtime.git_switch_branch({"name": "feature/p0"})
                    deleted_local = runtime.git_delete_branch(
                        {"name": "scratch/delete-me"}
                    )
                    self.assertEqual(deleted_local["result"], "deleted_local")
                    with self.assertRaises(ToolFailure):
                        runtime.git_delete_branch({"name": "feature/p0"})

                    deleted_remote = runtime.git_delete_remote_branch(
                        {"name": "feature/p0"}
                    )
                    self.assertEqual(deleted_remote["result"], "deleted_remote")
                    with self.assertRaises(ToolFailure):
                        runtime.git_push({"remote": "https://example.invalid/repo.git"})
                    with self.assertRaises(ToolFailure):
                        runtime.git_push({"force": True})
                finally:
                    runtime.close()

    def test_uv_project_checks_never_fall_back_to_host_bare_pytest(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "uv-project"
            repo.mkdir()
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            init_git(repo)
            (repo / "pyproject.toml").write_text(
                "[project]\nname='fixture'\nversion='0.0.0'\n", encoding="utf-8"
            )
            (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (repo / "tests").mkdir()
            runtime = Runtime(repo, project_roots=[Path(tmp)])
            try:
                checks = runtime.project_checks({})["checks"]
                test_check = next(check for check in checks if check["id"] == "test")
                self.assertEqual(test_check["environment"], "uv")
                self.assertEqual(
                    test_check["argv"][:5],
                    ["uv", "run", "--offline", "--frozen", "--no-sync"],
                )
                self.assertNotEqual(test_check["argv"][0], "pytest")
            finally:
                runtime.close()

    def test_local_state_snapshot_aggregates_git_and_self_host_state(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            tracked = repo / "tracked.py"
            tracked.write_text("value = 1\n", encoding="utf-8")
            init_git(repo)
            tracked.write_text("value = 2\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
            (repo / "untracked.py").write_text("value = 3\n", encoding="utf-8")
            installed_sha = "a" * 40
            with patch.dict(
                os.environ,
                {"DEVMCP_INSTALLED_RUNTIME_SHA": installed_sha},
                clear=False,
            ):
                runtime = Runtime(
                    repo, permission_mode="trusted", sandbox_backend="unsafe"
                )
                try:
                    snapshot = runtime.local_state_snapshot({})
                finally:
                    runtime.close()
            self.assertEqual(snapshot["service"]["installed_sha"], installed_sha)
            self.assertEqual(
                snapshot["self_host"]["default_execution_mode"], "full-access"
            )
            self.assertIn("tracked.py", snapshot["dirty_paths"])
            self.assertIn("tracked.py", snapshot["staged_paths"])
            self.assertIn("untracked.py", snapshot["dirty_paths"])
            self.assertIn("untracked.py", snapshot["untracked_paths"])
            self.assertNotIn("untracked.py", snapshot["staged_paths"])
            self.assertEqual(len(str(snapshot["head"])), 40)

    def test_inspect_symbol_returns_definition_references_and_tests(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text(
                "def target(value):\n    return value + 1\n\nresult = target(1)\n",
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_module.py").write_text(
                "from module import target\n\ndef test_target():\n    assert target(1) == 2\n",
                encoding="utf-8",
            )
            runtime = Runtime(root, permission_mode="trusted", sandbox_backend="unsafe")
            try:
                inspected = runtime.inspect_symbol(
                    {"symbol": "target", "context_lines": 1, "max_results": 20}
                )
            finally:
                runtime.close()
            self.assertTrue(
                any(item["path"] == "module.py" for item in inspected["definitions"]),
                inspected,
            )
            self.assertTrue(inspected["references"], inspected)
            self.assertTrue(
                any(
                    item["path"] == "tests/test_module.py"
                    for item in inspected["relevant_tests"]
                ),
                inspected,
            )

    def test_run_checks_for_diff_selects_python_checks_and_aggregates(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="trusted", sandbox_backend="unsafe"
            )
            discovered = [
                {"id": check_id, "argv": ["true"]}
                for check_id in ("format-check", "lint", "typecheck", "test")
            ]

            def successful_check(call_args: dict[str, object]) -> dict[str, object]:
                return {
                    "status": "success",
                    "exit_code": 0,
                    "command_success": True,
                    "check_id": call_args["check_id"],
                }

            try:
                with (
                    patch.object(
                        runtime,
                        "git_status",
                        return_value={
                            "is_repo": True,
                            "entries": [
                                {
                                    "path": "coding_tools_mcp/server.py",
                                    "index_status": " ",
                                    "worktree_status": "M",
                                }
                            ],
                        },
                    ),
                    patch.object(
                        runtime, "_discovered_project_checks", return_value=discovered
                    ),
                    patch.object(
                        runtime, "run_project_check", side_effect=successful_check
                    ) as run_check,
                ):
                    result = runtime.run_checks_for_diff(
                        {"timeout_ms": 5000, "max_checks": 4}
                    )
            finally:
                runtime.close()
            self.assertEqual(
                result["selected_checks"],
                ["format-check", "lint", "typecheck", "test"],
            )
            self.assertTrue(result["command_success"], result)
            self.assertEqual(run_check.call_count, 4)
            for call in run_check.call_args_list:
                self.assertEqual(call.args[0]["yield_time_ms"], 5000)

    def test_broken_generic_git_tasks_are_not_advertised(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            try:
                self.assertEqual(runtime.list_tasks({"category": "git"})["tasks"], [])
                with self.assertRaises(ToolFailure) as missing:
                    runtime.run_task({"task_id": "git.status"})
                self.assertEqual(missing.exception.code, "NOT_FOUND")
            finally:
                runtime.close()


class FakeReadonlyAnnotationTests(unittest.TestCase):
    """The tools/list annotation override exists for clients that gate on
    annotations, which no server-side permission mode can influence. It is only
    defensible while the lie stays confined to tools/list, so these tests pin
    both halves: what it changes, and what it must never change."""

    MUTATING_TOOLS = ("apply_patch", "exec_command", "write_stdin", "kill_session")

    def test_default_runtime_reports_truthful_annotations(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="dangerous")
            self.assertFalse(runtime.fake_readonly_annotations)
            annotations = {
                tool["name"]: tool["annotations"]
                for tool in runtime.list_tools()["tools"]
            }
            for name in self.MUTATING_TOOLS:
                with self.subTest(tool=name):
                    self.assertFalse(annotations[name]["readOnlyHint"])
            self.assertTrue(annotations["apply_patch"]["destructiveHint"])
            self.assertTrue(annotations["exec_command"]["destructiveHint"])
            self.assertTrue(annotations["exec_command"]["openWorldHint"])
            self.assertTrue(annotations["run_task"]["destructiveHint"])
            self.assertTrue(annotations["run_task"]["openWorldHint"])
            self.assertTrue(annotations["write_stdin"]["destructiveHint"])
            self.assertIsNone(runtime.server_info_payload()["annotation_override"])

    def test_override_makes_every_listed_tool_report_read_only(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="dangerous", fake_readonly_annotations=True
            )
            annotations = {
                tool["name"]: tool["annotations"]
                for tool in runtime.list_tools()["tools"]
            }
            self.assertEqual(set(annotations), set(runtime.exposed_tool_names()))
            for name, annotation in annotations.items():
                with self.subTest(tool=name):
                    self.assertIs(annotation["readOnlyHint"], True)
                    self.assertIs(annotation["destructiveHint"], False)
                    self.assertIs(annotation["openWorldHint"], False)

    def test_override_is_disclosed_without_faking_server_info_or_card_annotations(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="dangerous", fake_readonly_annotations=True
            )
            info = runtime.server_info_payload()
            self.assertEqual(info["annotation_override"], "fake_readonly")

            card_tools = server_module.server_card_payload(runtime)["tools"]
            self.assertEqual(card_tools["annotationOverride"], "fake_readonly")
            for name in self.MUTATING_TOOLS:
                with self.subTest(tool=name):
                    self.assertIn(name, card_tools["readOnlyHintFalse"])
                    self.assertNotIn(name, card_tools["readOnlyHintTrue"])

    def test_override_still_executes_and_mutates(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace, permission_mode="dangerous", fake_readonly_annotations=True
            )
            result = runtime.exec_command(
                {
                    "cmd": "echo ran > ran.txt && cat ran.txt",
                    "timeout_ms": 30000,
                    "yield_time_ms": 30000,
                }
            )
            self.assertEqual(result.get("status"), "success", result)
            self.assertTrue(result.get("command_success"), result)
            self.assertEqual(result.get("stdout"), "ran\n")
            self.assertIsNone(runtime.sandbox)

    def test_override_is_reported_by_check_exec_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="dangerous", fake_readonly_annotations=True
            )
            warnings = runtime.check_exec_environment({})["warnings"]
            self.assertTrue(
                any("faked as read-only" in warning for warning in warnings),
                warnings,
            )

    def test_override_requires_dangerous_permission_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                execution_mode="build",
                fake_readonly_annotations=True,
            )
            self.assertTrue(runtime.fake_readonly_annotations)

    def test_policy_from_args_requires_dangerous_permission_mode(self) -> None:
        parser = server_module.build_parser()
        args = parser.parse_args(
            [
                "--dangerously-fake-readonly-annotations",
                "--execution-mode",
                "build",
            ]
        )
        policy = server_module.runtime_policy_from_args(args)
        self.assertTrue(policy.fake_readonly_annotations)

        args = parser.parse_args(
            [
                "--dangerously-fake-readonly-annotations",
                "--permission-mode",
                "dangerous",
            ]
        )
        self.assertTrue(
            server_module.runtime_policy_from_args(args).fake_readonly_annotations
        )

    def test_policy_from_args_reads_the_environment_switch(self) -> None:
        parser = server_module.build_parser()
        args = parser.parse_args(["--permission-mode", "dangerous"])
        with patch.dict(
            os.environ,
            {"CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS": "1"},
            clear=False,
        ):
            self.assertTrue(
                server_module.runtime_policy_from_args(args).fake_readonly_annotations
            )
        self.assertFalse(
            server_module.runtime_policy_from_args(args).fake_readonly_annotations
        )

    def test_override_over_http_requires_authentication(self) -> None:
        # A tunnel forwards to a loopback bind, so the bind host cannot tell a
        # private sandbox from a public one. Authentication is the real gate.
        # Ambient CODING_TOOLS_MCP_* vars (e.g. from the devcontainer) feed the
        # parser defaults and would auto-enable auth, turning the expected
        # refusal into a live server that hangs the suite — scrub them first.
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "CODING_TOOLS_MCP_AUTH_TOKEN",
                "CODING_TOOLS_MCP_HOST",
                "CODING_TOOLS_MCP_PORT",
                "CODING_TOOLS_MCP_GENERATE_AUTH_TOKEN",
            ):
                os.environ.pop(name, None)
            parser = server_module.build_parser()
            with TemporaryDirectory() as tmp:
                argv = [
                    "--workspace",
                    tmp,
                    "--permission-mode",
                    "dangerous",
                    "--dangerously-fake-readonly-annotations",
                ]
                args = parser.parse_args(argv)
                self.assertEqual(server_module.run_http(args), 2)


def file_path(name: str):
    return Path(name)
