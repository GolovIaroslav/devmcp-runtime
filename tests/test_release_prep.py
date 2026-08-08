from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.devmcp.ui import UIState
from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.config import load_config, paths, redact_config, save_config
from coding_tools_mcp.policy import CAPABILITIES, decision, profile_rules
from coding_tools_mcp.server import Runtime


class ReleaseConfigTests(unittest.TestCase):
    def test_new_config_is_balanced_and_secret_status_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
                selected = paths()
                config = load_config(selected, workspace=tmp)
                self.assertEqual(config["profile"], "balanced")
                self.assertEqual(selected.root.stat().st_mode & 0o777, 0o700)
                self.assertNotIn("super-secret-value", str(redact_config(config)))
                save_config(config, selected)
                self.assertTrue(selected.config_file.is_file())

    def test_policy_profiles_keep_floor_and_custom_is_data(self) -> None:
        self.assertEqual(set(profile_rules("balanced")), set(CAPABILITIES))
        self.assertEqual(decision("safe", "workspace.delete"), "ask")
        self.assertEqual(decision("safe", "git.branch"), "ask")
        self.assertEqual(decision("power", "server.public"), "deny")
        self.assertEqual(decision("power", "workspace.delete"), "auto")
        self.assertEqual(decision("balanced", "workspace.delete"), "ask")

    def test_ui_is_loopback_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
            UIState.load("127.0.0.1", 47158)
            with self.assertRaises(ValueError):
                UIState.load("0.0.0.0", 47158)


class ReleaseLifecycleTests(unittest.TestCase):
    def test_expired_approvals_are_marked_and_clear_does_not_touch_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = ApprovalEngine(Path(tmp) / "approvals.db")
            pending = engine.request_approval("curl https://example.invalid", tmp, "network", "network", True)
            self.assertEqual(pending["status"], "approval_required")
            with sqlite3.connect(engine.db_path) as connection:
                connection.execute("UPDATE requests SET expires_at = 0")
            self.assertEqual(engine.list_pending(), [])
            self.assertGreaterEqual(engine.clear_expired(), 1)

    def test_list_files_dot_returns_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (workspace / "test_calc.py").write_text("def test_add():\n    assert 2 + 3 == 5\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Release Test"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=workspace, check=True)
            runtime = Runtime(workspace)
            try:
                payload = runtime.list_files({"path": "."})
            finally:
                runtime.close()
            listed = {item["path"] for item in payload["files"]}
            self.assertIn("calc.py", listed)
            self.assertIn("test_calc.py", listed)

    def test_balanced_delete_is_previewed_and_requires_one_time_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}, clear=False):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            target = workspace / "remove-me.txt"
            target.write_text("keep an approval record\n", encoding="utf-8")
            runtime = Runtime(workspace, policy_profile="balanced")
            try:
                patch_text = "*** Begin Patch\n*** Delete File: remove-me.txt\n*** End Patch"
                pending = runtime.apply_patch({"patch": patch_text})
                self.assertEqual(pending["status"], "approval_required")
                ApprovalEngine().approve(pending["approval_id"])
                applied = runtime.apply_patch({"patch": patch_text, "approval_id": pending["approval_id"]})
                self.assertTrue(applied["clean"])
                self.assertFalse(target.exists())
                with self.assertRaises(Exception):
                    runtime.apply_patch({"patch": patch_text, "approval_id": pending["approval_id"]})
            finally:
                runtime.close()

    def test_custom_policy_rules_drive_delete_without_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "remove-me.txt"
            target.write_text("custom policy\n", encoding="utf-8")
            runtime = Runtime(workspace, policy_profile="custom", policy_rules={"workspace.delete": "auto"})
            try:
                result = runtime.apply_patch(
                    {"patch": "*** Begin Patch\n*** Delete File: remove-me.txt\n*** End Patch"}
                )
                self.assertTrue(result["clean"])
                self.assertFalse(target.exists())
                self.assertEqual(runtime.server_info_payload()["policy_rules"]["workspace.delete"], "auto")
            finally:
                runtime.close()
