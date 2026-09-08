from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from coding_tools_mcp.protocol import dispatch_rpc
from coding_tools_mcp.server import MCPHandler
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
    def test_closed_peer_during_response_write_is_not_a_server_failure(self) -> None:
        class ClosedPeer:
            def write(self, _body: bytes) -> None:
                raise ConnectionAbortedError("peer closed")

        class FakeHandler:
            wfile = ClosedPeer()
            close_connection = False

        handler = FakeHandler()
        MCPHandler._write_body_safely(handler, b"response")  # type: ignore[arg-type]
        self.assertTrue(handler.close_connection)

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
            self.assertEqual(
                manager.stats(),
                {
                    "capacity": MAX_HTTP_SESSIONS,
                    "total": MAX_HTTP_SESSIONS,
                    "creating": 0,
                    "active_sessions": 0,
                    "active_requests": 0,
                    "closing": 0,
                },
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

    def test_evictability_check_does_not_block_concurrent_requests(self) -> None:
        factory = RuntimeFactory()
        manager = HTTPSessionManager(factory)
        try:
            runtimes = []
            for _ in range(MAX_HTTP_SESSIONS):
                rt = manager.create()
                manager.release(rt.http_session_id)
                runtimes.append(rt)

            in_evictable = threading.Event()
            allow_evictable = threading.Event()

            def blocking_evictable() -> bool:
                in_evictable.set()
                allow_evictable.wait(timeout=2.0)
                return True

            runtimes[0].http_session_evictable = blocking_evictable

            create_result: list[Any] = []

            def do_create() -> None:
                new_rt = manager.create()
                create_result.append(new_rt)

            t = threading.Thread(target=do_create, daemon=True)
            t.start()

            self.assertTrue(in_evictable.wait(timeout=2.0))

            stats = manager.stats()
            self.assertEqual(stats["capacity"], MAX_HTTP_SESSIONS)
            active_rt = manager.get(runtimes[1].http_session_id)
            self.assertIs(active_rt, runtimes[1])
            manager.release(runtimes[1].http_session_id)

            allow_evictable.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(len(create_result), 1)
        finally:
            manager.close()

    def test_close_process_streams_skips_alive_reader_threads(self) -> None:
        from coding_tools_mcp.processes import ExecSession

        class FakeStream:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        stdin = FakeStream()
        stdout = FakeStream()
        stderr = FakeStream()

        class FakeProc:
            pass

        proc = FakeProc()
        proc.stdin = stdin
        proc.stdout = stdout
        proc.stderr = stderr

        alive_thread = threading.Thread(target=lambda: time.sleep(1), daemon=True)
        alive_thread.start()

        session = ExecSession(
            session_id="test-session",
            process=proc,
            reader_threads=[alive_thread],
        )

        session.close_process_streams()

        self.assertTrue(stdin.closed)
        self.assertFalse(stdout.closed)
        self.assertFalse(stderr.closed)


class BearerAuthorizationTests(unittest.TestCase):
    def test_auth_disabled_returns_true(self) -> None:
        class FakeRuntimeNoAuth:
            auth_token = None
            auth_tokens: tuple[str, ...] = ()
            oauth_config = None

            def auth_enabled(self) -> bool:
                return False

        class FakeHandler:
            runtime = FakeRuntimeNoAuth()
            headers: dict[str, str] = {}

        self.assertTrue(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

    def test_single_token_accepted_with_bearer_and_raw_rejected(self) -> None:
        class FakeRuntimeSingle:
            auth_token = "mcp-primary-secret"
            auth_tokens = ("mcp-primary-secret",)
            oauth_config = None

            def auth_enabled(self) -> bool:
                return True

        class FakeHandler:
            runtime = FakeRuntimeSingle()
            headers: dict[str, str] = {"Authorization": "Bearer mcp-primary-secret"}

        self.assertTrue(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

        # Raw token without Bearer scheme must be strictly rejected
        FakeHandler.headers = {"Authorization": "mcp-primary-secret"}
        self.assertFalse(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

        FakeHandler.headers = {"Authorization": "Bearer wrong-secret"}
        self.assertFalse(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

    def test_multiple_tokens_accepted(self) -> None:
        class FakeRuntimeMulti:
            auth_token = "mcp-primary-secret"
            auth_tokens = ("mcp-primary-secret", "mcp-secondary-secret")
            oauth_config = None

            def auth_enabled(self) -> bool:
                return True

        class FakeHandler:
            runtime = FakeRuntimeMulti()
            headers: dict[str, str] = {"Authorization": "Bearer mcp-secondary-secret"}

        self.assertTrue(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

        FakeHandler.headers = {"Authorization": "Bearer mcp-primary-secret"}
        self.assertTrue(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

        # Raw token without Bearer prefix rejected
        FakeHandler.headers = {"Authorization": "mcp-secondary-secret"}
        self.assertFalse(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

        FakeHandler.headers = {"Authorization": "Bearer some-random-attacker-token"}
        self.assertFalse(MCPHandler.is_authorized(FakeHandler()))  # type: ignore[arg-type]

    def test_server_discover_uninitialized(self) -> None:
        class FakeUninitRuntime:
            initialized = False
            protocol_version = "2025-11-25"

            def server_instructions(self) -> str:
                return "test instructions"

        runtime = FakeUninitRuntime()
        request = {
            "jsonrpc": "2.0",
            "id": "openai-mcp-discover",
            "method": "server/discover",
        }
        response = dispatch_rpc(runtime, request)
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.get("id"), "openai-mcp-discover")
        result = response.get("result", {})
        self.assertIn("2025-11-25", result.get("supportedVersions", []))
        self.assertIn("tools", result.get("capabilities", {}))
        self.assertEqual(result.get("serverInfo", {}).get("name"), "devmcp-runtime")
        self.assertEqual(result.get("instructions"), "test instructions")

    def test_protocol_version_header_optional_on_subsequent_requests(self) -> None:
        import json
        from io import BytesIO
        from email.message import Message
        from coding_tools_mcp.server import MCPHandler

        # Mocks to simulate a request to the server
        class MockServer:
            control_runtime = None

            def __init__(self):
                self.sessions = self

            def get(self, session_id):
                class FakeRuntime:
                    http_session_id = session_id
                    protocol_version = "2025-11-25"
                    auth_tokens = ()

                    def auth_enabled(self):
                        return False

                    def initialize(self, info):
                        pass

                return FakeRuntime() if session_id == "valid-session" else None

            def touch(self, session_id):
                pass

            def release(self, session_id):
                pass

        class MockRequest:
            def makefile(self, *args, **kwargs):
                return BytesIO(b"")

        # Basic request template
        def simulate_request(header_dict: dict[str, str]) -> dict[str, Any]:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(
                "utf-8"
            )

            headers = Message()
            for k, v in header_dict.items():
                headers[k] = v
            headers["Content-Length"] = str(len(body))
            headers["Content-Type"] = "application/json"

            handler = MCPHandler(
                MockRequest(), client_address=("127.0.0.1", 12345), server=MockServer()
            )  # type: ignore
            handler.path = "/mcp"
            handler.headers = headers  # type: ignore
            handler.rfile = BytesIO(body)
            handler.wfile = BytesIO()
            handler.client_address = ("127.0.0.1", 12345)
            handler._runtime = handler.server.get("valid-session")

            # Monkeypatch send_rpc_error and send_json to capture response
            handler.response_data = None
            handler.error_data = None

            def send_json(payload, **kwargs):
                handler.response_data = payload

            def send_rpc_error(code, msg, **kwargs):
                handler.error_data = {"code": code, "message": msg, **kwargs}

            handler.send_json = send_json
            handler.send_rpc_error = send_rpc_error
            handler.handle_rpc = lambda req: {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {},
            }

            handler.do_POST()
            return {"response": handler.response_data, "error": handler.error_data}

        # 1. Missing header -> Accepted
        res1 = simulate_request({"Mcp-Session-Id": "valid-session"})
        self.assertIsNone(res1["error"])
        self.assertIsNotNone(res1["response"])

        # 2. Matching header -> Accepted
        res2 = simulate_request(
            {"Mcp-Session-Id": "valid-session", "MCP-Protocol-Version": "2025-11-25"}
        )
        self.assertIsNone(res2["error"])
        self.assertIsNotNone(res2["response"])

        # 3. Mismatched header -> Rejected
        res3 = simulate_request(
            {"Mcp-Session-Id": "valid-session", "MCP-Protocol-Version": "2025-06-18"}
        )
        self.assertIsNotNone(res3["error"])
        self.assertEqual(res3["error"]["code"], -32600)
        self.assertIn("does not match", res3["error"]["message"])


if __name__ == "__main__":
    unittest.main()
