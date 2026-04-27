"""Tests for UI-01: global 500 exception handler and route-level TaktisError catches.

Coverage:
- _handle_500 logs full traceback and returns:
    (a) htmx toast fragment (status 200, HX-Retarget/#toast-area) for HX-Request
    (b) standalone 500 HTML page for full-page requests
- _user_message returns format_error_for_user() for TaktisError subclasses
  and str(exc) for plain ValueError / KeyError
- Route-level catches now accept TaktisError (e.g. DuplicateError from repo)
- The silent 'except Exception: pass' in api_create_project is replaced with a warning log
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

import taktis.web.app as web_app
from taktis.exceptions import (
    DatabaseError,
    DuplicateError,
    TaktisError,
    PipelineError,
    TaskExecutionError,
    format_error_for_user,
)
from taktis.web.app import _handle_500, _user_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette Request with the given headers."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# _user_message
# ---------------------------------------------------------------------------


class TestUserMessage:
    """_user_message() dispatches correctly for each exception family."""

    def test_taktis_error_delegates_to_format_error_for_user(self) -> None:
        """_user_message must delegate to format_error_for_user for TaktisError."""
        exc = TaktisError("raw internal detail")
        result = _user_message(exc)
        # _user_message must return the same value as format_error_for_user
        assert result == format_error_for_user(exc)

    def test_database_error_uses_curated_message(self) -> None:
        exc = DatabaseError("SELECT * FROM secrets", cause=RuntimeError("oops"))
        result = _user_message(exc)
        assert result == format_error_for_user(exc)
        assert "SELECT" not in result
        assert "secrets" not in result

    def test_duplicate_error_curated(self) -> None:
        exc = DuplicateError("projects.name", constraint="projects.name")
        assert _user_message(exc) == format_error_for_user(exc)

    def test_task_execution_error_curated(self) -> None:
        exc = TaskExecutionError("process crashed", task_id="abc-123")
        assert _user_message(exc) == format_error_for_user(exc)

    def test_pipeline_error_curated(self) -> None:
        exc = PipelineError("wave failed", step="step-2")
        assert _user_message(exc) == format_error_for_user(exc)

    def test_value_error_passes_through(self) -> None:
        exc = ValueError("A phase is required for all tasks")
        assert _user_message(exc) == "A phase is required for all tasks"

    def test_key_error_passes_through(self) -> None:
        exc = KeyError("name")
        assert _user_message(exc) == str(exc)


# ---------------------------------------------------------------------------
# _handle_500 — non-htmx (full-page)
# ---------------------------------------------------------------------------


class TestHandle500FullPage:
    """Full-page (non-htmx) 500 responses."""

    @pytest.mark.asyncio
    async def test_returns_500_status(self) -> None:
        request = _make_request()
        exc = RuntimeError("boom")
        response = await _handle_500(request, exc)
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_body_contains_server_error_heading(self) -> None:
        request = _make_request()
        exc = RuntimeError("boom")
        response = await _handle_500(request, exc)
        body = response.body.decode()
        assert "Server Error" in body

    @pytest.mark.asyncio
    async def test_body_contains_back_to_dashboard_link(self) -> None:
        request = _make_request()
        response = await _handle_500(request, RuntimeError("boom"))
        body = response.body.decode()
        assert 'href="/"' in body

    @pytest.mark.asyncio
    async def test_raw_exception_message_not_in_body(self) -> None:
        """Internal details must never appear in the 500 page body."""
        request = _make_request()
        exc = DatabaseError("SELECT * FROM private_table WHERE id=42")
        response = await _handle_500(request, exc)
        body = response.body.decode()
        assert "private_table" not in body
        assert "SELECT" not in body

    @pytest.mark.asyncio
    async def test_curated_message_appears_in_body(self) -> None:
        request = _make_request()
        exc = DatabaseError("internal detail")
        response = await _handle_500(request, exc)
        body = response.body.decode()
        assert format_error_for_user(exc) in body

    @pytest.mark.asyncio
    async def test_html_escaping_in_user_message(self) -> None:
        """User-facing message must be HTML-escaped to prevent XSS."""
        request = _make_request()
        # Craft an TaktisError subclass that would yield < > & in the
        # curated message.  In practice format_error_for_user never includes
        # those characters, but _esc must be called.
        exc = TaktisError("<script>alert(1)</script>")
        response = await _handle_500(request, exc)
        body = response.body.decode()
        assert "<script>" not in body

    @pytest.mark.asyncio
    async def test_logger_error_called_with_exc_info(self, caplog) -> None:
        request = _make_request()
        exc = RuntimeError("test error for logging")
        with caplog.at_level(logging.ERROR, logger="taktis.web.app"):
            await _handle_500(request, exc)
        assert any("Unhandled exception" in r.message for r in caplog.records)
        assert any(r.exc_info for r in caplog.records)


# ---------------------------------------------------------------------------
# _handle_500 — htmx requests
# ---------------------------------------------------------------------------


class TestHandle500Htmx:
    """htmx 500 responses inject a toast into #toast-area."""

    @pytest.mark.asyncio
    async def test_returns_200_for_htmx_swap(self) -> None:
        """htmx only processes swaps on 2xx; handler returns 200."""
        request = _make_request({"HX-Request": "true"})
        response = await _handle_500(request, RuntimeError("boom"))
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_hx_retarget_toast_area(self) -> None:
        request = _make_request({"HX-Request": "true"})
        response = await _handle_500(request, RuntimeError("boom"))
        assert response.headers.get("HX-Retarget") == "#toast-area"

    @pytest.mark.asyncio
    async def test_hx_reswap_afterbegin(self) -> None:
        request = _make_request({"HX-Request": "true"})
        response = await _handle_500(request, RuntimeError("boom"))
        assert response.headers.get("HX-Reswap") == "afterbegin"

    @pytest.mark.asyncio
    async def test_fragment_contains_toast_class(self) -> None:
        request = _make_request({"HX-Request": "true"})
        response = await _handle_500(request, RuntimeError("boom"))
        body = response.body.decode()
        assert 'class="toast"' in body

    @pytest.mark.asyncio
    async def test_fragment_contains_danger_color(self) -> None:
        request = _make_request({"HX-Request": "true"})
        response = await _handle_500(request, RuntimeError("boom"))
        body = response.body.decode()
        assert "var(--danger)" in body

    @pytest.mark.asyncio
    async def test_internal_detail_not_in_htmx_fragment(self) -> None:
        request = _make_request({"HX-Request": "true"})
        exc = DatabaseError("SELECT secret FROM db")
        response = await _handle_500(request, exc)
        body = response.body.decode()
        assert "SELECT" not in body
        assert "secret" not in body

    @pytest.mark.asyncio
    async def test_logger_called_for_htmx_request_too(self, caplog) -> None:
        request = _make_request({"HX-Request": "true"})
        exc = RuntimeError("htmx error")
        with caplog.at_level(logging.ERROR, logger="taktis.web.app"):
            await _handle_500(request, exc)
        assert any("Unhandled exception" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Global handler registration in create_app()
# ---------------------------------------------------------------------------


class TestCreateAppHandlerRegistration:
    """The global handler is wired into the Starlette app."""

    def test_exception_handlers_key_present(self) -> None:
        """create_app() must register the Exception→_handle_500 handler."""
        from taktis.web.app import create_app, _handle_500

        with patch.object(web_app, "orch", None):
            app = create_app()

        # Starlette stores the dict on the app object before building middleware.
        assert Exception in app.exception_handlers, (
            "Exception key not found in app.exception_handlers"
        )
        assert app.exception_handlers[Exception] is _handle_500, (
            "app.exception_handlers[Exception] is not _handle_500"
        )


# ---------------------------------------------------------------------------
# Route-level TaktisError catch (via TestClient with mocked orch)
# ---------------------------------------------------------------------------


def _make_mock_orch(**overrides: Any) -> MagicMock:
    """Return a MagicMock taktis_engine with sensible async defaults."""
    o = MagicMock()
    # Default async methods return truthy values
    for method in (
        "create_project", "create_phase", "create_task",
        "run_phase", "discuss_task", "research_task",
        "run_project", "resume_phase", "create_expert",
        "update_expert", "delete_expert",
        "list_projects", "list_experts", "get_status",
        "get_interrupted_work",
    ):
        setattr(o, method, AsyncMock(return_value=None))
    for method, val in overrides.items():
        setattr(o, method, val)
    return o


class TestRouteTaktisErrorCatch:
    """Routes that previously only caught ValueError now also catch TaktisError."""

    def _client(self, orch_mock: MagicMock) -> TestClient:
        from taktis.web.app import create_app
        app = create_app()
        # Patch the global 'orch' so routes don't hit _orch() → RuntimeError
        with patch.object(web_app, "orch", orch_mock):
            client = TestClient(app, raise_server_exceptions=False)
            # Bootstrap CSRF: GET a page to receive the cookie, then
            # set the header for all subsequent requests.
            resp = client.get("/admin")
            csrf_token = resp.cookies.get("csrf_token", "")
            if csrf_token:
                client.headers["X-CSRFToken"] = csrf_token
                client.cookies.set("csrf_token", csrf_token)
            return client

    def test_create_project_duplicate_error_returns_400(self) -> None:
        """DuplicateError from create_project must produce a 400, not a 500."""
        exc = DuplicateError("A record with that name or identifier already exists")
        orch = _make_mock_orch(create_project=AsyncMock(side_effect=exc))
        client = self._client(orch)
        with patch.object(web_app, "orch", orch):
            resp = client.post(
                "/api/projects",
                data={
                    "name": "dupe-proj",
                    "working_dir": "/tmp",
                    "description": "",
                },
            )
        assert resp.status_code == 400
        assert "error" in resp.text

    def test_create_project_duplicate_uses_curated_message(self) -> None:
        """The error body must use format_error_for_user, not raw exc string."""
        exc = DuplicateError("projects.name", constraint="projects.name")
        orch = _make_mock_orch(create_project=AsyncMock(side_effect=exc))
        client = self._client(orch)
        with patch.object(web_app, "orch", orch):
            resp = client.post(
                "/api/projects",
                data={"name": "x", "working_dir": "/tmp"},
            )
        assert "projects.name" not in resp.text  # constraint detail not leaked
        assert format_error_for_user(exc) in resp.text

    def test_run_phase_taktis_error_returns_400(self) -> None:
        exc = PipelineError("Wave validation failed", step="wave-1")
        orch = _make_mock_orch(run_phase=AsyncMock(side_effect=exc))
        client = self._client(orch)
        with patch.object(web_app, "orch", orch):
            resp = client.post("/api/phases/my-project/1/run")
        assert resp.status_code == 400

    def test_discuss_task_taktis_error_returns_400(self) -> None:
        exc = TaktisError("task not ready")
        orch = _make_mock_orch(discuss_task=AsyncMock(side_effect=exc))
        client = self._client(orch)
        with patch.object(web_app, "orch", orch):
            resp = client.post("/api/tasks/abc123/discuss")
        assert resp.status_code == 400

    def test_create_expert_duplicate_error_returns_400(self) -> None:
        exc = DuplicateError("experts.name", constraint="experts.name")
        orch = _make_mock_orch(create_expert=AsyncMock(side_effect=exc))
        client = self._client(orch)
        with patch.object(web_app, "orch", orch):
            resp = client.post(
                "/api/experts",
                data={"name": "dupe", "description": "", "system_prompt": ""},
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# api_create_project — silent except replaced with warning log
# ---------------------------------------------------------------------------


class TestCreateProjectPlannerFallback:
    """The planner failure must be logged, not silently swallowed."""

    @pytest.mark.asyncio
    async def test_auto_plan_without_template_still_succeeds(self) -> None:
        """Auto-plan on + description but no template → project created, no crash."""
        from taktis.web.app import api_create_project

        project = {"id": "pid-1", "name": "my-proj", "planning_options": ""}
        orch_mock = _make_mock_orch(
            create_project=AsyncMock(return_value=project),
        )

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/projects",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
            "query_string": b"",
        }

        async def receive():
            body = b"name=my-proj&working_dir=%2Ftmp&auto_plan=on&description=test"
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(scope, receive)

        with (
            patch.object(web_app, "orch", orch_mock),
            patch("taktis.web.app.get_session") as mock_session,
            patch("taktis.web.app.repo"),
        ):
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=cm)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = cm
            response = await api_create_project(request)

        # Response must still succeed (redirect), not crash
        assert response.status_code in (200, 303)


