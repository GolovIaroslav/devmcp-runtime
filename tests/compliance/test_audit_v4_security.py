from __future__ import annotations

import os
import socket
import shutil
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.tasks import TaskRegistry, TaskTemplate
from tests.compliance.test_support import ComplianceTestCase


class AuditV4SecurityTests(ComplianceTestCase):
    def test_host_mirror_replaces_sandbox_symlink_without_following_target(
        self,
    ) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap is required for the host-mirror regression")
        sentinel = self.workspace.root.parent / "host-sentinel.txt"
        sentinel.write_text("must remain byte-identical\n", encoding="utf-8")
        link_script = self.workspace.root / "make_link.py"
        link_script.write_text(
            "import os\n"
            "import time\n"
            f"os.symlink({str(sentinel)!r}, 'future-file.txt')\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        runtime = Runtime(self.workspace.root)
        session_id = ""
        try:
            result = runtime.exec_command(
                {
                    "cmd": "python3 make_link.py",
                    "timeout_ms": 30000,
                    "yield_time_ms": 1000,
                }
            )
            self.assertEqual(result.get("status"), "running", result)
            self.assertIn("READY", result.get("stdout", ""))
            session_id = str(result["session_id"])
            sandbox = runtime.sandbox
            self.assertIsNotNone(sandbox)
            assert sandbox is not None
            patch = "*** Begin Patch\n*** Add File: future-file.txt\n+safe mirror content\n*** End Patch\n"
            applied = runtime.apply_patch({"patch": patch})
            self.assertEqual(applied.get("risk"), "ALLOW", applied)
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "must remain byte-identical\n"
            )
            self.assertEqual(
                (sandbox.sandbox_dir / "future-file.txt").read_text(encoding="utf-8"),
                "safe mirror content\n",
            )
            self.assertFalse((sandbox.sandbox_dir / "future-file.txt").is_symlink())
        finally:
            if session_id:
                runtime.cancel_session(session_id)
            runtime.close()

    def test_snapshot_hides_secrets_from_a_normal_script(self) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap is required for the snapshot regression")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "AUDIT_V4_SECRET=must-not-leak\n", encoding="utf-8"
            )
            (root / ".env.local").write_text(
                "AUDIT_V4_SECRET=must-not-leak\n", encoding="utf-8"
            )
            (root / "certificate.pem").write_text(
                "private material\n", encoding="utf-8"
            )
            (root / "example.key").write_text("private material\n", encoding="utf-8")
            (root / ".env.example").write_text(
                "AUDIT_V4_SECRET=example\n", encoding="utf-8"
            )
            (root / "read_secret.py").write_text(
                "from pathlib import Path\n"
                "path = Path('.env')\n"
                "print(path.exists())\n"
                "print(path.read_text() if path.exists() else 'missing')\n"
                "print(Path('.env.example').exists())\n",
                encoding="utf-8",
            )
            runtime = Runtime(root)
            try:
                result = runtime.exec_command(
                    {
                        "cmd": "python3 read_secret.py",
                        "timeout_ms": 5000,
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(result.get("exit_code"), 0, result)
                self.assertNotIn("AUDIT_V4_SECRET", result.get("stdout", ""))
                self.assertNotIn("must-not-leak", result.get("stdout", ""))
                self.assertEqual(result.get("stdout"), "False\nmissing\nTrue\n")
                self.assertIsNone(runtime.sandbox)
            finally:
                runtime.close()

    def test_network_script_reaches_real_bwrap_and_is_isolated(self) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap is required for the network namespace regression")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "network_probe.py").write_text(
                "import socket\n"
                "checks = {\n"
                "  'tcp_localhost': lambda: socket.create_connection(('127.0.0.1', 9), .2),\n"
                "  'tcp_public': lambda: socket.create_connection(('1.1.1.1', 80), .2),\n"
                "  'dns': lambda: socket.getaddrinfo('example.com', 80),\n"
                "}\n"
                "for name, check in checks.items():\n"
                "    try:\n"
                "        check()\n"
                "        print(name + '=CONNECTED')\n"
                "    except Exception as exc:\n"
                "        print(name + '=' + type(exc).__name__)\n",
                encoding="utf-8",
            )
            runtime = Runtime(root)
            try:
                result = runtime.exec_command(
                    {
                        "cmd": "python3 network_probe.py",
                        "timeout_ms": 5000,
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(result.get("exit_code"), 0, result)
                stdout = result.get("stdout", "")
                self.assertNotIn("=CONNECTED", stdout)
                self.assertIn("tcp_localhost=", stdout)
                self.assertIn("tcp_public=", stdout)
                self.assertIn("dns=", stdout)
            finally:
                runtime.close()

    def test_network_grant_is_required_and_can_reach_a_controlled_listener(
        self,
    ) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap is required for the network approval regression")
        with TemporaryDirectory() as tmp, TemporaryDirectory() as home:
            root = Path(tmp)
            (root / "connect_probe.py").write_text(
                "import os, socket\n"
                "port = int(os.environ['AUDIT_V4_LISTENER_PORT'])\n"
                "try:\n"
                "    with socket.create_connection(('127.0.0.1', port), .5):\n"
                "        print('CONNECTED')\n"
                "except Exception as exc:\n"
                "    print('BLOCKED=' + type(exc).__name__)\n",
                encoding="utf-8",
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listener.settimeout(0.2)
            listener_port = listener.getsockname()[1]
            accepted = threading.Event()
            stop = threading.Event()

            def accept_connections() -> None:
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    accepted.set()
                    connection.close()

            thread = threading.Thread(target=accept_connections, daemon=True)
            thread.start()
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                runtime = Runtime(root)
                try:
                    command = "python3 connect_probe.py"
                    env = {"AUDIT_V4_LISTENER_PORT": str(listener_port)}
                    denied = runtime.exec_command(
                        {
                            "cmd": command,
                            "env": env,
                            "timeout_ms": 5000,
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(denied.get("exit_code"), 0, denied)
                    self.assertNotIn("CONNECTED", denied.get("stdout", ""))
                    self.assertFalse(accepted.is_set())

                    requested = runtime.exec_command(
                        {
                            "cmd": command,
                            "env": env,
                            "network_required": True,
                            "timeout_ms": 5000,
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(
                        requested.get("status"), "approval_required", requested
                    )
                    approval_id = requested["approval_id"]
                    ApprovalEngine().approve(approval_id)
                    allowed = runtime.exec_command(
                        {
                            "cmd": command,
                            "env": env,
                            "network_required": True,
                            "approval_id": approval_id,
                            "timeout_ms": 5000,
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(allowed.get("exit_code"), 0, allowed)
                    self.assertIn("CONNECTED", allowed.get("stdout", ""))
                    self.assertTrue(
                        accepted.wait(1),
                        "approved operation did not reach the host listener",
                    )
                finally:
                    runtime.close()
            stop.set()
            listener.close()
            thread.join(timeout=1)

    def test_run_task_is_argv_safe_and_executes_a_real_task(self) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap is required for task execution")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root)
            try:
                result = runtime.call_tool(
                    "run_task",
                    {"task_id": "test.echo", "args": ["$(touch escaped.txt)"]},
                )
                payload = result["structuredContent"]
                self.assertFalse(result.get("isError", False), result)
                self.assertIn("$(touch escaped.txt)", payload.get("stdout", ""))
                self.assertFalse((root / "escaped.txt").exists())
                self.assertIsNone(runtime.sandbox)
            finally:
                runtime.close()

    def test_task_templates_validate_declared_argument_types(self) -> None:
        template = TaskTemplate(
            "typed.test",
            "test",
            "typed test",
            "printf",
            ["%s", "{count}"],
            {"count": {"type": "integer", "required": True}},
        )
        registry = TaskRegistry()
        self.assertEqual(
            registry.build_argv(template, {"count": 7}), ["printf", "%s", "7"]
        )
        with self.assertRaises(ToolFailure):
            registry.build_argv(template, {"count": "7"})

    def test_read_files_is_implemented_and_trusted_mode_runs_unknown_commands(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            runtime = Runtime(root, permission_mode="trusted")
            try:
                files = runtime.read_files({"paths": ["one.txt", "two.txt"]})
                self.assertEqual(
                    [item["content"] for item in files["files"]], ["one\n", "two\n"]
                )
                result = runtime.exec_command(
                    {
                        "cmd": "command-that-is-not-in-the-safe-prefix",
                        "timeout_ms": 5000,
                    }
                )
                self.assertNotEqual(result.get("status"), "approval_required", result)
            finally:
                runtime.close()


class ApprovalV4Tests(unittest.TestCase):
    def test_canonical_operation_digest_uses_raw_env_and_is_single_use(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = ApprovalEngine(Path(tmp) / "approvals.db")
            requested = engine.request_approval(
                ["python3", "-c", "print(1)"],
                ".",
                "run exact argv",
                "inline_script",
                False,
                env={"FOO": "bar"},
                task_id="test.dummy",
                sandbox_id="session-1",
                capabilities=["inline_script"],
            )
            approval_id = requested["approval_id"]
            engine.approve(approval_id)
            granted = engine.consume(
                approval_id,
                ["python3", "-c", "print(1)"],
                ".",
                env={"FOO": "bar"},
                task_id="test.dummy",
                sandbox_id="session-1",
            )
            self.assertEqual(granted, ["inline_script"])
            with self.assertRaises(ToolFailure):
                engine.consume(
                    approval_id,
                    ["python3", "-c", "print(1)"],
                    ".",
                    env={"FOO": "bar"},
                    task_id="test.dummy",
                    sandbox_id="session-1",
                )

    def test_approval_cannot_be_approved_after_deny_or_expiry(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = ApprovalEngine(Path(tmp) / "approvals.db")
            denied = engine.request_approval(
                "unknown-command", ".", "deny", "exec", False
            )
            engine.deny(denied["approval_id"])
            with self.assertRaises(ToolFailure):
                engine.approve(denied["approval_id"])

            expired = engine.request_approval(
                "another-command", ".", "expire", "exec", False
            )
            with sqlite3.connect(engine.db_path) as conn:
                conn.execute(
                    "UPDATE requests SET expires_at = ? WHERE id = ?",
                    (0, expired["approval_id"]),
                )
            with self.assertRaises(ToolFailure):
                engine.approve(expired["approval_id"])
            self.assertEqual(engine.get_status(expired["approval_id"]), "expired")

    def test_runtime_approval_retry_overrides_only_granted_inline_capability(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as tmp,
            TemporaryDirectory() as _home,
            patch.dict(os.environ, {"HOME": tmp}, clear=False),
        ):
            root = Path(tmp) / "workspace"
            root.mkdir()
            runtime = Runtime(root)
            try:
                command = 'python3 -c "print(42)"'
                requested = runtime.exec_command({"cmd": command, "timeout_ms": 5000})
                self.assertEqual(
                    requested.get("status"), "approval_required", requested
                )
                approval_id = requested["approval_id"]
                ApprovalEngine().approve(approval_id)
                executed = runtime.exec_command(
                    {
                        "cmd": command,
                        "approval_id": approval_id,
                        "timeout_ms": 5000,
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(executed.get("exit_code"), 0, executed)
                self.assertEqual(executed.get("stdout"), "42\n")
            finally:
                runtime.close()
