from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .errors import ToolFailure


ExecutorName = Literal[
    "local_sandbox",
    "inherited_sandbox",
    "unsafe_host",
    "isolated_worktree",
    "ephemeral_container",
    "remote",
]


@dataclass(frozen=True)
class ExecutionRequirements:
    writable_roots: int = 1
    readable_roots: int = 1
    network: bool = False
    network_targets: bool = False
    transactional_apply: bool = False
    interactive_tty: bool = False


@dataclass(frozen=True)
class ExecutorBackend:
    name: ExecutorName
    configured: bool
    secure: bool
    supports_additional_roots: bool
    supports_transaction: bool
    supports_tty: bool
    supports_network: bool
    supports_network_targets: bool
    reason: str
    trusted_runner: str | None = None

    def describe(self) -> dict[str, object]:
        return asdict(self)

    def satisfies(self, requirements: ExecutionRequirements) -> bool:
        if not self.configured:
            return False
        if not self.secure and self.name != "unsafe_host":
            return False
        if (
            requirements.readable_roots > 1 or requirements.writable_roots > 1
        ) and not self.supports_additional_roots:
            return False
        if requirements.transactional_apply and not self.supports_transaction:
            return False
        if requirements.interactive_tty and not self.supports_tty:
            return False
        if requirements.network and not self.supports_network:
            return False
        if requirements.network_targets and not self.supports_network_targets:
            return False
        return True


