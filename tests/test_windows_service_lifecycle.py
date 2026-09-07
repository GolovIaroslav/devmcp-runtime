from __future__ import annotations

import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.devmcp import cli
from coding_tools_mcp import server as server_module
from coding_tools_mcp.config import (
    generate_mcp_token,
    load_config,
    paths,
    save_config,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipUnless(os.name == "nt", "requires Windows")
class WindowsServiceLifecycleTests(unittest.TestCase):
    def test_stale_pid_is_rejected_when_process_identity_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "service.pid"
            pid_file.write_text(str(os.getpid()), encoding="utf-8")

            self.assertIsNone(
                cli._read_pid(pid_file, command_marker="definitely-not-this-process")
            )
            self.assertFalse(pid_file.exists())

    def test_start_is_idempotent_and_repairs_stale_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root = root / "config"
            workspace = root / "workspace"
            workspace.mkdir()
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                selected = paths()
                config = load_config(selected, workspace=str(workspace))
                config["mcp_host"] = "127.0.0.1"
                config["mcp_port"] = free_port()
                config["sandbox_backend"] = "none"
                config["tunnel_id"] = ""
                save_config(config, selected)
                generate_mcp_token(selected)

                try:
                    self.assertEqual(cli._windows_service_action("start"), 0)
                    first_listener = cli._windows_listener_pid(config["mcp_port"])
                    self.assertIsNotNone(first_listener)
                    assert first_listener is not None
                    self.assertTrue(
                        cli._pid_matches_command(first_listener, cli.MCP_PROCESS_MARKER)
                    )

                    self.assertEqual(cli._windows_service_action("start"), 0)
                    self.assertEqual(
                        cli._windows_listener_pid(config["mcp_port"]), first_listener
                    )

                    pid_file = selected.root / "run" / "mcp.pid"
                    pid_file.write_text("999999", encoding="utf-8")
                    self.assertEqual(cli._windows_service_action("start"), 0)
                    self.assertEqual(
                        cli._windows_listener_pid(config["mcp_port"]), first_listener
                    )
                    repaired = int(pid_file.read_text(encoding="utf-8").strip())
                    self.assertEqual(repaired, first_listener)
                finally:
                    self.assertEqual(cli._windows_service_action("stop"), 0)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if cli._windows_listener_pid(config["mcp_port"]) is None:
                            break
                        time.sleep(0.1)
                    self.assertIsNone(cli._windows_listener_pid(config["mcp_port"]))

    def test_windows_http_job_poll_is_bounded_without_changing_stdio(self) -> None:
        self.assertEqual(
            server_module.job_status_wait_limit_ms("http"),
            server_module.WINDOWS_HTTP_JOB_STATUS_WAIT_MAX_MS,
        )
        self.assertEqual(
            server_module.job_status_wait_limit_ms("stdio"),
            server_module.JOB_STATUS_MAX_WAIT_MS,
        )

    def test_start_refuses_foreign_listener_without_terminating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                port = int(listener.getsockname()[1])
                with patch.dict(
                    os.environ,
                    {"DEVMCP_CONFIG_DIR": str(root / "config")},
                    clear=False,
                ):
                    selected = paths()
                    config = load_config(selected, workspace=str(workspace))
                    config["mcp_host"] = "127.0.0.1"
                    config["mcp_port"] = port
                    save_config(config, selected)
                    pid_file = selected.root / "run" / "mcp.pid"
                    pid_file.parent.mkdir(parents=True, exist_ok=True)
                    with patch.object(cli, "_mcp_health", return_value=False):
                        self.assertEqual(
                            cli._windows_start_mcp(
                                selected,
                                config,
                                pid_file,
                                selected.root / "logs" / "mcp.log",
                            ),
                            1,
                        )
                    self.assertFalse(pid_file.exists())
                    self.assertEqual(listener.getsockname()[1], port)


if __name__ == "__main__":
    unittest.main()
