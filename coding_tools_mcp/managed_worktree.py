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


def create_managed_worktree(
    canonical_project: Path, context_id: str, *, base_revision: str = "HEAD"
) -> tuple[Path, str]:
    git = shutil.which("git")
    if git is None:
        raise ToolFailure(
            "GIT_ERROR",
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
            base_revision,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ToolFailure(
            "GIT_ERROR",
            "Failed to create a managed linked worktree for the logical context.",
            category="runtime",
            details={
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "branch": branch,
                "path": str(path),
                "base_revision": base_revision,
            },
        )
    return path.resolve(strict=True), branch


def managed_worktree_root(canonical_project: Path) -> Path:
    return state_root(canonical_project) / "worktrees"


def _registered_managed_worktrees(canonical_project: Path) -> list[Path]:
    git = shutil.which("git")
    if git is None:
        return []
    root = managed_worktree_root(canonical_project).resolve()
    result = subprocess.run(
        [git, "-C", str(canonical_project), "worktree", "list", "--porcelain"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.removeprefix("worktree ")).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        paths.append(path)
    return paths


def cleanup_managed_worktree(canonical_project: Path, worktree: Path) -> str:
    """Remove one clean DevMCP-owned worktree without deleting its branch."""

    canonical = canonical_project.resolve(strict=True)
    path = worktree.resolve(strict=False)
    root = managed_worktree_root(canonical).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "ignored_unmanaged"
    if path not in _registered_managed_worktrees(canonical):
        return "ignored_unregistered"
    git = shutil.which("git")
    if git is None or not path.is_dir():
        return "preserved_error"
    status = subprocess.run(
        [git, "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if status.returncode != 0:
        return "preserved_error"
    if status.stdout.strip():
        return "preserved_dirty"
    removed = subprocess.run(
        [git, "-C", str(canonical), "worktree", "remove", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    return "removed_clean" if removed.returncode == 0 else "preserved_error"


def recover_managed_worktrees(canonical_project: Path) -> dict[str, int]:
    counts = {
        "found": 0,
        "removed_clean": 0,
        "preserved_dirty": 0,
        "preserved_error": 0,
    }
    for worktree in _registered_managed_worktrees(canonical_project):
        counts["found"] += 1
        outcome = cleanup_managed_worktree(canonical_project, worktree)
        if outcome in counts:
            counts[outcome] += 1
    return counts
