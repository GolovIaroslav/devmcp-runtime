"""Operator CLI for DevMCP Runtime.

All commands are local and deliberately avoid printing secret values.  The
legacy environment variables and command aliases remain supported where that
does not weaken the new configuration model.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from coding_tools_mcp import __version__
from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.config import (
    ConfigPaths,
    ensure_mcp_authorization_header,
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


MCP_SERVICE = "devmcp-runtime.service"
TUNNEL_SERVICE = "devmcp-tunnel.service"
TUNNEL_BIN = Path(
    os.environ.get("TUNNEL_CLIENT_BIN", "~/.local/bin/tunnel-client")
).expanduser()


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


def _mcp_call(
    url: str,
    token_file: Path,
    method: str,
    params: dict[str, Any],
    *,
    session_id: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("MCP bearer file is empty")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Authorization": f"Bearer {token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read()
        return (
            json.loads(body.decode("utf-8")) if body else {},
            response.headers.get("Mcp-Session-Id"),
        )


def _mcp_delete(url: str, token_file: Path, session_id: str) -> None:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        return
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Session-Id": session_id,
            "Authorization": f"Bearer {token}",
        },
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=3):
        pass


def _mcp_health(config: dict[str, Any], selected: ConfigPaths) -> bool:
    url = f"http://{config.get('mcp_host', '127.0.0.1')}:{int(config.get('mcp_port', 47157))}/mcp"
    session_id: str | None = None
    try:
        initialize, session_id = _mcp_call(
            url,
            selected.mcp_token,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "devmcp", "version": __version__},
            },
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
        result, _ = _mcp_call(
            url,
            selected.mcp_token,
            "tools/call",
            {"name": "health", "arguments": {}},
            session_id=session_id,
        )
        return (
            result.get("result", {}).get("structuredContent", {}).get("status") == "ok"
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return False
    finally:
        if session_id:
            try:
                _mcp_delete(url, selected.mcp_token, session_id)
            except (OSError, RuntimeError, urllib.error.URLError):
                pass


def _unit_loaded(unit: str) -> bool:
    result = _systemctl("show", "--property=LoadState", "--value", unit)
    return result.returncode == 0 and result.stdout.strip() != "not-found"


def _wait_for_mcp_health(timeout_seconds: float = 30.0) -> bool:
    selected, config = _config()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _active(MCP_SERVICE) and _mcp_health(config, selected):
            return True
        time.sleep(0.25)
    return False


def _tunnel_status(selected: ConfigPaths) -> dict[str, Any]:
    """Probe the foreground ``tunnel-client run`` daemon, not native runtimes."""

    if not TUNNEL_BIN.exists():
        return {}
    result = subprocess.run(
        [
            str(TUNNEL_BIN),
            "health",
            "--url-file",
            str(selected.tunnel_health_url),
            "--require-control-plane-poll",
            "--json",
        ],
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
        return any(
            (key in keys and item is True) or _find_bool(item, keys)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_find_bool(item, keys) for item in value)
    return False


def _tunnel_health_flags(tunnel: dict[str, Any]) -> tuple[bool, bool]:
    healthy = _find_bool(tunnel, {"healthy", "health_ok"})
    ready = _find_bool(tunnel, {"ready", "readiness"})
    healthz = tunnel.get("healthz")
    readyz = tunnel.get("readyz")
    if isinstance(healthz, dict):
        healthy = healthy or healthz.get("ok") is True
    if isinstance(readyz, dict):
        ready = ready or readyz.get("ok") is True
    return healthy, ready


def _status(_: argparse.Namespace) -> int:
    selected, config = _config()
    tunnel = _tunnel_status(selected)
    healthy, ready = _tunnel_health_flags(tunnel)
    pending = len(ApprovalEngine(selected.approvals_db).list_pending())
    print(f"DevMCP Runtime {__version__}")
    print(f"runtime sha: {config.get('installed_runtime_sha') or 'unknown'}")
    print(
        f"development runtime: {'yes' if config.get('installed_runtime_development_mode') else 'no'}"
    )
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
    print(
        f"auth: mcp={'configured' if secret_status(selected)['mcp_token_configured'] else 'not configured'}, tunnel={'configured' if secret_status(selected)['control_plane_key_configured'] else 'not configured'}"
    )
    return 0


def _service_action(action: str) -> int:
    tunnel_loaded = _unit_loaded(TUNNEL_SERVICE)
    units = [MCP_SERVICE, *([TUNNEL_SERVICE] if tunnel_loaded else [])]
    if action == "stop":
        units.reverse()
    if action == "restart":
        result = _systemctl("restart", MCP_SERVICE)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            return result.returncode
        if not _wait_for_mcp_health():
            print("MCP health did not recover after restart", file=sys.stderr)
            return 1
        if tunnel_loaded:
            result = _systemctl("restart", TUNNEL_SERVICE)
            if result.returncode != 0:
                sys.stderr.write(result.stderr)
                return result.returncode
        return 0
    for unit in units:
        result = _systemctl(action, unit)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            return result.returncode
    return 0


def _read_service_logs(selected: ConfigPaths) -> tuple[str, str, int]:
    """Read a bounded, redacted service log view for the CLI and local UI."""

    result = subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            MCP_SERVICE,
            "-u",
            TUNNEL_SERVICE,
            "-n",
            "200",
            "--no-pager",
            "--output",
            "cat",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = result.stdout
    for secret_path in (
        selected.mcp_token,
        selected.control_plane_key,
        Path.home() / ".config/tunnel-client/control-plane-api-key",
    ):
        try:
            secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""
        if secret:
            output = output.replace(secret, "[REDACTED]")
    return output, result.stderr, result.returncode


def _logs(_: argparse.Namespace) -> int:
    selected, _config_data = _config()
    output, stderr, returncode = _read_service_logs(selected)
    sys.stdout.write(output)
    sys.stderr.write(stderr)
    return returncode


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
        print(
            f"[{request['id']}] {request['command_or_action']} in {request['working_directory']}"
        )
    return 0


def _show(args: argparse.Namespace) -> int:
    selected, _ = _config()
    request = next(
        (
            item
            for item in ApprovalEngine(selected.approvals_db).list_pending()
            if item["id"] == args.id
        ),
        None,
    )
    if request is None:
        print(f"Request {args.id} not found or not pending.")
        return 1
    for key, value in request.items():
        print(f"{key}: {value}")
    return 0


def _approve(args: argparse.Namespace) -> int:
    selected, _ = _config()
    ApprovalEngine(selected.approvals_db).approve(args.id, pattern=args.pattern)
    print(
        f"Approved {args.id} ({'always allow matching rule' if args.pattern else 'once'})"
    )
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
            print(
                f"profile must be one of: {', '.join(PROFILE_NAMES)}", file=sys.stderr
            )
            return 2
        config["profile"] = profile
        save_config(config, selected)
        print(f"Policy profile: {profile}")
        return 0
    if args.policy_action == "export":
        rules = effective_rules(
            str(config.get("profile", "balanced")),
            config.get("policy", {}).get("custom", {}),
        )
        payload = {
            "profile": config.get("profile", "balanced"),
            "rules": rules,
            "patch": config.get("patch", {}),
        }
        if args.file:
            Path(args.file).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.policy_action == "import":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("policy export must be an object")
        raw_rules = payload.get("rules", {})
        if not isinstance(raw_rules, dict):
            raise ValueError("policy rules must be an object")
        rules = validate_rules(raw_rules)
        imported_patch = payload.get("patch")
        if "patch" in payload:
            if not isinstance(imported_patch, dict):
                raise ValueError("patch thresholds must be an object")
            unknown_patch_fields = sorted(
                set(imported_patch) - {"max_removed_lines", "max_removed_percent"}
            )
            if unknown_patch_fields:
                raise ValueError(
                    f"unknown patch threshold fields: {', '.join(unknown_patch_fields)}"
                )
            current_patch = config.get("patch", {})
            max_removed_lines = imported_patch.get(
                "max_removed_lines", current_patch.get("max_removed_lines", 200)
            )
            max_removed_percent = imported_patch.get(
                "max_removed_percent", current_patch.get("max_removed_percent", 30.0)
            )
            if isinstance(max_removed_lines, bool) or not isinstance(
                max_removed_lines, int
            ):
                raise ValueError("max_removed_lines must be an integer")
            if isinstance(max_removed_percent, bool) or not isinstance(
                max_removed_percent, (int, float)
            ):
                raise ValueError("max_removed_percent must be a number")
            if not math.isfinite(float(max_removed_percent)):
                raise ValueError("max_removed_percent must be finite")
            if max_removed_lines < 0 or max_removed_percent < 0:
                raise ValueError("patch thresholds cannot be negative")
        config["profile"] = "custom"
        config.setdefault("policy", {})["custom"] = rules
        if "patch" in payload:
            config["patch"] = {
                "max_removed_lines": max_removed_lines,
                "max_removed_percent": float(max_removed_percent),
            }
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
        workspaces = [
            str(item)
            for item in config.get("workspaces", [])
            if str(item) != str(workspace)
        ]
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
    if (
        not args.no_tunnel
        and not args.yes
        and not secret_status(selected)["control_plane_key_configured"]
        and sys.stdin.isatty()
    ):
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
        (
            "workspace",
            Path(str(config["workspace"])).is_dir(),
            str(config["workspace"]),
        ),
        (
            "python",
            bool(shutil.which("python3") or shutil.which("python")),
            "python executable",
        ),
        ("git", bool(shutil.which("git")), "git executable"),
        (
            "bwrap",
            backend != "bwrap" or bool(shutil.which("bwrap")),
            f"bubblewrap executable ({backend} backend)",
        ),
        (
            "mcp token",
            secret_status(selected)["mcp_token_configured"],
            "0600 secret file",
        ),
        (
            "tunnel key",
            secret_status(selected)["control_plane_key_configured"],
            "optional Secure MCP Tunnel key",
        ),
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
    if args.auth_action == "import-git-credentials":
        source = Path(args.from_file).expanduser()
        try:
            value = source.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Unable to read Git credential store: {exc}", file=sys.stderr)
            return 1
        write_secret(selected.git_credentials, value)
        print(f"Imported Git credentials to {selected.git_credentials}")
        return 0
    return 2


def _tunnel_command(args: argparse.Namespace) -> int:
    selected, config = _config()
    if args.tunnel_action == "status":
        status = _tunnel_status(selected)
        print(
            json.dumps(
                {
                    "alias": config.get("tunnel_alias"),
                    "tunnel_id": config.get("tunnel_id"),
                    "status": status,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if status else 1
    if not TUNNEL_BIN.exists():
        print("tunnel-client is not installed", file=sys.stderr)
        return 1
    if args.tunnel_action == "run":
        tunnel_id = str(config.get("tunnel_id", "")).strip()
        auth = secret_status(selected)
        if (
            not tunnel_id
            or not auth["control_plane_key_configured"]
            or not auth["mcp_token_configured"]
        ):
            print(
                "tunnel id, control-plane key, and MCP token must be configured before starting the tunnel",
                file=sys.stderr,
            )
            return 2
        try:
            mcp_authorization_header = ensure_mcp_authorization_header(selected)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        command = [
            str(TUNNEL_BIN),
            "run",
            "--profile",
            str(config.get("tunnel_profile", "sample_mcp_with_dcr")),
            "--control-plane.tunnel-id",
            tunnel_id,
            "--control-plane.api-key",
            f"file:{selected.control_plane_key}",
            "--mcp.server-url",
            f"http://{config.get('mcp_host', '127.0.0.1')}:{int(config.get('mcp_port', 47157))}/mcp",
            "--mcp.extra-headers",
            f"Authorization: file:{mcp_authorization_header}",
            "--health.listen-addr",
            "127.0.0.1:0",
            "--health.url-file",
            str(selected.tunnel_health_url),
        ]
        return subprocess.run(command).returncode
    command = [
        str(TUNNEL_BIN),
        "doctor",
        "--profile",
        str(config.get("tunnel_profile", "sample_mcp_with_dcr")),
        "--explain",
    ]
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    print(result.stdout, end="")
    return result.returncode


def _unit_quote(value: str | Path) -> str:
    """Quote an argument for systemd's ExecStart/Environment parser."""

    return shlex.quote(str(value))


