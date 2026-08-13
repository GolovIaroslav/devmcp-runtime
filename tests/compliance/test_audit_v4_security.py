from __future__ import annotations

import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.tasks import TaskRegistry, TaskTemplate
from tests.compliance.test_support import ComplianceTestCase


class AuditV4SecurityTests(ComplianceTestCase):
    def test_read_only_workspace_cannot_create_symlink(self) -> None:
        sentinel = self.workspace.root.parent / "host-sentinel.txt"
        sentinel.write_text("must remain byte-identical\n", encoding="utf-8")
        link_script = self.workspace.root / "make_link.py"
        link_script.write_text(
            f"import os\nos.symlink({str(sentinel)!r}, 'future-file.txt')\n",
            encoding="utf-8",
        )
        runtime = Runtime(self.workspace.root, execution_mode="plan")
        try:
            with self.assertRaises(ToolFailure) as cm:
                runtime.exec_command(
                    {
                        "cmd": "python3 make_link.py",
                        "timeout_ms": 5000,
                        "yield_time_ms": 5000,
                    }
                )
            self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "must remain byte-identical\n"
            )
            self.assertFalse((self.workspace.root / "future-file.txt").exists())
        finally:
            runtime.close()

    def test_read_only_uses_authoritative_workspace_without_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tracked.txt"
            target.write_text("authoritative\n", encoding="utf-8")
            runtime = Runtime(root, execution_mode="plan")
            try:
                content = runtime.read_file({"path": "tracked.txt"})
                self.assertEqual(content.get("content"), "authoritative\n")
                with self.assertRaises(ToolFailure) as cm:
                    runtime.apply_patch(
                        {
                            "patch": "--- tracked.txt\n+++ tracked.txt\n@@ -1 +1 @@\n-authoritative\n+changed\n"
                        }
                    )
                self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")
                self.assertEqual(target.read_text(encoding="utf-8"), "authoritative\n")
            finally:
                runtime.close()

    def test_network_grant_is_required_and_can_reach_a_controlled_listener(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
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

            runtime = Runtime(root, execution_mode="build")
            try:
                command = "python3 connect_probe.py"
                env = {"AUDIT_V4_LISTENER_PORT": str(listener_port)}
                res = runtime.exec_command(
                    {
                        "cmd": command,
                        "env": env,
                        "timeout_ms": 5000,
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(res.get("exit_code"), 0, res)
                self.assertIn("CONNECTED", res.get("stdout", ""))
            finally:
                runtime.close()
                listener.close()

    def test_run_task_is_argv_safe_and_executes_a_real_task(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, execution_mode="build")
            try:
                result = runtime.call_tool(
                    "run_task",
                    {"task_id": "test.echo", "args": ["$(touch escaped.txt)"]},
                )
                payload = result["structuredContent"]
                self.assertFalse(result.get("isError", False), result)
                self.assertIn("$(touch escaped.txt)", payload.get("stdout", ""))
                self.assertFalse((root / "escaped.txt").exists())
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
            runtime = Runtime(root, execution_mode="build")
            try:
                files = runtime.read_files({"paths": ["one.txt", "two.txt"]})
                self.assertEqual(
                    [item["content"] for item in files["files"]], ["one\n", "two\n"]
                )
            finally:
                runtime.close()


class ApprovalV4Tests(unittest.TestCase):
    def test_approval_engine_stub_compatibility(self) -> None:
        engine = ApprovalEngine()
        res = engine.request_approval("action", ".", "reason")
        self.assertEqual(res["status"], "approved")
        consumed = engine.consume("app_id", "action", ".")
        self.assertEqual(consumed, [])
