from __future__ import annotations

import threading

import os
import shlex
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from unittest.mock import patch

from tests.compliance.fixtures import init_git
from tests.compliance.mcp_client import MCPClient


def structured(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        raise AssertionError(f"tool result lacks structuredContent: {result!r}")
    return payload


class HTTPSessionStateTests(unittest.TestCase):
    def _repo(self, root: Path, name: str, *, long_check: bool = False) -> Path:
        repo = root / name
        repo.mkdir(parents=True)
        (repo / "README.md").write_text(f"{name}\n", encoding="utf-8")
        check_body = "@printf 'check-ok\\n'"
        if long_check:
            check_body = "@printf 'job-started\\n'; sleep 30"
        (repo / "Makefile").write_text(f"test:\n\t{check_body}\n", encoding="utf-8")
        init_git(repo)
        return repo

    @contextmanager
    def _server(
        self,
        initial: Path,
        project_root: Path,
        *,
        active_project_file: Path | None = None,
        logical_context_ttl: int = 3600,
        completed_job_ttl: int = 300,
        execution_mode: str = "build",
    ) -> Iterator[MCPClient]:
        command = (
            "{python} -m coding_tools_mcp --workspace {workspace} "
            f"--project-root {shlex.quote(str(project_root))} "
            f"--execution-mode {execution_mode} --host 127.0.0.1 --port {{port}}"
        )
        if os.environ.get("DEVMCP_HTTP_TEST_NESTED") == "1":
            # Local self-dogfood already runs inside DevMCP bwrap; a second
            # nested bwrap cannot start on the CI/dev host. Production/default
            # tests keep the normal secure backend; only this harness override
            # avoids double sandboxing while exercising the HTTP lifecycle.
            command += " --sandbox-backend unsafe --permission-mode trusted"
        env = {
            "CODING_TOOLS_MCP_SERVER_CMD": command,
            "DEVMCP_LOGICAL_CONTEXT_TTL_SECONDS": str(logical_context_ttl),
            "DEVMCP_COMPLETED_JOB_TTL_SECONDS": str(completed_job_ttl),
            "DEVMCP_GRANTABLE_ROOTS": str(project_root),
        }
        if active_project_file is not None:
            env["DEVMCP_ACTIVE_PROJECT_FILE"] = str(active_project_file)
        with patch.dict(os.environ, env, clear=False):
            with MCPClient(initial) as client:
                yield client

    def test_selected_project_stays_bound_across_http_tool_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a")
            self._repo(projects, "b")
            (repo_a / "nested").mkdir()
            with self._server(repo_a, projects) as client:
                selected = structured(
                    client.call_tool("select_project", {"project": "a"})
                )
                context_id = selected["context_id"]
                for _ in range(2):
                    read = structured(
                        client.call_tool("read_file", {"path": "README.md"})
                    )
                    self.assertEqual(read["content"], "a\n")
                    self.assertEqual(read["workspace"], str(repo_a.resolve()))
                    self.assertEqual(read["context_id"], context_id)
                current = structured(client.call_tool("current_project", {}))
                self.assertEqual(current["relative_path"], "a")
                self.assertEqual(current["workspace"], str(repo_a.resolve()))
                self.assertEqual(current["context_id"], context_id)
                changed_cwd = structured(
                    client.call_tool("set_default_cwd", {"path": "nested"})
                )
                self.assertEqual(changed_cwd["default_cwd"], "nested")
                with MCPClient(repo_a, url=client.url) as reconnect:
                    resumed_cwd = structured(
                        reconnect.call_tool(
                            "get_default_cwd", {"context_id": context_id}
                        )
                    )
                    self.assertEqual(resumed_cwd["default_cwd"], "nested")
                    self.assertEqual(resumed_cwd["workspace"], str(repo_a.resolve()))

    def test_build_external_default_cwd_survives_same_context_and_exec(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            outside = root / "outside"
            outside.mkdir()
            with self._server(repo, projects, execution_mode="build") as client:
                current = structured(client.call_tool("current_project", {}))
                context_id = current["context_id"]
                changed = structured(
                    client.call_tool(
                        "set_default_cwd",
                        {"path": str(outside), "context_id": context_id},
                    )
                )
                self.assertEqual(changed["default_cwd"], str(outside.resolve()))

                with MCPClient(repo, url=client.url) as reconnect:
                    resumed = structured(
                        reconnect.call_tool(
                            "get_default_cwd", {"context_id": context_id}
                        )
                    )
                    self.assertEqual(resumed["default_cwd"], str(outside.resolve()))
                    executed = structured(
                        reconnect.call_tool(
                            "exec_argv",
                            {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "import os; print(os.getcwd())",
                                ],
                                "context_id": context_id,
                            },
                        )
                    )
                    self.assertEqual(executed["status"], "success", executed)
                    self.assertEqual(executed["stdout"].strip(), str(outside.resolve()))

    def test_external_default_cwd_survives_worktree_contention(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            outside = root / "outside"
            outside.mkdir()
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo, projects) as client_a:
                    with MCPClient(repo, url=client_a.url) as client_b:
                        context_a = structured(
                            client_a.call_tool("current_project", {})
                        )["context_id"]
                        context_b = structured(
                            client_b.call_tool("current_project", {})
                        )["context_id"]
                        client_b.call_tool(
                            "set_default_cwd",
                            {"path": str(outside), "context_id": context_b},
                        )
                        client_a.call_tool(
                            "git_create_branch",
                            {"name": "external-cwd-owner", "context_id": context_a},
                        )
                        isolated = structured(
                            client_b.call_tool(
                                "git_create_branch",
                                {
                                    "name": "external-cwd-isolated",
                                    "context_id": context_b,
                                },
                            )
                        )
                        self.assertNotEqual(Path(isolated["workspace"]), repo.resolve())
                        current_cwd = structured(
                            client_b.call_tool(
                                "get_default_cwd", {"context_id": context_b}
                            )
                        )
                        self.assertEqual(
                            current_cwd["default_cwd"], str(outside.resolve())
                        )

    def test_internal_default_cwd_remaps_into_contended_worktree(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            nested = repo / "nested"
            nested.mkdir()
            (nested / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", "nested/tracked.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", "add nested cwd"],
                check=True,
            )
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo, projects) as client_a:
                    with MCPClient(repo, url=client_a.url) as client_b:
                        context_a = structured(
                            client_a.call_tool("current_project", {})
                        )["context_id"]
                        context_b = structured(
                            client_b.call_tool("current_project", {})
                        )["context_id"]
                        client_b.call_tool(
                            "set_default_cwd",
                            {"path": "nested", "context_id": context_b},
                        )
                        client_a.call_tool(
                            "git_create_branch",
                            {"name": "internal-cwd-owner", "context_id": context_a},
                        )
                        isolated = structured(
                            client_b.call_tool(
                                "git_create_branch",
                                {
                                    "name": "internal-cwd-isolated",
                                    "context_id": context_b,
                                },
                            )
                        )
                        worktree = Path(isolated["workspace"])
                        self.assertNotEqual(worktree, repo.resolve())
                        current_cwd = structured(
                            client_b.call_tool(
                                "get_default_cwd", {"context_id": context_b}
                            )
                        )
                        self.assertEqual(current_cwd["default_cwd"], "nested")
                        executed = structured(
                            client_b.call_tool(
                                "exec_argv",
                                {
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "import os; print(os.getcwd())",
                                    ],
                                    "context_id": context_b,
                                },
                            )
                        )
                        self.assertEqual(
                            executed["stdout"].strip(),
                            str((worktree / "nested").resolve()),
                        )

    def test_parallel_http_clients_are_workspace_isolated_and_do_not_persist_selection(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a")
            repo_b = self._repo(projects, "b")
            active_file = root / "active-project"
            active_file.write_text(str(repo_a.resolve()) + "\n", encoding="utf-8")
            with self._server(
                repo_a, projects, active_project_file=active_file
            ) as client_a:
                with MCPClient(repo_a, url=client_a.url) as client_b:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        select_a = pool.submit(
                            client_a.call_tool,
                            "select_project",
                            {"project": "a"},
                        )
                        select_b = pool.submit(
                            client_b.call_tool,
                            "select_project",
                            {"project": "b"},
                        )
                        payload_a = structured(select_a.result(timeout=10))
                        payload_b = structured(select_b.result(timeout=10))
                    self.assertNotEqual(
                        payload_a["context_id"], payload_b["context_id"]
                    )
                    self.assertEqual(payload_a["workspace"], str(repo_a.resolve()))
                    self.assertEqual(payload_b["workspace"], str(repo_b.resolve()))

                    read_a = structured(
                        client_a.call_tool("read_file", {"path": "README.md"})
                    )
                    read_b = structured(
                        client_b.call_tool("read_file", {"path": "README.md"})
                    )
                    self.assertEqual(read_a["content"], "a\n")
                    self.assertEqual(read_b["content"], "b\n")
                    self.assertEqual(
                        structured(client_a.call_tool("current_project", {}))[
                            "relative_path"
                        ],
                        "a",
                    )
                    self.assertEqual(
                        structured(client_b.call_tool("current_project", {}))[
                            "relative_path"
                        ],
                        "b",
                    )
                    self.assertEqual(
                        active_file.read_text(encoding="utf-8").strip(),
                        str(repo_a.resolve()),
                    )

                    with MCPClient(repo_a, url=client_a.url) as fresh_client:
                        fresh = structured(
                            fresh_client.call_tool("current_project", {})
                        )
                        self.assertEqual(fresh["relative_path"], "a")

    def test_continuation_resume_uses_explicit_context_after_project_switch(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a")
            repo_b = self._repo(projects, "b")
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo_a, projects) as client:
                    selected = structured(
                        client.call_tool("select_project", {"project": "a"})
                    )
                    context_c = selected["context_id"]
                    switched = structured(
                        client.call_tool(
                            "select_project",
                            {"project": "b", "context_id": context_c},
                        )
                    )
                    self.assertEqual(switched["context_id"], context_c)
                    self.assertEqual(switched["relative_path"], "b")
                    self.assertEqual(switched["workspace"], str(repo_b.resolve()))

                    written = structured(
                        client.call_tool(
                            "continuation_checkpoint",
                            {
                                "action": "write",
                                "logical_task": "http-explicit-context-resume",
                                "payload": {
                                    "objective": "resume through explicit HTTP context"
                                },
                                "context_id": context_c,
                            },
                        )
                    )
                    self.assertEqual(written["context_id"], context_c)

                    with MCPClient(repo_a, url=client.url) as reconnect:
                        default_context = structured(
                            reconnect.call_tool("current_project", {})
                        )
                        context_d = default_context["context_id"]
                        self.assertNotEqual(context_d, context_c)
                        self.assertEqual(default_context["relative_path"], "a")

                        resume_result = reconnect.call_tool(
                            "continuation_checkpoint",
                            {
                                "action": "resume",
                                "logical_task": "http-explicit-context-resume",
                                "context_id": context_c,
                            },
                        )
                        self.assertFalse(
                            resume_result["isError"], structured(resume_result)
                        )
                        resumed = structured(resume_result)
                        self.assertEqual(resumed["status"], "resumed")
                        self.assertEqual(resumed["context_id"], context_c)
                        self.assertEqual(resumed["workspace"], str(repo_b.resolve()))

                        again = structured(
                            reconnect.call_tool(
                                "continuation_checkpoint",
                                {
                                    "action": "resume",
                                    "logical_task": "http-explicit-context-resume",
                                    "context_id": context_c,
                                },
                            )
                        )
                        self.assertEqual(again["status"], "already_resumed")
                        self.assertEqual(again["context_id"], context_c)
                        self.assertEqual(again["workspace"], str(repo_b.resolve()))

                        default_again = structured(
                            reconnect.call_tool("current_project", {})
                        )
                        self.assertEqual(default_again["context_id"], context_d)
                        self.assertEqual(default_again["relative_path"], "a")
                        self.assertEqual(
                            default_again["workspace"], str(repo_a.resolve())
                        )

    def test_competing_mutating_context_gets_linked_worktree_and_reuses_it(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo, projects) as client_a:
                    with MCPClient(repo, url=client_a.url) as client_b:
                        state_a = structured(client_a.call_tool("current_project", {}))
                        state_b = structured(client_b.call_tool("current_project", {}))
                        context_a = state_a["context_id"]
                        context_b = state_b["context_id"]
                        self.assertNotEqual(context_a, context_b)

                        base_head = subprocess.run(
                            ["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout.strip()
                        client_a.call_tool(
                            "git_create_branch",
                            {"name": "context-a-feature", "context_id": context_a},
                        )

                        write_a = structured(
                            client_a.call_tool(
                                "exec_argv",
                                {
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "from pathlib import Path; Path('same.txt').write_text('A\\n')",
                                    ],
                                    "context_id": context_a,
                                    "state_effect": "selected_repo",
                                },
                            )
                        )
                        self.assertEqual(write_a["workspace"], str(repo.resolve()))
                        self.assertEqual((repo / "same.txt").read_text(), "A\n")
                        client_a.call_tool(
                            "git_commit",
                            {
                                "message": "context A change",
                                "paths": ["same.txt"],
                                "context_id": context_a,
                            },
                        )
                        context_a_head = subprocess.run(
                            ["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout.strip()
                        self.assertNotEqual(context_a_head, base_head)

                        write_b = structured(
                            client_b.call_tool(
                                "exec_argv",
                                {
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "from pathlib import Path; Path('same.txt').write_text('B\\n')",
                                    ],
                                    "context_id": context_b,
                                    "state_effect": "selected_repo",
                                },
                            )
                        )
                        worktree = Path(write_b["workspace"])
                        self.assertNotEqual(worktree, repo.resolve())
                        self.assertEqual(
                            write_b["active_project"]["path"], str(repo.resolve())
                        )
                        self.assertEqual((repo / "same.txt").read_text(), "A\n")
                        self.assertEqual((worktree / "same.txt").read_text(), "B\n")
                        self.assertEqual(
                            subprocess.run(
                                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                                check=True,
                                text=True,
                                stdout=subprocess.PIPE,
                            ).stdout.strip(),
                            base_head,
                        )
                        branch = subprocess.run(
                            ["git", "-C", str(worktree), "branch", "--show-current"],
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout.strip()
                        self.assertTrue(branch.startswith("devmcp/context-"))

                        read_b = structured(
                            client_b.call_tool(
                                "read_file",
                                {"path": "same.txt", "context_id": context_b},
                            )
                        )
                        self.assertEqual(read_b["content"], "B\n")
                        self.assertEqual(read_b["workspace"], str(worktree))

                        selected_again = structured(
                            client_b.call_tool(
                                "select_project",
                                {"project": "a", "context_id": context_b},
                            )
                        )
                        self.assertEqual(selected_again["workspace"], str(worktree))

                        switched = structured(
                            client_b.call_tool(
                                "git_create_branch",
                                {"name": "context-b-feature", "context_id": context_b},
                            )
                        )
                        self.assertEqual(switched["workspace"], str(worktree))
                        canonical_branch = subprocess.run(
                            ["git", "-C", str(repo), "branch", "--show-current"],
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout.strip()
                        self.assertEqual(canonical_branch, "context-a-feature")
                        self.assertEqual(
                            subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(worktree),
                                    "branch",
                                    "--show-current",
                                ],
                                check=True,
                                text=True,
                                stdout=subprocess.PIPE,
                            ).stdout.strip(),
                            "context-b-feature",
                        )

                        unmanaged = structured(
                            client_b.call_tool(
                                "exec_argv",
                                {
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "from pathlib import Path; Path('unmanaged.txt').write_text('drift\\n')",
                                    ],
                                    "context_id": context_b,
                                },
                            )
                        )
                        self.assertTrue(unmanaged["command_success"])
                        blocked = client_b.call_tool(
                            "git_create_branch",
                            {"name": "must-not-create", "context_id": context_b},
                        )
                        self.assertTrue(blocked["isError"])
                        self.assertEqual(
                            structured(blocked)["error"]["code"], "STATE_DRIFT"
                        )

                        with MCPClient(repo, url=client_a.url) as reconnect:
                            resumed = structured(
                                reconnect.call_tool(
                                    "read_file",
                                    {"path": "same.txt", "context_id": context_b},
                                )
                            )
                            self.assertEqual(resumed["content"], "B\n")
                            self.assertEqual(resumed["workspace"], str(worktree))

    def test_job_handle_survives_new_http_session_and_enforces_context_owner(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a", long_check=True)
            repo_b = self._repo(projects, "b")
            with self._server(repo_a, projects) as client_a:
                selected = structured(
                    client_a.call_tool("select_project", {"project": "a"})
                )
                context_a = selected["context_id"]
                started = structured(
                    client_a.call_tool(
                        "run_project_check",
                        {
                            "check_id": "test",
                            "yield_time_ms": 0,
                            "timeout_ms": 60_000,
                        },
                    )
                )
                self.assertEqual(started["status"], "running")
                self.assertIsNone(started["command_success"])
                handle = started["session_id"]
                self.assertTrue(handle.startswith("job_"))

                with MCPClient(repo_a, url=client_a.url) as reconnect:
                    status = structured(
                        reconnect.call_tool(
                            "job_status",
                            {"session_id": handle, "context_id": context_a},
                        )
                    )
                    self.assertEqual(status["status"], "running", status)
                    self.assertIsNone(status["command_success"])
                    time.sleep(0.2)
                    output = structured(
                        reconnect.call_tool(
                            "job_output",
                            {"session_id": handle, "context_id": context_a},
                        )
                    )
                    self.assertIn("job-started", output["content"])

                    switch_while_running = reconnect.call_tool(
                        "select_project",
                        {"project": "b", "context_id": context_a},
                    )
                    self.assertTrue(switch_while_running["isError"])
                    self.assertEqual(
                        structured(switch_while_running)["error"]["code"],
                        "INVALID_STATE",
                    )

                    with MCPClient(repo_a, url=client_a.url) as client_b:
                        selected_b = structured(
                            client_b.call_tool("select_project", {"project": "b"})
                        )
                        context_b = selected_b["context_id"]
                        foreign = client_b.call_tool(
                            "job_cancel",
                            {"session_id": handle, "context_id": context_b},
                        )
                        self.assertTrue(foreign["isError"])
                        self.assertEqual(
                            structured(foreign)["error"]["code"], "ACCESS_DENIED"
                        )

                    cancelled = structured(
                        reconnect.call_tool(
                            "job_cancel",
                            {"session_id": handle, "context_id": context_a},
                        )
                    )
                    self.assertIn(
                        cancelled["status"],
                        {"terminated", "killed", "exited", "terminating"},
                    )
                    self.assertFalse(cancelled["command_success"])
                    final_status = structured(
                        reconnect.call_tool(
                            "job_status",
                            {"session_id": handle, "context_id": context_a},
                        )
                    )
                    self.assertEqual(final_status["status"], "failed")
                    self.assertFalse(final_status["command_success"])

                self.assertEqual(
                    structured(client_a.call_tool("current_project", {}))[
                        "relative_path"
                    ],
                    "a",
                )
                self.assertFalse((repo_b / "job-started").exists())

    def test_http_job_status_preview_keeps_full_output_available(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            with self._server(repo, projects) as client:
                context_id = structured(client.call_tool("current_project", {}))[
                    "context_id"
                ]
                started = structured(
                    client.call_tool(
                        "exec_argv",
                        {
                            "argv": [
                                sys.executable,
                                "-c",
                                (
                                    "import sys,time; print('http-stdout', flush=True); "
                                    "print('http-stderr', file=sys.stderr, flush=True); "
                                    "time.sleep(0.2)"
                                ),
                            ],
                            "yield_time_ms": 0,
                            "timeout_ms": 10_000,
                            "context_id": context_id,
                        },
                    )
                )
                status = structured(
                    client.call_tool(
                        "job_status",
                        {
                            "session_id": started["session_id"],
                            "wait_ms": 10_000,
                            "include_output": True,
                            "preview_bytes": 128,
                            "context_id": context_id,
                        },
                    )
                )
                self.assertEqual(status["status"], "success", status)
                self.assertIn("http-stdout", status["preview"])
                self.assertIn("http-stderr", status["preview"])
                self.assertTrue(status["full_output_available"])

                full_stdout = structured(
                    client.call_tool(
                        "job_output",
                        {
                            "session_id": started["session_id"],
                            "context_id": context_id,
                        },
                    )
                )
                full_stderr = structured(
                    client.call_tool(
                        "read_output",
                        {
                            "output_ref": status["output_refs"]["stderr"],
                            "context_id": context_id,
                        },
                    )
                )
                self.assertIn("http-stdout", full_stdout["content"])
                self.assertIn("http-stderr", full_stderr["content"])

    def test_context_expiration_is_explicit_and_active_transport_rolls_context(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a")
            with self._server(repo_a, projects, logical_context_ttl=1) as client:
                selected = structured(
                    client.call_tool("select_project", {"project": "a"})
                )
                expired_context = selected["context_id"]
                time.sleep(1.2)

                renewed = structured(client.call_tool("current_project", {}))
                self.assertEqual(renewed["relative_path"], "a")
                self.assertNotEqual(renewed["context_id"], expired_context)

                with MCPClient(repo_a, url=client.url) as reconnect:
                    stale = reconnect.call_tool(
                        "current_project", {"context_id": expired_context}
                    )
                    self.assertTrue(stale["isError"])
                    self.assertEqual(
                        structured(stale)["error"]["code"], "CONTEXT_NOT_FOUND"
                    )

    def test_completed_job_handle_expires_but_context_remains_valid(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a")
            with self._server(
                repo_a,
                projects,
                logical_context_ttl=30,
                completed_job_ttl=1,
            ) as client:
                selected = structured(
                    client.call_tool("select_project", {"project": "a"})
                )
                context_id = selected["context_id"]
                finished = structured(
                    client.call_tool(
                        "run_project_check",
                        {
                            "check_id": "test",
                            "yield_time_ms": 5000,
                            "timeout_ms": 10_000,
                        },
                    )
                )
                self.assertEqual(finished["status"], "success", finished)
                self.assertTrue(finished["command_success"])
                handle = finished["session_id"]
                time.sleep(1.2)
                with MCPClient(repo_a, url=client.url) as reconnect:
                    expired = structured(
                        reconnect.call_tool(
                            "job_status",
                            {"session_id": handle, "context_id": context_id},
                        )
                    )
                    self.assertEqual(expired["status"], "not_found")
                    current = structured(
                        reconnect.call_tool(
                            "current_project", {"context_id": context_id}
                        )
                    )
                    self.assertEqual(current["relative_path"], "a")


    def test_stateless_http_patches_apply_directly_to_canonical_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            (repo / "file.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add file.txt"], check=True)
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo, projects) as client_1:
                    patch1 = (
                        "*** Begin Patch\n"
                        "*** Update File: file.txt\n"
                        "@@ -1,1 +1,1 @@\n"
                        "-initial\n"
                        "+modified_step1\n"
                        "*** End Patch\n"
                    )
                    res1 = structured(client_1.call_tool("apply_patch", {"patch": patch1}))
                    self.assertEqual(res1["workspace"], str(repo.resolve()))
                    self.assertEqual((repo / "file.txt").read_text(), "modified_step1\n")

                    # Second tool call arrives via new stateless HTTP session (simulating ChatGPT)
                    with MCPClient(repo, url=client_1.url) as client_2:
                        patch2 = (
                            "*** Begin Patch\n"
                            "*** Update File: file.txt\n"
                            "@@ -1,1 +1,1 @@\n"
                            "-modified_step1\n"
                            "+modified_step2\n"
                            "*** End Patch\n"
                        )
                        res2 = structured(client_2.call_tool("apply_patch", {"patch": patch2}))
                        self.assertEqual(res2["workspace"], str(repo.resolve()))
                        self.assertEqual((repo / "file.txt").read_text(), "modified_step2\n")

                    # Third tool call (read_file / git_status) arrives via fresh stateless session
                    with MCPClient(repo, url=client_1.url) as client_3:
                        read_res = structured(client_3.call_tool("read_file", {"path": "file.txt"}))
                        self.assertEqual(read_res["workspace"], str(repo.resolve()))
                        self.assertEqual(read_res["content"], "modified_step2\n")

                        status_res = structured(client_3.call_tool("git_status", {}))
                        self.assertEqual(status_res["workspace"], str(repo.resolve()))
                        self.assertFalse(status_res.get("clean", True))
                        paths = [e["path"] for e in status_res.get("entries", [])]
                        self.assertIn("file.txt", paths)


    def test_concurrent_stateless_http_patches_isolate_parallel_writers(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            (repo / "file.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add file.txt"], check=True)
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo, projects) as client_1:
                    t1_res: dict[str, Any] = {}
                    t2_res: dict[str, Any] = {}
                    started = threading.Event()

                    def run_t1():
                        with MCPClient(repo, url=client_1.url) as c1:
                            started.set()
                            cmd = "python3 -c \"import time; time.sleep(1.0); open('t1.txt', 'w').write('done')\""
                            res = structured(c1.call_tool("exec_command", {"cmd": cmd, "yield_time_ms": 3000}))
                            t1_res.update(res)

                    def run_t2():
                        started.wait(timeout=5)
                        time.sleep(0.2)
                        with MCPClient(repo, url=client_1.url) as c2:
                            patch2 = (
                                "*** Begin Patch\n"
                                "*** Update File: file.txt\n"
                                "@@ -1,1 +1,1 @@\n"
                                "-initial\n"
                                "+parallel_writer2\n"
                                "*** End Patch\n"
                            )
                            res = structured(c2.call_tool("apply_patch", {"patch": patch2}))
                            t2_res.update(res)

                    th1 = threading.Thread(target=run_t1)
                    th2 = threading.Thread(target=run_t2)
                    th1.start()
                    th2.start()
                    th1.join()
                    th2.join()

                    self.assertEqual(t1_res.get("workspace"), str(repo.resolve()))
                    self.assertNotEqual(t2_res.get("workspace"), str(repo.resolve()))
                    self.assertIn("worktrees", str(t2_res.get("workspace", "")))
                    self.assertTrue((repo / "t1.txt").is_file())
                    self.assertEqual((repo / "file.txt").read_text(), "initial\n")

    def test_explicit_context_http_patches_and_isolation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = self._repo(projects, "a")
            (repo / "file.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add file.txt"], check=True)
            config_root = root / "config"
            with patch.dict(
                os.environ, {"DEVMCP_CONFIG_DIR": str(config_root)}, clear=False
            ):
                with self._server(repo, projects) as client_1:
                    info1 = structured(client_1.call_tool("server_info", {}))
                    ctx_1 = info1["context_id"]

                    patch_text = (
                        "*** Begin Patch\n"
                        "*** Update File: file.txt\n"
                        "@@ -1,1 +1,1 @@\n"
                        "-initial\n"
                        "+explicit_client1\n"
                        "*** End Patch\n"
                    )
                    preview = structured(client_1.call_tool("preview_patch", {
                        "patch": patch_text,
                        "context_id": ctx_1,
                    }))
                    self.assertTrue(preview.get("clean"))

                    res1 = structured(client_1.call_tool("apply_patch", {
                        "patch": patch_text,
                        "context_id": ctx_1,
                    }))
                    self.assertEqual(res1["workspace"], str(repo.resolve()))
                    self.assertEqual((repo / "file.txt").read_text(), "explicit_client1\n")

                    read_back = structured(client_1.call_tool("read_file", {
                        "path": "file.txt",
                        "context_id": ctx_1,
                    }))
                    self.assertEqual(read_back["content"], "explicit_client1\n")

                    # Second client connects with fresh context
                    with MCPClient(repo, url=client_1.url) as client_2:
                        info2 = structured(client_2.call_tool("server_info", {}))
                        ctx_2 = info2["context_id"]
                        self.assertNotEqual(ctx_1, ctx_2)

                        patch_text2 = (
                            "*** Begin Patch\n"
                            "*** Update File: file.txt\n"
                            "@@ -1,1 +1,1 @@\n"
                            "-explicit_client1\n"
                            "+explicit_client2\n"
                            "*** End Patch\n"
                        )
                        res2 = structured(client_2.call_tool("apply_patch", {
                            "patch": patch_text2,
                            "context_id": ctx_2,
                        }))
                        self.assertNotEqual(res2["workspace"], str(repo.resolve()))
                        self.assertIn("worktrees", str(res2.get("workspace", "")))
                        self.assertEqual((repo / "file.txt").read_text(), "explicit_client1\n")


if __name__ == "__main__":
    unittest.main()
