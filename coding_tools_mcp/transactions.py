from __future__ import annotations

import difflib
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import ToolFailure
from .patching import AtomicPatchCommitter, FileBaseline, StagedFile


MAX_TRANSACTION_FILES = 50_000
MAX_TRANSACTION_CHANGED_FILES = 2_000
MAX_TRANSACTION_APPLY_BYTES = 128 * 1024 * 1024
MAX_TRANSACTION_DIFF_BYTES = 256 * 1024


@dataclass(frozen=True)
class SnapshotEntry:
    kind: str
    digest: str | None
    mode: int | None
    size: int
    link_target: str | None = None


@dataclass(frozen=True)
class TransactionChange:
    path: str
    operation: str
    bytes: int
    mode: int | None


def _ignored_name(name: str) -> bool:
    return name == ".git" or name.startswith(".devmcp-")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), size


def capture_snapshot(root: Path) -> dict[str, SnapshotEntry]:
    """Hash one execution snapshot without following symlinks."""

    root = root.resolve(strict=True)
    result: dict[str, SnapshotEntry] = {}
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        directory, rel_parts = stack.pop()
        for entry in os.scandir(directory):
            if _ignored_name(entry.name):
                continue
            child_parts = rel_parts + (entry.name,)
            rel = "/".join(child_parts)
            if len(result) >= MAX_TRANSACTION_FILES:
                raise ToolFailure(
                    "TRANSACTION_TOO_LARGE",
                    "Execution snapshot contains too many paths for a safe transaction.",
                    category="runtime",
                    details={"max_paths": MAX_TRANSACTION_FILES},
                )
            if entry.is_symlink():
                try:
                    target = os.readlink(entry.path)
                    mode = stat.S_IMODE(os.lstat(entry.path).st_mode)
                except OSError as exc:
                    raise ToolFailure(
                        "TRANSACTION_SNAPSHOT_FAILED",
                        f"Could not inspect snapshot symlink: {rel}",
                        category="runtime",
                    ) from exc
                result[rel] = SnapshotEntry("symlink", None, mode, 0, target)
                continue
            if entry.is_dir(follow_symlinks=False):
                mode = stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode)
                result[rel] = SnapshotEntry("dir", None, mode, 0)
                stack.append((Path(entry.path), child_parts))
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ToolFailure(
                    "TRANSACTION_UNSAFE_CHANGE",
                    f"Unsupported filesystem object in execution snapshot: {rel}",
                    category="security",
                )
            digest, size = _hash_file(Path(entry.path))
            mode = stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode)
            result[rel] = SnapshotEntry("file", digest, mode, size)
    return result


