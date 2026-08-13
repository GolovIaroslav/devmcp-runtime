from __future__ import annotations

from tests.compliance.test_support import ComplianceTestCase


class SecurityComplianceTests(ComplianceTestCase):
    fixture_name = "malicious-project"
    permission_mode = "safe"

    def test_path_traversal_absolute_paths_and_symlink_escape_are_rejected(
        self,
    ) -> None:
        self.assert_denied_or_permission_required(
            "read_file", {"path": "../outside-secret.txt"}
        )

        absolute_patch = f"""*** Begin Patch
*** Add File: {self.workspace.outside_secret}
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required(
            "apply_patch", {"patch": absolute_patch}
        )

        traversal_patch = """*** Begin Patch
*** Update File: ../outside-secret.txt
@@
-TOP_SECRET_DO_NOT_READ
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required(
            "apply_patch", {"patch": traversal_patch}
        )

    def test_plan_mode_denies_exec_command(self) -> None:
        res = self.client.call_tool(
            "exec_command",
            {"cmd": "echo 123", "timeout_ms": 5000},
        )
        self.assertTrue(res.get("isError"))
        err = res.get("structuredContent", {}).get("error", {})
        self.assertEqual(err.get("code"), "PERMISSION_REQUIRED")

    def test_exec_command_long_non_path_tokens_do_not_become_internal_errors(
        self,
    ) -> None:
        with self.client_with_permission("trusted") as client:
            result = client.call_tool(
                "exec_command",
                {
                    "cmd": "printf " + ("a" * 300),
                    "timeout_ms": 5000,
                    "max_output_bytes": 4096,
                },
            )
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("status"), "success", payload)
        self.assertTrue(payload.get("command_success"), payload)
        self.assertEqual(payload.get("exit_code"), 0, payload)

    def test_exec_command_timeout_is_enforced_after_running_session_is_returned(
        self,
    ) -> None:
        with self.client_with_permission("trusted") as client:
            started = client.call_tool(
                "exec_command",
                {
                    "cmd": "sleep 5",
                    "timeout_ms": 100,
                    "yield_time_ms": 0,
                    "max_output_bytes": 4096,
                },
            )
            payload = self.assert_tool_success(started)
            session_id = payload.get("session_id")
            self.assertIsNotNone(session_id)
