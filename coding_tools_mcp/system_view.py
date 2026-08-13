from __future__ import annotations

import os
from pathlib import Path


# Declarative, audited read-only host metadata exposed to sandboxed developer
# tooling.  Keep credentials and account secrets out of these categories.
TOOLCHAIN_RUNTIME_DIRS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
    "/snap",
    "/etc/alternatives",
    "/usr/local/sdkman/candidates",
)

OS_METADATA_PATHS = (
    "/etc/os-release",
    "/etc/debian_version",
    "/etc/lsb-release",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/machine-id",
)

TOOLCHAIN_CONFIG_PATHS = (
    "/etc/maven",
    "/etc/gradle",
    "/etc/npmrc",
    "/etc/npm",
    "/etc/fonts",
    "/etc/xml",
)

TOOLCHAIN_CONFIG_GLOBS = ("/etc/java-*",)

DYNAMIC_LINKER_PATHS = (
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
)

NETWORK_METADATA_PATHS = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/gai.conf",
    "/etc/protocols",
    "/etc/services",
    "/run/systemd/resolve",
    "/run/resolvconf",
)

CA_CERTIFICATE_PATHS = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
)


def readonly_system_paths(*, allow_network: bool) -> tuple[Path, ...]:
    raw = [
        *TOOLCHAIN_RUNTIME_DIRS,
        *OS_METADATA_PATHS,
        *DYNAMIC_LINKER_PATHS,
        *TOOLCHAIN_CONFIG_PATHS,
    ]
    if allow_network:
        raw.extend(NETWORK_METADATA_PATHS)
        raw.extend(CA_CERTIFICATE_PATHS)
    paths: list[Path] = []
    for item in raw:
        path = Path(item)
        if path.exists() and path not in paths:
            paths.append(path)
    for pattern in TOOLCHAIN_CONFIG_GLOBS:
        parent = Path(pattern).parent
        for path in sorted(parent.glob(Path(pattern).name)):
            if path.exists() and path not in paths:
                paths.append(path)
    return tuple(paths)


def readonly_user_toolchain_paths() -> tuple[Path, ...]:
    """Expose only executable/runtime roots needed by user-installed toolchains.

    PATH directories are mounted read-only rather than the whole home. A small
    set of runtime roots is added for launchers whose executable is only a proxy
    (notably rustup/cargo).
    """

    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return ()
    paths: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        try:
            path = Path(raw).expanduser().resolve(strict=True)
            relative = path.relative_to(home)
        except (OSError, ValueError):
            continue
        if not path.is_dir() or not relative.parts:
            continue
        if relative.parts[0] in {".ssh", ".aws", ".config"}:
            continue
        if path not in paths:
            paths.append(path)
        if relative.parts[0] == ".cargo":
            rustup = home / ".rustup"
            if rustup.is_dir() and rustup not in paths:
                paths.append(rustup)
    return tuple(paths)


def forbidden_system_paths() -> tuple[str, ...]:
    """High-value examples used by security tests and diagnostics."""

    return (
        "/etc/shadow",
        "/etc/gshadow",
        "/root/.ssh",
        "/root/.aws",
        "/root/.config/gcloud",
    )
