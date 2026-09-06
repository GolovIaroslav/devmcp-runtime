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
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_tools_mcp import __version__
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
from coding_tools_mcp.policy import EXECUTION_MODES, resolve_execution_mode
from coding_tools_mcp.protocol import PROTOCOL_VERSION


MCP_SERVICE = "devmcp-runtime.service"
TUNNEL_SERVICE = "devmcp-tunnel.service"
MCP_PROCESS_MARKER = "-m apps.devmcp.cli serve"
TUNNEL_PROCESS_MARKER = "-m apps.devmcp.cli tunnel run"


def _resolve_tunnel_bin() -> Path:
    raw = os.environ.get("TUNNEL_CLIENT_BIN")
    if raw:
        return Path(raw).expanduser()
    default_path = Path("~/.local/bin/tunnel-client").expanduser()
    if os.name == "nt":
        if default_path.with_suffix(".exe").exists():
            return default_path.with_suffix(".exe")
        which = shutil.which("tunnel-client")
        if which:
            return Path(which)
        return default_path.with_suffix(".exe")
    return default_path


TUNNEL_BIN = _resolve_tunnel_bin()


def _config() -> tuple[ConfigPaths, dict[str, Any]]:
    selected = paths()
    return selected, load_config(selected)


def _sanitize_repo_identity(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    if "@" in value and ":" in value and "://" not in value:
        user_host, path = value.split(":", 1)
        return f"{user_host.split('@', 1)[-1]}/{path.removesuffix('.git')}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or parsed.netloc.split("@")[-1]
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{host}{parsed.path.removesuffix('.git')}"
    return value.removesuffix(".git")


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = getattr(ctypes, "windll").kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle == 0:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        STILL_ACTIVE = 259
        return exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _windows_process_command_line(pid: int) -> str:
    if os.name != "nt" or pid <= 0:
        return ""
    command = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' "
        "-ErrorAction SilentlyContinue; "
        "if ($null -ne $p) { $p.CommandLine }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _pid_matches_command(pid: int, marker: str) -> bool:
    command_line = _windows_process_command_line(pid)
    return bool(command_line) and marker.lower() in command_line.lower()


def _read_pid(file: Path, *, command_marker: str | None = None) -> int | None:
    try:
        raw = file.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    if not _is_pid_running(pid):
        file.unlink(missing_ok=True)
        return None
    if (
        os.name == "nt"
        and command_marker
        and not _pid_matches_command(pid, command_marker)
    ):
        file.unlink(missing_ok=True)
        return None
    return pid


def _windows_listener_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat.exe", "-ano", "-p", "tcp"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
                local_addr = parts[1]
                if local_addr.endswith(f":{port}"):
                    pid = int(parts[4])
                    if pid > 0:
                        return pid
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass

    command = (
        f"$c = Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($null -ne $c) { $c.OwningProcess }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        pid = int(result.stdout.strip())
        return pid if pid > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _windows_tunnel_pid(tunnel_id: str) -> int | None:
    if os.name != "nt" or not tunnel_id:
        return None
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'tunnel-client.exe'\" "
        "-ErrorAction SilentlyContinue | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        parsed = json.loads(result.stdout)
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    records = parsed if isinstance(parsed, list) else [parsed]
    for record in records:
        if not isinstance(record, dict):
            continue
        command_line = str(record.get("CommandLine") or "")
        if (
            "--control-plane.tunnel-id" not in command_line
            or tunnel_id not in command_line
        ):
            continue
        raw_pid = record.get("ProcessId")
        if raw_pid is None:
            continue
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            return pid
    return None


def _windows_kill_process_tree(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 or not _is_pid_running(pid)


@contextmanager
def _windows_service_lock(selected: ConfigPaths):
    run_dir = selected.root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_file = run_dir / "service.lock"
    handle = lock_file.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        import msvcrt

        getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        try:
            yield
        finally:
            handle.seek(0)
            getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
    finally:
        handle.close()


def _active(unit: str) -> bool:
    if os.name == "nt":
        selected, config = _config()
        run_dir = selected.root / "run"
        if unit == MCP_SERVICE:
            if _read_pid(run_dir / "mcp.pid", command_marker=MCP_PROCESS_MARKER):
                return True
            return _mcp_health(config, selected)
        if unit == TUNNEL_SERVICE:
            if _read_pid(run_dir / "tunnel.pid", command_marker=TUNNEL_PROCESS_MARKER):
                return True
            return bool(_tunnel_status(selected))
        return False
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


def _mcp_runtime_state(
    config: dict[str, Any], selected: ConfigPaths
) -> dict[str, Any] | None:
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
            return None
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
            {"name": "local_state_snapshot", "arguments": {}},
            session_id=session_id,
        )
        structured = result.get("result", {}).get("structuredContent", {})
        return structured if isinstance(structured, dict) else None
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None
    finally:
        if session_id:
            try:
                _mcp_delete(url, selected.mcp_token, session_id)
            except (OSError, RuntimeError, urllib.error.URLError):
                pass


def _unit_loaded(unit: str) -> bool:
    if os.name == "nt":
        _selected, config = _config()
        if unit == MCP_SERVICE:
            return bool(config.get("workspace"))
        if unit == TUNNEL_SERVICE:
            return bool(config.get("tunnel_id"))
        return False
    result = _systemctl("show", "--property=LoadState", "--value", unit)
    return result.returncode == 0 and result.stdout.strip() != "not-found"


def _wait_for_mcp_health(timeout_seconds: float = 30.0) -> bool:
    selected, config = _config()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _mcp_health(config, selected):
            return True
        if os.name != "nt" and not _active(MCP_SERVICE):
            time.sleep(0.25)
            continue
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


def _wait_for_tunnel_health(
    selected: ConfigPaths, timeout_seconds: float = 15.0
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        healthy, ready = _tunnel_health_flags(_tunnel_status(selected))
        if healthy and ready:
            return True
        time.sleep(0.25)
    return False


def _status(_: argparse.Namespace) -> int:
    selected, config = _config()
    tunnel = _tunnel_status(selected)
    healthy, ready = _tunnel_health_flags(tunnel)
    print(f"DevMCP Runtime {__version__}")
    print(f"runtime sha: {config.get('installed_runtime_sha') or 'unknown'}")
    print(f"runtime branch: {config.get('installed_runtime_branch') or 'unknown'}")
    print(f"runtime source: {config.get('installed_runtime_source_repo') or 'unknown'}")
    print(
        f"runtime installed at: {config.get('installed_runtime_installed_at') or 'unknown'}"
    )
    dirty_build = config.get("installed_runtime_dirty_build")
    print(
        "runtime dirty build: "
        + ("unknown" if dirty_build is None else "yes" if dirty_build else "no")
    )
    print(f"protocol: {PROTOCOL_VERSION}")
    print(
        f"development runtime: {'yes' if config.get('installed_runtime_development_mode') else 'no'}"
    )
    print(f"MCP process: {'running' if _active(MCP_SERVICE) else 'stopped'}")
    print(f"MCP health: {'ok' if _mcp_health(config, selected) else 'fail'}")
    print(f"MCP workspace: {config.get('workspace')}")
    execution_mode = str(config.get("execution_mode", "build"))
    print(f"execution_mode: {execution_mode}")
    print(
        f"effective_access: {'read-only' if execution_mode == 'plan' else 'full-access'}"
    )
    print(
        f"effective_executor: {'not-applicable' if execution_mode == 'plan' else 'host'}"
    )
    print("sandbox: none")
    print(f"tunnel process: {'running' if _active(TUNNEL_SERVICE) else 'stopped'}")
    print(f"tunnel ready: {'yes' if ready and healthy else 'no'}")
    print(f"tunnel id: {config.get('tunnel_id') or 'not configured'}")
    print(
        f"auth: mcp={'configured' if secret_status(selected)['mcp_token_configured'] else 'not configured'}, tunnel={'configured' if secret_status(selected)['control_plane_key_configured'] else 'not configured'}"
    )
    return 0


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _spawn_windows_cli(selected: ConfigPaths, args: list[str], log_file: Path) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    error_file = log_file.with_name(f"{log_file.stem}.err{log_file.suffix}")
    run_dir = selected.root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_output = run_dir / f".spawn-{os.getpid()}-{time.time_ns()}.pid"
    argument_list = ", ".join(
        _powershell_literal(item) for item in ["-u", "-m", "apps.devmcp.cli", *args]
    )
    command = (
        f"$env:DEVMCP_CONFIG_DIR = {_powershell_literal(selected.root)}; "
        f"$p = Start-Process -FilePath {_powershell_literal(sys.executable)} "
        f"-ArgumentList @({argument_list}) -WindowStyle Hidden "
        f"-RedirectStandardOutput {_powershell_literal(log_file)} "
        f"-RedirectStandardError {_powershell_literal(error_file)} -PassThru; "
        f"[IO.File]::WriteAllText({_powershell_literal(pid_output)}, [string]$p.Id)"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode != 0:
            raise OSError(f"PowerShell launcher exited with {result.returncode}")
        pid = int(pid_output.read_text(encoding="utf-8").strip())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"unable to launch background process: {exc}") from exc
    finally:
        pid_output.unlink(missing_ok=True)
    if pid <= 0:
        raise OSError("background process did not return a valid PID")
    return pid


def _windows_mcp_pid(config: dict[str, Any], pid_file: Path) -> int | None:
    pid = _read_pid(pid_file, command_marker=MCP_PROCESS_MARKER)
    if pid is not None:
        return pid
    listener = _windows_listener_pid(int(config.get("mcp_port", 47157)))
    if listener is not None and _pid_matches_command(listener, MCP_PROCESS_MARKER):
        return listener
    return None


def _windows_stop_services(
    selected: ConfigPaths,
    config: dict[str, Any],
    mcp_pid_file: Path,
    tunnel_pid_file: Path,
) -> int:
    failed = False
    tunnel_pid = _read_pid(
        tunnel_pid_file, command_marker=TUNNEL_PROCESS_MARKER
    ) or _windows_tunnel_pid(str(config.get("tunnel_id", "")).strip())
    if tunnel_pid is not None:
        if _windows_kill_process_tree(tunnel_pid):
            print(f"Stopped Tunnel process (PID {tunnel_pid})")
        else:
            print(
                f"Failed to stop Tunnel process (PID {tunnel_pid})",
                file=sys.stderr,
            )
            failed = True
    tunnel_pid_file.unlink(missing_ok=True)

    mcp_pid = _windows_mcp_pid(config, mcp_pid_file)
    listener = _windows_listener_pid(int(config.get("mcp_port", 47157)))
    if mcp_pid is not None:
        if _windows_kill_process_tree(mcp_pid):
            print(f"Stopped MCP process (PID {mcp_pid})")
        else:
            print(f"Failed to stop MCP process (PID {mcp_pid})", file=sys.stderr)
            failed = True
    elif listener is not None:
        command_line = _windows_process_command_line(listener)
        print(
            "Refusing to terminate the listener on the configured MCP port because "
            f"it is not an identified DevMCP process (PID {listener}): {command_line}",
            file=sys.stderr,
        )
        failed = True
    mcp_pid_file.unlink(missing_ok=True)
    return 1 if failed else 0


def _windows_start_mcp(
    selected: ConfigPaths,
    config: dict[str, Any],
    mcp_pid_file: Path,
    log_file: Path,
) -> int:
    if _mcp_health(config, selected):
        listener = _windows_listener_pid(int(config.get("mcp_port", 47157)))
        if listener is not None and _pid_matches_command(listener, MCP_PROCESS_MARKER):
            mcp_pid_file.write_text(str(listener), encoding="utf-8")
        print("MCP server is already running")
        return 0

    listener = _windows_listener_pid(int(config.get("mcp_port", 47157)))
    if listener is not None:
        command_line = _windows_process_command_line(listener)
        if _pid_matches_command(listener, MCP_PROCESS_MARKER):
            message = (
                "An existing DevMCP process owns the configured MCP port but is not "
                "healthy; run `devmcp restart` instead of starting another serve process."
            )
        else:
            message = (
                "The configured MCP port is already owned by a non-DevMCP process "
                f"(PID {listener}): {command_line}"
            )
        print(message, file=sys.stderr)
        return 1

    try:
        pid = _spawn_windows_cli(selected, ["serve"], log_file)
    except OSError as exc:
        print(f"Failed to start MCP server: {exc}", file=sys.stderr)
        return 1
    mcp_pid_file.write_text(str(pid), encoding="utf-8")
    if not _wait_for_mcp_health():
        _windows_kill_process_tree(pid)
        mcp_pid_file.unlink(missing_ok=True)
        print("MCP service failed to become healthy", file=sys.stderr)
        return 1
    print(f"Started MCP server (PID {pid})")
    return 0


def _windows_start_tunnel(
    selected: ConfigPaths,
    config: dict[str, Any],
    tunnel_pid_file: Path,
    log_file: Path,
) -> int:
    tunnel_id = str(config.get("tunnel_id", "")).strip()
    auth = secret_status(selected)
    if not (
        tunnel_id
        and auth["control_plane_key_configured"]
        and auth["mcp_token_configured"]
    ):
        return 0
    healthy, ready = _tunnel_health_flags(_tunnel_status(selected))
    if healthy and ready:
        pid = _windows_tunnel_pid(tunnel_id)
        if pid is not None:
            tunnel_pid_file.write_text(str(pid), encoding="utf-8")
        print("Tunnel process is already running")
        return 0
    try:
        pid = _spawn_windows_cli(selected, ["tunnel", "run"], log_file)
    except OSError as exc:
        print(f"Failed to start tunnel: {exc}", file=sys.stderr)
        return 1
    tunnel_pid_file.write_text(str(pid), encoding="utf-8")
    if not _wait_for_tunnel_health(selected):
        _windows_kill_process_tree(pid)
        tunnel_pid_file.unlink(missing_ok=True)
        print("Tunnel service failed to become ready", file=sys.stderr)
        return 1
    print(f"Started Secure MCP Tunnel (PID {pid})")
    return 0


def _windows_service_action(action: str) -> int:
    selected, config = _config()
    run_dir = selected.root / "run"
    logs_dir = selected.root / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    mcp_pid_file = run_dir / "mcp.pid"
    tunnel_pid_file = run_dir / "tunnel.pid"
    with _windows_service_lock(selected):
        if action in {"stop", "restart"}:
            stopped = _windows_stop_services(
                selected, config, mcp_pid_file, tunnel_pid_file
            )
            if stopped != 0 or action == "stop":
                return stopped
        if action in {"start", "restart"}:
            started = _windows_start_mcp(
                selected, config, mcp_pid_file, logs_dir / "mcp.log"
            )
            if started != 0:
                return started
            return _windows_start_tunnel(
                selected, config, tunnel_pid_file, logs_dir / "tunnel.log"
            )
    return 0


def _service_action(action: str) -> int:
    if os.name == "nt":
        return _windows_service_action(action)

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

    if os.name == "nt":
        logs_dir = selected.root / "logs"
        sections: list[str] = []
        log_sources = (
            ("MCP Service Log", logs_dir / "mcp.log"),
            ("MCP Service Error Log", logs_dir / "mcp.err.log"),
            ("Tunnel Service Log", logs_dir / "tunnel.log"),
            ("Tunnel Service Error Log", logs_dir / "tunnel.err.log"),
        )
        for title, log_path in log_sources:
            if not log_path.is_file():
                continue
            if sections:
                sections.append("")
            sections.append(f"=== {title} ===")
            content = log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            sections.extend(content[-100:])
        output = "\n".join(sections)
    else:
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
    stderr = result.stderr if os.name != "nt" else ""
    returncode = result.returncode if os.name != "nt" else 0
    return output, stderr, returncode


def _logs(_: argparse.Namespace) -> int:
    selected, _config_data = _config()
    output, stderr, returncode = _read_service_logs(selected)
    sys.stdout.write(output)
    sys.stderr.write(stderr)
    return returncode


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
    raw_mode = args.execution_mode or args.permission_mode
    mode, _ = resolve_execution_mode(execution_mode=raw_mode)
    config["execution_mode"] = mode
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
            "--mcp.discovery-extra-headers",
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
    if os.name == "nt":
        startup_dir = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        startup_dir.mkdir(parents=True, exist_ok=True)
        vbs_path = startup_dir / "devmcp-autostart.vbs"
        python_exe = sys.executable
        vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """{python_exe}"" -m apps.devmcp.cli start", 0, False
'''
        vbs_path.write_text(vbs_content, encoding="utf-8")
        print(f"Installed Windows autostart script at {vbs_path}")
        return 0

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
    remote_result = subprocess.run(
        [git, "-C", str(source), "remote", "get-url", "origin"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    source_repo = _sanitize_repo_identity(
        remote_result.stdout.strip() if remote_result.returncode == 0 else None
    )
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
    config["installed_runtime_installed_at"] = datetime.now(timezone.utc).isoformat()
    config["installed_runtime_source_repo"] = source_repo
    config["installed_runtime_dirty_build"] = False
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
    runtime_state = _mcp_runtime_state(config, selected)
    installed_sha = (
        runtime_state.get("service", {}).get("installed_sha")
        if isinstance(runtime_state, dict)
        and isinstance(runtime_state.get("service"), dict)
        else None
    )
    if installed_sha != expected_sha:
        print(
            "DevMCP service restarted but did not report the requested runtime SHA",
            file=sys.stderr,
        )
        return 1
    print(f"Updated DevMCP runtime from {source}")
    return 0


def _serve(_: argparse.Namespace) -> int:
    """Stable service launcher: resolve every runtime option from config.toml."""

    selected, config = _config()
    if not secret_status(selected)["mcp_token_configured"]:
        generate_mcp_token(selected)
    from coding_tools_mcp.stateful_server import main as server_main

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
        "--execution-mode",
        str(config.get("execution_mode", "build")),
        "--sandbox-backend",
        str(config.get("sandbox_backend", "bwrap")),
        "--max-removed-lines",
        str(int(config.get("patch", {}).get("max_removed_lines", 200))),
        "--max-removed-percent",
        str(float(config.get("patch", {}).get("max_removed_percent", 30.0))),
    ]
    if secret_status(selected)["control_plane_key_configured"]:
        server_args.extend(["--extra-auth-token-file", str(selected.control_plane_key)])
    for project_root in config.get("workspaces", [config["workspace"]]):
        server_args.extend(["--project-root", str(project_root)])
    return server_main(server_args)


def _service_uninstall(_: argparse.Namespace) -> int:
    if os.name == "nt":
        startup_dir = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        vbs_path = startup_dir / "devmcp-autostart.vbs"
        vbs_path.unlink(missing_ok=True)
        _service_action("stop")
        print(
            "Removed DevMCP Windows autostart script and stopped services; "
            "configuration, secrets, audit log, and workspaces were preserved."
        )
        return 0

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
        description="DevMCP Runtime: local coding runtime for MCP clients.",
    )
    parser.add_argument(
        "--version", action="version", version=f"DevMCP Runtime {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="run the first-run configuration wizard").add_argument(
        "--workspace"
    )
    setup = sub.choices["setup"]
    setup.add_argument("--execution-mode", choices=EXECUTION_MODES, default="build")
    setup.add_argument(
        "--permission-mode",
        choices=("safe", "trusted", "dangerous"),
        help=argparse.SUPPRESS,
    )
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
