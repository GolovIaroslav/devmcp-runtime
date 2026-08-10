from __future__ import annotations

import time
import unittest

from coding_tools_mcp.transport_http import (
    HTTP_SESSION_TTL_SECONDS,
    MAX_HTTP_SESSIONS,
    HTTPSessionManager,
)


class FakeRuntime:
    def __init__(self, session_id: str, *, evictable: bool = True) -> None:
        self.http_session_id = session_id
        self.close_count = 0
        self.evictable = evictable

    def close(self) -> None:
        self.close_count += 1

    def http_session_evictable(self) -> bool:
        return self.evictable


class RuntimeFactory:
    def __init__(self) -> None:
        self.created: list[FakeRuntime] = []

    def __call__(self) -> FakeRuntime:
        runtime = FakeRuntime(f"session-{len(self.created)}")
        self.created.append(runtime)
        return runtime


class HTTPSessionManagerTests(unittest.TestCase):
    def test_repeated_abandoned_sessions_stay_bounded_at_capacity(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        try:
            total = MAX_HTTP_SESSIONS + 32
            for _ in range(total):
                runtime = manager.create()
                manager.release(runtime.http_session_id)

            self.assertEqual(len(manager._sessions), MAX_HTTP_SESSIONS)
            self.assertEqual(
                sum(runtime.close_count for runtime in factory.created),
                total - MAX_HTTP_SESSIONS,
            )
            self.assertTrue(
                all(
                    record.active_requests == 0 for record in manager._sessions.values()
                )
            )
        finally:
            manager.close()
        self.assertTrue(all(runtime.close_count == 1 for runtime in factory.created))

    def test_bounded_mixed_http_lifecycle_stress(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        try:
            for index in range(256):
                runtime = manager.create()
                manager.release(runtime.http_session_id)
                if index % 3 == 0:
                    self.assertIs(manager.get(runtime.http_session_id), runtime)
                    manager.release(runtime.http_session_id)
                if index % 5 == 0:
                    self.assertTrue(manager.delete(runtime.http_session_id))

            self.assertLessEqual(len(manager._sessions), MAX_HTTP_SESSIONS)
            self.assertEqual(
                sum(runtime.close_count for runtime in factory.created),
                sum(1 for runtime in factory.created if runtime.close_count),
            )
        finally:
            manager.close()
        self.assertTrue(all(runtime.close_count == 1 for runtime in factory.created))

    def test_capacity_never_evicts_an_active_session(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        try:
            active = manager.create()
            for _ in range(MAX_HTTP_SESSIONS - 1):
                idle = manager.create()
                manager.release(idle.http_session_id)

            replacement = manager.create()
            self.assertEqual(active.close_count, 0)
            self.assertIn(active.http_session_id, manager._sessions)
            self.assertEqual(len(manager._sessions), MAX_HTTP_SESSIONS)
            manager.release(replacement.http_session_id)
            manager.release(active.http_session_id)
        finally:
            manager.close()

    def test_capacity_rejects_only_when_every_session_is_active(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        try:
            for _ in range(MAX_HTTP_SESSIONS):
                manager.create()
            with self.assertRaisesRegex(
                RuntimeError, "maximum HTTP session count reached"
            ):
                manager.create()
        finally:
            manager.close()

    def test_capacity_rejects_when_every_idle_runtime_is_non_evictable(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        try:
            for _ in range(MAX_HTTP_SESSIONS):
                runtime = manager.create()
                manager.release(runtime.http_session_id)
                runtime.evictable = False

            with self.assertRaisesRegex(
                RuntimeError, "maximum HTTP session count reached"
            ):
                manager.create()
            self.assertTrue(
                all(runtime.close_count == 0 for runtime in factory.created)
            )
        finally:
            manager.close()

    def test_delete_waits_for_in_flight_request_before_closing_runtime(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        runtime = manager.create()

        self.assertTrue(manager.delete(runtime.http_session_id))
        self.assertEqual(runtime.close_count, 0)
        self.assertIsNone(manager.get(runtime.http_session_id))

        manager.release(runtime.http_session_id)
        self.assertEqual(runtime.close_count, 1)
        self.assertNotIn(runtime.http_session_id, manager._sessions)
        manager.release(runtime.http_session_id)
        self.assertEqual(runtime.close_count, 1)

    def test_get_request_lease_blocks_delete_until_release(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        runtime = manager.create()
        manager.release(runtime.http_session_id)

        self.assertIs(manager.get(runtime.http_session_id), runtime)
        self.assertTrue(manager.delete(runtime.http_session_id))
        self.assertEqual(runtime.close_count, 0)
        manager.release(runtime.http_session_id)
        self.assertEqual(runtime.close_count, 1)

    def test_prune_closes_expired_idle_runtime_but_not_active_runtime(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        idle = manager.create()
        manager.release(idle.http_session_id)
        active = manager.create()

        expired = time.monotonic() - HTTP_SESSION_TTL_SECONDS - 1
        with manager._lock:
            manager._sessions[idle.http_session_id].last_seen = expired
            manager._sessions[active.http_session_id].last_seen = expired

        manager.prune()

        self.assertEqual(idle.close_count, 1)
        self.assertNotIn(idle.http_session_id, manager._sessions)
        self.assertEqual(active.close_count, 0)
        self.assertIn(active.http_session_id, manager._sessions)
        manager.release(active.http_session_id)
        manager.close()

    def test_prune_does_not_close_idle_runtime_with_background_exec(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        runtime = manager.create()
        manager.release(runtime.http_session_id)
        runtime.evictable = False
        with manager._lock:
            manager._sessions[runtime.http_session_id].last_seen = (
                time.monotonic() - HTTP_SESSION_TTL_SECONDS - 1
            )

        manager.prune()

        self.assertEqual(runtime.close_count, 0)
        self.assertIn(runtime.http_session_id, manager._sessions)
        manager.close()

    def test_pressure_eviction_skips_runtime_with_background_exec(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        background = manager.create()
        manager.release(background.http_session_id)
        background.evictable = False
        for _ in range(MAX_HTTP_SESSIONS - 1):
            idle = manager.create()
            manager.release(idle.http_session_id)

        replacement = manager.create()

        self.assertEqual(background.close_count, 0)
        self.assertIn(background.http_session_id, manager._sessions)
        manager.release(replacement.http_session_id)
        manager.close()


if __name__ == "__main__":
    unittest.main()
