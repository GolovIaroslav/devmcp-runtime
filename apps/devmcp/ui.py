"""Small loopback-only operator UI for DevMCP Runtime.

The UI intentionally uses the standard library.  It is an operator surface,
not an internet-facing web application: it binds to loopback, validates Host
and Origin on mutations, uses a per-process CSRF token, and never renders
secret values.
"""

from __future__ import annotations

import html
import http.server
import json
import secrets
import shutil
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_tools_mcp.approval import ApprovalEngine
from coding_tools_mcp.audit import read_recent_events
from coding_tools_mcp.config import (
    ConfigPaths,
    generate_mcp_token,
    load_config,
    paths,
    save_config,
    secret_status,
    write_secret,
)
from coding_tools_mcp.policy import CAPABILITIES, PROFILE_NAMES, effective_rules, validate_rules
from coding_tools_mcp.tasks import TaskRegistry
from coding_tools_mcp import __version__


UI_DEFAULT_HOST = "127.0.0.1"
UI_DEFAULT_PORT = 47158
_CSRF_HEADER = "X-DevMCP-CSRF"


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


@dataclass
class UIState:
    config_paths: ConfigPaths
    config: dict[str, Any]
    csrf_token: str
    host: str
    port: int

    @classmethod
    def load(cls, host: str = UI_DEFAULT_HOST, port: int = UI_DEFAULT_PORT) -> "UIState":
        if not _is_loopback(host):
            raise ValueError("DevMCP UI only binds to loopback")
        config_paths = paths()
        return cls(config_paths, load_config(config_paths), secrets.token_urlsafe(32), host, port)

    @property
    def origin(self) -> str:
        display_host = "127.0.0.1" if self.host in {"", "localhost"} else self.host
        return f"http://{display_host}:{self.port}"

    def reload(self) -> None:
        self.config = load_config(self.config_paths)


