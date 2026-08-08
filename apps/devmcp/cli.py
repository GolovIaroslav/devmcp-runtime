"""Operator CLI for DevMCP Runtime.

All commands are local and deliberately avoid printing secret values.  The
legacy environment variables and command aliases remain supported where that
does not weaken the new configuration model.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from coding_tools_mcp import __version__
from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.config import (
    ConfigPaths,
    generate_mcp_token,
    get_key,
    load_config,
    paths,
    redact_config,
    save_config,
    secret_status,
    set_key,
    write_secret,
)
from coding_tools_mcp.policy import PROFILE_NAMES, effective_rules, validate_rules
from coding_tools_mcp.protocol import PROTOCOL_VERSION


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
MCP_SERVICE = "devmcp-runtime.service"
TUNNEL_SERVICE = "devmcp-tunnel.service"
LEGACY_MCP_SERVICE = "chatgpt-dev-runtime.service"
LEGACY_TUNNEL_SERVICE = "tunnel-client-chatgpt-dev-runtime.service"
TUNNEL_BIN = Path(os.environ.get("TUNNEL_CLIENT_BIN", "~/.local/bin/tunnel-client")).expanduser()


def _config() -> tuple[ConfigPaths, dict[str, Any]]:
    selected = paths()
    return selected, load_config(selected)


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


def _mcp_call(url: str, token_file: Path, method: str, params: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("MCP bearer file is empty")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Authorization": f"Bearer {token}",
    }
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read()
        return (json.loads(body.decode("utf-8")) if body else {}, response.headers.get("Mcp-Session-Id"))


def _mcp_health(config: dict[str, Any], selected: ConfigPaths) -> bool:
    url = f"http://{config.get('mcp_host', '127.0.0.1')}:{int(config.get('mcp_port', 47157))}/mcp"
    try:
        initialize, session_id = _mcp_call(
            url,
            selected.mcp_token,
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "devmcp", "version": __version__}},
        )
        if "error" in initialize or not session_id:
            return False
        token = selected.mcp_token.read_text(encoding="utf-8").strip()
        notification = urllib.request.Request(
            url,
            data=b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}',
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Session-Id": session_id,
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(notification, timeout=3):
            pass
        result, _ = _mcp_call(url, selected.mcp_token, "tools/call", {"name": "health", "arguments": {}})
        return result.get("result", {}).get("structuredContent", {}).get("status") == "ok"
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError):
        return False


def _tunnel_status(config: dict[str, Any]) -> dict[str, Any]:
    if not TUNNEL_BIN.exists():
        return {}
    result = subprocess.run(
        [str(TUNNEL_BIN), "runtimes", "status", str(config.get("tunnel_alias", "devmcp-runtime")), "--json"],
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
        return any((key in keys and item is True) or _find_bool(item, keys) for key, item in value.items())
    if isinstance(value, list):
        return any(_find_bool(item, keys) for item in value)
    return False


def _status(_: argparse.Namespace) -> int:
    selected, config = _config()
    tunnel = _tunnel_status(config)
    healthy = _find_bool(tunnel, {"healthy", "health_ok"})
    ready = _find_bool(tunnel, {"ready", "readiness"})
    pending = len(ApprovalEngine(selected.approvals_db).list_pending())
    print(f"DevMCP Runtime {__version__}")
    print(f"MCP process: {'running' if _active(MCP_SERVICE) else 'stopped'}")
    print(f"MCP health: {'ok' if _mcp_health(config, selected) else 'fail'}")
    print(f"MCP workspace: {config.get('workspace')}")
    backend = str(config.get("sandbox_backend", "bwrap"))
    print(f"sandbox: {backend if backend != 'unsafe' else 'SANDBOX: UNSAFE HOST MODE'}")
    print(f"policy: {config.get('profile', 'balanced')}")
    print(f"tunnel process: {'running' if _active(TUNNEL_SERVICE) else 'stopped'}")
    print(f"tunnel ready: {'yes' if ready and healthy else 'no'}")
    print(f"tunnel id: {config.get('tunnel_id') or 'not configured'}")
    print(f"pending approvals: {pending}")
    print(f"auth: mcp={'configured' if secret_status(selected)['mcp_token_configured'] else 'not configured'}, tunnel={'configured' if secret_status(selected)['control_plane_key_configured'] else 'not configured'}")
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
    selected, _ = _config()
    result = subprocess.run(
        ["journalctl", "--user", "-u", MCP_SERVICE, "-u", TUNNEL_SERVICE, "-n", "200", "--no-pager", "--output", "cat"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = result.stdout
    for secret_path in (selected.mcp_token, selected.control_plane_key, Path.home() / ".config/tunnel-client/control-plane-api-key"):
        try:
            secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""
        if secret:
            output = output.replace(secret, "[REDACTED]")
    sys.stdout.write(output)
    sys.stderr.write(result.stderr)
    return result.returncode


def _approvals(args: argparse.Namespace) -> int:
    selected, _ = _config()
    engine = ApprovalEngine(selected.approvals_db)
    if args.approval_action == "prune":
        print(f"Pruned {engine.prune_expired()} expired approvals.")
        return 0
    if args.approval_action == "clear-expired":
        print(f"Cleared {engine.clear_expired()} expired approvals.")
        return 0
    pending = engine.list_pending()
    if not pending:
        print("No pending approvals.")
        return 0
    for request in pending:
        print(f"[{request['id']}] {request['command_or_action']} in {request['working_directory']}")
    return 0


def _show(args: argparse.Namespace) -> int:
    selected, _ = _config()
    request = next((item for item in ApprovalEngine(selected.approvals_db).list_pending() if item["id"] == args.id), None)
    if request is None:
        print(f"Request {args.id} not found or not pending.")
        return 1
    for key, value in request.items():
        print(f"{key}: {value}")
    return 0


def _approve(args: argparse.Namespace) -> int:
    selected, _ = _config()
    ApprovalEngine(selected.approvals_db).approve(args.id, pattern=args.pattern)
    print(f"Approved {args.id} ({'always allow matching rule' if args.pattern else 'once'})")
    return 0


def _deny(args: argparse.Namespace) -> int:
    selected, _ = _config()
    ApprovalEngine(selected.approvals_db).deny(args.id)
    print(f"Denied {args.id}")
    return 0


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _config_command(args: argparse.Namespace) -> int:
    selected, config = _config()
    if args.config_action == "show":
        print(json.dumps(redact_config(config), indent=2, sort_keys=True))
        return 0
    if args.config_action == "validate":
        print(f"Configuration valid: {selected.config_file}")
        return 0
    if args.config_action == "get":
        try:
            print(json.dumps(get_key(config, args.key), sort_keys=True))
        except KeyError:
            print(f"Unknown configuration key: {args.key}", file=sys.stderr)
            return 1
        return 0
    if args.config_action == "set":
        set_key(config, args.key, _parse_value(args.value))
        save_config(config, selected)
        print(f"Updated {args.key}")
        return 0
    return 2


def _policy_command(args: argparse.Namespace) -> int:
    selected, config = _config()
    if args.policy_action == "profile":
        profile = args.profile.lower()
        if profile not in PROFILE_NAMES:
            print("profile must be safe, balanced, power, or custom", file=sys.stderr)
            return 2
        config["profile"] = profile
        save_config(config, selected)
        print(f"Policy profile: {profile}")
        return 0
    if args.policy_action == "export":
        rules = effective_rules(str(config.get("profile", "balanced")), config.get("policy", {}).get("custom", {}))
        payload = {"profile": config.get("profile", "balanced"), "rules": rules, "patch": config.get("patch", {})}
        if args.file:
            Path(args.file).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.policy_action == "import":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        rules = validate_rules(payload.get("rules", {}))
        config["profile"] = "custom"
        config.setdefault("policy", {})["custom"] = rules
        save_config(config, selected)
        print("Imported custom policy")
        return 0
    return 2


def _setup(args: argparse.Namespace) -> int:
    selected, config = _config()
    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.is_dir():
            print(f"workspace does not exist: {workspace}", file=sys.stderr)
            return 2
        config["workspace"] = str(workspace)
        workspaces = [str(item) for item in config.get("workspaces", []) if str(item) != str(workspace)]
        config["workspaces"] = [str(workspace), *workspaces]
    profile = args.profile or str(config.get("profile", "balanced"))
    if profile not in PROFILE_NAMES:
        return 2
    config["profile"] = profile
    if args.tunnel_id:
        config["tunnel_id"] = args.tunnel_id
    save_config(config, selected)
    if not secret_status(selected)["mcp_token_configured"]:
        generate_mcp_token(selected)
    if not args.no_tunnel and not args.yes and not secret_status(selected)["control_plane_key_configured"] and sys.stdin.isatty():
        print("OpenAI Secure MCP Tunnel key is not configured")
        key = getpass.getpass("Control-plane key (hidden, optional): ")
        if key.strip():
            write_secret(selected.control_plane_key, key)
    print("DevMCP setup complete")
    print(f"workspace: {config['workspace']}")
    print(f"profile: {config['profile']}")
    print(f"MCP token: configured at {selected.mcp_token}")
    if not secret_status(selected)["control_plane_key_configured"]:
        print("OpenAI Secure MCP Tunnel key is not configured")
    if args.install_services or args.start_services:
        result = _service_install(args)
        if result != 0:
            return result
    if args.start_services:
        result = _service_action("start")
        if result != 0:
            return result
    print("ChatGPT setup: see docs/CHATGPT.md after the MCP health check.")
    print("Run: devmcp doctor")
    return 0


def _doctor(_: argparse.Namespace) -> int:
    selected, config = _config()
    backend = str(config.get("sandbox_backend", "bwrap"))
    checks: list[tuple[str, bool, str]] = [
        ("workspace", Path(str(config["workspace"])).is_dir(), str(config["workspace"])),
        ("python", bool(shutil.which("python3") or shutil.which("python")), "python executable"),
        ("git", bool(shutil.which("git")), "git executable"),
        ("bwrap", backend != "bwrap" or bool(shutil.which("bwrap")), f"bubblewrap executable ({backend} backend)"),
        ("mcp token", secret_status(selected)["mcp_token_configured"], "0600 secret file"),
        ("tunnel key", secret_status(selected)["control_plane_key_configured"], "optional Secure MCP Tunnel key"),
    ]
    good = True
    for name, ok, detail in checks:
        print(f"{name}: {'ok' if ok else 'missing'} ({detail})")
        if name != "tunnel key":
            good = good and ok
    if not secret_status(selected)["control_plane_key_configured"]:
        print("OpenAI Secure MCP Tunnel key is not configured")
    return 0 if good else 1


def _auth_command(args: argparse.Namespace) -> int:
    selected, _ = _config()
    if args.auth_action == "status":
        print(json.dumps(secret_status(selected), indent=2, sort_keys=True))
        return 0
    if args.auth_action == "rotate-mcp-token":
        generate_mcp_token(selected)
        print(f"Rotated MCP token at {selected.mcp_token}")
        return 0
    return 2


def _tunnel_command(args: argparse.Namespace) -> int:
    _, config = _config()
    if args.tunnel_action == "status":
        status = _tunnel_status(config)
        print(json.dumps({"alias": config.get("tunnel_alias"), "tunnel_id": config.get("tunnel_id"), "status": status}, indent=2, sort_keys=True))
        return 0 if status else 1
    if not TUNNEL_BIN.exists():
        print("tunnel-client is not installed", file=sys.stderr)
        return 1
    command = [str(TUNNEL_BIN), "doctor", "--profile", str(config.get("tunnel_profile", "sample_mcp_with_dcr")), "--explain"]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    return result.returncode


def _service_install(_: argparse.Namespace) -> int:
    selected, config = _config()
    systemd_dir = Path.home() / ".config/systemd/user"
    systemd_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = sys.executable
    patch_config = config.get("patch", {})
    mcp_unit = f"""[Unit]
