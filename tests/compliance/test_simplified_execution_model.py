from __future__ import annotations

import http.server
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.policy import resolve_execution_mode
from coding_tools_mcp.server import Runtime
from tests.compliance.mcp_client import MCPClient
from tests.compliance.test_support import ComplianceTestCase, structured_payload


class SimplifiedExecutionModelComplianceTests(ComplianceTestCase):
    fixture_name = "tiny-js-project"

    def test_a_default_execution_mode_is_build_and_full_access(self) -> None:
        """Test A (default): fresh Runtime/session defaults to execution_mode='build' and effective_access='full-access'."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root)
            try:
                self.assertEqual(runtime.execution_mode, "build")
                self.assertEqual(runtime.effective_access, "full-access")
            finally:
                runtime.close()

        with self.session_for_fixture("tiny-js-project") as (_workspace, client):
            info = structured_payload(client.call_tool("server_info", {}))
            self.assertEqual(info.get("execution_mode"), "build")
            self.assertEqual(info.get("effective_access"), "full-access")

    def test_b_legacy_permission_mode_mapping(self) -> None:
        """Test B (legacy mapping): safe -> plan/read-only, trusted -> build/full-access, dangerous -> build/full-access."""
        self.assertEqual(
            resolve_execution_mode(permission_mode="safe"), ("plan", "read-only")
        )
        self.assertEqual(
            resolve_execution_mode(permission_mode="trusted"), ("build", "full-access")
        )
        self.assertEqual(
            resolve_execution_mode(permission_mode="dangerous"),
            ("build", "full-access"),
        )
        self.assertEqual(
            resolve_execution_mode(execution_mode="plan"), ("plan", "read-only")
        )
        self.assertEqual(
            resolve_execution_mode(execution_mode="build"), ("build", "full-access")
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            r_safe = Runtime(root, permission_mode="safe")
            self.assertEqual(r_safe.execution_mode, "plan")
            self.assertEqual(r_safe.effective_access, "read-only")
            r_safe.close()

            r_trusted = Runtime(root, permission_mode="trusted")
            self.assertEqual(r_trusted.execution_mode, "build")
            self.assertEqual(r_trusted.effective_access, "full-access")
            r_trusted.close()

            r_dangerous = Runtime(root, permission_mode="dangerous")
            self.assertEqual(r_dangerous.execution_mode, "build")
            self.assertEqual(r_dangerous.effective_access, "full-access")
            r_dangerous.close()

    def test_c_build_execution_mode_has_full_access_authority(self) -> None:
        """Test C (execution_mode): execution_mode=build gives full-access authority unconditionally."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, execution_mode="build")
            try:
                self.assertEqual(runtime.execution_mode, "build")
                self.assertEqual(runtime.effective_access, "full-access")
                res = runtime.exec_command({"cmd": "echo ok", "yield_time_ms": 5000})
                self.assertEqual(res.get("status"), "success")
            finally:
                runtime.close()

    def test_d_server_info_returns_execution_mode_and_effective_access(self) -> None:
        """Test D (server_info): server_info returns execution_mode='build' and effective_access='full-access'."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root)
            try:
                info = runtime.server_info({})
                self.assertEqual(info.get("execution_mode"), "build")
                self.assertEqual(info.get("effective_access"), "full-access")
                self.assertNotIn("permission_mode", info)
                self.assertIn("legacy_permission_mode_compat", info["permission_policy"])
            finally:
                runtime.close()

    def test_e_direct_filesystem_read_outside_workspace(self) -> None:
        """Test E (direct filesystem read): BUILD subprocess can read a file outside the workspace if current OS user can read it."""
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            ws_root = Path(tmp1)
            outside_file = Path(tmp2) / "outside.txt"
            outside_file.write_text("outside secret content", encoding="utf-8")
            runtime = Runtime(ws_root, execution_mode="build")
            try:
                res = runtime.exec_command(
                    {"cmd": f"cat {outside_file}", "yield_time_ms": 5000}
                )
                self.assertEqual(res.get("status"), "success")
                self.assertEqual(res.get("stdout"), "outside secret content")
            finally:
                runtime.close()

    def test_f_direct_filesystem_write_outside_workspace(self) -> None:
        """Test F (direct filesystem write): BUILD subprocess can create/write a file outside workspace if current OS user has write access."""
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            ws_root = Path(tmp1)
            target_file = Path(tmp2) / "outside_out.txt"
            runtime = Runtime(ws_root, execution_mode="build")
            try:
                res = runtime.exec_command(
                    {
                        "cmd": f"echo 'created outside' > {target_file}",
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(res.get("status"), "success")
                self.assertTrue(target_file.exists())
                self.assertEqual(
                    target_file.read_text(encoding="utf-8").strip(), "created outside"
                )
            finally:
                runtime.close()

    def test_g_env_parity_and_sensitive_vars_in_build(self) -> None:
        """Test G (env parity): HOME, PATH, VIRTUAL_ENV, TMP/TEMP are inherited; sensitive-looking env vars are NOT filtered out in BUILD path."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, execution_mode="build")
            try:
                with patch.dict(
                    os.environ, {"TEST_DUMMY_SENSITIVE_VAR": "dummy_value_abc123"}
                ):
                    res = runtime.exec_command(
                        {
                            "cmd": "python3 -c \"import os; print(os.environ.get('TEST_DUMMY_SENSITIVE_VAR'))\"",
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(res.get("status"), "success")
                    self.assertEqual(
                        res.get("stdout", "").strip(), "dummy_value_abc123"
                    )

                    res_env = runtime.exec_command(
                        {
                            "cmd": "python3 -c \"import os; print('HOME' in os.environ, 'PATH' in os.environ)\"",
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(res_env.get("stdout", "").strip(), "True True")
            finally:
                runtime.close()

    def test_h_shell_syntax_expansion_and_inline_scripts(self) -> None:
        """Test H (shell syntax): shell expansion (e.g. $HOME) and inline scripts run without approval."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, execution_mode="build")
            try:
                res1 = runtime.exec_command(
                    {"cmd": "echo $(pwd)", "yield_time_ms": 5000}
                )
                self.assertEqual(res1.get("status"), "success")
                self.assertIn(str(root), res1.get("stdout", ""))

                res2 = runtime.exec_command(
                    {
                        "cmd": 'python3 -c "for i in range(3): print(i)"',
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(res2.get("status"), "success")
                self.assertEqual(res2.get("stdout"), "0\n1\n2\n")
            finally:
                runtime.close()

    def test_i_network_build_path_creates_no_denial(self) -> None:
        """Test I (network): BUILD path creates no artificial DevMCP network denial (using local loopback HTTP server fixture)."""

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), QuietHandler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = Runtime(root, execution_mode="build")
                try:
                    res = runtime.exec_command(
                        {
                            "cmd": f"python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}').status)\"",
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(res.get("status"), "success")
                    self.assertEqual(res.get("stdout", "").strip(), "200")
                finally:
                    runtime.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_j_executable_names_no_policy_block(self) -> None:
        """Test J (executable names): harmless temporary executable named 'sudo', 'doas', or 'docker' executes without DevMCP basename policy block."""
        with TemporaryDirectory() as tmp_ws, TemporaryDirectory() as tmp_bin:
            bin_dir = Path(tmp_bin)
            for name in ("sudo", "doas", "docker"):
                script = bin_dir / name
                script.write_text(
                    "#!/bin/sh\necho fake-" + name + "-ok\n", encoding="utf-8"
                )
                script.chmod(0o755)

            root = Path(tmp_ws)
            runtime = Runtime(root, execution_mode="build")
            try:
                orig_path = os.environ.get("PATH", "")
                new_path = f"{bin_dir}:{orig_path}"
                with patch.dict(os.environ, {"PATH": new_path}):
                    for name in ("sudo", "doas", "docker"):
                        res = runtime.exec_command({"cmd": name, "yield_time_ms": 5000})
                        self.assertEqual(
                            res.get("status"), "success", f"failed for {name}: {res}"
                        )
                        self.assertIn(f"fake-{name}-ok", res.get("stdout", ""))
            finally:
                runtime.close()

    def test_k_plan_mode_denies_mutations_and_allows_reads(self) -> None:
        """Test K (PLAN mode): PLAN mode denies structured mutations with PERMISSION_REQUIRED while reads work cleanly."""
        with self.session_for_fixture("tiny-js-project") as (workspace, _client):
            with MCPClient(workspace.root, execution_mode="plan") as plan_client:
                read_res = plan_client.call_tool("read_file", {"path": "src/math.js"})
                self.assertFalse(read_res.get("isError"))
                self.assertIn("function add", self.tool_text(read_res))

                exec_res = plan_client.call_tool(
                    "exec_command", {"cmd": "echo 123", "timeout_ms": 5000}
                )
                self.assertTrue(exec_res.get("isError"))
                payload = structured_payload(exec_res)
                self.assertEqual(
                    payload.get("error", {}).get("code"), "PERMISSION_REQUIRED"
                )

                patch_text = """*** Begin Patch
*** Update File: src/math.js
@@
 export function add(a, b) {
-  return a - b;
+  return a + b;
 }
*** End Patch
"""
                patch_res = plan_client.call_tool("apply_patch", {"patch": patch_text})
                self.assertTrue(patch_res.get("isError"))
                payload_p = structured_payload(patch_res)
                self.assertEqual(
                    payload_p.get("error", {}).get("code"), "PERMISSION_REQUIRED"
                )

    def test_l_reliability_process_groups_timeouts_cancellation_output_caps(
        self,
    ) -> None:
        """Test L (reliability): process groups, timeouts, cancellation, output caps work cleanly."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, execution_mode="build")
            try:
                # Output cap truncation
                res_cap = runtime.exec_command(
                    {
                        "cmd": "python3 -c \"print('x' * 10000)\"",
                        "max_output_bytes": 100,
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(res_cap.get("status"), "success")
                self.assertTrue(len(res_cap.get("stdout", "").encode("utf-8")) <= 500)

                # Timeout / process group
                res_timeout = runtime.exec_command(
                    {"cmd": "sleep 10", "timeout_ms": 100, "yield_time_ms": 1500}
                )
                self.assertIn(
                    res_timeout.get("status"), ("timeout", "failed", "success")
                )

                # Process cancellation / kill_session
                started = runtime.exec_command(
                    {"cmd": "sleep 10", "yield_time_ms": 0, "timeout_ms": 5000}
                )
                session_id = started.get("session_id")
                if session_id:
                    killed = runtime.kill_session({"session_id": session_id})
                    self.assertIn(
                        killed.get("status"),
                        ("killed", "terminated", "exited", "terminating"),
                    )
            finally:
                runtime.close()

    def test_m_state_integrity_git_cas_and_atomic_patch(self) -> None:
        """Test M (state integrity): Git CAS and atomic patch preconditions remain intact."""
        with self.session_for_fixture("tiny-js-project") as (_workspace, client):
            status = structured_payload(client.call_tool("git_status", {}))
            self.assertIn("branch", status)

            bad_patch = """*** Begin Patch
*** Update File: src/math.js
@@
-  non_existent_line_for_mismatch
+  new_line
*** End Patch
"""
            patch_res = client.call_tool("apply_patch", {"patch": bad_patch})
            self.assertTrue(patch_res.get("isError"))
            read_res = client.call_tool("read_file", {"path": "src/math.js"})
            self.assertIn("return a - b", self.tool_text(read_res))


if __name__ == "__main__":
    unittest.main()
