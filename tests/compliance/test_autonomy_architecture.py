from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.executors import ExecutionRequirements, ExecutorRegistry
from coding_tools_mcp.policy import legacy_profile
from coding_tools_mcp.sandbox import (
    ExecutionSandbox,
    SandboxBackend,
    _linux_effective_capabilities_dropped,
    _linux_mountinfo_has_private_tmp,
    detect_sandbox_backend,
    inherited_sandbox_backend,
    legacy_devmcp_parent_sandbox_backend,
)
from coding_tools_mcp.server import Runtime, tool_definition
from coding_tools_mcp.system_view import forbidden_system_paths, readonly_system_paths
from coding_tools_mcp.transactions import ExecutionTransaction


class AutonomyArchitectureTests(unittest.TestCase):
    def _runtime(self, root: Path, **kwargs: object) -> Runtime:
        kwargs.setdefault("grantable_roots", [root.parent])
        return Runtime(
            root,
            policy_profile="autonomous",
            sandbox_backend="unsafe",
            project_roots=[root.parent],
            **kwargs,
        )

    def test_explicit_profile_is_authoritative_over_legacy_permission_mode(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(
                root,
                execution_mode="build",
                policy_profile="autonomous",
                sandbox_backend="unsafe",
            )
            try:
                result = runtime.exec_command(
                    {
                        "cmd": "printf '%s' \"$(printf profile-wins)\"",
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["stdout"], "profile-wins")
            finally:
                runtime.close()

    def test_permission_profile_matrix_and_legacy_adapter_are_deterministic(
        self,
    ) -> None:
        self.assertEqual(legacy_profile("safe"), "safe")
        self.assertEqual(legacy_profile("trusted"), "power")
        self.assertEqual(legacy_profile("dangerous"), "autonomous")

    def test_legacy_dangerous_mode_is_full_access_for_shell_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SECRET=x\n", encoding="utf-8")
            runtime = Runtime(
                root,
                permission_mode="dangerous",
                sandbox_backend="unsafe",
            )
            try:
                self.assertTrue(runtime.dangerously_skip_all_permissions)
                executed = runtime.exec_command(
                    {"cmd": "cat .env", "timeout_ms": 5000, "yield_time_ms": 5000}
                )
                self.assertEqual(executed.get("exit_code"), 0, executed)
                self.assertEqual(executed.get("stdout"), "SECRET=x\n")
                res = runtime.read_file({"path": str(root / ".env")})
                self.assertEqual(res.get("content"), "SECRET=x\n")
            finally:
                runtime.close()

    def test_absolute_paths_inside_workspace_are_allowed_but_escapes_are_not(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            inside = root / "src" / "a.txt"
            inside.parent.mkdir()
            inside.write_text("inside\n", encoding="utf-8")
            runtime = self._runtime(root)
            try:
                result = runtime.read_file({"path": str(inside)})
                self.assertEqual(result["content"], "inside\n")
                self.assertEqual(
                    runtime.resolve_for_write(str(root / "new.txt")).path,
                    root / "new.txt",
                )
            finally:
                runtime.close()

    def test_scoped_additional_root_grants_are_owner_local_and_once_is_consumed(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            sibling = base / "lib"
            repo.mkdir()
            sibling.mkdir()
            target = sibling / "lib.txt"
            target.write_text("library\n", encoding="utf-8")
            runtime = self._runtime(repo)
            try:
                lease = runtime.grant_root(
                    {"path": str(sibling), "access": "read", "scope": "once"}
                )
                self.assertTrue(str(lease["lease_id"]).startswith("lease_"))
                first = runtime.call_tool("read_file", {"path": str(target)})
                self.assertFalse(first["isError"])
                self.assertEqual(first["structuredContent"]["content"], "library\n")
                consumed = runtime.call_tool("read_file", {"path": str(target)})
                self.assertTrue(consumed["isError"])
                self.assertEqual(
                    consumed["structuredContent"]["error"]["code"],
                    "PATH_OUTSIDE_WORKSPACE",
                )

                session_lease = runtime.grant_root(
                    {"path": str(sibling), "access": "write", "scope": "session"}
                )
                self.assertEqual(session_lease["scope"], "session")
                resolved = runtime.resolve_for_write(str(sibling / "generated.txt"))
                self.assertEqual(resolved.path, sibling / "generated.txt")
                roots = runtime.workspace_info({})
                self.assertIn(str(sibling.resolve()), roots["readable_roots"])
                self.assertIn(str(sibling.resolve()), roots["writable_roots"])
            finally:
                runtime.close()

    def test_project_discovery_roots_are_not_implicit_grant_authority(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            sibling = base / "sibling"
            repo.mkdir()
            sibling.mkdir()
            runtime = Runtime(
                repo,
                policy_profile="autonomous",
                sandbox_backend="unsafe",
                project_roots=[base],
            )
            try:
                self.assertEqual(runtime.workspace_info({})["grantable_roots"], [])
                with self.assertRaises(ToolFailure) as denied:
                    runtime.grant_root(
                        {"path": str(sibling), "access": "read", "scope": "session"}
                    )
                self.assertEqual(denied.exception.code, "ACCESS_DENIED")
            finally:
                runtime.close()

    def test_task_scoped_root_grant_is_portable_and_revoked_on_scope_end(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            sibling = base / "lib"
            repo.mkdir()
            sibling.mkdir()
            target = sibling / "lib.txt"
            target.write_text("task-library\n", encoding="utf-8")
            runtime = self._runtime(repo)
            try:
                granted = runtime.call_tool(
                    "grant_root",
                    {"path": str(sibling), "access": "read", "scope": "task"},
                )
                self.assertFalse(granted["isError"])
                payload = granted["structuredContent"]
                task_scope_id = payload["task_scope_id"]
                self.assertTrue(task_scope_id.startswith("task_"))

                outside_scope = runtime.call_tool("read_file", {"path": str(target)})
                self.assertTrue(outside_scope["isError"])
                within_scope = runtime.call_tool(
                    "read_file",
                    {"path": str(target), "task_scope_id": task_scope_id},
                )
                self.assertFalse(within_scope["isError"])
                self.assertEqual(
                    within_scope["structuredContent"]["content"], "task-library\n"
                )

                ended = runtime.call_tool(
                    "end_task_scope", {"task_scope_id": task_scope_id}
                )
                self.assertFalse(ended["isError"])
                self.assertEqual(ended["structuredContent"]["revoked_leases"], 1)
                after_end = runtime.call_tool(
                    "read_file",
                    {"path": str(target), "task_scope_id": task_scope_id},
                )
                self.assertTrue(after_end["isError"])
                self.assertEqual(
                    after_end["structuredContent"]["error"]["code"],
                    "PATH_OUTSIDE_WORKSPACE",
                )
            finally:
                runtime.close()

    def test_exec_argv_is_first_class_and_does_not_invoke_shell(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime(root)
            try:
                result = runtime.exec_argv(
                    {
                        "argv": ["printf", "%s", "literal; printf not-shell"],
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["stdout"], "literal; printf not-shell")
                definition = tool_definition("exec_argv")
                self.assertIn("argv", definition["inputSchema"]["properties"])
            finally:
                runtime.close()

    def test_sensitive_host_environment_requires_exact_name_lease(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime(root)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "DUMMY_SECRET_KEY_B": "leased-secret",
                        "DUMMY_SECRET_KEY_A": "must-stay-hidden",
                    },
                    clear=False,
                ):
                    baseline = runtime.exec_argv(
                        {
                            "argv": [
                                sys.executable,
                                "-c",
                                "import os; print(os.getenv('DUMMY_SECRET_KEY_B')); print(os.getenv('DUMMY_SECRET_KEY_A'))",
                            ],
                            "yield_time_ms": 5000,
                        }
                    )
                    self.assertEqual(
                        baseline["stdout"].splitlines(),
                        ["leased-secret", "must-stay-hidden"],
                    )
            finally:
                runtime.close()

    def test_sandbox_attestation_env_cannot_be_model_supplied(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp))
            try:
                res = runtime.exec_argv(
                    {
                        "argv": ["true"],
                        "env": {"DEVMCP_INHERITED_SANDBOX": "1"},
                    }
                )
                self.assertEqual(res["status"], "success")
            finally:
                runtime.close()

    def test_minimal_system_view_does_not_mount_whole_etc_or_shadow(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = ExecutionSandbox.create(root, owner_root=root.parent / "owner")
            try:
                offline = sandbox.get_bwrap_args(allow_network=False)
                online = sandbox.get_bwrap_args(allow_network=True)
                joined_offline = "\n".join(offline)
                joined_online = "\n".join(online)
                self.assertNotIn("--ro-bind\n/etc\n/etc", joined_offline)
                for forbidden in forbidden_system_paths():
                    self.assertNotIn(forbidden, joined_offline)
                    self.assertNotIn(forbidden, joined_online)
                for path in readonly_system_paths(allow_network=False):
                    self.assertIn(str(path), offline)
                for path in readonly_system_paths(allow_network=True):
                    self.assertIn(str(path), online)
                if Path("/etc/ssl").exists():
                    self.assertNotIn("/etc/ssl", offline)
                    self.assertIn("/etc/ssl", online)
                self.assertIn("--tmpfs", offline)
                self.assertIn("/tmp", offline)
            finally:
                sandbox.cleanup()

    def test_bwrap_mounts_filtered_snapshots_at_canonical_multi_root_paths(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            sibling = base / "library"
            repo.mkdir()
            sibling.mkdir()
            (repo / "main.txt").write_text("main\n", encoding="utf-8")
            (sibling / "lib.txt").write_text("lib\n", encoding="utf-8")
            (sibling / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
            primary = ExecutionSandbox.create(repo, owner_root=base / "primary-owner")
            extra = ExecutionSandbox.create(sibling, owner_root=base / "extra-owner")
            try:
                args = primary.get_bwrap_args(
                    root_mounts=[
                        (primary.sandbox_dir, repo, True),
                        (extra.sandbox_dir, sibling, False),
                    ]
                )
                triples = [
                    tuple(args[index : index + 3]) for index in range(len(args) - 2)
                ]
                self.assertIn(
                    ("--bind", str(primary.sandbox_dir.resolve()), str(repo.resolve())),
                    triples,
                )
                self.assertIn(
                    (
                        "--ro-bind",
                        str(extra.sandbox_dir.resolve()),
                        str(sibling.resolve()),
                    ),
                    triples,
                )
                self.assertNotIn(
                    ("--ro-bind", str(sibling.resolve()), str(sibling.resolve())),
                    triples,
                )
                self.assertFalse((extra.sandbox_dir / ".env").exists())
                self.assertIn("--unshare-user", args)
                self.assertIn("--disable-userns", args)
            finally:
                primary.cleanup()
                extra.cleanup()

    def test_approval_engine_closes_sqlite_connections(self) -> None:
        engine = ApprovalEngine()
        self.assertEqual(engine.get_status("id"), "approved")

    def test_transaction_preserves_preexisting_wip_and_applies_binary_changes(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            tracked = root / "tracked.txt"
            tracked.write_text("user-wip\n", encoding="utf-8")
            sandbox = ExecutionSandbox.create(root, owner_root=root.parent / "owner")
            try:
                transaction = ExecutionTransaction(
                    authoritative_root=root,
                    snapshot_root=sandbox.sandbox_dir,
                    validate_relative_path=lambda path: None,
                )
                (sandbox.sandbox_dir / "tracked.txt").write_text(
                    "user-wip\nagent-change\n", encoding="utf-8"
                )
                (sandbox.sandbox_dir / "artifact.bin").write_bytes(b"\x00\xffartifact")
                result = transaction.finish(apply=True)
                self.assertEqual(result["status"], "applied")
                self.assertEqual(
                    tracked.read_text(encoding="utf-8"),
                    "user-wip\nagent-change\n",
                )
                self.assertEqual(
                    (root / "artifact.bin").read_bytes(), b"\x00\xffartifact"
                )
                self.assertIn("user-wip", tracked.read_text(encoding="utf-8"))
            finally:
                sandbox.cleanup()

    def test_transaction_conflict_never_overwrites_concurrent_wip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            target = root / "file.txt"
            target.write_text("before\n", encoding="utf-8")
            sandbox = ExecutionSandbox.create(root, owner_root=root.parent / "owner")
            try:
                transaction = ExecutionTransaction(
                    authoritative_root=root,
                    snapshot_root=sandbox.sandbox_dir,
                    validate_relative_path=lambda path: None,
                )
                (sandbox.sandbox_dir / "file.txt").write_text(
                    "agent\n", encoding="utf-8"
                )
                target.write_text("concurrent-user-change\n", encoding="utf-8")
                with self.assertRaises(ToolFailure) as conflict:
                    transaction.finish(apply=True)
                self.assertEqual(conflict.exception.code, "TRANSACTION_CONFLICT")
                self.assertEqual(
                    target.read_text(encoding="utf-8"), "concurrent-user-change\n"
                )
            finally:
                sandbox.cleanup()

    def test_transaction_discard_and_symlink_change_never_touch_authoritative_tree(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            target = root / "file.txt"
            target.write_text("before\n", encoding="utf-8")
            sandbox = ExecutionSandbox.create(root, owner_root=root.parent / "owner")
            try:
                transaction = ExecutionTransaction(
                    authoritative_root=root,
                    snapshot_root=sandbox.sandbox_dir,
                    validate_relative_path=lambda path: None,
                )
                (sandbox.sandbox_dir / "file.txt").write_text(
                    "failed-command-change\n", encoding="utf-8"
                )
                discarded = transaction.finish(apply=False)
                self.assertEqual(discarded["status"], "discarded")
                self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            finally:
                sandbox.cleanup()

        if os.name != "nt":
            with TemporaryDirectory() as tmp:
                root = Path(tmp) / "repo"
                root.mkdir()
                (root / "file.txt").write_text("before\n", encoding="utf-8")
                sandbox = ExecutionSandbox.create(
                    root, owner_root=root.parent / "owner"
                )
                try:
                    transaction = ExecutionTransaction(
                        authoritative_root=root,
                        snapshot_root=sandbox.sandbox_dir,
                        validate_relative_path=lambda path: None,
                    )
                    (sandbox.sandbox_dir / "escape").symlink_to("/etc/passwd")
                    with self.assertRaises(ToolFailure) as unsafe:
                        transaction.finish(apply=True)
                    self.assertEqual(unsafe.exception.code, "TRANSACTION_UNSAFE_CHANGE")
                    self.assertFalse((root / "escape").exists())
                finally:
                    sandbox.cleanup()

    def test_shell_constructs_are_policy_signals_not_blanket_security_denials(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime(root)
            try:
                result = runtime.exec_command(
                    {
                        "cmd": "printf 'alpha\\n' | tr a-z A-Z > out.txt && cat <<'EOF' >> out.txt\nbeta\nEOF\nprintf '%s' \"$(cat out.txt)\"",
                        "yield_time_ms": 5000,
                    }
                )
                self.assertEqual(result["status"], "success")
                self.assertIn("ALPHA", result["stdout"])
                self.assertIn("beta", result["stdout"])
                self.assertEqual(
                    (root / "out.txt").read_text(encoding="utf-8"),
                    "ALPHA\nbeta\n",
                )
            finally:
                runtime.close()

    def test_command_path_scanner_uses_canonical_containment(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX symlink fixture")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            inside = root / "inside.txt"
            inside.write_text("ok\n", encoding="utf-8")
            sibling = base / "outside.txt"
            sibling.write_text("no\n", encoding="utf-8")
            link = root / "escape-link"
            link.symlink_to(sibling)
            runtime = self._runtime(root)
            try:
                runtime._check_command_path_candidate(str(inside))
                runtime._check_command_path_candidate("sub/../inside.txt")
                with self.assertRaises(ToolFailure):
                    runtime._check_command_path_candidate(str(sibling))
                with self.assertRaises(ToolFailure):
                    runtime._check_command_path_candidate("../outside.txt")
                with self.assertRaises(ToolFailure):
                    runtime._check_command_path_candidate(str(link))
            finally:
                runtime.close()

    def test_code_diagnostics_is_optional_normalization_over_authorized_roots(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            source = root / "src.py"
            source.write_text("print('x')\n", encoding="utf-8")
            outside = base / "outside.py"
            outside.write_text("print('no')\n", encoding="utf-8")
            runtime = self._runtime(root)
            try:
                result = runtime.code_diagnostics(
                    {
                        "text": (
                            f"{source}:1:3: error: bad call\n"
                            f"{outside}:2: warning: outside diagnostic\n"
                            '  File "src.py", line 1, in <module>\n'
                        )
                    }
                )
                self.assertEqual(result["provider"], "compiler-text")
                self.assertEqual(result["count"], 3)
                self.assertTrue(result["diagnostics"][0]["path_authorized"])
                self.assertFalse(result["diagnostics"][1]["path_authorized"])
                self.assertEqual(
                    result["diagnostics"][1]["path_error"],
                    "PATH_OUTSIDE_WORKSPACE",
                )
                self.assertTrue(result["diagnostics"][2]["path_authorized"])
                self.assertIn(
                    "compiler-text",
                    runtime._exec_environment_summary()["diagnostic_providers"],
                )
            finally:
                runtime.close()

    def test_patch_engine_uses_same_additional_writable_root_authority(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            sibling = base / "library"
            repo.mkdir()
            sibling.mkdir()
            target = sibling / "lib.txt"
            target.write_text("before\n", encoding="utf-8")

            denied = self._runtime(repo)
            try:
                with self.assertRaises(ToolFailure) as blocked:
                    denied.apply_patch(
                        {
                            "patch": (
                                "*** Begin Patch\n"
                                f"*** Update File: {target}\n"
                                "@@\n-before\n+denied\n"
                                "*** End Patch"
                            )
                        }
                    )
                self.assertEqual(blocked.exception.code, "PATH_OUTSIDE_WORKSPACE")
                self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            finally:
                denied.close()

            allowed = self._runtime(repo)
            try:
                allowed.grant_root(
                    {"path": str(sibling), "access": "write", "scope": "session"}
                )
                result = allowed.apply_patch(
                    {
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Update File: {target}\n"
                            "@@\n-before\n+after\n"
                            "*** End Patch"
                        )
                    }
                )
                self.assertEqual(result["summary"], f"M {target}")
                self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            finally:
                allowed.close()

    def test_private_temp_is_normal_writable_temp_not_host_tmp_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(
                root, policy_profile="autonomous", sandbox_backend="bwrap"
            )
            try:
                env = runtime._exec_environment_summary()
                self.assertIsNotNone(env.get("tmpdir"))
            finally:
                runtime.close()

    def test_read_files_supports_partial_success_and_budgets(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
            (root / "binary.bin").write_bytes(b"\xff\x00")
            runtime = self._runtime(root)
            try:
                result = runtime.read_files(
                    {
                        "paths": [
                            {"path": "a.txt", "start_line": 2, "end_line": 3},
                            "missing.txt",
                            "binary.bin",
                        ],
                        "per_file_max_bytes": 8,
                        "total_max_bytes": 32,
                    }
                )
                self.assertEqual(len(result["files"]), 3)
                self.assertTrue(result["files"][0]["ok"])
                self.assertTrue(result["files"][0]["content"].startswith("two"))
                self.assertTrue(result["files"][0]["truncated"])
                self.assertFalse(result["files"][1]["ok"])
                self.assertEqual(result["files"][1]["error"]["code"], "NOT_FOUND")
                self.assertFalse(result["files"][2]["ok"])
                self.assertIn(
                    result["files"][2]["error"]["code"],
                    {"BINARY_FILE", "UNSUPPORTED_ENCODING"},
                )
            finally:
                runtime.close()

    def test_activate_policy_profile_noop_does_not_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                policy_profile="autonomous",
                sandbox_backend="unsafe",
            )
            try:
                with (
                    patch.object(runtime, "_schedule_devmcp_restart") as restart,
                    patch("coding_tools_mcp.server.subprocess.run") as run,
                ):
                    result = runtime.activate_policy_profile({"profile": "autonomous"})
                self.assertEqual(result["status"], "unchanged")
                restart.assert_not_called()
                run.assert_not_called()
            finally:
                runtime.close()

    @unittest.skipIf(
        os.name == "nt", "Linux namespace inheritance is POSIX/Linux specific"
    )
    def test_bwrap_backend_uses_inherited_sandbox_when_kernel_namespace_is_confirmed(
        self,
    ) -> None:
        inherited = SandboxBackend(
            "inherited",
            True,
            True,
            "already isolated by a parent DevMCP sandbox",
        )
        with patch(
            "coding_tools_mcp.sandbox.inherited_sandbox_backend", return_value=inherited
        ):
            backend = detect_sandbox_backend("bwrap")
        self.assertEqual(backend.name, "inherited")
        self.assertTrue(backend.secure)

    @unittest.skipUnless(
        sys.platform == "linux", "inherited sandbox evidence is Linux-specific"
    )
    def test_inherited_sandbox_requires_dropped_caps_and_private_tmp(self) -> None:
        self.assertTrue(
            _linux_effective_capabilities_dropped("CapEff:\t0000000000000000\n")
        )
        self.assertFalse(
            _linux_effective_capabilities_dropped("CapEff:\t0000000000000001\n")
        )
        mountinfo = "25 23 0:22 / /tmp rw,nosuid - tmpfs tmpfs rw\n"
        self.assertTrue(_linux_mountinfo_has_private_tmp(mountinfo))
        self.assertFalse(
            _linux_mountinfo_has_private_tmp(
                "25 23 8:1 / /tmp rw - ext4 /dev/sda1 rw\n"
            )
        )

        def proc_text(path: Path, *, encoding: str = "ascii") -> str:
            del encoding
            if str(path) == "/proc/self/uid_map":
                return "0 1000 1\n"
            if str(path) == "/proc/self/status":
                return "CapEff:\t0000000000000000\n"
            if str(path) == "/proc/self/mountinfo":
                return mountinfo
            raise OSError(path)

        with (
            patch.dict(os.environ, {"DEVMCP_INHERITED_SANDBOX": "1"}, clear=False),
            patch.object(Path, "read_text", proc_text),
        ):
            backend = inherited_sandbox_backend()
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertTrue(backend.secure)

    @unittest.skipUnless(
        sys.platform == "linux",
        "legacy inherited sandbox detection is Linux-specific",
    )
    def test_legacy_parent_detection_requires_explicit_self_host_opt_in(self) -> None:
        with TemporaryDirectory(prefix="coding-tools-mcp-") as tmp:
            root = (
                Path(tmp)
                / "coding-tools-mcp"
                / "instance"
                / "sandboxes"
                / "sandbox-fixture"
            )
            root.mkdir(parents=True)
            home = root / ".devmcp-home"
            private_tmp = root / ".devmcp-tmp"
            home.mkdir()
            private_tmp.mkdir()
            env = {
                "PWD": str(root),
                "HOME": str(home),
                "TMPDIR": str(private_tmp),
                "TMP": str(private_tmp),
                "TEMP": str(private_tmp),
            }
            with patch.dict(os.environ, env, clear=False):
                legacy = legacy_devmcp_parent_sandbox_backend()
                self.assertIsNotNone(legacy)
                with patch(
                    "coding_tools_mcp.sandbox.inherited_sandbox_backend",
                    return_value=None,
                ):
                    default = detect_sandbox_backend("bwrap")
                    self_host = detect_sandbox_backend(
                        "bwrap", allow_legacy_inherited=True
                    )
            self.assertNotEqual(default.name, "inherited")
            self.assertEqual(self_host.name, "inherited")
            self.assertTrue(self_host.secure)

    def test_executor_registry_prefers_secure_local_and_fails_cleanly_for_missing_container(
        self,
    ) -> None:
        registry = ExecutorRegistry(
            sandbox_backend_name="bwrap",
            sandbox_secure=True,
            sandbox_available=True,
        )
        selected = registry.select(ExecutionRequirements(), preferred="auto")
        self.assertEqual(selected.name, "local_sandbox")
        with self.assertRaises(ToolFailure) as unavailable:
            registry.select(
                ExecutionRequirements(transactional_apply=True),
                preferred="ephemeral_container",
            )
        self.assertEqual(unavailable.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertIn(
            "DEVMCP_EPHEMERAL_CONTAINER_RUNNER",
            unavailable.exception.details["backend"]["reason"],
        )

    def test_executor_registry_preserves_explicit_unsafe_legacy_backend_only(
        self,
    ) -> None:
        registry = ExecutorRegistry(
            sandbox_backend_name="unsafe",
            sandbox_secure=False,
            sandbox_available=True,
        )
        selected = registry.select(ExecutionRequirements(), preferred="auto")
        self.assertEqual(selected.name, "unsafe_host")
        self.assertFalse(selected.secure)
        with self.assertRaises(ToolFailure):
            registry.select(
                ExecutionRequirements(transactional_apply=True),
                preferred="unsafe_host",
            )

    @unittest.skipIf(os.name == "nt", "fake runner fixture uses a POSIX shebang")
    def test_container_runner_not_used_in_build_mode(self) -> None:
        pass

    @unittest.skipIf(os.name == "nt", "runner trust fixture uses POSIX permissions")
    def test_container_runner_must_not_be_group_or_world_writable(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            runner = base / "devmcp-container-runner"
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(0o722)
            with patch.dict(
                os.environ,
                {"DEVMCP_EPHEMERAL_CONTAINER_RUNNER": str(runner)},
                clear=False,
            ):
                with self.assertRaises(ToolFailure) as rejected:
                    Runtime(
                        repo,
                        policy_profile="autonomous",
                        sandbox_backend="unsafe",
                    )
            self.assertEqual(rejected.exception.code, "ACCESS_DENIED")

    @unittest.skipIf(os.name == "nt", "runner trust fixture uses POSIX hard links")
    def test_container_runner_must_not_have_hardlink_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            trusted_dir = base / "trusted"
            trusted_dir.mkdir(mode=0o700)
            runner = trusted_dir / "devmcp-container-runner"
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(0o700)
            os.link(runner, repo / "runner-alias")
            with patch.dict(
                os.environ,
                {"DEVMCP_EPHEMERAL_CONTAINER_RUNNER": str(runner)},
                clear=False,
            ):
                with self.assertRaises(ToolFailure) as rejected:
                    Runtime(
                        repo,
                        policy_profile="autonomous",
                        sandbox_backend="unsafe",
                    )
            self.assertEqual(rejected.exception.code, "ACCESS_DENIED")

    @unittest.skipIf(
        os.name == "nt", "runner alias fixture uses POSIX executable paths"
    )
    def test_direct_host_container_cli_cannot_be_configured_as_runner(self) -> None:
        with TemporaryDirectory() as tmp:
            fake_docker = Path(tmp) / "docker"
            fake_docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o700)
            with self.assertRaises(ToolFailure) as blocked:
                ExecutorRegistry(
                    sandbox_backend_name="bwrap",
                    sandbox_secure=True,
                    sandbox_available=True,
                    container_runner=str(fake_docker),
                )
            self.assertEqual(blocked.exception.code, "ACCESS_DENIED")


if __name__ == "__main__":
    unittest.main()