class ExecutionTransaction:
    """Apply only command-created snapshot deltas back to an authoritative root.

    The authoritative tree is never reset.  Baselines are content+mode hashes
    captured from the secret-filtered execution snapshot.  AtomicPatchCommitter
    verifies every authoritative target still matches that baseline before any
    replacement, and rolls the whole staged set back if installation fails.
    """

    def __init__(
        self,
        *,
        authoritative_root: Path,
        snapshot_root: Path,
        validate_relative_path: Callable[[str], None],
    ) -> None:
        self.authoritative_root = authoritative_root.resolve(strict=True)
        self.snapshot_root = snapshot_root.resolve(strict=True)
        self.validate_relative_path = validate_relative_path
        self.before = capture_snapshot(self.snapshot_root)

    def _diff_entries(
        self,
    ) -> tuple[dict[str, SnapshotEntry], list[tuple[str, SnapshotEntry | None, SnapshotEntry | None]]]:
        after = capture_snapshot(self.snapshot_root)
        raw: list[tuple[str, SnapshotEntry | None, SnapshotEntry | None]] = []
        for rel in sorted(set(self.before) | set(after)):
            before = self.before.get(rel)
            current = after.get(rel)
            if before == current:
                continue
            if (before and before.kind == "symlink") or (
                current and current.kind == "symlink"
            ):
                raise ToolFailure(
                    "TRANSACTION_UNSAFE_CHANGE",
                    f"Transactional execution cannot create, replace, or modify symlinks: {rel}",
                    category="security",
                    details={"path": rel},
                )
            if before is not None and current is not None and before.kind != current.kind:
                raise ToolFailure(
                    "TRANSACTION_UNSAFE_CHANGE",
                    f"Transactional execution cannot change filesystem object type: {rel}",
                    category="security",
                    details={"path": rel, "before": before.kind, "after": current.kind},
                )
            # Empty directory creation/removal is intentionally not persisted;
            # parent directories for created files are created atomically by
            # the committer.  File<->directory replacement was rejected above.
            if (before is None or before.kind == "dir") and (
                current is None or current.kind == "dir"
            ):
                continue
            raw.append((rel, before, current))
        if len(raw) > MAX_TRANSACTION_CHANGED_FILES:
            raise ToolFailure(
                "TRANSACTION_TOO_LARGE",
                "Command changed too many files for bounded transactional apply.",
                category="runtime",
                details={
                    "changed_files": len(raw),
                    "max_changed_files": MAX_TRANSACTION_CHANGED_FILES,
                },
            )
        return after, raw

    def prepare(
        self,
    ) -> tuple[list[StagedFile], list[TransactionChange], str]:
        _, raw_changes = self._diff_entries()
        staged: list[StagedFile] = []
        summary: list[TransactionChange] = []
        diff_parts: list[str] = []
        apply_bytes = 0
        diff_bytes = 0

        for rel, before, after in raw_changes:
            self.validate_relative_path(rel)
            authoritative = self.authoritative_root.joinpath(*rel.split("/"))
            snapshot = self.snapshot_root.joinpath(*rel.split("/"))
            if before is None:
                baseline = FileBaseline(data=None, mode=None, digest=None)
                operation = "create"
            else:
                assert before.kind == "file"
                baseline = FileBaseline(
                    data=b"",
                    mode=before.mode,
                    digest=before.digest,
                )
                operation = "delete" if after is None else "update"

            if after is None:
                content: bytes | None = None
                mode = None
                size = 0
            else:
                if after.kind != "file":
                    raise ToolFailure(
                        "TRANSACTION_UNSAFE_CHANGE",
                        f"Unsupported transactional output type: {rel}",
                        category="security",
                    )
                content = snapshot.read_bytes()
                mode = after.mode
                size = len(content)
                apply_bytes += size
                if apply_bytes > MAX_TRANSACTION_APPLY_BYTES:
                    raise ToolFailure(
                        "TRANSACTION_TOO_LARGE",
                        "Transactional output exceeds the bounded apply budget.",
                        category="runtime",
                        details={
                            "apply_bytes": apply_bytes,
                            "max_apply_bytes": MAX_TRANSACTION_APPLY_BYTES,
                        },
                    )

            staged.append(
                StagedFile(
                    display=rel,
                    path=authoritative,
                    content=content,
                    baseline=baseline,
                    mode=mode,
                )
            )
            summary.append(TransactionChange(rel, operation, size, mode))

            if diff_bytes < MAX_TRANSACTION_DIFF_BYTES:
                before_bytes = b""
                if before is not None and authoritative.is_file():
                    try:
                        before_bytes = authoritative.read_bytes()
                    except OSError:
                        before_bytes = b""
                after_bytes = content or b""
                try:
                    before_text = before_bytes.decode("utf-8").splitlines(keepends=True)
                    after_text = after_bytes.decode("utf-8").splitlines(keepends=True)
                except UnicodeDecodeError:
                    diff_parts.append(f"Binary change: {rel} ({operation}, {size} bytes)\n")
                else:
                    part = "".join(
                        difflib.unified_diff(
                            before_text,
                            after_text,
                            fromfile=f"a/{rel}",
                            tofile=f"b/{rel}",
                        )
                    )
                    encoded = part.encode("utf-8")
                    remaining = MAX_TRANSACTION_DIFF_BYTES - diff_bytes
                    if len(encoded) > remaining:
                        encoded = encoded[:remaining]
                        part = encoded.decode("utf-8", errors="ignore") + "\n... transaction diff truncated ...\n"
                    diff_parts.append(part)
                    diff_bytes += len(encoded)

        return staged, summary, "".join(diff_parts)

    def finish(self, *, apply: bool) -> dict[str, object]:
        staged, summary, diff = self.prepare()
        payload: dict[str, object] = {
            "status": "ready" if apply else "discarded",
            "changed_files": [
                {
                    "path": change.path,
                    "operation": change.operation,
                    "bytes": change.bytes,
                    "mode": change.mode,
                }
                for change in summary
            ],
            "changed_count": len(summary),
            "diff": diff,
        }
        if not apply or not staged:
            payload["status"] = "discarded" if not apply else "unchanged"
            return payload
        try:
            AtomicPatchCommitter().commit(staged)
        except ToolFailure as exc:
            if exc.code in {"PATCH_CONFLICT", "PATCH_ROLLBACK_FAILED"}:
                raise ToolFailure(
                    "TRANSACTION_CONFLICT",
                    "Transactional command output could not be applied without risking concurrent or pre-existing workspace changes.",
                    category="conflict",
                    retryable=exc.retryable,
                    details={"cause": exc.to_dict() if hasattr(exc, "to_dict") else str(exc)},
                ) from exc
            raise
        payload["status"] = "applied"
        return payload
