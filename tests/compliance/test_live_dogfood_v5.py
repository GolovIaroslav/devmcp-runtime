from __future__ import annotations

import unittest

from coding_tools_mcp.tasks import TaskRegistry
from tests.compliance.test_support import ComplianceTestCase, structured_payload


class LiveDogfoodV5Tests(ComplianceTestCase):
    fixture_name = "coding-loop-project"

    def test_coding_loop_uses_safe_patch_and_registered_tests(self) -> None:

        self.assertEqual(
            structured_payload(self.client.call_tool("health", {})).get("status"), "ok"
        )
        self.assertEqual(
            structured_payload(self.client.call_tool("workspace_info", {})).get(
                "workspace"
            ),
            str(self.workspace.root),
        )
        source = structured_payload(
            self.client.call_tool("read_file", {"path": "calc.py"})
        )
        self.assertIn("return a - b", source.get("content", ""))

        failed = structured_payload(
            self.client.call_tool(
                "run_task",
                {
                    "task_id": "pytest.all",
                    "env": {
                        "AUTHORITATIVE_WORKSPACE": str(self.workspace.root),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    "timeout_ms": 10000,
                    "yield_time_ms": 10000,
                },
            )
        )
        self.assertEqual(failed.get("exit_code"), 1, failed)
        self.assertNotEqual(failed.get("status"), "approval_required", failed)
        self.assertIn(
            "assert", f"{failed.get('stdout', '')}{failed.get('stderr', '')}".lower()
        )

        patch = """*** Begin Patch
*** Update File: calc.py
@@
 def add(a, b):
-    return a - b
+    return a + b
*** End Patch
"""
        preview = structured_payload(
            self.client.call_tool("preview_patch", {"patch": patch})
        )
        self.assertEqual(preview.get("risk"), "ALLOW", preview)
        applied = structured_payload(
            self.client.call_tool("apply_patch", {"patch": patch})
        )
        self.assertTrue(applied.get("clean"), applied)
        self.assertNotIn("approval_id", applied)

        passed = structured_payload(
            self.client.call_tool(
                "run_task",
                {
                    "task_id": "pytest.all",
                    "env": {
                        "AUTHORITATIVE_WORKSPACE": str(self.workspace.root),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
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

        self.assertEqual(
            (self.workspace.root / "calc.py").read_text(encoding="utf-8"),
            "def add(a, b):\n    return a + b\n",
        )


class RegisteredTaskPolicyV5Tests(unittest.TestCase):
    def test_common_coding_workflows_are_registered_non_network_argv_tasks(
        self,
    ) -> None:
        registry = TaskRegistry()
        for task_id, argv in (
            ("pytest.all", ["python3", "-m", "pytest"]),
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
                self.assertEqual(registry.match_direct_argv(argv), task)


if __name__ == "__main__":
    unittest.main()
