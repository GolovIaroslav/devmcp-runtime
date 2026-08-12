from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from .errors import ToolFailure
from .path_security import sensitive_path_reason
from .system_view import readonly_system_paths


@dataclass(frozen=True)
class SandboxBackend:
    """Describes a selectable execution backend without hiding its limits."""

    name: str
    secure: bool
    available: bool
    description: str


def _linux_uid_map_is_namespaced(text: str) -> bool:
    """Return true only for a constrained Linux user namespace mapping."""

    ranges: list[int] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            ranges.append(int(parts[2]))
        except ValueError:
            return False
    return bool(ranges) and sum(ranges) < 1_000_000


def _linux_effective_capabilities_dropped(text: str) -> bool:
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "CapEff":
            try:
                return int(value.strip().split()[0], 16) == 0
            except (IndexError, ValueError):
                return False
    return False


def _linux_mountinfo_has_private_tmp(text: str) -> bool:
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if (
            separator + 1 < len(fields)
            and fields[4] == "/tmp"
            and fields[separator + 1] == "tmpfs"
        ):
            return True
    return False


def inherited_sandbox_backend() -> SandboxBackend | None:
    """Detect a DevMCP namespace sandbox inherited from a parent execution."""

    if sys.platform != "linux" or os.environ.get("DEVMCP_INHERITED_SANDBOX") != "1":
        return None
    try:
        uid_map = Path("/proc/self/uid_map").read_text(encoding="ascii")
        status = Path("/proc/self/status").read_text(encoding="ascii")
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="ascii")
    except OSError:
        return None
    if not _linux_uid_map_is_namespaced(uid_map):
        return None
    if not _linux_effective_capabilities_dropped(status):
        return None
    if not _linux_mountinfo_has_private_tmp(mountinfo):
        return None
    return SandboxBackend(
        "inherited",
        True,
        True,
        "execution confined by an attested parent DevMCP namespace sandbox",
    )


def detect_sandbox_backend(preference: str = "bwrap") -> SandboxBackend:
    """Return backend facts; never silently downgrade a requested backend."""

    normalized = preference.strip().lower()
    if normalized == "bwrap":
        inherited = inherited_sandbox_backend()
        if inherited is not None:
            return inherited
        available = shutil.which("bwrap") is not None
        return SandboxBackend(
            "bwrap", True, available, "bubblewrap namespace and filesystem isolation"
        )
    if normalized == "podman":
        available = shutil.which("podman") is not None
        return SandboxBackend(
            "podman",
            True,
            available,
            "optional rootless Podman backend; verify before enabling",
        )
    if normalized in {"unsafe", "host"}:
        return SandboxBackend(
            "unsafe",
            False,
            True,
            "UNSAFE HOST MODE: explicit local execution without sandbox isolation",
        )
    if normalized in {"inherited", "external"}:
        inherited = inherited_sandbox_backend()
        if inherited is not None:
            return inherited
        return SandboxBackend(
            "inherited",
            True,
            False,
            "inherited sandbox requested but DevMCP namespace attestation is absent",
        )
    raise ToolFailure(
        "INVALID_ARGUMENT",
        f"Unknown sandbox backend: {preference}",
        category="validation",
    )


def _relative_parts(raw_path: str) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "Sandbox path must be a non-empty relative string.",
            category="validation",
        )
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ToolFailure(
            "PATH_OUTSIDE_WORKSPACE",
            "Sandbox path must stay beneath the sandbox root.",
            category="security",
        )
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


def _portable_parent(root: Path, parts: tuple[str, ...], *, create: bool) -> Path:
    """Walk a directory without resolving symlinks on platforms without dirfd APIs."""
    current = root
    if current.is_symlink() or not current.is_dir():
        raise ToolFailure(
            "SANDBOX_FAILED",
            "Sandbox root is not a regular directory.",
            category="security",
        )
    for part in parts:
        current /= part
        if current.is_symlink():
            if not create:
                raise ToolFailure(
                    "SANDBOX_FAILED",
                    "Sandbox path contains a symlink.",
                    category="security",
                )
            current.unlink()
        if current.exists() and not current.is_dir():
            raise ToolFailure(
                "SANDBOX_FAILED",
                "Sandbox path component is not a directory.",
                category="security",
            )
        if create:
            current.mkdir(mode=0o755, exist_ok=True)
        elif not current.exists():
            raise FileNotFoundError(current)
    return current


