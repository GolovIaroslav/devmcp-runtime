from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from apps.devmcp import cli


class RuntimeStatusTests(unittest.TestCase):
    def test_build_status_does_not_claim_configured_bwrap_sandbox(self) -> None:
        selected = SimpleNamespace(approvals_db="unused")
        config = {
            "execution_mode": "build",
            "sandbox_backend": "bwrap",
            "workspace": "/tmp/workspace",
        }
        out = io.StringIO()
        with (
            patch.object(cli, "_config", return_value=(selected, config)),
            patch.object(cli, "_tunnel_status", return_value={}),
            patch.object(cli, "_active", return_value=False),
            patch.object(cli, "_mcp_health", return_value=True),
            patch.object(
                cli,
                "secret_status",
                return_value={
                    "mcp_token_configured": False,
                    "control_plane_key_configured": False,
                },
            ),
            redirect_stdout(out),
        ):
            self.assertEqual(cli._status(argparse.Namespace()), 0)

        rendered = out.getvalue()
        self.assertIn("execution_mode: build", rendered)
        self.assertIn("effective_access: full-access", rendered)
        self.assertIn("effective_executor: host", rendered)
        self.assertIn("sandbox: none", rendered)
        self.assertNotIn("sandbox: bwrap", rendered)


if __name__ == "__main__":
    unittest.main()
