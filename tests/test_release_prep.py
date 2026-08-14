from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.devmcp import cli
from apps.devmcp.ui import UIState
from coding_tools_mcp.config import (
    load_config,
    paths,
    redact_config,
    save_config,
    write_secret,
)
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime, run_http


class ReleaseConfigTests(unittest.TestCase):
    def test_new_config_is_valid_and_secret_status_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
                selected = paths()
                config = load_config(selected, workspace=tmp)
                self.assertEqual(selected.root.stat().st_mode & 0o777, 0o700)
                self.assertNotIn("super-secret-value", str(redact_config(config)))
                save_config(config, selected)
                self.assertTrue(selected.config_file.is_file())

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
            runtime = Runtime(workspace, execution_mode="build")
            try:
                patch_text = (
                    "*** Begin Patch\n*** Delete File: remove-me.txt\n*** End Patch"
                )
                applied = runtime.apply_patch({"patch": patch_text})
                self.assertTrue(applied["clean"])
                self.assertFalse(target.exists())
            finally:
                runtime.close()

    def test_custom_policy_rules_drive_delete_without_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "remove-me.txt"
            target.write_text("custom policy\n", encoding="utf-8")
            runtime = Runtime(
                workspace,
                execution_mode="build",
            )
            try:
                result = runtime.apply_patch(
                    {
                        "patch": "*** Begin Patch\n*** Delete File: remove-me.txt\n*** End Patch"
                    }
                )
                self.assertTrue(result["clean"])
                self.assertFalse(target.exists())
            finally:
                runtime.close()

    def test_active_profile_controls_exec_and_network_not_legacy_safe_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), execution_mode="build")
            try:
                with patch.object(
                    runtime, "_execute_command_legacy", return_value={"reached": True}
                ):
                    self.assertEqual(
                        runtime.exec_command({"cmd": "curl https://example.invalid"}),
                        {"reached": True},
                    )
            finally:
                runtime.close()

    def test_autonomous_profile_runs_arbitrary_exec_without_approval_but_not_sudo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), execution_mode="build")
            try:
                with patch.object(
                    runtime, "_execute_command_legacy", return_value={"reached": True}
                ):
                    self.assertEqual(
                        runtime.exec_command({"cmd": "printf autonomous"}),
                        {"reached": True},
                    )
            finally:
                runtime.close()

    def test_autonomous_profile_runs_host_diagnostics_and_schedules_restart(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0, "operator-ok\n", "")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), execution_mode="build")
            try:
                with patch(
                    "coding_tools_mcp.server.subprocess.run", return_value=completed
                ) as run:
                    status = runtime.service_status({})
                    self.assertEqual(status["stdout"], "operator-ok\n")
                    self.assertIn("status", run.call_args.args[0])

                cli = Path(tmp) / "probe-cli"
                cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                cli.chmod(0o755)
                with patch(
                    "coding_tools_mcp.server.subprocess.run", return_value=completed
                ) as run:
                    probe = runtime.host_cli_probe(
                        {"path": "probe-cli", "probe": "help"}
                    )
                    self.assertEqual(probe["stdout"], "operator-ok\n")
                    self.assertEqual(run.call_args.args[0], [str(cli), "--help"])
                    self.assertEqual(run.call_args.kwargs["cwd"], str(Path(tmp)))
                    self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])

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

    def test_autonomous_profile_schedules_service_update_from_synced_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            source = projects / "devmcp-runtime"
            (source / "apps" / "devmcp").mkdir(parents=True)
            (source / "pyproject.toml").write_text(
                '[project]\nname = "devmcp-runtime"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            (source / "apps" / "devmcp" / "cli.py").write_text(
                "# fixture\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-qb", "main"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Test"],
                cwd=source,
                check=True,
            )
            subprocess.run(["git", "add", "-A"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", head],
                cwd=source,
                check=True,
            )
            runtime = Runtime(
                source,
                execution_mode="build",
                project_roots=[projects],
            )
            try:
                with patch.object(
                    runtime,
                    "_schedule_devmcp_update",
                    return_value={"status": "scheduled"},
                ) as schedule:
                    result = runtime.service_update(
                        {"source_project": "devmcp-runtime"}
                    )
                self.assertEqual(result["status"], "scheduled")
                schedule.assert_called_once_with(
                    source.resolve(), head, development_mode=False
                )
            finally:
                runtime.close()

    def test_service_update_development_mode_allows_clean_feature_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            source = projects / "devmcp-runtime"
            (source / "apps" / "devmcp").mkdir(parents=True)
            (source / "pyproject.toml").write_text(
                '[project]\nname = "devmcp-runtime"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            (source / "apps" / "devmcp" / "cli.py").write_text(
                "# fixture\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "init", "-qb", "feature/self-host"], cwd=source, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Test"],
                cwd=source,
                check=True,
            )
            subprocess.run(["git", "add", "-A"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            runtime = Runtime(
                source,
                execution_mode="build",
                project_roots=[projects],
            )
            try:
                with self.assertRaises(ToolFailure):
                    runtime.service_update({"source_project": "devmcp-runtime"})
                with patch.object(
                    runtime,
                    "_schedule_devmcp_update",
                    return_value={"status": "scheduled"},
                ) as schedule:
                    result = runtime.service_update(
                        {
                            "source_project": "devmcp-runtime",
                            "development_mode": True,
                        }
                    )
                self.assertEqual(result["status"], "scheduled")
                schedule.assert_called_once_with(
                    source.resolve(), head, development_mode=True
                )
            finally:
                runtime.close()

    def test_service_update_rejects_dirty_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "devmcp-runtime"
            (source / "apps" / "devmcp").mkdir(parents=True)
            tracked = source / "apps" / "devmcp" / "cli.py"
            tracked.write_text("# fixture\n", encoding="utf-8")
            (source / "pyproject.toml").write_text(
                '[project]\nname = "devmcp-runtime"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-qb", "main"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Test"],
                cwd=source,
                check=True,
            )
            subprocess.run(["git", "add", "-A"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", head],
                cwd=source,
                check=True,
            )
            tracked.write_text("# dirty\n", encoding="utf-8")
            runtime = Runtime(
                source,
                execution_mode="build",
                project_roots=[root],
            )
            try:
                with self.assertRaises(ToolFailure) as denied:
                    runtime.service_update({})
                self.assertEqual(denied.exception.code, "INVALID_STATE")
            finally:
                runtime.close()

    def test_cli_service_update_revalidates_sha_and_runs_install_restart_sequence(
        self,
    ) -> None:
        expected_sha = "a" * 40
        completed = [
            subprocess.CompletedProcess([], 0, expected_sha + "\n", ""),
            subprocess.CompletedProcess([], 0, "main\n", ""),
            subprocess.CompletedProcess(
                [], 0, "https://github.com/example/devmcp-runtime.git\n", ""
            ),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "installed\n", ""),
            subprocess.CompletedProcess([], 0, "units\n", ""),
            subprocess.CompletedProcess([], 0, "restarted\n", ""),
        ]
        source = Path("/tmp/devmcp-runtime-source")
        with (
            patch.object(cli, "_validated_runtime_source", return_value=source),
            patch.object(cli, "_config", return_value=(object(), {})),
            patch.object(
                cli,
                "_mcp_runtime_state",
                return_value={"service": {"installed_sha": expected_sha}},
            ),
            patch.object(cli, "save_config") as save_config,
            patch.object(
                cli.shutil,
                "which",
                side_effect=lambda name: {
                    "git": "/usr/bin/git",
                    "uv": "/usr/bin/uv",
                }.get(name),
            ),
            patch.object(cli.subprocess, "run", side_effect=completed) as run,
        ):
            result = cli._service_update(
                SimpleNamespace(
                    source=str(source),
                    expected_sha=expected_sha,
                    development_mode=False,
                )
            )
        self.assertEqual(result, 0)
        save_config.assert_called_once()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-2:], ["rev-parse", "HEAD"])
        self.assertEqual(commands[1][-2:], ["branch", "--show-current"])
        self.assertEqual(commands[2][-3:], ["remote", "get-url", "origin"])
        self.assertEqual(commands[5][:4], ["/usr/bin/uv", "tool", "install", "--force"])
        self.assertEqual(commands[6][-2:], ["service", "install"])
        self.assertEqual(commands[7][-1], "restart")

    def test_cli_service_update_rejects_mismatched_running_sha(self) -> None:
        expected_sha = "a" * 40
        completed = [
            subprocess.CompletedProcess([], 0, expected_sha + "\n", ""),
            subprocess.CompletedProcess([], 0, "main\n", ""),
            subprocess.CompletedProcess(
                [], 0, "https://github.com/example/repo.git\n", ""
            ),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "installed\n", ""),
            subprocess.CompletedProcess([], 0, "units\n", ""),
            subprocess.CompletedProcess([], 0, "restarted\n", ""),
        ]
        source = Path("/tmp/devmcp-runtime-source")
        with (
            patch.object(cli, "_validated_runtime_source", return_value=source),
            patch.object(cli, "_config", return_value=(object(), {})),
            patch.object(
                cli,
                "_mcp_runtime_state",
                return_value={"service": {"installed_sha": "b" * 40}},
            ),
            patch.object(cli, "save_config"),
            patch.object(
                cli.shutil,
                "which",
                side_effect=lambda name: {
                    "git": "/usr/bin/git",
                    "uv": "/usr/bin/uv",
                }.get(name),
            ),
            patch.object(cli.subprocess, "run", side_effect=completed),
        ):
            result = cli._service_update(
                SimpleNamespace(
                    source=str(source),
                    expected_sha=expected_sha,
                    development_mode=False,
                )
            )
        self.assertEqual(result, 1)

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

    def test_custom_ask_requires_and_consumes_an_approval_before_execution(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime = Runtime(
                workspace,
                execution_mode="build",
            )
            try:
                command = "printf custom-policy-approved"
                completed = runtime.exec_command({"cmd": command})
                self.assertTrue(completed["ok"])
                self.assertEqual(completed["status"], "success")
                self.assertTrue(completed["command_success"])
                self.assertIn("custom-policy-approved", completed["stdout"])
            finally:
                runtime.close()

    def test_custom_exec_deny_blocks_behavior_even_with_trusted_legacy_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                execution_mode="plan",
            )
            try:
                with self.assertRaises(ToolFailure) as raised:
                    runtime.exec_command({"cmd": "echo blocked"})
                self.assertEqual(raised.exception.code, "PERMISSION_REQUIRED")
            finally:
                runtime.close()

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
            self.assertIn("--execution-mode", first)
            self.assertEqual(second[second.index("--port") + 1], "48999")
