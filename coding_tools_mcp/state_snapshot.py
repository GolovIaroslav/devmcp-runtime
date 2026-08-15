from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any

from .errors import ToolFailure
from .path_security import sensitive_raw_path_reason

MAX_HASH_BYTES = 2 * 1024 * 1024
DRIFT_FIELDS = (
    "branch",
    "local_head",
    "upstream",
    "dirty_paths",
    "staged_paths",
    "untracked_paths",
    "content_hashes",
)


def run_git(
    project: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise ToolFailure("GIT_NOT_FOUND", "git is required.", category="environment")
    return subprocess.run(
        [git, "-C", str(project), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
    )


def git_text(
    project: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 10,
) -> str | None:
    try:
        result = run_git(project, args, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, ToolFailure):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_paths(
    project: Path, args: list[str], *, env: dict[str, str] | None = None
) -> list[str]:
    git = shutil.which("git")
    if git is None:
        return []
    try:
        result = subprocess.run(
            [git, "-C", str(project), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def sanitize_repo_url(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    if re.match(r"^[^/@:]+@[^:]+:.+$", value):
        user_host, path = value.split(":", 1)
        host = user_host.split("@", 1)[1]
        return f"{host}/{path.removesuffix('.git')}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or parsed.netloc.split("@")[-1]
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{host}{parsed.path.removesuffix('.git')}"
    return value.removesuffix(".git")


def inventory_key(path: str) -> str:
    if sensitive_raw_path_reason(path) is None:
        return path
    return "sensitive:" + hashlib.sha256(path.encode()).hexdigest()[:16]


def content_hashes(project: Path, paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in paths:
        if sensitive_raw_path_reason(relative) is not None:
            continue
        candidate = project / relative
        try:
            stat = candidate.lstat()
        except OSError:
            continue
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or stat.st_size > MAX_HASH_BYTES
        ):
            continue
        digest = hashlib.sha256()
        try:
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 128), b""):
                    digest.update(chunk)
        except OSError:
            continue
        hashes[relative] = digest.hexdigest()
    return dict(sorted(hashes.items()))


def compare_snapshots(
    expected: dict[str, Any], actual: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        field: {"expected": expected.get(field), "actual": actual.get(field)}
        for field in DRIFT_FIELDS
        if expected.get(field) != actual.get(field)
    }


def state_fingerprint(snapshot: dict[str, Any]) -> str:
    canonical = {field: snapshot.get(field) for field in DRIFT_FIELDS}
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def collect_state_snapshot(
    project: Path,
    *,
    project_id: str | None,
    installed_service_version: str,
    installed_service_git_sha: str | None,
    protocol_version: str,
    writer_owner: str | None,
    logical_task: str | None,
    git_env: dict[str, str] | None = None,
    push_verified: bool | None = None,
    authoritative_remote_head: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    branch = git_text(project, ["branch", "--show-current"], env=git_env) or None
    local_head = git_text(project, ["rev-parse", "HEAD"], env=git_env)
    upstream = (
        git_text(
            project,
            [
                "for-each-ref",
                "--format=%(upstream:short)",
                f"refs/heads/{branch}",
            ],
            env=git_env,
        )
        if branch
        else None
    ) or None
    remote_tracking_head = (
        git_text(project, ["rev-parse", upstream], env=git_env) if upstream else None
    )
    unstaged = git_paths(project, ["diff", "--name-only", "-z"], env=git_env)
    staged = git_paths(project, ["diff", "--cached", "--name-only", "-z"], env=git_env)
    untracked = git_paths(
        project, ["ls-files", "--others", "--exclude-standard", "-z"], env=git_env
    )
    dirty_raw = sorted(set(unstaged) | set(staged))
    dirty_paths = sorted({inventory_key(item) for item in dirty_raw})
    staged_paths = sorted({inventory_key(item) for item in staged})
    untracked_paths = sorted({inventory_key(item) for item in untracked})
    hashes = content_hashes(project, sorted(set(dirty_raw) | set(untracked)))
    repo = sanitize_repo_url(
        git_text(project, ["remote", "get-url", "origin"], env=git_env)
    )
    last_raw = git_text(
        project,
        ["log", "-1", "--format=%H%x1f%P%x1f%an%x1f%aI%x1f%s"],
        env=git_env,
    )
    last_commit: dict[str, Any] | None = None
    if last_raw:
        fields = last_raw.split("\x1f", 4)
        if len(fields) == 5:
            last_commit = {
                "sha": fields[0],
                "parents": fields[1].split() if fields[1] else [],
                "author": fields[2],
                "author_date": fields[3],
                "subject": fields[4],
            }
    return {
        "repo": repo,
        "project_id": project_id,
        "branch": branch,
        "local_head": local_head,
        "upstream": upstream,
        "remote_head": authoritative_remote_head,
        "remote_tracking_head": remote_tracking_head,
        "pr_number": None,
        "pr_head": None,
        "dirty_paths": dirty_paths,
        "staged_paths": staged_paths,
        "untracked_paths": untracked_paths,
        "content_hashes": hashes,
        "last_commit": last_commit,
        "push_verified": push_verified,
        "workflow_runs_by_sha": {},
        "job_ids": [],
        "attempts": [],
        "pending_failures": [],
        "installed_service_version": installed_service_version,
        "installed_service_git_sha": installed_service_git_sha,
        "protocol_version": protocol_version,
        "writer_owner": writer_owner,
        "logical_task": logical_task,
        "timestamp": timestamp,
    }


def filter_ci_runs_for_sha(
    current_sha: str, runs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for run in runs:
        target = current if str(run.get("commit_sha") or "") == current_sha else stale
        target.append(run)
    return current, stale


def read_build_identity(
    *,
    config_path: str | None,
    package_version: str,
    protocol_version: str,
    env_sha: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if config_path:
        try:
            with Path(config_path).open("rb") as handle:
                loaded = tomllib.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
    sha = (env_sha or str(data.get("installed_runtime_sha") or "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sha = ""
    return {
        "package_version": package_version,
        "git_sha": sha or None,
        "build_sha": sha or None,
        "build_install_timestamp": data.get("installed_runtime_installed_at"),
        "source_repo": data.get("installed_runtime_source_repo"),
        "source_branch": data.get("installed_runtime_branch"),
        "dirty_build": data.get("installed_runtime_dirty_build"),
        "development_mode": bool(data.get("installed_runtime_development_mode", False)),
        "protocol_version": protocol_version,
    }


def handoff_text(
    *, checkpoint: dict[str, Any] | None, current: dict[str, Any], drift: dict[str, Any]
) -> str:
    lines = [
        "DEVMCP_CONTINUATION_STATE_V2",
        f"repo={current.get('repo')}",
        f"project_id={current.get('project_id')}",
        f"branch={current.get('branch')}",
        f"local_head={current.get('local_head')}",
        f"upstream={current.get('upstream')}",
        f"remote_head={current.get('remote_head')}",
        f"remote_tracking_head={current.get('remote_tracking_head')}",
        f"dirty_paths={json.dumps(current.get('dirty_paths', []), ensure_ascii=False)}",
        f"staged_paths={json.dumps(current.get('staged_paths', []), ensure_ascii=False)}",
        f"untracked_paths={json.dumps(current.get('untracked_paths', []), ensure_ascii=False)}",
        f"installed_service_version={current.get('installed_service_version')}",
        f"installed_service_git_sha={current.get('installed_service_git_sha')}",
        f"writer_owner={current.get('writer_owner')}",
        f"logical_task={current.get('logical_task')}",
        f"checkpoint_id={(checkpoint or {}).get('checkpoint_id')}",
        f"state_drift={json.dumps(drift, ensure_ascii=False, sort_keys=True)}",
        "github_external_state=not_collected_by_devmcp",
    ]
    if drift:
        lines.append(
            "next_safe_action=inspect drift and explicitly reconcile the actual branch/head before mutation"
        )
    elif (
        current.get("installed_service_git_sha")
        and current.get("local_head")
        and current.get("installed_service_git_sha") != current.get("local_head")
    ):
        lines.append(
            "next_safe_action=verify or update the installed DevMCP service build"
        )
    elif current.get("dirty_paths") or current.get("untracked_paths"):
        lines.append("next_safe_action=review the current working-tree inventory")
    else:
        lines.append(
            "next_safe_action=refresh authoritative GitHub PR/CI state before remote decisions"
        )
    return "\n".join(lines)
