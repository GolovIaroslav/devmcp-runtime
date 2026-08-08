"""Data-driven permission profiles for the public DevMCP Runtime surface.

The low-level MCP runtime keeps its older ``safe``/``trusted`` execution modes
for backwards compatibility.  This module is the stable, vendor-neutral
policy vocabulary used by configuration, the CLI, and the local UI.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PROFILE_NAMES = ("safe", "balanced", "power", "custom")
DECISIONS = ("auto", "ask", "deny")
DEFAULT_PROFILE = "balanced"

CAPABILITIES = (
    "workspace.read",
    "workspace.patch_small",
    "workspace.patch_destructive",
    "workspace.create",
    "workspace.delete",
    "workspace.move",
    "exec.registered",
    "exec.arbitrary",
    "network.public",
    "network.host_local",
    "deps.install",
    "db.migrate",
    "server.loopback",
    "server.public",
    "git.branch",
    "git.commit",
    "git.branch",
    "git.push",
    "env.sensitive",
    "agent.delegate",
)


def _profile(auto: set[str], ask: set[str], deny: set[str]) -> dict[str, str]:
    result = {capability: "deny" for capability in CAPABILITIES}
    result.update({capability: "auto" for capability in auto})
    result.update({capability: "ask" for capability in ask})
    result.update({capability: "deny" for capability in deny})
    return result


_FLOOR_DENY = {
    "server.public",
    "env.sensitive",
    "agent.delegate",
}

_SAFE_AUTO = {
    "workspace.read",
    "workspace.patch_small",
    "exec.registered",
    "server.loopback",
}

_SAFE_ASK = {
    "workspace.create",
    "workspace.delete",
    "workspace.move",
    "exec.arbitrary",
    "network.public",
    "network.host_local",
    "deps.install",
    "db.migrate",
    "server.public",
    "git.branch",
    "git.commit",
    "git.push",
    "workspace.patch_destructive",
    "env.sensitive",
}

_PROFILES: dict[str, dict[str, str]] = {
    "safe": _profile(_SAFE_AUTO, _SAFE_ASK, _FLOOR_DENY),
    "balanced": _profile(
        _SAFE_AUTO | {"workspace.create", "git.branch", "git.commit"},
        _SAFE_ASK | {"workspace.delete", "workspace.move", "workspace.patch_destructive"},
        _FLOOR_DENY,
    ),
    "power": _profile(
        _SAFE_AUTO
        | {
            "workspace.create",
            "workspace.delete",
            "workspace.move",
            "exec.registered",
            "exec.arbitrary",
            "network.public",
            "network.host_local",
            "deps.install",
            "db.migrate",
            "server.loopback",
            "git.branch",
            "git.commit",
            "workspace.patch_destructive",
        },
        {"git.push", "server.public"},
        _FLOOR_DENY,
    ),
}


def profile_rules(name: str) -> dict[str, str]:
    """Return a detached capability matrix for a built-in profile."""

    normalized = name.strip().lower()
    if normalized not in PROFILE_NAMES:
        raise ValueError(f"unknown policy profile: {name}")
    if normalized == "custom":
        return {capability: "deny" for capability in CAPABILITIES}
    return deepcopy(_PROFILES[normalized])


def validate_rules(rules: dict[str, Any]) -> dict[str, str]:
    """Validate and normalize a custom capability matrix."""

    normalized: dict[str, str] = {}
    for capability in CAPABILITIES:
        value = str(rules.get(capability, "deny")).lower()
        if value not in DECISIONS:
            raise ValueError(f"policy decision for {capability} must be auto, ask, or deny")
        if capability in _FLOOR_DENY and value != "deny":
            raise ValueError(f"minimum security floor denies {capability}")
        normalized[capability] = value
    unknown = sorted(set(rules) - set(CAPABILITIES))
    if unknown:
        raise ValueError(f"unknown policy capabilities: {', '.join(unknown)}")
    return normalized


def effective_rules(profile: str, custom: dict[str, Any] | None = None) -> dict[str, str]:
    if profile == "custom":
        return validate_rules(custom or {})
    return profile_rules(profile)


def decision(profile: str, capability: str, custom: dict[str, Any] | None = None) -> str:
    rules = effective_rules(profile, custom)
    if capability not in rules:
        raise ValueError(f"unknown policy capability: {capability}")
    return rules[capability]
