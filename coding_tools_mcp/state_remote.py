from __future__ import annotations

from pathlib import Path

from .state_snapshot import git_text


def verify_remote_branch_head(
    project: Path,
    branch: str,
    remote: str,
    *,
    git_env: dict[str, str] | None = None,
) -> tuple[bool, str | None, str | None]:
    local_head = git_text(project, ["rev-parse", "HEAD"], env=git_env)
    remote_line = git_text(
        project,
        ["ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        env=git_env,
        timeout=30,
    )
    remote_head = remote_line.split()[0] if remote_line else None
    return bool(local_head and remote_head == local_head), local_head, remote_head
