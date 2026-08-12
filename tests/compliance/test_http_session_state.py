from __future__ import annotations

import os
import shlex
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
    ) -> Iterator[MCPClient]:
        command = (
            "{python} -m coding_tools_mcp --workspace {workspace} "
            f"--project-root {shlex.quote(str(project_root))} "
            "--policy-profile autonomous --host 127.0.0.1 --port {port}"
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

    def test_capability_root_leases_survive_reconnect_are_owner_scoped_and_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo_a = self._repo(projects, "a")
            repo_b = self._repo(projects, "b")
            library = projects / "library"
            library.mkdir()
            target = library / "lib.txt"
            target.write_text("shared-library\n", encoding="utf-8")

            with self._server(repo_a, projects) as client_a:
                selected_a = structured(
                    client_a.call_tool("select_project", {"project": "a"})
                )
                context_a = selected_a["context_id"]
                session_lease = structured(
                    client_a.call_tool(
                        "grant_root",
                        {
                            "path": str(library),
                            "access": "read",
                            "scope": "session",
                        },
                    )
                )
                self.assertTrue(session_lease["lease_id"].startswith("lease_"))

                with MCPClient(repo_a, url=client_a.url) as reconnect:
                    resumed = structured(
                        reconnect.call_tool(
                            "read_file",
                            {"path": str(target), "context_id": context_a},
                        )
                    )
                    self.assertEqual(resumed["content"], "shared-library\n")
                    leases = structured(
                        reconnect.call_tool(
                            "list_capability_leases", {"context_id": context_a}
                        )
                    )["leases"]
                    self.assertEqual(len(leases), 1)

                    with MCPClient(repo_b, url=client_a.url) as client_b:
                        selected_b = structured(
                            client_b.call_tool("select_project", {"project": "b"})
                        )
                        context_b = selected_b["context_id"]
                        foreign_read = client_b.call_tool(
                            "read_file",
                            {"path": str(target), "context_id": context_b},
                        )
                        self.assertTrue(foreign_read["isError"])
                        self.assertEqual(
                            structured(foreign_read)["error"]["code"],
                            "PATH_OUTSIDE_WORKSPACE",
                        )
                        foreign_leases = structured(
                            client_b.call_tool(
                                "list_capability_leases", {"context_id": context_b}
                            )
                        )["leases"]
                        self.assertEqual(foreign_leases, [])

                    structured(
                        reconnect.call_tool(
                            "revoke_capability_lease",
                            {
                                "lease_id": session_lease["lease_id"],
                                "context_id": context_a,
                            },
                        )
                    )
                    once = structured(
                        reconnect.call_tool(
                            "grant_root",
                            {
                                "path": str(library),
                                "access": "read",
                                "scope": "once",
                                "context_id": context_a,
                            },
                        )
                    )
                    self.assertTrue(once["lease_id"].startswith("lease_"))

                with MCPClient(repo_a, url=client_a.url) as once_reconnect:
                    first = structured(
                        once_reconnect.call_tool(
                            "read_file",
                            {"path": str(target), "context_id": context_a},
                        )
                    )
                    self.assertEqual(first["content"], "shared-library\n")
                    replay = once_reconnect.call_tool(
                        "read_file",
                        {"path": str(target), "context_id": context_a},
                    )
                    self.assertTrue(replay["isError"])
                    self.assertEqual(
                        structured(replay)["error"]["code"],
                        "PATH_OUTSIDE_WORKSPACE",
                    )


if __name__ == "__main__":
    unittest.main()
