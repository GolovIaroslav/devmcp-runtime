#!/usr/bin/env python3
"""Exercise a normal DevMCP checkout through its MCP surface.

Fixture preparation happens before the server starts. After that point this
target uses only MCP tools, so it covers the same boundary a local ChatGPT app
would use without touching the maintainer's checkout.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LARGE_FILE = "dogfood_large_source.py"
FAILURE_TEST = "tests/test_self_dogfood_failure.py"
TARGET_TEST = "tests/test_self_dogfood_target.py"
BROAD_SUITE = "dogfood_suite"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.mcp_http import McpHttpClient, connect_with_retry  # noqa: E402


def tool_data(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return result


def result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(json.dumps(structured, sort_keys=True))
    content = result.get("content")
    if isinstance(content, list):
        parts.extend(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return "\n".join(parts)


def require(result: dict[str, Any], label: str) -> dict[str, Any]:
    if result.get("isError") is True:
        raise RuntimeError(f"{label} failed: {result_text(result)[:2000]}")
    return tool_data(result)


def require_contains(result: dict[str, Any], needle: str, label: str) -> None:
    require(result, label)
    if needle not in result_text(result):
        raise RuntimeError(f"{label} did not contain {needle!r}")


def expect_failure(result: dict[str, Any], label: str) -> None:
    if result.get("isError") is True:
        return
    code = tool_data(result).get("exit_code")
    if isinstance(code, int) and code != 0:
        return
    raise RuntimeError(f"{label} unexpectedly succeeded: {result_text(result)[:2000]}")


def require_process_success(
    client: McpHttpClient, result: dict[str, Any], label: str
) -> None:
    require(result, label)
    data = tool_data(result)
    code = data.get("exit_code")
    session_id = find_value(data, {"session_id", "sessionId"})
    if code is None and session_id:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status_result = call(
                client,
                "job_status",
                {"session_id": session_id},
            )
            status = tool_data(status_result)
            if status.get("status") != "running":
                data = status
                code = data.get("exit_code")
                break
            time.sleep(0.5)
        else:
            call(client, "kill_session", {"session_id": session_id})
            raise RuntimeError(f"{label} did not finish within 180 seconds")
    if code != 0:
        raise RuntimeError(
            f"{label} exited with {code!r}: {result_text(result)[:2000]}"
        )


def find_value(value: Any, names: set[str]) -> str | None:
    if isinstance(value, dict):
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = find_value(child, names)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_value(child, names)
            if found:
                return found
    return None


def run_git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=workspace, check=True, capture_output=True, text=True
    )


def prepare_workspace(workspace: Path) -> int:
    subprocess.run(
        ["git", "clone", "--quiet", str(ROOT), str(workspace)],
        check=True,
        capture_output=True,
        text=True,
    )
    run_git(workspace, "config", "user.name", "DevMCP dogfood")
    run_git(workspace, "config", "user.email", "dogfood@example.invalid")

    lines = ['START_MARKER = "DOGFOOD_START"']
    lines.extend(f"# filler line {number}" for number in range(2, 5001))
    lines.append('MIDDLE_MARKER = "DOGFOOD_MIDDLE"')
    lines.extend(f"# filler line {number}" for number in range(5002, 10003))
    lines.extend(
        [
            'END_MARKER = "DOGFOOD_END"',
            "",
            "def marker_values():",
            "    return START_MARKER, MIDDLE_MARKER, END_MARKER",
        ]
    )
    (workspace / LARGE_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (workspace / FAILURE_TEST).write_text(
        "def test_dogfood_failure_is_fixed():\n    assert False\n",
        encoding="utf-8",
    )
    (workspace / TARGET_TEST).write_text(
        "def test_dogfood_target():\n    assert True\n",
        encoding="utf-8",
    )
    suite = workspace / BROAD_SUITE
    suite.mkdir()
    (suite / "test_large_source.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_all_markers_are_present():\n"
        '    text = Path("dogfood_large_source.py").read_text()\n'
        '    assert "DOGFOOD_START" in text\n'
        '    assert "DOGFOOD_MIDDLE" in text\n'
        '    assert "DOGFOOD_END_PATCHED" in text\n',
        encoding="utf-8",
    )
    (suite / "test_target_contract.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_fixture_has_large_source():\n"
        f"    assert len(Path({LARGE_FILE!r}).read_text().splitlines()) >= 10000\n",
        encoding="utf-8",
    )
    (suite / "test_surgical_target.py").write_text(
        "def test_surgical_target():\n    assert True\n",
        encoding="utf-8",
    )
    run_git(
        workspace,
        "add",
        LARGE_FILE,
        FAILURE_TEST,
        TARGET_TEST,
        BROAD_SUITE,
    )
    run_git(workspace, "commit", "--quiet", "-m", "dogfood baseline")
    return len(lines)


def call(client: McpHttpClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return client.call_tool(name, arguments)


def exercise(client: McpHttpClient, total_lines: int) -> None:
    require(call(client, "workspace_info", {}), "workspace_info")
    require(
        call(
            client,
            "list_files",
            {"path": ".", "patterns": [LARGE_FILE], "max_results": 20},
        ),
        "list_files",
    )
    for marker in ("DOGFOOD_START", "DOGFOOD_MIDDLE", "DOGFOOD_END"):
        require_contains(
            call(client, "search_text", {"path": ".", "query": marker}),
            marker,
            f"search_text {marker}",
        )

    bounded = require(
        call(client, "read_file", {"path": LARGE_FILE, "max_bytes": 256}),
        "bounded large read",
    )
    if bounded.get("total_lines", 0) < 10000 or not bounded.get("truncated"):
        raise RuntimeError(f"large read was not bounded: {bounded}")
    for start_line, marker in (
        (1, "DOGFOOD_START"),
        (5001, "DOGFOOD_MIDDLE"),
        (total_lines - 3, "DOGFOOD_END"),
    ):
        require_contains(
            call(
                client,
                "read_file",
                {"path": LARGE_FILE, "start_line": start_line, "max_lines": 4},
            ),
            marker,
            f"ranged read at {start_line}",
        )
    require_contains(
        call(
            client,
            "read_files",
            {
                "paths": [
                    {"path": LARGE_FILE, "start_line": total_lines - 3},
                    TARGET_TEST,
                ]
            },
        ),
        "DOGFOOD_END",
        "read_files large source",
    )

    for name, arguments in (
        ("git_status", {}),
        ("git_log", {"max_count": 2}),
        ("git_show", {"rev": "HEAD", "path": LARGE_FILE, "max_bytes": 2048}),
        (
            "git_blame",
            {"path": LARGE_FILE, "start_line": total_lines - 2, "max_lines": 3},
        ),
    ):
        require(call(client, name, arguments), name)

    failing = call(
        client,
        "run_task",
        {
            "task_id": "pytest.file",
            "path": FAILURE_TEST,
            "timeout_ms": 60000,
            "yield_time_ms": 1000,
            "max_output_bytes": 20000,
        },
    )
    expect_failure(failing, "failing registered task")

    patch = """*** Begin Patch
