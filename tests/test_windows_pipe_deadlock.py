from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apps.devmcp.cli import _spawn_windows_cli

from coding_tools_mcp.processes import ExecSession, start_reader_threads
from coding_tools_mcp.server import Runtime


@unittest.skipUnless(os.name == "nt", "Windows CRT pipe behavior is Windows-only")
class WindowsPipeDeadlockRegressionTests(unittest.TestCase):
    def test_delayed_windows_cli_launch_handles_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devmcp service ") as tmp:
            root = Path(tmp)
            log = root / "service.log"
            pid = _spawn_windows_cli(
                SimpleNamespace(root=root),
                ["config", "validate"],
                log,
                delay_seconds=1,
            )
            self.assertGreater(pid, 0)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if log.exists() and "Configuration valid" in log.read_text():
                    break
                time.sleep(0.1)
            else:
                self.fail("delayed CLI did not complete config validation")

    def test_windows_service_actions_do_not_use_systemd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), sandbox_backend="unsafe")
            try:
                with patch.object(
                    runtime,
                    "_schedule_windows_service",
                    return_value={"status": "scheduled"},
                ) as schedule:
                    runtime._schedule_devmcp_restart()
                    schedule.assert_called_with(["restart"])
                    runtime._schedule_devmcp_update(
                        Path(tmp), "a" * 40, development_mode=True
                    )
                    schedule.assert_called_with(
                        [
                            "service",
                            "update",
                            "--source",
                            tmp,
                            "--expected-sha",
                            "a" * 40,
                            "--development-mode",
                        ]
                    )
            finally:
                runtime.close()

    def test_cleanup_returns_while_stdin_writer_is_blocked(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        session = ExecSession(session_id="blocked-stdin", process=process)
        start_reader_threads(session)
        writer_done = threading.Event()
        close_done = threading.Event()

        def write() -> None:
            try:
                session.write_input(b"x" * 1_048_576)
            except Exception:
                pass
            finally:
                writer_done.set()

        def close() -> None:
            session.close_process_streams()
            close_done.set()

        writer = threading.Thread(target=write, daemon=True)
        closer = threading.Thread(target=close, daemon=True)
        try:
            writer.start()
            deadline = time.monotonic() + 5
            while not session._stdin_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue(session._stdin_lock.locked())
            self.assertFalse(writer_done.wait(0.05))
            closer.start()
            self.assertTrue(close_done.wait(0.5), "cleanup blocked on stdin writer")
        finally:
            process.kill()
            process.wait(timeout=5)
            writer.join(timeout=5)
            if closer.ident is not None:
                closer.join(timeout=5)
            for reader in session.reader_threads:
                reader.join(timeout=5)
        self.assertTrue(writer_done.is_set())
        self.assertTrue(process.stdin.closed)

    def test_close_process_streams_returns_while_pipe_readers_are_blocked(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        session = ExecSession(session_id="windows-pipe-deadlock", process=process)
        start_reader_threads(session)
        self.assertEqual(len(session.reader_threads), 2)
        self.assertTrue(all(thread.is_alive() for thread in session.reader_threads))

        close_returned = threading.Event()

        def close_streams() -> None:
            session.close_process_streams()
            close_returned.set()

        closer = threading.Thread(target=close_streams, daemon=True)
        closer.start()
        returned_before_process_exit = close_returned.wait(timeout=0.5)

        if not returned_before_process_exit:
            process.kill()
            process.wait(timeout=5)
            closer.join(timeout=5)
            for thread in session.reader_threads:
                thread.join(timeout=5)
            self.fail(
                "close_process_streams blocked while os.read() held the Windows CRT pipe lock"
            )

        self.assertIsNotNone(process.stdout)
        self.assertIsNotNone(process.stderr)
        assert process.stdout is not None
        assert process.stderr is not None
        self.assertFalse(process.stdout.closed)
        self.assertFalse(process.stderr.closed)

        process.kill()
        process.wait(timeout=5)
        for thread in session.reader_threads:
            thread.join(timeout=5)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_http_evictability_does_not_prune_exec_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), sandbox_backend="unsafe")
            original_prune = runtime._prune_sessions

            def fail_if_pruned() -> None:
                raise AssertionError(
                    "http_session_evictable must not prune process sessions while the HTTP manager lock is held"
                )

            runtime._prune_sessions = fail_if_pruned  # type: ignore[method-assign]
            try:
                self.assertTrue(runtime.http_session_evictable())
            finally:
                runtime._prune_sessions = original_prune  # type: ignore[method-assign]
                runtime.close()


if __name__ == "__main__":
    unittest.main()
