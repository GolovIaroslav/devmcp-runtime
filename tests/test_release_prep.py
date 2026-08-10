from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.devmcp import cli
from apps.devmcp.ui import UIState
from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.config import (
    load_config,
    paths,
    redact_config,
    save_config,
    write_secret,
)
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.policy import (
    CAPABILITIES,
    UNIMPLEMENTED_CAPABILITIES,
    decision,
    profile_rules,
    validate_rules,
)
from coding_tools_mcp.server import Runtime, run_http


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

    def test_policy_profiles_cover_each_capability_and_custom_is_data(self) -> None:
        self.assertEqual(set(profile_rules("balanced")), set(CAPABILITIES))
        self.assertEqual(len(CAPABILITIES), len(set(CAPABILITIES)))
        rules = {
            name: "auto"
            for name in CAPABILITIES
            if name not in UNIMPLEMENTED_CAPABILITIES
        }
        self.assertEqual(validate_rules(rules)["agent.delegate"], "deny")
        with self.assertRaisesRegex(ValueError, "not implemented"):
            validate_rules({name: "auto" for name in CAPABILITIES})
        self.assertEqual(decision("safe", "workspace.delete"), "ask")
        self.assertEqual(decision("safe", "git.branch"), "ask")
        self.assertEqual(decision("balanced", "git.branch"), "auto")
        self.assertEqual(decision("balanced", "git.commit"), "auto")
        self.assertEqual(decision("balanced", "git.push"), "ask")
        self.assertEqual(decision("power", "server.public"), "ask")
        self.assertEqual(decision("power", "workspace.delete"), "auto")
        self.assertEqual(decision("power", "service.manage"), "auto")
        self.assertEqual(decision("balanced", "policy.manage"), "ask")
        self.assertEqual(decision("power", "policy.manage"), "ask")
        self.assertEqual(decision("balanced", "workspace.delete"), "ask")
        for capability in set(CAPABILITIES) - set(UNIMPLEMENTED_CAPABILITIES):
            with self.subTest(profile="autonomous", capability=capability):
                self.assertEqual(decision("autonomous", capability), "auto")
        self.assertEqual(decision("autonomous", "agent.delegate"), "deny")

    def test_policy_export_import_preserves_patch_thresholds(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["patch"] = {"max_removed_lines": 17, "max_removed_percent": 12.5}
            save_config(config, selected)
            exported = Path(tmp) / "policy.json"

            self.assertEqual(
                cli._policy_command(
                    SimpleNamespace(policy_action="export", file=str(exported))
                ),
                0,
            )
            config = load_config(selected)
            config["patch"] = {"max_removed_lines": 99, "max_removed_percent": 88.0}
            save_config(config, selected)

            self.assertEqual(
                cli._policy_command(
                    SimpleNamespace(policy_action="import", file=str(exported))
                ),
                0,
            )
            self.assertEqual(
                load_config(selected)["patch"],
                {"max_removed_lines": 17, "max_removed_percent": 12.5},
            )

    def test_policy_import_rejects_invalid_patch_thresholds_without_saving(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["patch"] = {"max_removed_lines": 23, "max_removed_percent": 19.0}
            save_config(config, selected)
            imported = Path(tmp) / "policy.json"
            imported.write_text(
                json.dumps(
                    {
                        "rules": profile_rules("balanced"),
                        "patch": {"max_removed_lines": -1, "max_removed_percent": 12.5},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "patch thresholds cannot be negative"
            ):
                cli._policy_command(
                    SimpleNamespace(policy_action="import", file=str(imported))
                )
            self.assertEqual(
                load_config(selected)["patch"],
                {"max_removed_lines": 23, "max_removed_percent": 19.0},
            )

    def test_policy_import_without_patch_preserves_current_thresholds(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["patch"] = {"max_removed_lines": 23, "max_removed_percent": 19.0}
            save_config(config, selected)
            imported = Path(tmp) / "policy.json"
            imported.write_text(
                json.dumps({"rules": profile_rules("safe")}), encoding="utf-8"
            )

            self.assertEqual(
                cli._policy_command(
                    SimpleNamespace(policy_action="import", file=str(imported))
                ),
                0,
            )
            self.assertEqual(
                load_config(selected)["patch"],
                {"max_removed_lines": 23, "max_removed_percent": 19.0},
            )

    def test_policy_import_partial_patch_preserves_omitted_threshold(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["patch"] = {"max_removed_lines": 23, "max_removed_percent": 19.0}
            save_config(config, selected)
            imported = Path(tmp) / "policy.json"
            imported.write_text(
                json.dumps(
                    {"rules": profile_rules("safe"), "patch": {"max_removed_lines": 7}}
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                cli._policy_command(
                    SimpleNamespace(policy_action="import", file=str(imported))
                ),
                0,
            )
            self.assertEqual(
                load_config(selected)["patch"],
                {"max_removed_lines": 7, "max_removed_percent": 19.0},
            )

    def test_policy_import_rejects_malformed_patch_without_saving(self) -> None:
        invalid_patches = (
            (None, "patch thresholds must be an object"),
            ([], "patch thresholds must be an object"),
            ({"max_removed_lines": True}, "max_removed_lines must be an integer"),
            ({"max_removed_lines": 1.5}, "max_removed_lines must be an integer"),
            ({"max_removed_percent": True}, "max_removed_percent must be a number"),
            (
                {"max_removed_percent": float("nan")},
                "max_removed_percent must be finite",
            ),
            (
                {"max_removed_percent": float("inf")},
                "max_removed_percent must be finite",
            ),
            (
                {"max_removed_percent": float("-inf")},
                "max_removed_percent must be finite",
            ),
            ({"unexpected": 1}, "unknown patch threshold fields: unexpected"),
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["patch"] = {"max_removed_lines": 23, "max_removed_percent": 19.0}
            save_config(config, selected)
            imported = Path(tmp) / "policy.json"

            for invalid_patch, message in invalid_patches:
                with self.subTest(patch=invalid_patch):
                    imported.write_text(
                        json.dumps(
                            {"rules": profile_rules("safe"), "patch": invalid_patch}
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        cli._policy_command(
                            SimpleNamespace(policy_action="import", file=str(imported))
                        )
                    self.assertEqual(
                        load_config(selected)["patch"],
                        {"max_removed_lines": 23, "max_removed_percent": 19.0},
                    )

    def test_policy_import_keeps_custom_rule_validation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            save_config(config, selected)
            imported = Path(tmp) / "policy.json"
            imported.write_text(
                json.dumps(
                    {
                        "rules": {
                            **profile_rules("balanced"),
                            "workspace.read": "invalid",
                        },
                        "patch": {"max_removed_lines": 17, "max_removed_percent": 12.5},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be auto, ask, or deny"):
                cli._policy_command(
                    SimpleNamespace(policy_action="import", file=str(imported))
                )
            self.assertEqual(load_config(selected)["profile"], "balanced")

    def test_ui_is_loopback_only(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            UIState.load("127.0.0.1", 47158)
            with self.assertRaises(ValueError):
                UIState.load("0.0.0.0", 47158)

    def test_mcp_health_reuses_initialize_session_for_health_call(self) -> None:
        class FakeResponse:
            def __init__(
                self, body: bytes, headers: dict[str, str] | None = None
            ) -> None:
                self.body = body
                self.headers = headers or {}

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return self.body

        responses = [
            FakeResponse(b'{"result": {}}', {"Mcp-Session-Id": "session-1"}),
            FakeResponse(b""),
            FakeResponse(b'{"result": {"structuredContent": {"status": "ok"}}}'),
            FakeResponse(b""),
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            write_secret(selected.mcp_token, "fixture-token")
            with patch.object(
                cli.urllib.request, "urlopen", side_effect=responses
            ) as urlopen:
                self.assertTrue(cli._mcp_health(config, selected))

            health_request = urlopen.call_args_list[2].args[0]
            self.assertEqual(health_request.get_header("Mcp-session-id"), "session-1")
            delete_request = urlopen.call_args_list[3].args[0]
            self.assertEqual(delete_request.method, "DELETE")
            self.assertEqual(delete_request.get_header("Mcp-session-id"), "session-1")

    def test_service_restart_waits_for_mcp_health_before_tunnel_restart(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_systemctl(
            *args: str, check: bool = False
        ) -> subprocess.CompletedProcess[str]:
            del check
            calls.append(args)
            if args[:2] == ("show", "--property=LoadState"):
                return subprocess.CompletedProcess(args, 0, "loaded\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch.object(cli, "_systemctl", side_effect=fake_systemctl),
            patch.object(cli, "_wait_for_mcp_health", return_value=True) as wait_health,
        ):
            self.assertEqual(cli._service_action("restart"), 0)

        wait_health.assert_called_once_with()
        self.assertEqual(
            calls,
            [
                ("show", "--property=LoadState", "--value", cli.TUNNEL_SERVICE),
                ("restart", cli.MCP_SERVICE),
                ("restart", cli.TUNNEL_SERVICE),
            ],
        )


class ReleaseLifecycleTests(unittest.TestCase):
    def test_expired_approvals_are_marked_and_clear_does_not_touch_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = ApprovalEngine(Path(tmp) / "approvals.db")
            pending = engine.request_approval(
                "curl https://example.invalid", tmp, "network", "network", True
            )
            self.assertEqual(pending["status"], "approval_required")
            with sqlite3.connect(engine.db_path) as connection:
                connection.execute("UPDATE requests SET expires_at = 0")
            self.assertEqual(engine.list_pending(), [])
            self.assertGreaterEqual(engine.clear_expired(), 1)

    def test_list_files_dot_returns_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            )
            (workspace / "test_calc.py").write_text(
                "def test_add():\n    assert 2 + 3 == 5\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Test"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fixture"], cwd=workspace, check=True
            )
            runtime = Runtime(workspace)
            try:
                payload = runtime.list_files({"path": "."})
            finally:
                runtime.close()
            listed = {item["path"] for item in payload["files"]}
            self.assertIn("calc.py", listed)
            self.assertIn("test_calc.py", listed)

    def test_balanced_delete_is_previewed_and_requires_one_time_approval(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HOME": tmp}, clear=False),
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            target = workspace / "remove-me.txt"
            target.write_text("keep an approval record\n", encoding="utf-8")
            runtime = Runtime(workspace, policy_profile="balanced")
            try:
                patch_text = (
                    "*** Begin Patch\n*** Delete File: remove-me.txt\n*** End Patch"
                )
                pending = runtime.apply_patch({"patch": patch_text})
                self.assertEqual(pending["status"], "approval_required")
                ApprovalEngine().approve(pending["approval_id"])
                applied = runtime.apply_patch(
                    {"patch": patch_text, "approval_id": pending["approval_id"]}
                )
                self.assertTrue(applied["clean"])
                self.assertFalse(target.exists())
                with self.assertRaises(Exception):
                    runtime.apply_patch(
                        {"patch": patch_text, "approval_id": pending["approval_id"]}
                    )
            finally:
                runtime.close()

    def test_custom_policy_rules_drive_delete_without_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "remove-me.txt"
            target.write_text("custom policy\n", encoding="utf-8")
            runtime = Runtime(
                workspace,
                policy_profile="custom",
                policy_rules={"workspace.delete": "auto"},
            )
            try:
                result = runtime.apply_patch(
                    {
                        "patch": "*** Begin Patch\n*** Delete File: remove-me.txt\n*** End Patch"
                    }
                )
                self.assertTrue(result["clean"])
                self.assertFalse(target.exists())
                self.assertEqual(
                    runtime.server_info_payload()["policy_rules"]["workspace.delete"],
                    "auto",
                )
            finally:
                runtime.close()

    def test_active_profile_controls_exec_and_network_not_legacy_safe_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe", policy_profile="power")
            try:
                self.assertEqual(
                    runtime._policy_decision_for_capabilities(
                        {"exec.arbitrary", "network.public"}
                    ),
                    "auto",
                )
                with patch.object(
                    runtime, "_execute_command_legacy", return_value={"reached": True}
                ) as execute:
                    self.assertEqual(
                        runtime.exec_command({"cmd": "curl https://example.invalid"}),
                        {"reached": True},
                    )
                executed_args = execute.call_args.args[0]
                self.assertEqual(executed_args["_network_capability"], "network.public")
                self.assertIn("network.public", executed_args["_approved_capabilities"])
            finally:
                runtime.close()

    def test_autonomous_profile_runs_arbitrary_exec_without_approval_but_not_sudo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp), permission_mode="safe", policy_profile="autonomous"
            )
            try:
                with patch.object(
                    runtime, "_execute_command_legacy", return_value={"reached": True}
                ) as execute:
                    self.assertEqual(
                        runtime.exec_command({"cmd": "printf autonomous"}),
                        {"reached": True},
                    )
                self.assertIn(
                    "exec.arbitrary",
                    execute.call_args.args[0]["_approved_capabilities"],
                )
                with self.assertRaisesRegex(ToolFailure, "denied"):
                    runtime.exec_command({"cmd": "sudo true"})
                with self.assertRaisesRegex(ToolFailure, "denied"):
                    runtime.exec_command({"cmd": "bwrap --bind / / true"})
            finally:
                runtime.close()

    def test_autonomous_profile_runs_host_diagnostics_and_schedules_restart(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0, "operator-ok\n", "")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), policy_profile="autonomous")
            try:
                with patch(
                    "coding_tools_mcp.server.subprocess.run", return_value=completed
                ) as run:
                    status = runtime.service_status({})
                    self.assertEqual(status["stdout"], "operator-ok\n")
                    self.assertIn("status", run.call_args.args[0])

                with (
                    patch(
                        "coding_tools_mcp.server.shutil.which",
                        side_effect=lambda name: f"/usr/bin/{name}",
                    ),
                    patch(
                        "coding_tools_mcp.server.subprocess.run", return_value=completed
                    ) as run,
                ):
                    restart = runtime.service_restart({})
                    self.assertEqual(restart["status"], "scheduled")
                    command = run.call_args.args[0]
                    self.assertEqual(command[0], "/usr/bin/systemd-run")
                    self.assertIn("apps.devmcp.cli", command)
                    self.assertEqual(command[-1], "restart")
            finally:
                runtime.close()

    def test_http_pressure_eviction_repairs_orphaned_sandbox_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), sandbox_backend="unsafe")
            try:
                sandbox = runtime._acquire_execution_sandbox()
                self.assertEqual(runtime.sandbox_users, 1)
                self.assertIs(runtime.sandbox, sandbox)

                self.assertTrue(runtime.http_session_evictable())

                self.assertEqual(runtime.sandbox_users, 0)
                self.assertIsNone(runtime.sandbox)
                self.assertFalse(sandbox.sandbox_dir.exists())
            finally:
                runtime.close()

    def test_autonomous_profile_can_activate_policy_profile_and_schedule_restart(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0, "Policy profile: power\n", "")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), policy_profile="autonomous")
            try:
                with (
                    patch(
                        "coding_tools_mcp.server.subprocess.run", return_value=completed
                    ) as run,
                    patch.object(
                        runtime,
                        "_schedule_devmcp_restart",
                        return_value={"status": "scheduled", "unit": "fixture"},
                    ) as schedule,
                ):
                    result = runtime.activate_policy_profile({"profile": "power"})

                self.assertEqual(result["profile"], "power")
                self.assertEqual(result["previous_profile"], "autonomous")
                self.assertEqual(result["status"], "scheduled")
                self.assertEqual(
                    run.call_args.args[0][-3:], ["policy", "profile", "power"]
                )
                schedule.assert_called_once_with()
            finally:
                runtime.close()

    def test_custom_ask_requires_and_consumes_an_approval_before_execution(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            rules = {name: "auto" for name in CAPABILITIES}
            rules["agent.delegate"] = "deny"
            rules["exec.arbitrary"] = "ask"
            runtime = Runtime(
                workspace,
                policy_profile="custom",
                policy_rules=rules,
                sandbox_backend="unsafe",
            )
            try:
                command = "printf custom-policy-approved"
                pending = runtime.exec_command({"cmd": command})
                self.assertEqual(pending["status"], "approval_required")
                ApprovalEngine().approve(pending["approval_id"])
                completed = runtime.exec_command(
                    {"cmd": command, "approval_id": pending["approval_id"]}
                )
                self.assertTrue(completed["ok"])
                self.assertEqual(completed["status"], "exited")
                self.assertIn("custom-policy-approved", completed["stdout"])
            finally:
                runtime.close()

    def test_custom_exec_deny_blocks_behavior_even_with_trusted_legacy_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="trusted",
                policy_profile="custom",
                policy_rules={"exec.arbitrary": "deny"},
            )
            try:
                with self.assertRaisesRegex(
                    ToolFailure, "disabled by the active policy profile"
                ):
                    runtime.exec_command({"cmd": "echo blocked"})
            finally:
                runtime.close()

    def test_profile_exec_categories_produce_real_policy_decisions(self) -> None:
        commands = {
            "exec.registered": ("echo hello", "test.echo"),
            "exec.arbitrary": ("printf policy", None),
            "network.public": ("curl https://example.invalid", None),
            "network.host_local": ("curl http://127.0.0.1:9", None),
            "deps.install": ("npm install", None),
            "db.migrate": ("alembic upgrade head", None),
            "git.branch": ("git branch", None),
            "git.commit": ("git commit -m policy", None),
            "git.sync": ("git fetch origin", None),
            "git.push": ("git push", None),
            "env.sensitive": ("printf policy", None),
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            for capability, (command, task_id) in commands.items():
                with self.subTest(capability=capability):
                    rules = {name: "auto" for name in CAPABILITIES}
                    rules["agent.delegate"] = "deny"
                    rules[capability] = "deny"
                    runtime = Runtime(
                        workspace, policy_profile="custom", policy_rules=rules
                    )
                    try:
                        args: dict[str, object] = {"cmd": command}
                        if capability == "env.sensitive":
                            args["env"] = {"EXAMPLE_API_KEY": "not-a-real-secret"}
                        with self.assertRaises(ToolFailure) as raised:
                            runtime.exec_command(args)
                        self.assertIn(
                            capability, raised.exception.details.get("capabilities", [])
                        )
                    finally:
                        runtime.close()

    def test_workspace_capabilities_block_their_real_operations(self) -> None:
        operations = {
            "workspace.read": lambda runtime: runtime.read_files(
                {"paths": ["existing.txt"]}
            ),
            "workspace.create": lambda runtime: runtime.apply_patch(
                {"patch": "*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch"}
            ),
            "workspace.patch_small": lambda runtime: runtime.apply_patch(
                {
                    "patch": "*** Begin Patch\n*** Update File: existing.txt\n@@\n-old\n+new\n*** End Patch"
                }
            ),
            "workspace.patch_destructive": lambda runtime: runtime.apply_patch(
                {
                    "patch": "*** Begin Patch\n*** Update File: existing.txt\n@@\n old\n-second\n*** End Patch"
                }
            ),
            "workspace.delete": lambda runtime: runtime.apply_patch(
                {
                    "patch": "*** Begin Patch\n*** Delete File: existing.txt\n*** End Patch"
                }
            ),
            "workspace.move": lambda runtime: runtime.apply_patch(
                {
                    "patch": "*** Begin Patch\n*** Move File: existing.txt to: moved.txt\n*** End Patch"
                }
            ),
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            for capability, operation in operations.items():
                with self.subTest(capability=capability):
                    (workspace / "existing.txt").write_text(
                        "old\nsecond\n"
                        if capability == "workspace.patch_destructive"
                        else "old\n",
                        encoding="utf-8",
                    )
                    rules = {name: "auto" for name in CAPABILITIES}
                    rules["agent.delegate"] = "deny"
                    rules[capability] = "deny"
                    runtime = Runtime(
                        workspace,
                        policy_profile="custom",
                        policy_rules=rules,
                        max_removed_lines=0
                        if capability == "workspace.patch_destructive"
                        else 200,
                    )
                    try:
                        with self.assertRaises(ToolFailure):
                            operation(runtime)
                    finally:
                        runtime.close()

    def test_server_capabilities_block_their_real_bind_operations(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["profile"] = "custom"
            config["policy"] = {"custom": {name: "auto" for name in CAPABILITIES}}
            config["policy"]["custom"]["agent.delegate"] = "deny"
            config["policy"]["custom"]["server.loopback"] = "deny"
            config["policy"]["custom"]["server.public"] = "deny"
            save_config(config, selected)
            args = SimpleNamespace(
                auth_token=None,
                auth_token_file=None,
                host="127.0.0.1",
                policy_profile="custom",
                permission_mode="trusted",
                allow_network=True,
                shell_env_inherit=None,
                oauth_mode=False,
            )
            with patch.dict(
                os.environ,
                {"DEVMCP_POLICY_CONFIG_FILE": str(selected.config_file)},
                clear=False,
            ):
                self.assertEqual(run_http(args), 2)
                args.host = "0.0.0.0"
                self.assertEqual(run_http(args), 2)

    def test_legacy_trusted_authenticated_public_bind_remains_compatible(self) -> None:
        class FakeServer:
            def __init__(self) -> None:
                self.served = False
                self.closed = False

            def serve_forever(self) -> None:
                self.served = True

            def server_close(self) -> None:
                self.closed = True

        args = SimpleNamespace(
            auth_token="smoke-token",
            auth_token_file=None,
            host="0.0.0.0",
            port=8765,
            policy_profile=None,
            permission_mode="trusted",
            allow_network=True,
            shell_env_inherit=None,
            oauth_mode=False,
        )
        fake_runtime = SimpleNamespace(
            auth_token="smoke-token", project_context=object()
        )
        fake_server = FakeServer()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "coding_tools_mcp.server.build_runtime", return_value=fake_runtime
            ) as build_runtime,
            patch(
                "coding_tools_mcp.server.RuntimeHTTPServer", return_value=fake_server
            ),
        ):
            self.assertEqual(run_http(args), 0)

        self.assertEqual(build_runtime.call_count, 1)
        self.assertTrue(fake_server.served)
        self.assertTrue(fake_server.closed)

    def test_foreground_tunnel_status_uses_tunnel_client_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
                selected = paths()
                fake_client = Path(tmp) / "tunnel-client"
                fake_client.touch()
                response = subprocess.CompletedProcess(
                    [], 0, '{"ready": true, "healthy": true}', ""
                )
                with (
                    patch.object(cli, "TUNNEL_BIN", fake_client),
                    patch.object(cli.subprocess, "run", return_value=response) as run,
                ):
                    self.assertEqual(
                        cli._tunnel_status(selected), {"ready": True, "healthy": True}
                    )
                command = run.call_args.args[0]
                self.assertEqual(command[1], "health")
                self.assertIn("--require-control-plane-poll", command)
                self.assertIn(str(selected.tunnel_health_url), command)
                self.assertNotIn("runtimes", command)

    def test_foreground_tunnel_run_writes_a_loopback_health_url(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["tunnel_id"] = "tunnel-fixture"
            save_config(config, selected)
            write_secret(selected.mcp_token, "fixture-token")
            write_secret(selected.control_plane_key, "fixture-key")
            fake_client = Path(tmp) / "tunnel-client"
            fake_client.touch()
            with (
                patch.object(cli, "TUNNEL_BIN", fake_client),
                patch.object(
                    cli.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run,
            ):
                self.assertEqual(
                    cli._tunnel_command(SimpleNamespace(tunnel_action="run")), 0
                )
            command = run.call_args.args[0]
            self.assertEqual(command[1:3], ["run", "--profile"])
            self.assertIn(f"file:{selected.control_plane_key}", command)
            self.assertIn("--health.listen-addr", command)
            self.assertIn("127.0.0.1:0", command)
            self.assertIn(str(selected.tunnel_health_url), command)

    def test_foreground_tunnel_run_passes_mcp_authorization_from_private_file(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["tunnel_id"] = "tunnel-fixture"
            save_config(config, selected)
            write_secret(selected.mcp_token, "fixture-token")
            write_secret(selected.control_plane_key, "fixture-key")
            fake_client = Path(tmp) / "tunnel-client"
            fake_client.touch()
            with (
                patch.object(cli, "TUNNEL_BIN", fake_client),
                patch.object(
                    cli.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run,
            ):
                self.assertEqual(
                    cli._tunnel_command(SimpleNamespace(tunnel_action="run")), 0
                )
            command = run.call_args.args[0]
            header_index = command.index("--mcp.extra-headers")
            self.assertEqual(
                command[header_index + 1],
                f"Authorization: file:{selected.mcp_authorization_header}",
            )
            self.assertEqual(
                selected.mcp_authorization_header.read_text(encoding="utf-8"),
                "Bearer fixture-token\n",
            )
            self.assertEqual(
                selected.mcp_authorization_header.stat().st_mode & 0o777, 0o600
            )

    def test_status_accepts_tunnel_health_endpoint_shape(self) -> None:
        healthy, ready = cli._tunnel_health_flags(
            {"healthz": {"ok": True}, "readyz": {"ok": True}}
        )
        self.assertTrue(healthy)
        self.assertTrue(ready)

    def test_service_units_use_config_launcher_not_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root = root / "config with spaces"
            home = root / "home with spaces"
            workspace = root / "workspace with spaces"
            workspace.mkdir()
            with patch.dict(
                os.environ,
                {"DEVMCP_CONFIG_DIR": str(config_root), "HOME": str(home)},
                clear=False,
            ):
                selected = paths()
                config = load_config(selected, workspace=str(workspace))
                config["profile"] = "power"
                config["mcp_port"] = 48999
                save_config(config, selected)
                completed = subprocess.CompletedProcess([], 0, "", "")
                with patch.object(cli, "_systemctl", return_value=completed):
                    self.assertEqual(cli._service_install(SimpleNamespace()), 0)
                unit_dir = home / ".config" / "systemd" / "user"
                mcp = (unit_dir / cli.MCP_SERVICE).read_text(encoding="utf-8")
                tunnel = (unit_dir / cli.TUNNEL_SERVICE).read_text(encoding="utf-8")
                self.assertIn("DEVMCP_CONFIG_DIR=", mcp)
                self.assertIn("-m apps.devmcp.cli serve", mcp)
                self.assertNotIn(str(workspace), mcp)
                self.assertNotIn("--policy-profile", mcp)
                self.assertIn("-m apps.devmcp.cli tunnel run", tunnel)
                self.assertNotIn("runtimes connect", tunnel)
                self.assertIn(f"After={cli.MCP_SERVICE}", tunnel)
                self.assertNotIn(f"Requires={cli.MCP_SERVICE}", tunnel)

    def test_serve_reads_the_current_config_on_every_start(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            config["profile"] = "balanced"
            config["mcp_port"] = 47157
            save_config(config, selected)
            with patch("coding_tools_mcp.server.main", return_value=0) as server_main:
                self.assertEqual(cli._serve(SimpleNamespace()), 0)
                config["profile"] = "power"
                config["mcp_port"] = 48999
                save_config(config, selected)
                self.assertEqual(cli._serve(SimpleNamespace()), 0)
            first, second = (list(call.args[0]) for call in server_main.call_args_list)
            self.assertEqual(first[first.index("--policy-profile") + 1], "balanced")
            self.assertEqual(second[second.index("--policy-profile") + 1], "power")
            self.assertEqual(second[second.index("--port") + 1], "48999")