def _unit_environment(name: str, value: str | Path) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{name}={escaped}"'


def _service_install(_: argparse.Namespace) -> int:
    selected, _config_data = _config()
    systemd_dir = Path.home() / ".config/systemd/user"
    systemd_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = sys.executable
    mcp_unit = f"""[Unit]
Description=DevMCP Runtime MCP server

[Service]
Type=simple
{_unit_environment("DEVMCP_CONFIG_DIR", selected.root)}
ExecStart={_unit_quote(python)} -m apps.devmcp.cli serve
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""
    tunnel_unit = f"""[Unit]
Description=DevMCP Runtime Secure MCP Tunnel
After={MCP_SERVICE}

[Service]
Type=simple
{_unit_environment("DEVMCP_CONFIG_DIR", selected.root)}
ExecStart={_unit_quote(python)} -m apps.devmcp.cli tunnel run
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


def _validated_runtime_source(raw_source: str) -> Path:
    try:
        source = Path(raw_source).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"DevMCP source checkout does not exist: {raw_source}"
        ) from exc
    if not source.is_dir() or not (
        (source / ".git").is_dir() or (source / ".git").is_file()
    ):
        raise ValueError("DevMCP update source must be a Git checkout")
    pyproject = source / "pyproject.toml"
    cli_module = source / "apps" / "devmcp" / "cli.py"
    if not pyproject.is_file() or not cli_module.is_file():
        raise ValueError("DevMCP update source is missing required runtime files")
    try:
        with pyproject.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Unable to read DevMCP source pyproject.toml") from exc
    if str(project.get("name", "")).strip() != "devmcp-runtime":
        raise ValueError("Update source is not the devmcp-runtime project")
    return source


