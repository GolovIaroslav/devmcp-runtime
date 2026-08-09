"""Persistent, non-secret DevMCP Runtime configuration.

Configuration is intentionally outside an authoritative workspace.  Secret
values are never serialized into the TOML file; only paths and boolean status
are exposed to operator-facing commands and the UI.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from .policy import CAPABILITIES, DEFAULT_PROFILE, effective_rules

CONFIG_SCHEMA_VERSION = 1
PUBLIC_CONFIG_ENV = "DEVMCP_CONFIG_DIR"
LEGACY_RUNTIME_DIR = Path("~/.config/chatgpt-dev-runtime").expanduser()
LEGACY_TUNNEL_DIR = Path("~/.config/tunnel-client").expanduser()


@dataclass(frozen=True)
class ConfigPaths:
    root: Path
    config_file: Path
    secrets_dir: Path
    mcp_token: Path
    mcp_authorization_header: Path
    control_plane_key: Path
    tunnel_health_url: Path
    approvals_db: Path
    audit_log: Path


def paths() -> ConfigPaths:
    configured = os.environ.get(PUBLIC_CONFIG_ENV, "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        root = (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "devmcp-runtime"
    secrets_dir = root / "secrets"
    return ConfigPaths(
        root=root,
        config_file=root / "config.toml",
        secrets_dir=secrets_dir,
        mcp_token=secrets_dir / "mcp-token",
        mcp_authorization_header=secrets_dir / "mcp-authorization-header",
        control_plane_key=secrets_dir / "control-plane-api-key",
        tunnel_health_url=root / "tunnel-health.url",
        approvals_db=root / "approvals.db",
        audit_log=root / "audit.jsonl",
    )


def default_config(workspace: str | None = None) -> dict[str, Any]:
    root = str(Path(workspace or os.getcwd()).expanduser().resolve())
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "workspace": root,
        "workspaces": [root],
        "profile": DEFAULT_PROFILE,
        "sandbox_backend": "bwrap",
        "mcp_host": "127.0.0.1",
        "mcp_port": 47157,
        "ui_host": "127.0.0.1",
        "ui_port": 47158,
        "tunnel_id": "",
        "tunnel_alias": "devmcp-runtime",
        "tunnel_profile": "sample_mcp_with_dcr",
        "patch": {"max_removed_lines": 200, "max_removed_percent": 30.0},
        "policy": {"custom": {capability: "deny" for capability in CAPABILITIES}},
        "telemetry": {"enabled": False},
    }


def ensure_dirs(target: ConfigPaths | None = None) -> ConfigPaths:
    selected = target or paths()
    selected.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected.secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod_private(selected.root)
    _chmod_private(selected.secrets_dir)
    return selected


def _chmod_private(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass


def _chmod_secret(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def migrate_legacy(target: ConfigPaths | None = None) -> list[str]:
    """Import known legacy secret files without removing the source files."""

    selected = ensure_dirs(target)
    imported: list[str] = []
    legacy_tokens = (LEGACY_RUNTIME_DIR / "mcp-token", LEGACY_RUNTIME_DIR / "secrets" / "mcp-token")
    legacy_keys = (LEGACY_TUNNEL_DIR / "control-plane-api-key", LEGACY_RUNTIME_DIR / "control-plane-api-key")
    source = next((item for item in legacy_tokens if item.is_file()), None)
    if source is not None and not selected.mcp_token.exists():
        _copy_secret(source, selected.mcp_token)
        imported.append("mcp-token")
    if not selected.control_plane_key.exists():
        source = next((item for item in legacy_keys if item.is_file()), None)
        if source is not None:
            _copy_secret(source, selected.control_plane_key)
            imported.append("control-plane-api-key")
    if not selected.approvals_db.exists():
        legacy_db = LEGACY_RUNTIME_DIR / "approvals.db"
        if legacy_db.is_file():
            shutil.copy2(legacy_db, selected.approvals_db)
            if os.name != "nt":
                selected.approvals_db.chmod(0o600)
            imported.append("approvals-db")
    return imported


def _copy_secret(source: Path, destination: Path) -> None:
    value = source.read_bytes()
    if not value:
        return
    _atomic_write(destination, value, mode=0o600)


def load_config(target: ConfigPaths | None = None, *, workspace: str | None = None) -> dict[str, Any]:
    selected = ensure_dirs(target)
    migrate_legacy(selected)
    if not selected.config_file.exists():
        config = default_config(workspace)
        _deep_merge(config, _legacy_settings())
        save_config(config, selected)
        return config
    try:
        with selected.config_file.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid configuration: {selected.config_file}: {exc}") from exc
    config = default_config(workspace)
    _deep_merge(config, loaded)
    validate_config(config)
    return config


def _legacy_settings() -> dict[str, Any]:
    """Recover non-secret tunnel settings without importing arbitrary YAML."""

    profile = LEGACY_TUNNEL_DIR / "sample_mcp_with_dcr.yaml"
    try:
        text = profile.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.search(r"[\"']?tunnel_id[\"']?\s*:\s*[\"']([^\"'\s,}]+)", text)
    if not match:
        return {}
    return {"tunnel_id": match.group(1), "tunnel_profile": profile.stem}


def validate_config(config: dict[str, Any]) -> None:
    if int(config.get("schema_version", 0)) != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"unsupported config schema version: {config.get('schema_version')}")
    workspace = Path(str(config.get("workspace", ""))).expanduser()
    if not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    workspaces = config.get("workspaces", [str(workspace)])
    if not isinstance(workspaces, list) or not all(Path(str(item)).expanduser().is_absolute() for item in workspaces):
        raise ValueError("workspaces must be a list of absolute paths")
    profile = str(config.get("profile", DEFAULT_PROFILE)).lower()
    if profile not in {"safe", "balanced", "power", "custom"}:
        raise ValueError("profile must be safe, balanced, power, or custom")
    patch = config.get("patch", {})
    if int(patch.get("max_removed_lines", 0)) < 0 or float(patch.get("max_removed_percent", 0)) < 0:
        raise ValueError("patch thresholds cannot be negative")
    custom = config.get("policy", {}).get("custom", {})
    effective_rules(profile, custom)


def _deep_merge(destination: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(destination.get(key), dict):
            _deep_merge(destination[key], value)
        else:
            destination[key] = value


def save_config(config: dict[str, Any], target: ConfigPaths | None = None) -> None:
    selected = ensure_dirs(target)
    validate_config(config)
    _atomic_write(selected.config_file, _toml_bytes(config), mode=0o600)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _toml_bytes(config: dict[str, Any]) -> bytes:
    lines: list[str] = []
    scalar = {key: value for key, value in config.items() if not isinstance(value, dict)}
    for key, value in scalar.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        lines.append("")
        lines.append(f"[{section}]")
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{key} = {_toml_value(value)}")
        for subsection, nested in values.items():
            if not isinstance(nested, dict):
                continue
            lines.append("")
            lines.append(f"[{section}.{subsection}]")
            for key, value in nested.items():
                lines.append(f"{json.dumps(str(key))} = {_toml_value(value)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, mode) if hasattr(os, "fchmod") else None
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _chmod_secret(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_secret(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_secret(path: Path, value: str) -> None:
    if not value.strip():
        raise ValueError("secret cannot be empty")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod_private(path.parent)
    _atomic_write(path, (value.strip() + "\n").encode("utf-8"), mode=0o600)


def ensure_mcp_authorization_header(target: ConfigPaths | None = None) -> Path:
    """Materialize the MCP bearer header for tunnel-client's file reference."""

    selected = ensure_dirs(target)
    token = read_secret(selected.mcp_token)
    if not token:
        raise ValueError("MCP bearer file is empty")
    write_secret(selected.mcp_authorization_header, f"Bearer {token}")
    return selected.mcp_authorization_header


