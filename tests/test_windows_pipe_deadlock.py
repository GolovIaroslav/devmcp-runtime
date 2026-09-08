from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from coding_tools_mcp.processes import ExecSession, start_reader_threads
from coding_tools_mcp.server import Runtime


@unittest.skipUnless(os.name == "nt", "Windows CRT pipe behavior is Windows-only")
class WindowsPipeDeadlockRegressionTests(unittest.TestCase):
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
