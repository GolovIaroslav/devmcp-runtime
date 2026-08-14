"""Execution-mode authority resolver for the DevMCP Runtime.

The runtime has two execution modes:

- plan  → read-only; apply_patch and exec_command are denied.
- build → full-access; direct OS user authority.  (default)

``resolve_execution_mode`` is the single entry-point used by the server,
CLI, and tests.  No policy-profile matrix, no approval engine, and no
per-capability decision table exists in this module.

Legacy ``safe``/``trusted``/``dangerous`` permission-mode values are
accepted only as thin ingress adapters at startup and are mapped
immediately to ``plan`` or ``build``; they carry no further authority.
"""

from __future__ import annotations

EXECUTION_MODES = ("plan", "build")
DEFAULT_EXECUTION_MODE = "build"

# Thin ingress-adapter constants — kept for CLI help text and tests only.
LEGACY_PERMISSION_MODES = ("safe", "trusted", "dangerous")


def resolve_execution_mode(
    execution_mode: str | None = None,
    permission_mode: str | None = None,
) -> tuple[str, str]:
    """Single runtime authority resolver for execution_mode and effective_access.

    Ingress mapping:
    - safe     → plan  (read-only)
    - trusted  → build (full-access)
    - dangerous → build (full-access)
    - plan     → plan  (read-only)
    - build    → build (full-access)

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