def _portable_write_relative(
    root: Path, parts: tuple[str, ...], data: bytes, mode: int | None
) -> None:
    parent = _portable_parent(root, parts[:-1], create=True)
    target = parent / parts[-1]
    if target.is_symlink():
        # os.replace below would replace the link too, but unlinking it first
        # makes the intended handling explicit and keeps directory targets
        # from being mistaken for regular files.
        target.unlink()
    if target.exists() and target.is_dir():
        raise ToolFailure(
            "SANDBOX_FAILED",
            f"Sandbox target is a directory: {'/'.join(parts)}",
            category="security",
        )

    fd, temp_name = tempfile.mkstemp(prefix=".mcp-write-", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Replacing a directory entry does not follow a malicious final
        # symlink. The parent was walked component-by-component above.
        os.replace(temp_name, target)
        if mode is not None:
            try:
                os.chmod(target, stat.S_IMODE(mode), follow_symlinks=False)
            except NotImplementedError:
                # Windows does not expose follow_symlinks for chmod. The
                # target was just installed with os.replace, so it is a
                # regular file rather than an attacker-controlled link.
                os.chmod(target, stat.S_IMODE(mode))
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _safe_write_relative(
    root: Path, raw_path: str, data: bytes, mode: int | None
) -> None:
    parts = _relative_parts(raw_path)
    if os.name == "nt":
        _portable_write_relative(root, parts, data, mode)
        return
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
                        raise ToolFailure(
                            "SANDBOX_FAILED",
                            f"Sandbox target is a directory: {raw_path}",
                            category="security",
                        ) from exc
                    raise
                # Unlink only the directory entry itself, then retry with
                # O_NOFOLLOW. A replacement symlink is still rejected.
                os.unlink(parts[-1], dir_fd=parent_fd)
        else:
            raise ToolFailure(
                "SANDBOX_FAILED",
                f"Sandbox target remained a symlink: {raw_path}",
                category="security",
            )
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


def _safe_symlink_relative(root: Path, parts: tuple[str, ...], target: str) -> None:
    if os.name == "nt":
        return
    parent_fd = _open_parent(root, parts[:-1], create=True)
    try:
        os.symlink(target, parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _safe_unlink_relative(root: Path, raw_path: str) -> None:
    parts = _relative_parts(raw_path)
    if os.name == "nt":
        try:
            parent = _portable_parent(root, parts[:-1], create=False)
        except FileNotFoundError:
            return
        target = parent / parts[-1]
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            if target.is_dir():
                raise ToolFailure(
                    "SANDBOX_FAILED",
                    f"Sandbox target is a directory: {raw_path}",
                    category="security",
                )
            target.unlink()
        return
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
            raise ToolFailure(
                "SANDBOX_FAILED",
                f"Sandbox target is a directory: {raw_path}",
                category="security",
            ) from exc
    finally:
        os.close(parent_fd)


@dataclass
class ExecutionSandbox:
    original_workspace: Path
    sandbox_dir: Path
    created_at: float
    owner_root: Path
    owned_path: Path
    ownership_token: str
    owner_marker: Path
    _cleanup_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    _cleaned: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        owner_root: Path | None = None,
    ) -> "ExecutionSandbox":
        if not workspace.is_dir():
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "Workspace must be a directory.",
                category="validation",
            )
        selected_owner = Path(
            owner_root
            or (Path(tempfile.gettempdir()) / "coding-tools-mcp" / "sandboxes")
        )
        try:
            selected_owner.mkdir(parents=True, mode=0o700, exist_ok=True)
            selected_owner = selected_owner.resolve(strict=True)
            if not selected_owner.is_dir():
                raise OSError("sandbox owner root is not a directory")
        except OSError as exc:
            raise ToolFailure(
                "SANDBOX_FAILED",
                f"Failed to prepare sandbox owner root: {exc}",
                category="internal",
            ) from exc
        sandbox_path: Path | None = None
        marker: Path | None = None
        ownership_token = secrets.token_urlsafe(24)
        try:
            sandbox_path = Path(
                tempfile.mkdtemp(prefix="sandbox-", dir=selected_owner)
            ).resolve()
            marker = selected_owner / f".{sandbox_path.name}.owner"
            marker.write_text(ownership_token, encoding="utf-8")
            if os.name != "nt":
                marker.chmod(0o600)
            cls._sync(workspace, sandbox_path)
        except BaseException as exc:
            if sandbox_path is not None:
                shutil.rmtree(sandbox_path, ignore_errors=True)
            if marker is not None:
                marker.unlink(missing_ok=True)
            if isinstance(exc, ToolFailure):
                raise
            if not isinstance(exc, Exception):
                raise
            raise ToolFailure(
                "SANDBOX_FAILED",
                f"Failed to create sandbox: {exc}",
                category="internal",
            ) from exc
        assert sandbox_path is not None
        assert marker is not None
        return cls(
            original_workspace=workspace,
            sandbox_dir=sandbox_path,
            created_at=time.time(),
            owner_root=selected_owner,
            owned_path=sandbox_path,
            ownership_token=ownership_token,
            owner_marker=marker,
        )

    def sync_from_authoritative(self) -> None:
        self.__class__._sync(self.original_workspace, self.sandbox_dir)

    @staticmethod
    def _is_secret_path(parts: tuple[str, ...]) -> bool:
        if any(
            part in {".devmcp-tmp", ".devmcp-home", ".devmcp-cache"} for part in parts
        ):
            return True
        return sensitive_path_reason(parts) is not None

    @classmethod
    def _clear_destination(cls, dest: Path) -> None:
        if dest.is_symlink() or not dest.is_dir():
            raise ToolFailure(
                "SANDBOX_FAILED",
                "Sandbox root is not a regular directory.",
                category="security",
            )
        for entry in os.scandir(dest):
            entry_path = Path(entry.path)
            if entry.is_symlink():
                entry_path.unlink()
            elif entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry_path)
            else:
                entry_path.unlink()

    @classmethod
    def _copy_tree(
        cls,
        source: Path,
        dest: Path,
        relative: tuple[str, ...] = (),
        *,
        source_root: Path | None = None,
    ) -> None:
        if source_root is None:
            source_root = source

        def beneath(path: Path, root: Path) -> bool:
            try:
                path.relative_to(root)
            except ValueError:
                return False
            return True

        for entry in os.scandir(source):
            child_rel = relative + (entry.name,)
            if cls._is_secret_path(child_rel):
                continue
            if entry.is_symlink():
                # Keep the general no-symlink snapshot rule. POSIX venv
                # interpreter links are a bounded exception so copied console
                # scripts can use the copied, secret-filtered site-packages.
                if child_rel[:2] == (".venv", "bin") and os.name != "nt":
                    try:
                        raw_target = os.readlink(entry.path)
                        resolved_target = Path(entry.path).resolve(strict=True)
                    except OSError:
                        continue
                    source_venv = source_root / ".venv"
                    allowed_external_roots = (
                        Path("/usr"),
                        Path("/bin"),
                        Path("/lib"),
                        Path("/lib64"),
                        Path("/sbin"),
                        Path.home() / ".local" / "share" / "uv",
                    )
                    if beneath(resolved_target, source_venv):
                        target = (
                            str(dest / resolved_target.relative_to(source_root))
                            if Path(raw_target).is_absolute()
                            else raw_target
                        )
                    elif any(
                        root.exists() and beneath(resolved_target, root)
                        for root in allowed_external_roots
                    ):
                        target = str(resolved_target)
                    else:
                        continue
                    _safe_symlink_relative(dest, child_rel, target)
                continue
            if entry.is_dir(follow_symlinks=False):
                if os.name == "nt":
                    _portable_parent(dest, child_rel, create=True)
                else:
                    fd = _open_parent(dest, child_rel, create=True)
                    os.close(fd)
                cls._copy_tree(
                    Path(entry.path),
                    dest,
                    child_rel,
                    source_root=source_root,
                )
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
            data = b"".join(chunks)
            if child_rel[:2] == (".venv", "bin") and data.startswith(b"#!"):
                first_line, separator, remainder = data.partition(b"\n")
                source_venv_bytes = str(source_root / ".venv").encode()
                if source_venv_bytes in first_line:
                    first_line = first_line.replace(
                        source_venv_bytes,
                        str(dest / ".venv").encode(),
                        1,
                    )
                    data = first_line + separator + remainder
            _safe_write_relative(dest, "/".join(child_rel), data, source_mode)

    @classmethod
    def _sync(cls, source: Path, dest: Path) -> None:
        """Create a secret-filtered snapshot without dereferencing symlinks.

        This intentionally uses one no-follow copy path for systems with and
        without rsync. The destination is attacker-controlled after creation,
        so all later writes use dirfds and O_NOFOLLOW as well.
        """
        cls._clear_destination(dest)
        cls._copy_tree(source, dest)

    def safe_write_file(
        self, rel_path: str, content: bytes | str, mode: int | None = None
    ) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        _safe_write_relative(self.sandbox_dir, rel_path, data, mode)
        # A patch can preserve both the byte length and the timestamp
        # granularity of a source file (for example ``a - b`` -> ``a + b``).
        # Remove interpreter bytecode beside updated Python sources so a
        # registered test cannot execute stale code from the sandbox snapshot.
        if rel_path.endswith(".py"):
            cache_dir = self.sandbox_dir.joinpath(
                *PurePosixPath(rel_path).parts[:-1], "__pycache__"
            )
            if cache_dir.is_symlink():
                cache_dir.unlink()
            elif cache_dir.is_dir():
                shutil.rmtree(cache_dir)

    def safe_delete_file(self, rel_path: str) -> None:
        _safe_unlink_relative(self.sandbox_dir, rel_path)

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            try:
                owner = self.owner_root.resolve(strict=True)
                owned = self.owned_path.resolve(strict=False)
                candidate = self.sandbox_dir.resolve(strict=False)
                marker = self.owner_marker.resolve(strict=False)
            except OSError:
                return
            if (
                candidate != owned
                or owned.parent != owner
                or not owned.name.startswith("sandbox-")
                or marker.parent != owner
                or marker.name != f".{owned.name}.owner"
            ):
                return
            try:
                if (
                    self.owner_marker.read_text(encoding="utf-8")
                    != self.ownership_token
                ):
                    return
            except OSError:
                return
            try:
                shutil.rmtree(owned)
            except FileNotFoundError:
                pass
            except OSError:
                return
            self.owner_marker.unlink(missing_ok=True)
            self._cleaned = True

    def _private_dir(self, name: str) -> Path:
        path = self.sandbox_dir / f".devmcp-{name}"
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    @property
    def temp_dir(self) -> Path:
        return self._private_dir("tmp")

    @property
    def home_dir(self) -> Path:
        return self._private_dir("home")

    @property
    def cache_dir(self) -> Path:
        return self._private_dir("cache")

    def translate_path_for_exec(self, raw_cwd: Path) -> Path:
        try:
            rel = raw_cwd.resolve().relative_to(self.original_workspace.resolve())
            return self.sandbox_dir.joinpath(rel)
        except ValueError:
            return self.sandbox_dir

    def resolve_sandbox_path(self, raw_path: str) -> Path:
        parts = _relative_parts(raw_path)
        return self.sandbox_dir.joinpath(*parts)

    def get_bwrap_args(
        self,
        allow_network: bool = False,
        *,
        root_mounts: Iterable[tuple[Path, Path, bool]] = (),
    ) -> list[str]:
        args = ["bwrap"]
        if allow_network:
            # Keep the network namespace joined to the host only for an
            # explicitly granted operation. All other namespaces remain new.
            args.extend(
                [
                    "--unshare-user",
                    "--unshare-pid",
                    "--unshare-ipc",
                    "--unshare-uts",
                    "--unshare-cgroup",
                ]
            )
        else:
            args.append("--unshare-all")
        args.extend(
            [
                "--cap-drop",
                "ALL",
                "--disable-userns",
                "--new-session",
                "--die-with-parent",
            ]
        )
        args.extend(["--setenv", "DEVMCP_INHERITED_SANDBOX", "1"])

        args.extend(["--dir", "/etc"])
        if allow_network:
            args.extend(["--dir", "/run"])
        for system_path in readonly_system_paths(allow_network=allow_network):
            args.extend(["--ro-bind", str(system_path), str(system_path)])
        args.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--bind",
                str(self.sandbox_dir),
                str(self.sandbox_dir),
            ]
        )
        python_dir = str(Path(sys.executable).parent.parent)
        if not python_dir.startswith(("/usr", "/bin", "/lib")):
            args.extend(["--ro-bind", python_dir, python_dir])
        real_executable = Path(os.path.realpath(sys.executable))
        python_real_dir = str(real_executable.parent.parent)
        if python_real_dir != python_dir and not python_real_dir.startswith(
            ("/usr", "/bin", "/lib")
        ):
            args.extend(["--ro-bind", python_real_dir, python_real_dir])
        uv_roots: list[Path] = []
        derived_uv_root = real_executable
        for _ in range(4):
            derived_uv_root = derived_uv_root.parent
        if derived_uv_root.name == "uv":
            uv_roots.append(derived_uv_root)
        try:
            service_home = Path.home()
        except (OSError, RuntimeError):
            service_home = None
        if service_home is not None:
            uv_roots.append(service_home / ".local" / "share" / "uv")
        seen_uv_roots: set[str] = set()
        for uv_root in uv_roots:
            if uv_root.exists() and str(uv_root) not in seen_uv_roots:
                seen_uv_roots.add(str(uv_root))
                args.extend(["--ro-bind", str(uv_root), str(uv_root)])
        mcp_pkg = Path(__file__).parent
        args.extend(["--ro-bind", str(mcp_pkg), str(mcp_pkg)])

        mounts = list(root_mounts)
        if not mounts:
            mounts = [(self.sandbox_dir, self.original_workspace, True)]
        created_dirs: set[str] = {"/tmp", "/etc", "/run"}
        for source, destination, writable in mounts:
            dest = destination.resolve(strict=False)
            for parent in reversed(dest.parents):
                text = str(parent)
                if text in {"/", ""} or text in created_dirs:
                    continue
                if any(
                    text == str(system_path)
                    or text.startswith(str(system_path) + os.sep)
                    for system_path in readonly_system_paths(
                        allow_network=allow_network
                    )
                    if system_path.is_dir()
                ):
                    continue
                args.extend(["--dir", text])
                created_dirs.add(text)
            args.extend(
                [
                    "--bind" if writable else "--ro-bind",
                    str(source.resolve(strict=True)),
                    str(dest),
                ]
            )
        return args
