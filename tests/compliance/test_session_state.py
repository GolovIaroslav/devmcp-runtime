from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.managed_worktree import create_managed_worktree
from coding_tools_mcp.processes import ExecSession
from coding_tools_mcp.session_state import LogicalContextRegistry, SharedJobRegistry


class SessionStateRegistryTests(unittest.TestCase):
    def test_context_tracks_canonical_effective_and_default_cwd(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            effective = root / "effective"
            nested = effective / "nested"
            canonical.mkdir()
            nested.mkdir(parents=True)
            registry = LogicalContextRegistry()
            state = registry.create(canonical, effective, nested)
            self.assertEqual(state.canonical_project_root, canonical.resolve())
            self.assertEqual(state.effective_workspace_root, effective.resolve())
            self.assertEqual(state.default_cwd, nested.resolve())

    def test_context_lease_prevents_ttl_expiration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = LogicalContextRegistry(ttl_seconds=1)
            state = registry.create(root, root, root)
            retained = registry.retain(state.context_id)
            self.assertIs(retained, state)
            state.last_seen -= 10
            registry.prune()
            self.assertIs(registry.get(state.context_id), state)
            registry.release(state.context_id)
            state.last_seen -= 10
            registry.prune()
            self.assertIsNone(registry.get(state.context_id))

    def test_mutation_claim_keeps_first_context_canonical_and_contends_next(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = LogicalContextRegistry()
            first = registry.create(root, root, root)
            second = registry.create(root, root, root)
            self.assertFalse(registry.claim_mutation_workspace(first))
            self.assertTrue(first.mutation_workspace_claimed)
            self.assertTrue(registry.claim_mutation_workspace(second))
            self.assertTrue(second.mutation_workspace_claimed)

    def test_mutation_claim_is_reused_after_context_workspace_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            isolated = root / "isolated"
            isolated.mkdir()
            registry = LogicalContextRegistry()
            first = registry.create(root, root, root)
            second = registry.create(root, root, root)
            self.assertFalse(registry.claim_mutation_workspace(first))
            self.assertTrue(registry.claim_mutation_workspace(second))
            registry.update(
                second,
                canonical_project_root=root,
                effective_workspace_root=isolated,
                default_cwd=isolated,
            )
            self.assertTrue(registry.claim_mutation_workspace(second))

    def test_project_switch_resets_mutation_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = root / "other"
            other.mkdir()
            registry = LogicalContextRegistry()
            state = registry.create(root, root, root)
            self.assertFalse(registry.claim_mutation_workspace(state))
            registry.update(
                state,
                canonical_project_root=other,
                effective_workspace_root=other,
                default_cwd=other,
            )
            self.assertFalse(state.mutation_workspace_claimed)

    def test_managed_worktree_is_linked_and_uses_private_generated_branch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "repo"
            storage = root / "state"
            canonical.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(canonical)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(canonical), "config", "user.name", "DevMCP Test"], check=True)
            subprocess.run(["git", "-C", str(canonical), "config", "user.email", "devmcp@example.invalid"], check=True)
            (canonical / "tracked.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(canonical), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(canonical), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)
            with patch("coding_tools_mcp.managed_worktree.state_root", return_value=storage):
                worktree, branch = create_managed_worktree(canonical, "ctx_test_context_123456")
            self.assertNotEqual(worktree, canonical.resolve())
            self.assertTrue((worktree / "tracked.txt").is_file())
            current_branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(current_branch, branch)
            self.assertTrue(branch.startswith("devmcp/context-"))

    def test_shared_job_pins_context_and_checks_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            contexts = LogicalContextRegistry(ttl_seconds=1)
            owner = contexts.create(root, root, root)
            other = contexts.create(root, root, root)
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
            owner = contexts.create(root, root, root)
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