Description=DevMCP Runtime MCP server

[Service]
Type=simple
WorkingDirectory={RUNTIME_ROOT}
Environment=DEVMCP_POLICY_CONFIG_FILE={selected.config_file}
ExecStart={python} -m coding_tools_mcp --workspace {config['workspace']} --host {config['mcp_host']} --port {config['mcp_port']} --auth-token-file {selected.mcp_token} --policy-profile {config.get('profile', 'balanced')} --sandbox-backend {config.get('sandbox_backend', 'bwrap')} --max-removed-lines {patch_config.get('max_removed_lines', 200)} --max-removed-percent {patch_config.get('max_removed_percent', 30.0)}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""
    tunnel_unit = f"""[Unit]
Description=DevMCP Runtime Secure MCP Tunnel
Requires={MCP_SERVICE}
After={MCP_SERVICE}

[Service]
Type=simple
ExecStartPre=/usr/bin/curl --fail --silent --show-error http://{config['mcp_host']}:{config['mcp_port']}/healthz
ExecStart={TUNNEL_BIN} runtimes connect --alias {config.get('tunnel_alias', 'devmcp-runtime')} --tunnel-id {config.get('tunnel_id', '')} --profile {config.get('tunnel_profile', 'sample_mcp_with_dcr')} --mcp-server-url http://{config['mcp_host']}:{config['mcp_port']}/mcp --runtime-api-key file:{selected.control_plane_key}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    (systemd_dir / MCP_SERVICE).write_text(mcp_unit, encoding="utf-8")
    (systemd_dir / TUNNEL_SERVICE).write_text(tunnel_unit, encoding="utf-8")
    for unit in (systemd_dir / MCP_SERVICE, systemd_dir / TUNNEL_SERVICE):
        unit.chmod(0o600)
    result = _systemctl("daemon-reload")
    if result.returncode == 0:
        result = _systemctl("enable", MCP_SERVICE, TUNNEL_SERVICE)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    print(f"Installed user services: {MCP_SERVICE}, {TUNNEL_SERVICE}")
    return 0


def _service_uninstall(_: argparse.Namespace) -> int:
    systemd_dir = Path.home() / ".config/systemd/user"
    for unit in (MCP_SERVICE, TUNNEL_SERVICE):
        _systemctl("disable", "--now", unit)
        (systemd_dir / unit).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    print("Removed DevMCP user service units; configuration, secrets, audit log, and workspaces were preserved.")
    return 0


def _ui(_: argparse.Namespace) -> int:
    from .ui import serve_ui

    _, config = _config()
    return serve_ui(str(config.get("ui_host", "127.0.0.1")), int(config.get("ui_port", 47158)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devmcp",
        description="DevMCP Runtime: local sandboxed coding runtime for MCP clients.",
    )
    parser.add_argument("--version", action="version", version=f"DevMCP Runtime {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="run the first-run configuration wizard").add_argument("--workspace")
    setup = sub.choices["setup"]
    setup.add_argument("--profile", choices=PROFILE_NAMES)
    setup.add_argument("--tunnel-id")
    setup.add_argument("--no-tunnel", action="store_true")
    setup.add_argument("--yes", action="store_true", help="do not prompt for the optional tunnel key")
    setup.add_argument("--install-services", action="store_true", help="install user services after setup")
    setup.add_argument("--start-services", action="store_true", help="install and start user services after setup")
    sub.add_parser("doctor", help="diagnose local runtime and optional tunnel prerequisites")
    sub.add_parser("status", help="show runtime, sandbox, policy, auth, and tunnel status")
    for name in ("start", "stop", "restart", "logs"):
        sub.add_parser(name)
    sub.add_parser("ui", help="start the loopback-only local admin UI")

    config = sub.add_parser("config", help="inspect and edit non-secret configuration")
    config_sub = config.add_subparsers(dest="config_action", required=True)
    for name in ("show", "validate"):
        config_sub.add_parser(name)
    get = config_sub.add_parser("get")
    get.add_argument("key")
    set_parser = config_sub.add_parser("set")
    set_parser.add_argument("key")
    set_parser.add_argument("value")

    policy = sub.add_parser("policy", help="select or exchange data-driven policy profiles")
    policy_sub = policy.add_subparsers(dest="policy_action", required=True)
    profile = policy_sub.add_parser("profile")
    profile.add_argument("profile", choices=PROFILE_NAMES)
    export = policy_sub.add_parser("export")
    export.add_argument("--file")
    imp = policy_sub.add_parser("import")
    imp.add_argument("file")

    approvals = sub.add_parser("approvals", help="inspect and clean local approval requests")
    approvals.add_argument("approval_action", nargs="?", choices=("prune", "clear-expired"), default="list")
    show = sub.add_parser("show")
    show.add_argument("id")
    approve = sub.add_parser("approve")
    approve.add_argument("id")
    approve.add_argument("--pattern")
    deny = sub.add_parser("deny")
    deny.add_argument("id")

    auth = sub.add_parser("auth", help="manage persistent auth files")
    auth_sub = auth.add_subparsers(dest="auth_action", required=True)
    auth_sub.add_parser("status")
    auth_sub.add_parser("rotate-mcp-token")

    tunnel = sub.add_parser("tunnel", help="inspect the optional Secure MCP Tunnel")
    tunnel_sub = tunnel.add_subparsers(dest="tunnel_action", required=True)
    tunnel_sub.add_parser("status")
    tunnel_sub.add_parser("doctor")

    service = sub.add_parser("service", help="install or remove Linux systemd user services")
    service_sub = service.add_subparsers(dest="service_action", required=True)
    service_sub.add_parser("install")
    service_sub.add_parser("uninstall")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        return _setup(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "status":
        return _status(args)
    if args.command in {"start", "stop", "restart"}:
        return _service_action(args.command)
    if args.command == "logs":
        return _logs(args)
    if args.command == "ui":
        return _ui(args)
    if args.command == "config":
        return _config_command(args)
    if args.command == "policy":
        return _policy_command(args)
    if args.command == "approvals":
        return _approvals(args)
    if args.command == "show":
        return _show(args)
    if args.command == "approve":
        return _approve(args)
    if args.command == "deny":
        return _deny(args)
    if args.command == "auth":
        return _auth_command(args)
    if args.command == "tunnel":
        return _tunnel_command(args)
    if args.command == "service":
        return _service_install(args) if args.service_action == "install" else _service_uninstall(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
