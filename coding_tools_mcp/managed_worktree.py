from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .errors import ToolFailure
from .state_store import state_root


def managed_worktree_branch(context_id: str) -> str:
    digest = hashlib.sha256(context_id.encode()).hexdigest()[:16]
    return f"devmcp/context-{digest}"


def create_managed_worktree(canonical_project: Path, context_id: str) -> tuple[Path, str]:
    git = shutil.which("git")
    if git is None:
        raise ToolFailure(
            "EXECUTABLE_NOT_FOUND",
            "git is required to create a managed linked worktree.",
            category="environment",
        )
    branch = managed_worktree_branch(context_id)
    digest = hashlib.sha256(context_id.encode()).hexdigest()[:24]
    worktree_root = state_root(canonical_project) / "worktrees"
    worktree_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = worktree_root / digest
    result = subprocess.run(
        [
            git,
            "-C",
            str(canonical_project),
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            "HEAD",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ToolFailure(
            "GIT_COMMAND_FAILED",
            "Failed to create a managed linked worktree for the logical context.",
            category="runtime",
            details={
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "branch": branch,
                "path": str(path),
            },
        )
    return path.resolve(strict=True), branch
