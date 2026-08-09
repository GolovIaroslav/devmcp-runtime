from __future__ import annotations

import os
import tempfile
import threading
from unittest.mock import patch

import pytest

from apps.devmcp.ui import UIHTTPServer, UIState
from coding_tools_mcp.approval import ApprovalEngine
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


def test_setup_save_preserves_existing_mcp_token() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
        state = UIState.load("127.0.0.1", 0)
        write_secret(state.config_paths.mcp_token, "fixture-token-that-must-not-rotate")
        original = state.config_paths.mcp_token.read_text(encoding="utf-8")
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
                page.goto(state.origin + "/setup")
                csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
                assert csrf
                for profile in ("balanced", "power"):
                    response = page.request.post(
                        state.origin + "/api/setup",
                        form={"workspace": state.config["workspace"], "profile": profile, "csrf": csrf},
                        headers={"Origin": state.origin},
                    )
                    assert response.status == 200
                assert state.config_paths.mcp_token.read_text(encoding="utf-8") == original
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_rotate_mcp_token_requires_explicit_confirmation() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
        state = UIState.load("127.0.0.1", 0)
        write_secret(state.config_paths.mcp_token, "fixture-token-before-explicit-rotation")
        original = state.config_paths.mcp_token.read_text(encoding="utf-8")
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
                page.goto(state.origin + "/setup")
                csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
                assert csrf
                rejected = page.request.post(
                    state.origin + "/api/auth/rotate-mcp-token",
                    form={"confirmation": "no", "csrf": csrf},
                    headers={"Origin": state.origin},
                )
                assert rejected.status == 400
                assert state.config_paths.mcp_token.read_text(encoding="utf-8") == original
                rotated = page.request.post(
                    state.origin + "/api/auth/rotate-mcp-token",
                    form={"confirmation": "ROTATE", "csrf": csrf},
                    headers={"Origin": state.origin},
                )
                assert rotated.status == 200
                assert state.config_paths.mcp_token.read_text(encoding="utf-8") != original
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_permissions_edit_destructive_patch_thresholds() -> None:
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
                assert page.get_by_text("Destructive patch thresholds").count() == 1
                csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
                response = page.request.post(
                    state.origin + "/api/policy/patch",
                    form={"max_removed_lines": "17", "max_removed_percent": "12.5", "csrf": csrf},
                    headers={"Origin": state.origin},
                )
                assert response.status == 200
                assert state.config["patch"] == {"max_removed_lines": 17, "max_removed_percent": 12.5}
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_approvals_show_risk_expiry_capabilities_and_scoped_allow() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEVMCP_CONFIG_DIR": tmp}, clear=False):
        state = UIState.load("127.0.0.1", 0)
        approval = ApprovalEngine(state.config_paths.approvals_db).request_approval(
            "curl https://example.invalid", state.config["workspace"], "network", "high", True, capabilities=["network.public"]
        )
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
                page.goto(state.origin + "/approvals")
                assert page.get_by_text("Risk").count() >= 1
                assert page.get_by_text("Expires").count() >= 1
                assert page.get_by_text("network.public").count() >= 1
                csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
                response = page.request.post(
                    state.origin + f"/api/approvals/{approval['approval_id']}/approve-always",
                    form={"pattern": "curl https://example.invalid", "csrf": csrf},
                    headers={"Origin": state.origin},
                )
                assert response.status == 200
                assert ApprovalEngine(state.config_paths.approvals_db).get_status(approval["approval_id"]) == "approved"
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_services_page_exposes_service_controls() -> None:
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
                page.goto(state.origin + "/services")
                for label in ("Start", "Stop", "Restart"):
                    assert page.get_by_role("button", name=label, exact=True).count() == 1
                assert page.get_by_role("link", name="Open redacted service logs").count() == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