def _service_update(args: argparse.Namespace) -> int:
    try:
        source = _validated_runtime_source(args.source)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    expected_sha = str(args.expected_sha).strip().lower()
    if len(expected_sha) != 40 or any(
        ch not in "0123456789abcdef" for ch in expected_sha
    ):
        print("--expected-sha must be a full 40-character Git SHA", file=sys.stderr)
        return 2
    git = shutil.which("git")
    if git is None:
        print("git is required to update the installed DevMCP runtime", file=sys.stderr)
        return 1
    development_mode = bool(args.development_mode)
    checks = [([git, "-C", str(source), "rev-parse", "HEAD"], expected_sha)]
    if not development_mode:
        checks.append(([git, "-C", str(source), "branch", "--show-current"], "main"))
    for command, expected in checks:
        check_result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if check_result.returncode != 0 or check_result.stdout.strip() != expected:
            print(
                "DevMCP update source changed after scheduling; refusing stale update",
                file=sys.stderr,
            )
            return 1
    if development_mode:
        branch_result = subprocess.run(
            [git, "-C", str(source), "branch", "--show-current"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            print(
                "DevMCP development update requires a named local branch",
                file=sys.stderr,
            )
            return 1
        source_branch = branch_result.stdout.strip()
    else:
        source_branch = "main"
    for diff_args in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        dirty_result = subprocess.run(
            [git, "-C", str(source), *diff_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if dirty_result.returncode != 0:
            print(
                "DevMCP update source has tracked or staged changes; refusing update",
                file=sys.stderr,
            )
            return 1
    uv = shutil.which("uv")
    if uv is None:
        fallback = Path.home() / ".local" / "bin" / "uv"
        if fallback.is_file() and os.access(fallback, os.X_OK):
            uv = str(fallback)
    if uv is None:
        print("uv is required to update the installed DevMCP runtime", file=sys.stderr)
        return 1

    install = subprocess.run(
        [uv, "tool", "install", "--force", str(source)],
        cwd=str(source),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
    )
    if install.returncode != 0:
        sys.stderr.write(install.stdout)
        return install.returncode

    selected, config = _config()
    config["installed_runtime_sha"] = expected_sha
    config["installed_runtime_branch"] = source_branch
    config["installed_runtime_development_mode"] = development_mode
    save_config(config, selected)

    refreshed_python = Path(sys.executable)
    if not refreshed_python.is_file():
        print(
            "DevMCP tool installation did not restore its Python runtime",
            file=sys.stderr,
        )
        return 1
    for service_command in (("service", "install"), ("restart",)):
        completed = subprocess.run(
            [str(refreshed_python), "-m", "apps.devmcp.cli", *service_command],
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout)
            return completed.returncode
    print(f"Updated DevMCP runtime from {source}")
    return 0


def _serve(_: argparse.Namespace) -> int:
    """Stable service launcher: resolve every runtime option from config.toml."""

    selected, config = _config()
    if not secret_status(selected)["mcp_token_configured"]:
        generate_mcp_token(selected)
    from coding_tools_mcp.server import main as server_main

    os.environ["DEVMCP_POLICY_CONFIG_FILE"] = str(selected.config_file)
    os.environ["DEVMCP_ACTIVE_PROJECT_FILE"] = str(selected.root / "active-project")
    installed_sha = str(config.get("installed_runtime_sha", "")).strip().lower()
    if len(installed_sha) == 40 and all(
        ch in "0123456789abcdef" for ch in installed_sha
    ):
        os.environ["DEVMCP_INSTALLED_RUNTIME_SHA"] = installed_sha
    if secret_status(selected)["git_credentials_configured"]:
        os.environ["DEVMCP_GIT_CREDENTIALS_FILE"] = str(selected.git_credentials)
    server_args = [
        "--workspace",
        str(config["workspace"]),
        "--host",
        str(config.get("mcp_host", "127.0.0.1")),
        "--port",
        str(int(config.get("mcp_port", 47157))),
        "--auth-token-file",
        str(selected.mcp_token),
        "--policy-profile",
        str(config.get("profile", "balanced")),
        "--sandbox-backend",
        str(config.get("sandbox_backend", "bwrap")),
        "--max-removed-lines",
        str(int(config.get("patch", {}).get("max_removed_lines", 200))),
        "--max-removed-percent",
        str(float(config.get("patch", {}).get("max_removed_percent", 30.0))),
    ]
    for project_root in config.get("workspaces", [config["workspace"]]):
        server_args.extend(["--project-root", str(project_root)])
    return server_main(server_args)


def _service_uninstall(_: argparse.Namespace) -> int:
    systemd_dir = Path.home() / ".config/systemd/user"
    for unit in (MCP_SERVICE, TUNNEL_SERVICE):
        _systemctl("disable", "--now", unit)
        (systemd_dir / unit).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    print(
        "Removed DevMCP user service units; configuration, secrets, audit log, and workspaces were preserved."
    )
    return 0


def _ui(_: argparse.Namespace) -> int:
    from .ui import serve_ui

    _selected, config = _config()
    return serve_ui(
        str(config.get("ui_host", "127.0.0.1")), int(config.get("ui_port", 47158))
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devmcp",
        description="DevMCP Runtime: local sandboxed coding runtime for MCP clients.",
    )
    parser.add_argument(
        "--version", action="version", version=f"DevMCP Runtime {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="run the first-run configuration wizard").add_argument(
        "--workspace"
    )
    setup = sub.choices["setup"]
    setup.add_argument("--profile", choices=PROFILE_NAMES)
    setup.add_argument("--tunnel-id")
    setup.add_argument("--no-tunnel", action="store_true")
    setup.add_argument(
        "--yes", action="store_true", help="do not prompt for the optional tunnel key"
    )
    setup.add_argument(
        "--install-services",
        action="store_true",
        help="install user services after setup",
    )
    setup.add_argument(
        "--start-services",
        action="store_true",
        help="install and start user services after setup",
    )
    sub.add_parser(
        "doctor", help="diagnose local runtime and optional tunnel prerequisites"
    )
    sub.add_parser(
        "status", help="show runtime, sandbox, policy, auth, and tunnel status"
    )
    for name in ("start", "stop", "restart", "logs"):
        sub.add_parser(name)
    sub.add_parser("ui", help="start the loopback-only local admin UI")
    sub.add_parser("serve", help="start MCP from the persistent DevMCP configuration")

    config = sub.add_parser("config", help="inspect and edit non-secret configuration")
    config_sub = config.add_subparsers(dest="config_action", required=True)
    for name in ("show", "validate"):
        config_sub.add_parser(name)
    get = config_sub.add_parser("get")
    get.add_argument("key")
    set_parser = config_sub.add_parser("set")
    set_parser.add_argument("key")
    set_parser.add_argument("value")

    policy = sub.add_parser(
        "policy", help="select or exchange data-driven policy profiles"
    )
    policy_sub = policy.add_subparsers(dest="policy_action", required=True)
    profile = policy_sub.add_parser("profile")
    profile.add_argument("profile", choices=PROFILE_NAMES)
    export = policy_sub.add_parser("export")
    export.add_argument("--file")
    imp = policy_sub.add_parser("import")
    imp.add_argument("file")

    approvals = sub.add_parser(
        "approvals", help="inspect and clean local approval requests"
    )
    approvals.add_argument(
        "approval_action", nargs="?", choices=("prune", "clear-expired"), default="list"
    )
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
    import_git_credentials = auth_sub.add_parser(
        "import-git-credentials",
        help="import a Git credential-store file into DevMCP's private secrets",
    )
    import_git_credentials.add_argument("--from-file", required=True)

    tunnel = sub.add_parser("tunnel", help="inspect the optional Secure MCP Tunnel")
    tunnel_sub = tunnel.add_subparsers(dest="tunnel_action", required=True)
    tunnel_sub.add_parser("status")
    tunnel_sub.add_parser("doctor")
    tunnel_sub.add_parser(
        "run", help="run tunnel-client in the foreground using the configured profile"
    )

    service = sub.add_parser(
        "service", help="install, update, or remove Linux systemd user services"
    )
    service_sub = service.add_subparsers(dest="service_action", required=True)
    service_sub.add_parser("install")
    service_update = service_sub.add_parser(
        "update",
        help="update the installed DevMCP runtime from a validated local source checkout",
    )
    service_update.add_argument("--source", required=True)
    service_update.add_argument("--expected-sha", required=True)
    service_update.add_argument(
        "--development-mode",
        action="store_true",
        help="permit a clean named non-main branch while still pinning the exact source HEAD",
    )
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
    if args.command == "serve":
        return _serve(args)
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
        if args.service_action == "install":
            return _service_install(args)
        if args.service_action == "update":
            return _service_update(args)
        return _service_uninstall(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
