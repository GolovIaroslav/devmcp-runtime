"""Static and runtime regression tests enforcing the DevMCP compatibility-surface retirement.

Verifies:
1. Retired tools (activate_policy_profile, approval_status, list_pending_approvals, etc.) are absent.
2. Active tool count is exactly 57.
3. policy.py exposes only execution_mode authority resolver functions.
4. server.py contains no live policy-profile authority logic or approval engine callers.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from coding_tools_mcp import policy
from coding_tools_mcp.server import TOOL_REGISTRY, Runtime

RETIRED_TOOLS = (
    "activate_policy_profile",
    "grant_root",
    "grant_capability",
    "list_capability_leases",
    "revoke_capability_lease",
    "end_task_scope",
    "approval_status",
    "list_pending_approvals",
)


class RetirementRegressionTests(unittest.TestCase):
    def test_tool_catalog_count_and_retired_tools_absence(self) -> None:
        self.assertEqual(
            len(TOOL_REGISTRY),
            57,
            f"Expected exactly 57 active MCP tools, got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}",
        )
        for tool_name in RETIRED_TOOLS:
            with self.subTest(tool=tool_name):
                self.assertNotIn(tool_name, TOOL_REGISTRY)

    def test_runtime_does_not_expose_activate_policy_profile(self) -> None:
        self.assertFalse(
            hasattr(Runtime, "activate_policy_profile"),
            "Runtime class must not have activate_policy_profile method",
        )

    def test_policy_module_surface(self) -> None:
        expected_symbols = {
            "EXECUTION_MODES",
            "DEFAULT_EXECUTION_MODE",
            "LEGACY_PERMISSION_MODES",
            "resolve_execution_mode",
            "effective_access",
        }
        for symbol in expected_symbols:
            with self.subTest(expected=symbol):
                self.assertTrue(
                    hasattr(policy, symbol),
                    f"policy module must export symbol {symbol}",
                )
        retired_symbols = (
            "PROFILE_NAMES",
            "CAPABILITIES",
            "DEFAULT_PROFILE",
            "profile_rules",
            "validate_rules",
            "effective_rules",
            "decision",
            "legacy_profile",
        )
        for symbol in retired_symbols:
            with self.subTest(symbol=symbol):
                self.assertFalse(
                    hasattr(policy, symbol),
                    f"policy module must not export retired symbol {symbol}",
                )

        mode, access = policy.resolve_execution_mode("build")
        self.assertEqual(mode, "build")
        self.assertEqual(access, "full-access")

        mode_plan, access_plan = policy.resolve_execution_mode("plan")
        self.assertEqual(mode_plan, "plan")
        self.assertEqual(access_plan, "read-only")

        mode_safe, access_safe = policy.resolve_execution_mode(permission_mode="safe")
        self.assertEqual(mode_safe, "plan")
        self.assertEqual(access_safe, "read-only")

        mode_trusted, access_trusted = policy.resolve_execution_mode(
            permission_mode="trusted"
        )
        self.assertEqual(mode_trusted, "build")
        self.assertEqual(access_trusted, "full-access")

    def test_server_source_has_no_active_profile_authority_calls(self) -> None:
        server_path = (
            Path(__file__).resolve().parent.parent / "coding_tools_mcp" / "server.py"
        )
        text = server_path.read_text(encoding="utf-8")

        forbidden_patterns = (
            "policy_decision(",
            "legacy_profile(",
            "effective_capability_rules",
            "approvals.db",
            "ModeCapabilities",
            "PERMISSION_MODE_CAPABILITIES",
            "AUTO_ALLOW_POLICY",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotIn(
                    pattern,
                    text,
                    f"server.py must not contain retired pattern: {pattern}",
                )


if __name__ == "__main__":
    unittest.main()
