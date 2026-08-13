"""Data-driven permission profiles for the public DevMCP Runtime surface.

The low-level MCP runtime keeps its older ``safe``/``trusted`` execution modes
as compatibility presets. This module is the authoritative, vendor-neutral
policy vocabulary used by configuration, the CLI, the local UI, and runtime
capability gates.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PROFILE_NAMES = ("safe", "balanced", "power", "autonomous", "custom")
DECISIONS = ("auto", "ask", "deny")
DEFAULT_PROFILE = "balanced"
UNIMPLEMENTED_CAPABILITIES: frozenset[str] = frozenset()

CAPABILITIES = (
    "workspace.read",
    "workspace.additional_read_root",
    "workspace.additional_write_root",
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
    "git.sync",
    "git.push",
    "env.sensitive",
    "service.manage",
    "policy.manage",
    "agent.delegate",
    "executor.container",
)


def _profile(auto: set[str], ask: set[str], deny: set[str]) -> dict[str, str]:
    overlap = (auto & ask) | (auto & deny) | (ask & deny)
    if overlap:
        raise ValueError(
            "policy profile capability sets must be disjoint: "
            + ", ".join(sorted(overlap))
        )
    result = {capability: "deny" for capability in CAPABILITIES}
    result.update({capability: "auto" for capability in auto})
    result.update({capability: "ask" for capability in ask})
    result.update({capability: "deny" for capability in deny})
    return result


# Profiles do not contain hidden product-policy denials. Real host-boundary
# protections (auth, loopback defaults, bwrap, path validation, and environment
# filtering) are enforced by the runtime independently of this matrix.
_FLOOR_DENY: set[str] = set()

_SAFE_AUTO = {
    "workspace.read",
    "workspace.patch_small",
    "exec.registered",
    "server.loopback",
}

_SAFE_ASK = {
    "workspace.additional_read_root",
    "workspace.additional_write_root",
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
    "git.sync",
    "git.push",
    "workspace.patch_destructive",
    "env.sensitive",
    "service.manage",
    "policy.manage",
    "executor.container",
}

_PROFILES: dict[str, dict[str, str]] = {
    "safe": _profile(_SAFE_AUTO, _SAFE_ASK, _FLOOR_DENY),
    "balanced": _profile(
        _SAFE_AUTO
        | {
            "workspace.create",
            "workspace.additional_read_root",
            "git.branch",
            "git.commit",
        },
        (_SAFE_ASK - {"workspace.create", "git.branch", "git.commit"})
        - {"workspace.additional_read_root"}
        | {"workspace.delete", "workspace.move", "workspace.patch_destructive"},
        _FLOOR_DENY,
    ),
    "power": _profile(
        _SAFE_AUTO
        | {
            "workspace.create",
            "workspace.additional_read_root",
            "workspace.additional_write_root",
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
            "git.sync",
            "workspace.patch_destructive",
            "service.manage",
        },
        {"git.push", "server.public", "env.sensitive", "policy.manage"},
        _FLOOR_DENY,
    ),
    "autonomous": _profile(
        set(CAPABILITIES) - set(UNIMPLEMENTED_CAPABILITIES),
        set(),
        set(UNIMPLEMENTED_CAPABILITIES),
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
            raise ValueError(
                f"policy decision for {capability} must be auto, ask, or deny"
            )
        if capability in UNIMPLEMENTED_CAPABILITIES and value != "deny":
            raise ValueError(f"{capability} is not implemented in this runtime release")
        normalized[capability] = value
    unknown = sorted(set(rules) - set(CAPABILITIES))
    if unknown:
        raise ValueError(f"unknown policy capabilities: {', '.join(unknown)}")
    return normalized


def effective_rules(
    profile: str, custom: dict[str, Any] | None = None
) -> dict[str, str]:
    if profile == "custom":
        return validate_rules(custom or {})
    return profile_rules(profile)


def decision(
    profile: str, capability: str, custom: dict[str, Any] | None = None
) -> str:
    rules = effective_rules(profile, custom)
    if capability not in rules:
        raise ValueError(f"unknown policy capability: {capability}")
    return rules[capability]


EXECUTION_MODES = ("plan", "build")
DEFAULT_EXECUTION_MODE = "build"


def resolve_execution_mode(
    execution_mode: str | None = None,
    permission_mode: str | None = None,
) -> tuple[str, str]:
    """Single runtime authority resolver for execution_mode and effective_access.

    Ingress mapping:
    - safe -> plan (read-only)
    - trusted -> build (full-access)
    - dangerous -> build (full-access)
    - plan -> plan (read-only)
    - build -> build (full-access)

    Default is build (full-access).
    """
    if execution_mode is not None and str(execution_mode).strip():
        mode = str(execution_mode).strip().lower()
        if mode not in EXECUTION_MODES:
            raise ValueError(f"unknown execution_mode: {execution_mode}")
        return mode, effective_access(mode)

    if permission_mode is not None and str(permission_mode).strip():
        perm = str(permission_mode).strip().lower()
        mapping = {
            "safe": ("plan", "read-only"),
            "trusted": ("build", "full-access"),
            "dangerous": ("build", "full-access"),
        }
        if perm in mapping:
            return mapping[perm]
        raise ValueError(f"unknown legacy permission_mode: {permission_mode}")

    return DEFAULT_EXECUTION_MODE, effective_access(DEFAULT_EXECUTION_MODE)


def effective_access(execution_mode: str) -> str:
    mode = str(execution_mode).strip().lower()
    if mode == "plan":
        return "read-only"
    if mode == "build":
        return "full-access"
    raise ValueError(f"unknown execution_mode: {execution_mode}")


def legacy_profile(permission_mode: str | None) -> str:
    """Map the retired execution modes to a profile when no profile was selected.

    The mapping is deliberately used only as a compatibility default. An
    explicitly selected profile always wins over a legacy command-line mode.
    """
    if not permission_mode:
        return "balanced"
    mode = permission_mode.strip().lower()
    mapping = {"safe": "safe", "trusted": "power", "dangerous": "autonomous"}
    return mapping.get(mode, "balanced")
