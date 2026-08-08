import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from dataclasses import dataclass
import time

from .errors import ToolFailure

@dataclass
class ExecutionSandbox:
    original_workspace: Path
    sandbox_dir: Path
    created_at: float

    @classmethod
    def create(cls, workspace: Path) -> "ExecutionSandbox":
        """Create a disposable execution sandbox synchronized with the authoritative workspace."""
        if not workspace.is_dir():
            raise ToolFailure("INVALID_ARGUMENT", "Workspace must be a directory.", category="validation")
        
        try:
            # We use a temp directory with restricted permissions (0700).
            raw_temp = tempfile.mkdtemp(prefix="chatgpt-dev-sandbox-")
            sandbox_path = Path(raw_temp).resolve()
        except OSError as exc:
            raise ToolFailure("SANDBOX_FAILED", f"Failed to create sandbox directory: {exc}", category="internal")

        # Sync the workspace to the sandbox
        cls._sync(workspace, sandbox_path)

        return cls(original_workspace=workspace, sandbox_dir=sandbox_path, created_at=time.time())

    def sync_from_authoritative(self) -> None:
        """Resync the sandbox from the authoritative workspace if files changed."""
        self.__class__._sync(self.original_workspace, self.sandbox_dir)

    @staticmethod
    def _sync(source: Path, dest: Path) -> None:
        # Prefer rsync for efficiency if available
        if shutil.which("rsync"):
            try:
                process = subprocess.Popen([
                    "rsync", "-a", "--delete", "--exclude=.git",
                    str(source) + "/",
                    str(dest) + "/"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                process.wait()
                if process.returncode == 0:
                    return
            except Exception:
                pass # Fallback to shutil

        # Fallback to shutil.copytree
        def ignore_git(directory, contents):
            return [f for f in contents if f == ".git"]

        try:
            shutil.copytree(source, dest, dirs_exist_ok=True, ignore=ignore_git)
        except OSError as exc:
            raise ToolFailure("SANDBOX_FAILED", f"Failed to sync workspace to sandbox: {exc}", category="internal")

    def cleanup(self) -> None:
        """Remove the sandbox directory and its contents."""
        try:
            shutil.rmtree(self.sandbox_dir)
        except OSError:
            pass

    def translate_path_for_exec(self, raw_cwd: Path) -> Path:
        """Translate a path from the authoritative workspace to the sandbox workspace."""
        try:
            rel = raw_cwd.resolve().relative_to(self.original_workspace.resolve())
            return self.sandbox_dir.joinpath(rel)
        except ValueError:
            return self.sandbox_dir

    def resolve_sandbox_path(self, raw_path: str) -> Path:
        """Resolve a path string relative to the sandbox directory."""
        return self.sandbox_dir.joinpath(raw_path).resolve()

    def get_bwrap_args(self) -> list[str]:
        """Constructs the bwrap arguments for sandbox execution."""
        args = [
            "bwrap",
            "--unshare-all",
            "--cap-drop", "ALL",
            "--new-session",
            "--die-with-parent",
        ]
        
        # Mount minimal host read-only, explicitly excluding /home and /root
        for bind_dir in ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"]:
            if os.path.exists(bind_dir):
                args.extend(["--ro-bind", bind_dir, bind_dir])
                
        # Required special files
        args.extend([
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", str(self.sandbox_dir), str(self.sandbox_dir),
        ])
        
        return args
