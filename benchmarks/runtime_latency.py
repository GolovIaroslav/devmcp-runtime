#!/usr/bin/env python3
"""Measure local MCP tool latency against direct developer-tool baselines."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.mcp_http import McpHttpClient, connect_with_retry  # noqa: E402


@dataclass
class Metric:
    name: str
    samples_ms: list[float]

    def summary(self) -> dict[str, Any]:
        ordered = sorted(self.samples_ms)
        return {
            "samples": len(ordered),
            "min_ms": round(ordered[0], 3),
            "p50_ms": round(percentile(ordered, 50), 3),
            "p95_ms": round(percentile(ordered, 95), 3),
            "max_ms": round(ordered[-1], 3),
        }


def percentile(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def prepare_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    common = "alpha beta gamma\n" * 80
    for index in range(40):
        text = common
        if index == 17:
            text += "TARGET_NEEDLE appears here\n"
        (workspace / "src" / f"file_{index:02d}.txt").write_text(text, encoding="utf-8")
    (workspace / "src" / "target.txt").write_text(
        "TARGET_NEEDLE\n" + common, encoding="utf-8"
    )
    (workspace / "test_smoke.py").write_text(
        "def test_smoke():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    git = shutil.which("git")
    if git:
        subprocess.run([git, "init", "-q"], cwd=workspace, check=True)
        subprocess.run(
            [git, "config", "user.email", "benchmark@example.invalid"],
            cwd=workspace,
            check=True,
        )
        subprocess.run(
            [git, "config", "user.name", "DevMCP Benchmark"],
            cwd=workspace,
            check=True,
        )
        subprocess.run([git, "add", "-A"], cwd=workspace, check=True)
        subprocess.run(
            [git, "commit", "-qm", "benchmark fixture"], cwd=workspace, check=True
        )
    return workspace


def add_unrelated_tree(workspace: Path, *, files: int, bytes_per_file: int) -> int:
    root = workspace / ".benchmark-unrelated"
    root.mkdir()
    payload = b"x" * bytes_per_file
    for index in range(files):
        bucket = root / f"{index // 200:03d}"
        bucket.mkdir(exist_ok=True)
        (bucket / f"payload-{index:05d}.bin").write_bytes(payload)
    return files * bytes_per_file


def start_server(command: str, workspace: Path, port: int) -> subprocess.Popen[bytes]:
    rendered = command.format(
        python=shlex.quote(sys.executable),
        workspace=shlex.quote(str(workspace)),
        port=port,
    )
    env = os.environ.copy()
    env["CODING_TOOLS_MCP_WORKSPACE"] = str(workspace)
    return subprocess.Popen(
        shlex.split(rendered),
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def connect(endpoint: str, timeout_seconds: float) -> McpHttpClient:
    client, _, error = connect_with_retry(endpoint, timeout_seconds)
    if client is None:
        raise RuntimeError(f"MCP server did not initialize: {error}")
    return client


def measure(name: str, iterations: int, warmup: int, func: Callable[[], Any]) -> Metric:
    for _ in range(warmup):
        func()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        func()
        samples.append((time.perf_counter() - started) * 1000)
    return Metric(name=name, samples_ms=samples)


def tool_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(json.dumps(structured, sort_keys=True))
    return "\n".join(parts)


def assert_tool_contains(result: dict[str, Any], needle: str) -> None:
    if result.get("isError") is True or needle not in tool_text(result):
        raise AssertionError(f"tool result did not contain {needle!r}: {result!r}")


def assert_command_ok(result: dict[str, Any]) -> None:
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured.get("exit_code") == 0:
        return
    if '"exit_code": 0' in tool_text(result):
        return
    raise AssertionError(f"exec_command did not report exit_code=0: {result!r}")


def assert_tool_ok(result: dict[str, Any], name: str) -> None:
    if result.get("isError") is True:
        raise AssertionError(f"{name} returned an MCP error: {result!r}")


def native_search(workspace: Path) -> None:
    rg = shutil.which("rg")
    if rg:
        completed = subprocess.run(
            [rg, "TARGET_NEEDLE", str(workspace / "src")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0 or "TARGET_NEEDLE" not in completed.stdout:
            raise AssertionError(completed.stderr or completed.stdout)
        return
    matches = [
        path
        for path in (workspace / "src").glob("*.txt")
        if "TARGET_NEEDLE" in path.read_text(encoding="utf-8")
    ]
    if not matches:
        raise AssertionError("TARGET_NEEDLE not found by Python fallback search")


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = Path(tempfile.mkdtemp(prefix="codex-mcp-latency-"))
    workspace = prepare_workspace(fixture_root)
    port = args.port or free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"
    server = start_server(args.server_command, workspace, port)
    try:
        client = connect(endpoint, args.startup_timeout)
        tools = {tool.get("name") for tool in client.list_tools()}
        required = {
            "tools/list",
            "read_file",
            "search_text",
            "exec_command",
            "git_status",
        }
        missing = sorted(required - (tools | {"tools/list"}))
        if missing:
            raise RuntimeError(f"missing benchmark tools: {missing}")

        metrics: list[Metric] = []
        metrics.extend(
            [
                measure(
                    "mcp.tools_list",
                    args.iterations,
                    args.warmup,
                    lambda: client.list_tools(),
                ),
                measure(
                    "mcp.read_file",
                    args.iterations,
                    args.warmup,
                    lambda: assert_tool_contains(
                        client.call_tool("read_file", {"path": "src/target.txt"}),
                        "TARGET_NEEDLE",
                    ),
                ),
                measure(
                    "mcp.search_text",
                    args.iterations,
                    args.warmup,
                    lambda: assert_tool_contains(
                        client.call_tool(
                            "search_text", {"query": "TARGET_NEEDLE", "path": "src"}
                        ),
                        "TARGET_NEEDLE",
                    ),
                ),
                measure(
                    "mcp.exec_true",
                    args.exec_iterations,
                    args.warmup,
                    lambda: assert_command_ok(
                        client.call_tool(
                            "exec_command",
                            {
                                "cmd": "true",
                                "timeout_ms": 5000,
                                "yield_time_ms": 5000,
                                "max_output_bytes": 4000,
                            },
                        )
                    ),
                ),
                measure(
                    "mcp.exec_python_pass",
                    args.exec_iterations,
                    args.warmup,
                    lambda: assert_command_ok(
                        client.call_tool(
                            "exec_command",
                            {
                                "cmd": 'python -c "pass"',
                                "timeout_ms": 5000,
                                "yield_time_ms": 5000,
                                "max_output_bytes": 4000,
                            },
                        )
                    ),
                ),
                measure(
                    "mcp.git_status",
                    args.exec_iterations,
                    args.warmup,
                    lambda: assert_tool_ok(
                        client.call_tool("git_status", {}), "git_status"
                    ),
                ),
                measure(
                    "mcp.exec_pytest_small",
                    args.exec_iterations,
                    args.warmup,
                    lambda: assert_command_ok(
                        client.call_tool(
                            "exec_command",
                            {
                                "cmd": "python -m pytest -q test_smoke.py",
                                "timeout_ms": 15000,
                                "yield_time_ms": 15000,
                                "max_output_bytes": 8000,
                            },
                        )
                    ),
                ),
                measure(
                    "native.read_text",
                    args.iterations,
                    args.warmup,
                    lambda: (workspace / "src" / "target.txt").read_text(
                        encoding="utf-8"
                    ),
                ),
                measure(
                    "native.search",
                    args.iterations,
                    args.warmup,
                    lambda: native_search(workspace),
                ),
                measure(
                    "native.exec_true",
                    args.exec_iterations,
                    args.warmup,
                    lambda: subprocess.run(
                        ["true"],
                        cwd=str(workspace),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=5,
                        check=True,
                    ),
                ),
            ]
        )
        unrelated_bytes = add_unrelated_tree(
            workspace,
            files=args.unrelated_files,
            bytes_per_file=args.unrelated_file_bytes,
        )
        metrics.append(
            measure(
                "mcp.exec_true_after_unrelated_tree",
                args.exec_iterations,
                args.warmup,
                lambda: assert_command_ok(
                    client.call_tool(
                        "exec_command",
                        {
                            "cmd": "true",
                            "timeout_ms": 5000,
                            "yield_time_ms": 5000,
                            "max_output_bytes": 4000,
                        },
                    )
                ),
            )
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    summaries = {metric.name: metric.summary() for metric in metrics}
    comparisons = comparison_rows(summaries)
    true_before = float(summaries["mcp.exec_true"]["p50_ms"])
    true_after = float(summaries["mcp.exec_true_after_unrelated_tree"]["p50_ms"])
    size_growth_ratio = round(true_after / true_before, 3) if true_before else None
    size_growth_ms = round(true_after - true_before, 3)
    failures = [
        f"{name} p95 {summary['p95_ms']}ms exceeded {args.max_p95_ms}ms"
        for name, summary in summaries.items()
        if name.startswith("mcp.") and float(summary["p95_ms"]) > args.max_p95_ms
    ]
    if true_after > true_before * args.max_size_growth_ratio + args.max_size_growth_ms:
        failures.append(
            "exec true startup regressed after unrelated tree: "
            f"p50 {true_before}ms -> {true_after}ms"
        )
    return {
        "conclusion": "PASS" if not failures else "FAIL",
        "endpoint": endpoint,
        "workspace": str(workspace),
        "iterations": args.iterations,
        "exec_iterations": args.exec_iterations,
        "warmup": args.warmup,
        "max_p95_ms": args.max_p95_ms,
        "unrelated_tree": {
            "files": args.unrelated_files,
            "bytes": unrelated_bytes,
            "p50_growth_ms": size_growth_ms,
            "p50_ratio": size_growth_ratio,
        },
        "metrics": summaries,
        "comparisons": comparisons,
        "failures": failures,
        "notes": [
            "Native baselines are local developer-tool primitives, not equivalent MCP substitutes.",
            "Latency thresholds are intentionally broad; this smoke benchmark catches transport regressions and records trend evidence.",
        ],
    }


def comparison_rows(summaries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = [
        ("read_file", "mcp.read_file", "native.read_text"),
        ("search_text", "mcp.search_text", "native.search"),
        ("exec_true", "mcp.exec_true", "native.exec_true"),
    ]
    rows = []
    for operation, mcp_name, native_name in pairs:
        mcp_p95 = float(summaries[mcp_name]["p95_ms"])
        native_p95 = float(summaries[native_name]["p95_ms"])
        rows.append(
            {
                "operation": operation,
                "mcp_p95_ms": mcp_p95,
                "native_p95_ms": native_p95,
                "p95_ratio": round(mcp_p95 / native_p95, 3) if native_p95 else None,
            }
        )
    return rows


def write_reports(report: dict[str, Any], report_json: Path, report_md: Path) -> None:
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report_md.write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MCP Runtime Latency Benchmark",
        "",
        f"- Conclusion: **{report['conclusion']}**",
        f"- Endpoint: `{report['endpoint']}`",
        f"- Iterations: `{report['iterations']}`",
        f"- Exec iterations: `{report['exec_iterations']}`",
        f"- Warmup iterations: `{report['warmup']}`",
        f"- Max MCP p95 threshold: `{report['max_p95_ms']} ms`",
        f"- Unrelated tree: `{report['unrelated_tree']['files']}` files / `{report['unrelated_tree']['bytes']}` bytes",
        f"- `true` p50 after unrelated tree: `{report['unrelated_tree']['p50_growth_ms']} ms` delta / `{report['unrelated_tree']['p50_ratio']}` ratio",
        "",
        "## Metrics",
        "",
        "| metric | samples | min ms | p50 ms | p95 ms | max ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in report["metrics"].items():
        lines.append(
            f"| `{name}` | {summary['samples']} | {summary['min_ms']} | {summary['p50_ms']} | {summary['p95_ms']} | {summary['max_ms']} |"
        )
    lines.extend(
        [
            "",
            "## Native Baseline Comparison",
            "",
            "| operation | MCP p95 ms | native p95 ms | ratio |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for row in report["comparisons"]:
        lines.append(
            f"| `{row['operation']}` | {row['mcp_p95_ms']} | {row['native_p95_ms']} | {row['p95_ratio']} |"
        )
    lines.extend(["", "## Failures", ""])
    if report["failures"]:
        lines.extend(f"- {failure}" for failure in report["failures"])
    else:
        lines.append("No failures recorded.")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in report["notes"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--exec-iterations", type=int, default=12)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--startup-timeout", type=float, default=10)
    parser.add_argument("--max-p95-ms", type=float, default=5000)
    parser.add_argument("--unrelated-files", type=int, default=4000)
    parser.add_argument("--unrelated-file-bytes", type=int, default=2048)
    parser.add_argument("--max-size-growth-ratio", type=float, default=2.0)
    parser.add_argument("--max-size-growth-ms", type=float, default=100.0)
    parser.add_argument(
        "--server-command",
        default="{python} -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port {port} --permission-mode trusted",
    )
    parser.add_argument(
        "--report-json", type=Path, default=ROOT / "reports/benchmark/mcp-latency.json"
    )
    parser.add_argument(
        "--report-md", type=Path, default=ROOT / "reports/benchmark/mcp-latency.md"
    )
    args = parser.parse_args(argv)

    report = run_benchmark(args)
    write_reports(report, args.report_json, args.report_md)
    return 0 if report["conclusion"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