def generate_mcp_token(target: ConfigPaths | None = None) -> Path:
    selected = ensure_dirs(target)
    write_secret(selected.mcp_token, secrets.token_urlsafe(32))
    ensure_mcp_authorization_header(selected)
    return selected.mcp_token


def secret_status(target: ConfigPaths | None = None) -> dict[str, Any]:
    selected = target or paths()
    return {
        "mcp_token_configured": bool(read_secret(selected.mcp_token)),
        "mcp_token_path": str(selected.mcp_token),
        "control_plane_key_configured": bool(read_secret(selected.control_plane_key)),
        "control_plane_key_path": str(selected.control_plane_key),
    }


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    result["secrets"] = secret_status()
    return result


def get_key(config: dict[str, Any], key: str) -> Any:
    current: Any = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(key)
        current = current[part]
    return current


def set_key(config: dict[str, Any], key: str, value: Any) -> None:
    parts = [part for part in key.split(".") if part]
    if not parts or any(part.lower() in {"secret", "token", "password", "api_key", "key"} for part in parts):
        raise ValueError("secret values must be stored through the auth setup flow")
    current = config
    for part in parts[:-1]:
        item = current.setdefault(part, {})
        if not isinstance(item, dict):
            raise ValueError(f"{key} is not a nested configuration key")
        current = item
    current[parts[-1]] = value
    validate_config(config)
