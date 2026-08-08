from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.protocol import PROTOCOL_VERSION


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
MCP_SERVICE = "chatgpt-dev-runtime.service"
TUNNEL_SERVICE = "tunnel-client-chatgpt-dev-runtime.service"
MCP_URL = os.environ.get("CODING_TOOLS_MCP_URL", "http://127.0.0.1:47157/mcp")
WORKSPACE = Path(
    os.environ.get("CODING_TOOLS_MCP_WORKSPACE", "/home/jar/Documents/projects/chatgpt-mcp-playground")
).expanduser()
TOKEN_FILE = Path(
    os.environ.get("CODING_TOOLS_MCP_TOKEN_FILE", "~/.config/chatgpt-dev-runtime/mcp-token")
).expanduser()
TUNNEL_BIN = Path(os.environ.get("TUNNEL_CLIENT_BIN", "~/.local/bin/tunnel-client")).expanduser()
TUNNEL_ALIAS = os.environ.get("TUNNEL_CLIENT_RUNTIME_ALIAS", "chatgpt-dev-runtime")
TUNNEL_ID = os.environ.get("TUNNEL_CLIENT_TUNNEL_ID", "tunnel_6a771229f2e48191b34d642ea92892c8")


def _token() -> str:
    value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"MCP bearer file is empty: {TOKEN_FILE}")
    return value


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _active(unit: str) -> bool:
    return _systemctl("is-active", "--quiet", unit).returncode == 0


def _mcp_call(method: str, params: dict[str, Any], *, session_id: str | None = None) -> tuple[dict[str, Any], str | None]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Authorization": f"Bearer {_token()}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        MCP_URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}, response.headers.get("Mcp-Session-Id")


def _mcp_health() -> bool:
    try:
        initialize, session_id = _mcp_call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "devmcp", "version": "v5"},
            },
        )
        if "error" in initialize or not session_id:
            return False
        notification = urllib.request.Request(
            MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}).encode("utf-8"),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Session-Id": session_id,
                "Authorization": f"Bearer {_token()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(notification, timeout=3):
            pass
        result, _ = _mcp_call("tools/call", {"name": "health", "arguments": {}}, session_id=session_id)
        structured = result.get("result", {}).get("structuredContent", {})
        return structured.get("status") == "ok"
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _tunnel_status() -> dict[str, Any]:
    if not TUNNEL_BIN.exists():
        return {}
    result = subprocess.run(
        [str(TUNNEL_BIN), "runtimes", "status", TUNNEL_ALIAS, "--json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _find_bool(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item is True:
                return True
            if _find_bool(item, keys):
                return True
    elif isinstance(value, list):
        return any(_find_bool(item, keys) for item in value)
    return False


def _status(_: argparse.Namespace) -> int:
    tunnel = _tunnel_status()
    process_running = _active(TUNNEL_SERVICE) and _find_bool(tunnel, {"process_running", "running"})
    healthy = _find_bool(tunnel, {"healthy", "health_ok"})
    ready = _find_bool(tunnel, {"ready", "readiness"})
    pending = len(ApprovalEngine().list_pending())
    print(f"MCP process: {'running' if _active(MCP_SERVICE) else 'stopped'}")
    print(f"MCP health: {'ok' if _mcp_health() else 'fail'}")
    print(f"MCP workspace: {WORKSPACE}")
    print(f"tunnel process: {'running' if process_running else 'stopped'}")
    print(f"tunnel ready: {'yes' if ready and healthy else 'no'}")
    print(f"tunnel id: {TUNNEL_ID}")
    print(f"pending approval count: {pending}")
    return 0


def _service_action(action: str) -> int:
    units = [MCP_SERVICE, TUNNEL_SERVICE]
    if action == "stop":
        units.reverse()
    for unit in units:
        result = _systemctl(action, unit)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            return result.returncode
    return 0


def _logs(_: argparse.Namespace) -> int:
    result = subprocess.run(
        ["journalctl", "--user", "-u", MCP_SERVICE, "-u", TUNNEL_SERVICE, "-n", "200", "--no-pager", "--output", "cat"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text = result.stdout
    for secret_path in (TOKEN_FILE, Path.home() / ".config/tunnel-client/control-plane-api-key"):
        try:
            secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""
        if secret:
            text = text.replace(secret, "[REDACTED]")
    sys.stdout.write(text)
    sys.stderr.write(result.stderr)
    return result.returncode


def _approvals(_: argparse.Namespace) -> int:
    pending = ApprovalEngine().list_pending()
    if not pending:
        print("No pending approvals.")
        return 0
    for request in pending:
        print(f"[{request['id']}] {request['command_or_action']} in {request['working_directory']}")
    return 0


def _show(args: argparse.Namespace) -> int:
    request = next((item for item in ApprovalEngine().list_pending() if item["id"] == args.id), None)
    if request is None:
        print(f"Request {args.id} not found or not pending.")
        return 1
    for key, value in request.items():
        print(f"{key}: {value}")
    return 0


def _approve(args: argparse.Namespace) -> int:
    ApprovalEngine().approve(args.id, pattern=args.pattern)
    print(f"Approved {args.id} (once)" if not args.pattern else f"Approved {args.id} and saved pattern: {args.pattern}")
    return 0


def _deny(args: argparse.Namespace) -> int:
    ApprovalEngine().deny(args.id)
    print(f"Denied {args.id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devmcp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    for name in ("start", "stop", "restart"):
        subparsers.add_parser(name)
    subparsers.add_parser("logs")
    subparsers.add_parser("approvals")
    show = subparsers.add_parser("show")
    show.add_argument("id")
    approve = subparsers.add_parser("approve")
    approve.add_argument("id")
    approve.add_argument("--once", action="store_true")
    approve.add_argument("--pattern")
    deny = subparsers.add_parser("deny")
    deny.add_argument("id")
    args = parser.parse_args(argv)
    if args.command == "status":
        return _status(args)
    if args.command in {"start", "stop", "restart"}:
        return _service_action(args.command)
    if args.command == "logs":
        return _logs(args)
    if args.command == "approvals":
        return _approvals(args)
    if args.command == "show":
        return _show(args)
    if args.command == "approve":
        return _approve(args)
    if args.command == "deny":
        return _deny(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
