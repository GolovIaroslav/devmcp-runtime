from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.tasks import TaskRegistry
from tests.compliance.test_support import ComplianceTestCase, structured_payload


class LiveDogfoodV5Tests(ComplianceTestCase):
    fixture_name = "coding-loop-project"

    def test_coding_loop_uses_safe_patch_and_registered_tests(self) -> None:
        before_pending = {
            item["id"] for item in structured_payload(self.client.call_tool("list_pending_approvals", {})).get(
                "pending_approvals", []
            )
        }

        self.assertEqual(structured_payload(self.client.call_tool("health", {})).get("status"), "ok")
        self.assertEqual(
            structured_payload(self.client.call_tool("workspace_info", {})).get("workspace"),
            str(self.workspace.root),
        )
        source = structured_payload(self.client.call_tool("read_file", {"path": "calc.py"}))
        self.assertIn("return a - b", source.get("content", ""))

        failed = structured_payload(
            self.client.call_tool(
                "run_task",
                {
                    "task_id": "pytest.all",
                    "env": {"AUTHORITATIVE_WORKSPACE": str(self.workspace.root)},
                    "timeout_ms": 10000,
                    "yield_time_ms": 10000,
                },
            )
        )
        self.assertEqual(failed.get("exit_code"), 1, failed)
        self.assertNotEqual(failed.get("status"), "approval_required", failed)
        self.assertIn("assert", f"{failed.get('stdout', '')}{failed.get('stderr', '')}".lower())

        patch = """*** Begin Patch
*** Update File: calc.py
@@
 def add(a, b):
-    return a - b
+    return a + b
*** End Patch
"""
        preview = structured_payload(self.client.call_tool("preview_patch", {"patch": patch}))
        self.assertEqual(preview.get("risk"), "ALLOW", preview)
        applied = structured_payload(self.client.call_tool("apply_patch", {"patch": patch}))
        self.assertTrue(applied.get("clean"), applied)
        self.assertNotIn("approval_id", applied)

        passed = structured_payload(
            self.client.call_tool(
                "run_task",
                {
                    "task_id": "pytest.all",
                    "env": {"AUTHORITATIVE_WORKSPACE": str(self.workspace.root)},
                    "timeout_ms": 10000,
                    "yield_time_ms": 10000,
                },
            )
        )
        self.assertEqual(passed.get("exit_code"), 0, passed)
        self.assertNotEqual(passed.get("status"), "approval_required", passed)

        diff = structured_payload(self.client.call_tool("git_diff", {})).get("diff", "")
        self.assertIn("-    return a - b", diff)
        self.assertIn("+    return a + b", diff)
        self.assertNotIn("test_calc.py", diff)
        self.assertEqual(diff.count("\n@@"), 1, diff)

        after_pending = {
            item["id"] for item in structured_payload(self.client.call_tool("list_pending_approvals", {})).get(
                "pending_approvals", []
            )
        }
        self.assertEqual(after_pending, before_pending)
        self.assertEqual((self.workspace.root / "calc.py").read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\n")


class ApprovalWorkflowV5Tests(unittest.TestCase):
    def test_risky_approval_is_out_of_band_exact_once_and_non_replayable(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {"HOME": tmp}, clear=False):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime = Runtime(workspace)
            try:
                command = 'python3 -c "print(42)"'
                requested = structured_payload(runtime.call_tool("exec_command", {"cmd": command, "timeout_ms": 5000}))
                self.assertEqual(requested.get("status"), "approval_required", requested)
                self.assertIsInstance(requested.get("approval_id"), str)
                self.assertEqual(requested.get("capabilities"), ["inline_script"])
                self.assertIn("operation_summary", requested)
                approval_id = requested["approval_id"]

                unknown = structured_payload(
                    runtime.call_tool("exec_command", {"cmd": "unregistered-v5-command", "timeout_ms": 5000})
                )
                self.assertEqual(unknown.get("status"), "approval_required", unknown)
                self.assertIsInstance(unknown.get("approval_id"), str)

                denied_socket = runtime.call_tool(
                    "exec_command", {"cmd": "printf /var/run/docker.sock", "timeout_ms": 5000}
                )
                self.assertTrue(denied_socket.get("isError"), denied_socket)
                self.assertEqual(structured_payload(denied_socket).get("error", {}).get("code"), "ACCESS_DENIED")

                ApprovalEngine().approve(approval_id)
                executed = structured_payload(
                    runtime.call_tool("exec_command", {"cmd": command, "approval_id": approval_id, "timeout_ms": 5000})
                )
                self.assertEqual(executed.get("exit_code"), 0, executed)
                self.assertEqual(executed.get("stdout"), "42\n")

                replay = runtime.call_tool(
                    "exec_command", {"cmd": command, "approval_id": approval_id, "timeout_ms": 5000}
                )
                self.assertTrue(replay.get("isError"), replay)

                modified = structured_payload(
                    runtime.call_tool(
                        "exec_command",
                        {"cmd": 'python3 -c "print(43)"', "approval_id": approval_id, "timeout_ms": 5000},
                    )
                )
                self.assertFalse(modified.get("ok", True), modified)

                denied = structured_payload(
                    runtime.call_tool("exec_command", {"cmd": 'python3 -c "print(1)"', "timeout_ms": 5000})
                )
                denied_id = denied["approval_id"]
                ApprovalEngine().deny(denied_id)
                denied_retry = runtime.call_tool(
                    "exec_command", {"cmd": 'python3 -c "print(1)"', "approval_id": denied_id, "timeout_ms": 5000}
                )
                self.assertTrue(denied_retry.get("isError"), denied_retry)
                with self.assertRaises(ToolFailure):
                    ApprovalEngine().approve(denied_id)

                expired = structured_payload(
                    runtime.call_tool("exec_command", {"cmd": 'python3 -c "print(2)"', "timeout_ms": 5000})
                )
                expired_id = expired["approval_id"]
                with sqlite3.connect(ApprovalEngine().db_path) as conn:
                    conn.execute("UPDATE requests SET expires_at = 0 WHERE id = ?", (expired_id,))
                expired_retry = runtime.call_tool(
                    "exec_command", {"cmd": 'python3 -c "print(2)"', "approval_id": expired_id, "timeout_ms": 5000}
                )
                self.assertTrue(expired_retry.get("isError"), expired_retry)
                with self.assertRaises(ToolFailure):
                    ApprovalEngine().approve(expired_id)
            finally:
                runtime.close()


class RegisteredTaskPolicyV5Tests(unittest.TestCase):
    def test_common_coding_workflows_are_registered_non_network_argv_tasks(self) -> None:
        registry = TaskRegistry()
        for task_id, argv in (
            ("pytest.all", ["pytest"]),
            ("unittest.all", ["python3", "-m", "unittest", "discover"]),
            ("vitest.run", ["vitest", "run"]),
            ("jest.run", ["jest"]),
            ("npm.lint", ["npm", "run", "lint"]),
            ("npm.typecheck", ["npm", "run", "typecheck"]),
            ("npm.build", ["npm", "run", "build"]),
        ):
            with self.subTest(task_id=task_id):
                task = registry.get_task(task_id)
                self.assertIsNotNone(task)
                assert task is not None
                self.assertFalse(task.network_requirement)
                self.assertEqual(task.approval_class, "ALLOW")
                self.assertEqual(registry.match_direct_argv(argv), task)


if __name__ == "__main__":
    unittest.main()
