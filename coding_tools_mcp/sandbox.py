from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ToolFailure


def _relative_parts(raw_path: str) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ToolFailure("INVALID_ARGUMENT", "Sandbox path must be a non-empty relative string.", category="validation")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Sandbox path must stay beneath the sandbox root.", category="security")
    return tuple(pure.parts)


def _open_parent(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if not create or exc.errno not in {errno.ELOOP, errno.ENOTDIR}:
                    raise
                # The final component of this lookup is removed relative to an
                # already-open directory. It can therefore never unlink a
                # host path reached through the malicious symlink.
                try:
                    os.unlink(part, dir_fd=fd)
                except IsADirectoryError:
                    os.rmdir(part, dir_fd=fd)
                os.mkdir(part, 0o755, dir_fd=fd)
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _safe_write_relative(root: Path, raw_path: str, data: bytes, mode: int | None) -> None:
    parts = _relative_parts(raw_path)
    parent_fd = _open_parent(root, parts[:-1], create=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        for _ in range(4):
            try:
                fd = os.open(parts[-1], flags, mode or 0o644, dir_fd=parent_fd)
                break
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    if exc.errno == errno.EISDIR:
                        raise ToolFailure("SANDBOX_FAILED", f"Sandbox target is a directory: {raw_path}", category="security") from exc
                    raise
                # Unlink only the directory entry itself, then retry with
                # O_NOFOLLOW. A replacement symlink is still rejected.
                os.unlink(parts[-1], dir_fd=parent_fd)
        else:
            raise ToolFailure("SANDBOX_FAILED", f"Sandbox target remained a symlink: {raw_path}", category="security")
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            if mode is not None:
                os.fchmod(fd, stat.S_IMODE(mode))
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _safe_unlink_relative(root: Path, raw_path: str) -> None:
    parts = _relative_parts(raw_path)
    try:
        parent_fd = _open_parent(root, parts[:-1], create=False)
    except FileNotFoundError:
        return
    try:
        try:
            os.unlink(parts[-1], dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except IsADirectoryError as exc:
            raise ToolFailure("SANDBOX_FAILED", f"Sandbox target is a directory: {raw_path}", category="security") from exc
    finally:
        os.close(parent_fd)


@dataclass
class ExecutionSandbox:
    original_workspace: Path
    sandbox_dir: Path
    created_at: float

    @classmethod
    def create(cls, workspace: Path) -> "ExecutionSandbox":
        if not workspace.is_dir():
            raise ToolFailure("INVALID_ARGUMENT", "Workspace must be a directory.", category="validation")
        try:
            sandbox_path = Path(tempfile.mkdtemp(prefix="chatgpt-dev-sandbox-")).resolve()
            cls._sync(workspace, sandbox_path)
        except OSError as exc:
            raise ToolFailure("SANDBOX_FAILED", f"Failed to create sandbox: {exc}", category="internal") from exc
        return cls(original_workspace=workspace, sandbox_dir=sandbox_path, created_at=time.time())

    def sync_from_authoritative(self) -> None:
        self.__class__._sync(self.original_workspace, self.sandbox_dir)

    @staticmethod
    def _is_secret_path(filename: str) -> bool:
        if filename == ".git":
            return True
        if filename == ".env.example":
            return False
        if filename == ".env" or filename.startswith(".env."):
            return True
        return filename.endswith(".pem") or filename.endswith(".key")

    @classmethod
    def _clear_destination(cls, dest: Path) -> None:
        if dest.is_symlink() or not dest.is_dir():
            raise ToolFailure("SANDBOX_FAILED", "Sandbox root is not a regular directory.", category="security")
        for entry in os.scandir(dest):
            entry_path = Path(entry.path)
            if entry.is_symlink():
                entry_path.unlink()
            elif entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry_path)
            else:
                entry_path.unlink()

    @classmethod
    def _copy_tree(cls, source: Path, dest: Path, relative: tuple[str, ...] = ()) -> None:
        for entry in os.scandir(source):
            if cls._is_secret_path(entry.name):
                continue
            if entry.is_symlink():
                # Symlinks are not needed for a safe execution snapshot. In
                # particular, do not copy a link to an excluded secret or an
                # absolute/external target into the allowed sandbox tree.
                continue
            child_rel = relative + (entry.name,)
            if entry.is_dir(follow_symlinks=False):
                fd = _open_parent(dest, child_rel, create=True)
                os.close(fd)
                cls._copy_tree(Path(entry.path), dest, child_rel)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            source_fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                source_mode = stat.S_IMODE(os.fstat(source_fd).st_mode)
            finally:
                os.close(source_fd)
            _safe_write_relative(dest, "/".join(child_rel), b"".join(chunks), source_mode)

    @classmethod
    def _sync(cls, source: Path, dest: Path) -> None:
        """Create a secret-filtered snapshot without dereferencing symlinks.

        This intentionally uses one no-follow copy path for systems with and
        without rsync. The destination is attacker-controlled after creation,
        so all later writes use dirfds and O_NOFOLLOW as well.
        """
        cls._clear_destination(dest)
        cls._copy_tree(source, dest)

    def safe_write_file(self, rel_path: str, content: bytes | str, mode: int | None = None) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        _safe_write_relative(self.sandbox_dir, rel_path, data, mode)

    def safe_delete_file(self, rel_path: str) -> None:
        _safe_unlink_relative(self.sandbox_dir, rel_path)

    def cleanup(self) -> None:
        try:
            shutil.rmtree(self.sandbox_dir)
        except OSError:
            pass

    def translate_path_for_exec(self, raw_cwd: Path) -> Path:
        try:
            rel = raw_cwd.resolve().relative_to(self.original_workspace.resolve())
            return self.sandbox_dir.joinpath(rel)
        except ValueError:
            return self.sandbox_dir

    def resolve_sandbox_path(self, raw_path: str) -> Path:
        parts = _relative_parts(raw_path)
        return self.sandbox_dir.joinpath(*parts)

    def get_bwrap_args(self, allow_network: bool = False) -> list[str]:
        args = ["bwrap"]
        if allow_network:
            # Keep the network namespace joined to the host only for an
            # explicitly granted operation. All other namespaces remain new.
            args.extend(["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup"])
        else:
            args.append("--unshare-all")
        args.extend(["--cap-drop", "ALL", "--new-session", "--die-with-parent"])

        for bind_dir in ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"]:
            if os.path.exists(bind_dir):
                args.extend(["--ro-bind", bind_dir, bind_dir])
        args.extend([
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", str(self.sandbox_dir), str(self.sandbox_dir),
        ])

        python_dir = str(Path(sys.executable).parent.parent)
        if not python_dir.startswith(("/usr", "/bin", "/lib")):
            args.extend(["--ro-bind", python_dir, python_dir])
        real_executable = Path(os.path.realpath(sys.executable))
        python_real_dir = str(real_executable.parent.parent)
        if python_real_dir != python_dir and not python_real_dir.startswith(("/usr", "/bin", "/lib")):
            args.extend(["--ro-bind", python_real_dir, python_real_dir])
        uv_roots: list[Path] = []
        derived_uv_root = real_executable
        for _ in range(4):
            derived_uv_root = derived_uv_root.parent
        if derived_uv_root.name == "uv":
            uv_roots.append(derived_uv_root)
        uv_roots.append(Path.home() / ".local" / "share" / "uv")
        seen_uv_roots: set[str] = set()
        for uv_root in uv_roots:
            if uv_root.exists() and str(uv_root) not in seen_uv_roots:
                seen_uv_roots.add(str(uv_root))
                args.extend(["--ro-bind", str(uv_root), str(uv_root)])
        mcp_pkg = Path(__file__).parent
        args.extend(["--ro-bind", str(mcp_pkg), str(mcp_pkg)])
        return args
