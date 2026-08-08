from __future__ import annotations

import os
import tempfile
import threading
from unittest.mock import patch

import pytest

from apps.devmcp.ui import UIHTTPServer, UIState
from coding_tools_mcp.config import write_secret


try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional release dependency
    pytest.skip("Playwright is an optional GUI test dependency", allow_module_level=True)


def test_dashboard_loads_and_secret_is_not_rendered() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
        state = UIState.load("127.0.0.1", 0)
        write_secret(state.config_paths.control_plane_key, "fixture-control-plane-secret")
        server = UIHTTPServer(("127.0.0.1", 0), state)
        state.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except PlaywrightError:
                    pytest.skip("Chromium is not installed")
                page = browser.new_page()
                page.goto(state.origin)
                assert page.get_by_text("DevMCP Runtime").count() >= 1
                assert "fixture-control-plane-secret" not in page.content()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_policy_switch_and_csrf_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
        state = UIState.load("127.0.0.1", 0)
        server = UIHTTPServer(("127.0.0.1", 0), state)
        state.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except PlaywrightError:
                    pytest.skip("Chromium is not installed")
                page = browser.new_page()
                page.goto(state.origin + "/permissions")
                csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
                assert csrf
                response = page.request.post(
                    state.origin + "/api/policy/profile",
                    form={"profile": "power", "csrf": csrf},
                    headers={"Origin": state.origin},
                )
                assert response.status == 200
                forbidden = page.request.post(
                    state.origin + "/api/policy/profile",
                    form={"profile": "safe", "csrf": csrf},
                    headers={"Origin": "http://evil.invalid"},
                )
                assert forbidden.status == 403
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