class ExecutorRegistry:
    """Small scheduler over security-enforced execution backends.

    Container execution is deliberately operator-mediated.  The runtime never
    exposes Docker/Podman sockets or lets the model choose a host container CLI.
    A future/optional trusted runner is configured out-of-band and may consume
    runtime-owned snapshots/artifacts through a separate adapter.
    """

    def __init__(
        self,
        *,
        sandbox_backend_name: str,
        sandbox_secure: bool,
        sandbox_available: bool,
        container_runner: str | None = None,
    ) -> None:
        self.sandbox_backend_name = sandbox_backend_name
        self.container_runner = self._validate_runner(container_runner)
        local_configured = (
            sandbox_backend_name == "bwrap" and sandbox_secure and sandbox_available
        )
        inherited_configured = (
            sandbox_backend_name == "inherited"
            and sandbox_secure
            and sandbox_available
        )
        unsafe_configured = sandbox_backend_name == "unsafe" and sandbox_available
        self.backends: dict[ExecutorName, ExecutorBackend] = {
            "local_sandbox": ExecutorBackend(
                "local_sandbox",
                local_configured,
                local_configured,
                True,
                True,
                True,
                True,
                False,
                (
                    "bubblewrap filesystem/process boundary"
                    if local_configured
                    else "local bubblewrap backend is unavailable"
                ),
            ),
            "inherited_sandbox": ExecutorBackend(
                "inherited_sandbox",
                inherited_configured,
                inherited_configured,
                False,
                False,
                True,
                True,
                False,
                (
                    "attested parent DevMCP namespace boundary"
                    if inherited_configured
                    else "no attested parent DevMCP sandbox"
                ),
            ),
            "unsafe_host": ExecutorBackend(
                "unsafe_host",
                unsafe_configured,
                False,
                True,
                False,
                True,
                True,
                False,
                (
                    "explicit operator legacy host execution without sandbox isolation"
                    if unsafe_configured
                    else "unsafe host execution was not explicitly selected"
                ),
            ),
            "isolated_worktree": ExecutorBackend(
                "isolated_worktree",
                True,
                True,
                False,
                True,
                False,
                False,
                False,
                "Git worktree isolation for delegated/batch workflows",
            ),
            "ephemeral_container": ExecutorBackend(
                "ephemeral_container",
                self.container_runner is not None,
                self.container_runner is not None,
                True,
                True,
                False,
                True,
                True,
                (
                    "operator-configured trusted ephemeral-container runner"
                    if self.container_runner is not None
                    else "DEVMCP_EPHEMERAL_CONTAINER_RUNNER is not configured"
                ),
                trusted_runner=self.container_runner,
            ),
            "remote": ExecutorBackend(
                "remote",
                False,
                False,
                True,
                True,
                False,
                True,
                True,
                "reserved extension point; no remote executor is configured",
            ),
        }

    @staticmethod
    def _validate_runner(raw: str | None) -> str | None:
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "DEVMCP_EPHEMERAL_CONTAINER_RUNNER must be an absolute executable path.",
                category="validation",
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ToolFailure(
                "CAPABILITY_UNAVAILABLE",
                "Configured ephemeral-container runner does not exist.",
                category="environment",
                details={"path": str(path)},
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ToolFailure(
                "CAPABILITY_UNAVAILABLE",
                "Configured ephemeral-container runner is not executable.",
                category="environment",
                details={"path": str(resolved)},
            )
        runner_stat = resolved.stat()
        parent_stat = resolved.parent.stat()
        if runner_stat.st_nlink != 1:
            raise ToolFailure(
                "ACCESS_DENIED",
                "Configured ephemeral-container runner must not have hard-link aliases.",
                category="security",
                details={"path": str(resolved), "link_count": runner_stat.st_nlink},
            )
        if runner_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ToolFailure(
                "ACCESS_DENIED",
                "Configured ephemeral-container runner must not be group/world writable.",
                category="security",
                details={"path": str(resolved)},
            )
        if parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ToolFailure(
                "ACCESS_DENIED",
                "Configured ephemeral-container runner directory must not be group/world writable.",
                category="security",
                details={"path": str(resolved.parent)},
            )
        if hasattr(os, "geteuid"):
            allowed_owners = {0, os.geteuid()}
            if runner_stat.st_uid not in allowed_owners or parent_stat.st_uid not in allowed_owners:
                raise ToolFailure(
                    "ACCESS_DENIED",
                    "Configured ephemeral-container runner and its directory must be owned by root or the DevMCP service user.",
                    category="security",
                    details={"path": str(resolved)},
                )
        if path.name.lower() in {"docker", "podman", "nerdctl"} or resolved.name.lower() in {
            "docker",
            "podman",
            "nerdctl",
        }:
            raise ToolFailure(
                "ACCESS_DENIED",
                "Direct host container CLIs are not valid DevMCP executor runners.",
                category="security",
            )
        return str(resolved)

    def reject_runner_below(self, roots: list[Path] | tuple[Path, ...]) -> None:
        """Do not host-execute a runner that model-authorized trees can replace."""

        if self.container_runner is None:
            return
        runner = Path(self.container_runner).resolve(strict=True)
        for raw_root in roots:
            root = raw_root.expanduser().resolve(strict=True)
            try:
                runner.relative_to(root)
            except ValueError:
                continue
            raise ToolFailure(
                "ACCESS_DENIED",
                "Configured ephemeral-container runner is inside a project/grantable tree and therefore is not a trusted host executable.",
                category="security",
                details={"runner": str(runner), "conflicting_root": str(root)},
            )

    @classmethod
    def from_environment(
        cls,
        *,
        sandbox_backend_name: str,
        sandbox_secure: bool,
        sandbox_available: bool,
    ) -> "ExecutorRegistry":
        return cls(
            sandbox_backend_name=sandbox_backend_name,
            sandbox_secure=sandbox_secure,
            sandbox_available=sandbox_available,
            container_runner=os.environ.get("DEVMCP_EPHEMERAL_CONTAINER_RUNNER"),
        )

    def describe(self) -> dict[str, dict[str, object]]:
        return {name: backend.describe() for name, backend in self.backends.items()}

    def select(
        self,
        requirements: ExecutionRequirements,
        *,
        preferred: str = "auto",
    ) -> ExecutorBackend:
        if preferred != "auto":
            if preferred not in self.backends:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"Unknown executor backend: {preferred}",
                    category="validation",
                )
            backend = self.backends[preferred]  # type: ignore[index]
            if not backend.configured:
                raise ToolFailure(
                    "CAPABILITY_UNAVAILABLE",
                    f"Requested executor backend '{preferred}' is unavailable.",
                    category="environment",
                    details={"backend": backend.describe()},
                )
            if not backend.satisfies(requirements):
                raise ToolFailure(
                    "CAPABILITY_UNAVAILABLE",
                    f"Executor backend '{preferred}' cannot satisfy the task requirements.",
                    category="environment",
                    details={
                        "backend": backend.describe(),
                        "requirements": asdict(requirements),
                    },
                )
            return backend

        # Prefer the least-powerful local execution boundary.  Containers are
        # used automatically only when local/inherited cannot meet the declared
        # requirements and the operator explicitly configured the runner.
        for name in (
            "local_sandbox",
            "inherited_sandbox",
            "ephemeral_container",
            "unsafe_host",
        ):
            backend = self.backends[name]
            if backend.satisfies(requirements):
                return backend
        raise ToolFailure(
            "CAPABILITY_UNAVAILABLE",
            "No configured executor backend can satisfy the task requirements.",
            category="environment",
            details={
                "requirements": asdict(requirements),
                "backends": self.describe(),
            },
        )