*** Update File: dogfood_large_source.py
@@
-END_MARKER = "DOGFOOD_END"
+END_MARKER = "DOGFOOD_END_PATCHED"
*** Update File: tests/test_self_dogfood_failure.py
@@
 def test_dogfood_failure_is_fixed():
-    assert False
+    assert True
*** End Patch
"""
    require(call(client, "preview_patch", {"patch": patch}), "preview_patch")
    require(call(client, "apply_patch", {"patch": patch}), "multi-file apply_patch")
    require_contains(
        call(
            client,
            "read_file",
            {"path": LARGE_FILE, "start_line": total_lines - 3, "max_lines": 4},
        ),
        "DOGFOOD_END_PATCHED",
        "surgical end patch",
    )
    require_process_success(
        client,
        call(
            client,
            "run_task",
            {
                "task_id": "pytest.file",
                "path": TARGET_TEST,
                "timeout_ms": 60000,
                "yield_time_ms": 1000,
                "max_output_bytes": 20000,
            },
        ),
        "passing targeted task",
    )
    require_process_success(
        client,
        call(
            client,
            "run_task",
            {
                "task_id": "pytest.file",
                "path": BROAD_SUITE,
                "timeout_ms": 120000,
                "yield_time_ms": 1000,
                "max_output_bytes": 30000,
            },
        ),
        "broader unit suite",
    )
    canonical_python = shlex.quote(sys.executable)
    for command, label in (
        (f"make PYTHON={canonical_python} lint", "lint command"),
        (f"make PYTHON={canonical_python} typecheck", "typecheck command"),
    ):
        require_process_success(
            client,
            call(
                client,
                "exec_command",
                {
                    "cmd": command,
                    "timeout_ms": 120000,
                    "yield_time_ms": 1000,
                    "max_output_bytes": 30000,
                },
            ),
            label,
        )
    require_contains(call(client, "git_diff", {}), "DOGFOOD_END_PATCHED", "git_diff")

    long_job = call(
        client,
        "exec_command",
        {
            "cmd": "python3 -c 'import time; print(\"dogfood-ready\", flush=True); time.sleep(30)'",
            "timeout_ms": 60000,
            "yield_time_ms": 0,
            "max_output_bytes": 4096,
        },
    )
    session_id = find_value(long_job, {"session_id", "sessionId"})
    if not session_id:
        raise RuntimeError(
            f"long-running job did not return a session: {result_text(long_job)}"
        )
    require(call(client, "job_status", {"session_id": session_id}), "job_status")
    require(
        call(
            client,
            "read_output",
            {"output_ref": f"session:{session_id}:stdout", "limit": 4096},
        ),
        "read_output",
    )
    require(
        call(client, "kill_session", {"session_id": session_id, "wait_ms": 5000}),
        "kill_session",
    )


def main() -> int:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="devmcp-self-dogfood-") as parent:
        workspace = Path(parent) / "worktree"
        total_lines = prepare_workspace(workspace)
        port = 18773
        endpoint = f"http://127.0.0.1:{port}/mcp"
        command = [
            sys.executable,
            "-m",
            "coding_tools_mcp",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--permission-mode",
            "trusted",
        ]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            client, _initialize, error = connect_with_retry(endpoint, 20.0)
            if client is None:
                stderr = process.stderr.read()[-4000:] if process.stderr else ""
                raise RuntimeError(
                    f"self-dogfood server did not start: {error}\n{stderr}"
                )
            exercise(client, total_lines)
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "workspace": "temporary git clone",
                        "large_file": LARGE_FILE,
                        "large_file_lines": total_lines,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
