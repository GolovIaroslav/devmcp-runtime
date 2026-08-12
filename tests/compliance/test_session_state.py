from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.processes import ExecSession
from coding_tools_mcp.session_state import LogicalContextRegistry, SharedJobRegistry


class SessionStateRegistryTests(unittest.TestCase):
    def test_context_lease_prevents_ttl_expiration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = LogicalContextRegistry(ttl_seconds=1)
            state = registry.create(root, root)
            retained = registry.retain(state.context_id)
            self.assertIs(retained, state)
            state.last_seen -= 10
            registry.prune()
            self.assertIs(registry.get(state.context_id), state)
            registry.release(state.context_id)
            state.last_seen -= 10
            registry.prune()
            self.assertIsNone(registry.get(state.context_id))

    def test_shared_job_pins_context_and_checks_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            contexts = LogicalContextRegistry(ttl_seconds=1)
            owner = contexts.create(root, root)
            other = contexts.create(root, root)
            jobs = SharedJobRegistry(
                completed_ttl_seconds=30, context_registry=contexts
            )
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            session = ExecSession(
                session_id=jobs.new_handle(),
                process=process,
                timeout_at=time.time() + 30,
            )
            try:
                jobs.register(
                    session,
                    owner_context_id=owner.context_id,
                    owner_runtime=object(),
                )
                owner.last_seen -= 10
                contexts.prune()
                self.assertIsNotNone(contexts.get(owner.context_id))
                status, found = jobs.lookup(
                    session.session_id, owner_context_id=owner.context_id
                )
                self.assertEqual(status, "found")
                self.assertIs(found, session)
                status, found = jobs.lookup(
                    session.session_id, owner_context_id=other.context_id
                )
                self.assertEqual(status, "forbidden")
                self.assertIsNone(found)
            finally:
                jobs.close()
            owner.last_seen -= 10
            contexts.prune()
            self.assertIsNone(contexts.get(owner.context_id))

    def test_completed_job_expiration_releases_context_lease(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            contexts = LogicalContextRegistry(ttl_seconds=1)
            owner = contexts.create(root, root)
            jobs = SharedJobRegistry(completed_ttl_seconds=1, context_registry=contexts)
            process = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            process.wait(timeout=5)
            session = ExecSession(
                session_id=jobs.new_handle(),
                process=process,
                timeout_at=time.time() + 30,
            )
            session.refresh_status()
            self.assertIsNotNone(session.completed_at)
            jobs.register(
                session,
                owner_context_id=owner.context_id,
                owner_runtime=object(),
            )
            assert session.completed_at is not None
            session.completed_at -= 10
            jobs.prune()
            status, _ = jobs.lookup(
                session.session_id, owner_context_id=owner.context_id
            )
            self.assertEqual(status, "missing")
            owner.last_seen -= 10
            contexts.prune()
            self.assertIsNone(contexts.get(owner.context_id))


if __name__ == "__main__":
    unittest.main()
