from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apps.devmcp import cli
from coding_tools_mcp.config import load_config, paths, save_config, write_secret


class AuthSeparationTests(unittest.TestCase):
    def test_serve_never_authorizes_control_plane_key_as_mcp_bearer(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False),
        ):
            selected = paths()
            config = load_config(selected, workspace=tmp)
            save_config(config, selected)
            write_secret(selected.mcp_token, "mcp-primary-secret")
            write_secret(selected.control_plane_key, "control-plane-secret")

            fake_os = SimpleNamespace(name="posix", environ=os.environ)
            with (
                patch.object(cli, "os", fake_os),
                patch(
                    "coding_tools_mcp.stateful_server.main", return_value=0
                ) as server_main,
            ):
                self.assertEqual(cli._serve(SimpleNamespace()), 0)

            server_args = list(server_main.call_args.args[0])
            self.assertIn("--auth-token-file", server_args)
            self.assertEqual(
                server_args[server_args.index("--auth-token-file") + 1],
                str(selected.mcp_token),
            )
            self.assertNotIn("--extra-auth-token", server_args)
            self.assertNotIn("--extra-auth-token-file", server_args)
            self.assertNotIn(str(selected.control_plane_key), server_args)

    def test_linux_health_gate_requires_active_systemd_unit_and_mcp_health(
        self,
    ) -> None:
        fake_os = SimpleNamespace(name="posix")
        fake_selected = object()
        fake_config: dict[str, object] = {}

        with (
            patch.object(cli, "os", fake_os),
            patch.object(cli, "_config", return_value=(fake_selected, fake_config)),
            patch.object(cli, "_active", return_value=False),
            patch.object(cli, "_mcp_health", return_value=True),
            patch.object(cli.time, "sleep"),
            patch.object(cli.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
        ):
            self.assertFalse(cli._wait_for_mcp_health(timeout_seconds=0.5))

        with (
            patch.object(cli, "os", fake_os),
            patch.object(cli, "_config", return_value=(fake_selected, fake_config)),
            patch.object(cli, "_active", return_value=True),
            patch.object(cli, "_mcp_health", return_value=True),
        ):
            self.assertTrue(cli._wait_for_mcp_health(timeout_seconds=0.5))


if __name__ == "__main__":
    unittest.main()