def _page(title: str, body: str, state: UIState) -> bytes:
    nav = " ".join(
        f'<a href="/{route}">{label}</a>'
        for route, label in (
            ("", "Dashboard"),
            ("setup", "Setup"),
            ("workspaces", "Workspaces"),
            ("permissions", "Permissions"),
            ("tasks", "Tasks"),
            ("approvals", "Approvals"),
            ("services", "Services"),
            ("diagnostics", "Diagnostics"),
            ("audit", "Audit log"),
            ("about", "About"),
        )
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · DevMCP Runtime</title>
<meta name="csrf-token" content="{html.escape(state.csrf_token)}">
<style>body{{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17202a}}nav{{display:flex;gap:.8rem;flex-wrap:wrap;margin:1rem 0 2rem}}a{{color:#075985}}section,table{{border:1px solid #cbd5e1;border-radius:.5rem;padding:1rem;margin:1rem 0}}table{{width:100%;border-collapse:collapse;padding:0}}th,td{{text-align:left;padding:.5rem;border-bottom:1px solid #e2e8f0}}code,.warning{{background:#f1f5f9;padding:.15rem .3rem;border-radius:.25rem}}.warning{{background:#fef3c7;color:#78350f}}button{{padding:.45rem .7rem}}</style></head>
<body><header><h1>DevMCP Runtime</h1><nav>{nav}</nav></header><main>{body}</main></body></html>"""
    return document.encode("utf-8")


def _table(rows: list[tuple[str, Any]]) -> str:
    return "<table>" + "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>" for key, value in rows
    ) + "</table>"


def _status(state: UIState) -> dict[str, Any]:
    approvals = ApprovalEngine(state.config_paths.approvals_db).list_pending()
    workspace = Path(str(state.config.get("workspace", "")))
    bwrap_available = shutil.which("bwrap") is not None
    backend = str(state.config.get("sandbox_backend", "bwrap"))
    secure = backend == "bwrap" and bwrap_available
    health_url = f"http://{state.config.get('mcp_host', '127.0.0.1')}:{int(state.config.get('mcp_port', 47157))}/healthz"
    try:
        with urllib.request.urlopen(health_url, timeout=0.5) as response:
            health_payload = json.loads(response.read().decode("utf-8"))
        mcp_running = response.status == 200 and health_payload.get("ready") is True
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        mcp_running = False
    return {
        "mcp_running": mcp_running,
        "mcp_health": "ok" if mcp_running else "stopped/unreachable",
        "workspace": str(workspace),
        "workspace_exists": workspace.is_dir(),
        "sandbox_backend": backend,
        "sandbox_secure": secure,
        "policy_profile": str(state.config.get("profile", "balanced")),
        "tunnel_id": str(state.config.get("tunnel_id", "")) or "not configured",
        "tunnel_state": "not configured" if not state.config.get("tunnel_id") else "configured",
        "pending_approvals": len(approvals),
        "version": __version__,
        "auth": secret_status(state.config_paths),
    }


class UIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "DevMCP-UI"

    @property
    def state(self) -> UIState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"devmcp_csrf={self.state.csrf_token}; Path=/; SameSite=Strict; HttpOnly")

    def _send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _reject_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self._send_json({"error": "invalid Host"}, 400)
            return True
        return False

    def _check_mutation(self, form: dict[str, list[str]]) -> bool:
        if self._reject_host():
            return False
        origin = self.headers.get("Origin", "")
        if origin.rstrip("/") != self.state.origin:
            self._send_json({"error": "invalid Origin"}, 403)
            return False
        token = self.headers.get(_CSRF_HEADER) or (form.get("csrf", [""])[0])
        if not token or not secrets.compare_digest(token, self.state.csrf_token):
            self._send_json({"error": "CSRF validation failed"}, 403)
            return False
        return True

    def do_GET(self) -> None:
        if self._reject_host():
            return
        route = urllib.parse.urlsplit(self.path).path.strip("/")
        if route == "api/status":
            self._send_json(_status(self.state))
            return
        body = self._route_page(route)
        self._send_html(body)

    def do_POST(self) -> None:
        length = min(int(self.headers.get("Content-Length", "0") or 0), 128 * 1024)
        raw = self.rfile.read(length)
        form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
        if not self._check_mutation(form):
            return
        route = urllib.parse.urlsplit(self.path).path.strip("/")
        try:
            if route == "api/policy/profile":
                profile = form.get("profile", [""])[0].lower()
                if profile not in PROFILE_NAMES:
                    raise ValueError("unknown profile")
                self.state.config["profile"] = profile
                save_config(self.state.config, self.state.config_paths)
                self.state.reload()
                self._send_json({"ok": True, "profile": profile})
                return
            if route == "api/policy/custom":
                rules = {key.removeprefix("rule."): values[0] for key, values in form.items() if key.startswith("rule.") and values}
                self.state.config.setdefault("policy", {})["custom"] = validate_rules(rules)
                self.state.config["profile"] = "custom"
                save_config(self.state.config, self.state.config_paths)
                self.state.reload()
                self._send_json({"ok": True, "profile": "custom"})
                return
            if route == "api/config/reset":
                from coding_tools_mcp.config import default_config

                save_config(default_config(self.state.config.get("workspace")), self.state.config_paths)
                self.state.reload()
                self._send_json({"ok": True})
                return
            if route == "api/setup":
                workspace = form.get("workspace", [""])[0]
                if not Path(workspace).expanduser().is_dir():
                    raise ValueError("workspace must be an existing directory")
                self.state.config["workspace"] = str(Path(workspace).expanduser().resolve())
                workspaces = [
                    str(item)
                    for item in self.state.config.get("workspaces", [])
                    if str(item) != self.state.config["workspace"]
                ]
                self.state.config["workspaces"] = [self.state.config["workspace"], *workspaces]
                profile = form.get("profile", ["balanced"])[0].lower()
                if profile not in PROFILE_NAMES:
                    raise ValueError("unknown profile")
                self.state.config["profile"] = profile
                save_config(self.state.config, self.state.config_paths)
                generate_mcp_token(self.state.config_paths)
                control_key = form.get("control_plane_key", [""])[0]
                if control_key.strip():
                    write_secret(self.state.config_paths.control_plane_key, control_key)
                self.state.reload()
                self._send_json({"ok": True, "auth": secret_status(self.state.config_paths)})
                return
            if route == "api/workspaces/add":
                root_path = Path(form.get("workspace", [""])[0]).expanduser().resolve()
                if not root_path.is_dir():
                    raise ValueError("workspace must be an existing directory")
                roots = [str(item) for item in self.state.config.get("workspaces", [])]
                if str(root_path) not in roots:
                    roots.append(str(root_path))
                self.state.config["workspaces"] = roots
                save_config(self.state.config, self.state.config_paths)
                self.state.reload()
                self._send_json({"ok": True, "workspaces": roots})
                return
            if route == "api/workspaces/remove":
                workspace = str(Path(form.get("workspace", [""])[0]).expanduser().resolve())
                active = str(self.state.config.get("workspace", ""))
                if workspace == active:
                    raise ValueError("the active workspace cannot be removed")
                roots = [str(item) for item in self.state.config.get("workspaces", []) if str(item) != workspace]
                self.state.config["workspaces"] = roots
                save_config(self.state.config, self.state.config_paths)
                self.state.reload()
                self._send_json({"ok": True, "workspaces": roots})
                return
            if route.startswith("api/approvals/"):
                parts = route.split("/")
                if len(parts) != 4 or parts[3] not in {"approve", "deny"}:
                    raise ValueError("invalid approval route")
                engine = ApprovalEngine(self.state.config_paths.approvals_db)
                if parts[3] == "approve":
                    engine.approve(parts[2])
                else:
                    engine.deny(parts[2])
                self._send_json({"ok": True})
                return
            self._send_json({"error": "unknown route"}, 404)
        except (OSError, ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, 400)

    def _route_page(self, route: str) -> bytes:
        status = _status(self.state)
        if route == "":
            body = "<h2>Dashboard</h2>" + _table(list(status.items()))
            if not status["sandbox_secure"]:
                body += '<p class="warning">SANDBOX: UNSAFE HOST MODE or unavailable</p>'
            return _page("Dashboard", body, self.state)
        if route == "permissions":
            profile = str(self.state.config.get("profile", "balanced"))
            rules = effective_rules(profile, self.state.config.get("policy", {}).get("custom", {}))
            options = "".join(f'<option value="{name}" {"selected" if name == profile else ""}>{name}</option>' for name in PROFILE_NAMES)
            rows: list[str] = []
            for capability in CAPABILITIES:
                decision_options = "".join(
                    f'<option value="{value}" {"selected" if value == rules[capability] else ""}>{value.upper()}</option>'
                    for value in ("auto", "ask", "deny")
                )
                rows.append(
                    f'<tr><td>{html.escape(capability)}</td><td><select name="rule.{html.escape(capability)}">{decision_options}</select></td></tr>'
                )
            rows_html = "".join(rows)
            body = f'<h2>Permissions</h2><form method="post" action="/api/policy/profile"><input type="hidden" name="csrf" value="{html.escape(self.state.csrf_token)}"><select name="profile">{options}</select><button>Apply profile</button></form><form method="post" action="/api/policy/custom"><input type="hidden" name="csrf" value="{html.escape(self.state.csrf_token)}"><table><tr><th>Capability</th><th>Decision</th></tr>{rows_html}</table><button>Save Custom matrix</button></form><p>Patch thresholds: {html.escape(str(self.state.config.get("patch")))}</p>'
            return _page("Permissions", body, self.state)
        if route == "setup":
            body = f'<h2>First-run setup</h2><form method="post" action="/api/setup"><input type="hidden" name="csrf" value="{html.escape(self.state.csrf_token)}"><label>Workspace <input name="workspace" value="{html.escape(str(self.state.config.get("workspace", "")))}" size="70"></label><br><label>Profile <select name="profile">{"".join(f"<option>{name}</option>" for name in PROFILE_NAMES)}</select></label><br><label>Secure MCP Tunnel key (optional, never displayed again) <input type="password" name="control_plane_key" autocomplete="off"></label><br><button>Save setup</button></form>'
            return _page("Setup", body, self.state)
        if route == "workspaces":
            roots = [str(item) for item in self.state.config.get("workspaces", [status["workspace"]])]
            rows_list: list[str] = []
            for root in roots:
                remove_form = ""
                if root != status["workspace"]:
                    remove_form = (
                        f'<form method="post" action="/api/workspaces/remove"><input type="hidden" '
                        f'name="csrf" value="{html.escape(self.state.csrf_token)}"><input type="hidden" '
                        f'name="workspace" value="{html.escape(root)}"><button>Remove</button></form>'
                    )
                rows_list.append(
                    f'<tr><td>{html.escape(root)}</td><td>{"active" if root == status["workspace"] else ""}</td><td>{remove_form}</td></tr>'
                )
            rows = "".join(rows_list)
            body = f'<h2>Configured workspaces</h2><table><tr><th>Root</th><th>State</th><th></th></tr>{rows}</table><form method="post" action="/api/workspaces/add"><input type="hidden" name="csrf" value="{html.escape(self.state.csrf_token)}"><input name="workspace" size="70" placeholder="/absolute/path"><button>Add root</button></form><p>The model cannot add or remove roots through MCP tools.</p>'
            return _page("Workspaces", body, self.state)
        if route == "approvals":
            pending = ApprovalEngine(self.state.config_paths.approvals_db).list_pending()
            rows = "".join(f'<tr><td>{html.escape(str(item["id"]))}</td><td>{html.escape(str(item["command_or_action"]))}</td><td><form method="post" action="/api/approvals/{html.escape(str(item["id"]))}/approve"><input type="hidden" name="csrf" value="{html.escape(self.state.csrf_token)}"><button>Approve once</button></form></td><td><form method="post" action="/api/approvals/{html.escape(str(item["id"]))}/deny"><input type="hidden" name="csrf" value="{html.escape(self.state.csrf_token)}"><button>Deny</button></form></td></tr>' for item in pending)
            return _page("Approvals", f"<h2>Pending approvals</h2><table><tr><th>ID</th><th>Operation</th><th></th><th></th></tr>{rows}</table>", self.state)
        if route == "diagnostics":
            return _page("Diagnostics", "<h2>Diagnostics</h2>" + _table([("bwrap", status["sandbox_backend"]), ("secure", status["sandbox_secure"]), ("MCP token", status["auth"]["mcp_token_configured"]), ("Tunnel key", status["auth"]["control_plane_key_configured"])]), self.state)
        if route == "about":
            return _page("About", "<h2>About</h2><p>DevMCP Runtime is an independent open-source project and is not affiliated with, endorsed by, or supported by OpenAI.</p>" + _table([("version", __version__), ("license", "Apache-2.0")]), self.state)
        if route == "tasks":
            tasks = TaskRegistry().list_tasks()
            rows = "".join(f"<tr><td>{html.escape(str(item['id']))}</td><td>{html.escape(str(item['category']))}</td><td>{html.escape(str(item['executable']))}</td><td>{html.escape(str(item['network_requirement']))}</td><td>{html.escape(str(item['approval_class']))}</td></tr>" for item in tasks)
            return _page("Tasks", f"<h2>Task registry</h2><table><tr><th>ID</th><th>Category</th><th>Executable</th><th>Network</th><th>Policy</th></tr>{rows}</table>", self.state)
        if route == "services":
            return _page("Services", "<h2>Services</h2>" + _table([("MCP", "inspect with devmcp status"), ("Tunnel", status["tunnel_state"])]), self.state)
        if route == "audit":
            events = read_recent_events(self.state.config_paths.audit_log)
            rows = "".join(
                "<tr>"
                + "".join(f"<td>{html.escape(str(item.get(key, '')))}</td>" for key in ("timestamp", "tool", "ok", "error_code", "policy_profile"))
                + "</tr>"
                for item in events
            )
            body = f'<h2>Audit log</h2><p>Recent local events; arguments, paths, commands, and secret values are excluded.</p><table><tr><th>Time</th><th>Tool</th><th>OK</th><th>Error</th><th>Policy</th></tr>{rows}</table>'
            return _page("Audit log", body, self.state)
        return _page("Not found", "<h2>Not found</h2>", self.state)


class UIHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: UIState):
        super().__init__(address, UIHandler)
        self.state = state


def serve_ui(host: str = UI_DEFAULT_HOST, port: int = UI_DEFAULT_PORT) -> int:
    state = UIState.load(host, port)
    server = UIHTTPServer((host, port), state)
    print(f"DevMCP UI listening on {state.origin}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0
