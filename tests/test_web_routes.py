"""Smoke tests for web UI routes.

Tests that routes return expected status codes and contain key HTML
elements.  Uses a real Taktis with in-memory DB — no mocking of
the data layer, only of the Starlette lifespan (since the test manages
its own Taktis lifecycle).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import patch, AsyncMock

import httpx
from contextlib import asynccontextmanager

import taktis.db as db_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def web_client(tmp_path, _golden_db_path):
    """Provide an httpx.AsyncClient wired to the Starlette app with a real
    Taktis (in-memory DB).  Bypasses the normal lifespan so we control
    init/shutdown ourselves.
    """
    import shutil
    from taktis.core.engine import Taktis
    from taktis.web.app import create_app
    import taktis.web.app as app_mod

    db_file = str(tmp_path / "web_test.db")
    original_path = db_mod.DATABASE_PATH

    shutil.copy2(_golden_db_path, db_file)
    db_mod.DATABASE_PATH = db_file

    orch = Taktis()
    with patch(
        "taktis.core.experts.ExpertRegistry.load_builtins",
        new_callable=AsyncMock,
    ), patch(
        "taktis.core.agent_templates.AgentTemplateRegistry.load_builtins",
        new_callable=AsyncMock,
    ):
        await orch.initialize()

    # Inject the taktis_engine directly into the web module
    original_orch = app_mod.orch
    app_mod.orch = orch

    # Create app with a no-op lifespan (we manage orch ourselves)
    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Bootstrap CSRF: GET any page to set the csrf_token cookie,
        # then configure the client to send it as a header on every request.
        bootstrap = await client.get("/")
        csrf_token = client.cookies.get("csrf_token", "")
        if csrf_token:
            client.headers["X-CSRFToken"] = csrf_token
        yield client, orch

    app_mod.orch = original_orch
    await orch.shutdown()
    db_mod.DATABASE_PATH = original_path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _create_project(orch, name="test-proj", working_dir="."):
    """Create a project and return its dict."""
    return await orch.create_project(
        name=name, working_dir=working_dir, description="Test project",
    )


async def _create_project_with_task(orch, project_name="test-proj"):
    """Create a project, phase, and task. Return (project, phase, task)."""
    project = await _create_project(orch, project_name)
    phase = await orch.create_phase(
        project_name=project_name, name="Phase 1", goal="Test goal",
    )
    task = await orch.create_task(
        project_name=project_name, prompt="Do something",
        phase_number=phase["phase_number"], name="Test task",
    )
    return project, phase, task


# ======================================================================
# GET page routes
# ======================================================================

class TestPageRoutes:
    """Verify all page routes return 200 with expected content."""

    @pytest.mark.asyncio
    async def test_dashboard(self, web_client):
        client, orch = web_client
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "dashboard" in resp.text.lower() or "<!DOCTYPE" in resp.text

    @pytest.mark.asyncio
    async def test_projects_page(self, web_client):
        client, orch = web_client
        resp = await client.get("/projects")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_experts_page(self, web_client):
        client, orch = web_client
        resp = await client.get("/experts")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_page(self, web_client):
        client, orch = web_client
        resp = await client.get("/admin")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_project_detail_exists(self, web_client):
        client, orch = web_client
        await _create_project(orch, "detail-proj")
        resp = await client.get("/projects/detail-proj")
        assert resp.status_code == 200
        assert "detail-proj" in resp.text

    @pytest.mark.asyncio
    async def test_project_detail_not_found(self, web_client):
        client, orch = web_client
        resp = await client.get("/projects/nonexistent")
        # Should return 200 with error message or redirect, not crash
        assert resp.status_code in (200, 302, 404)

    @pytest.mark.asyncio
    async def test_task_detail_exists(self, web_client):
        client, orch = web_client
        _, _, task = await _create_project_with_task(orch, "task-proj")
        resp = await client.get(f"/tasks/{task['id']}")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_task_detail_not_found(self, web_client):
        client, orch = web_client
        resp = await client.get("/tasks/nonexistent")
        assert resp.status_code in (200, 302, 404)


# ======================================================================
# GET partial routes (htmx fragments)
# ======================================================================

class TestPartialRoutes:

    @pytest.mark.asyncio
    async def test_status_cards(self, web_client):
        client, orch = web_client
        resp = await client.get("/partials/status-cards")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_active_tasks_empty(self, web_client):
        client, orch = web_client
        resp = await client.get("/partials/active-tasks")
        assert resp.status_code == 200
        assert "No active tasks" in resp.text

    @pytest.mark.asyncio
    async def test_project_list(self, web_client):
        client, orch = web_client
        await _create_project(orch, "listed-proj")
        resp = await client.get("/partials/project-list")
        assert resp.status_code == 200
        assert "listed-proj" in resp.text

    @pytest.mark.asyncio
    async def test_task_output_not_found(self, web_client):
        client, orch = web_client
        resp = await client.get("/partials/task-output/nonexistent")
        assert resp.status_code == 200  # Returns empty fragment

    @pytest.mark.asyncio
    async def test_task_status(self, web_client):
        client, orch = web_client
        _, _, task = await _create_project_with_task(orch, "status-proj")
        resp = await client.get(f"/partials/task-status/{task['id']}")
        assert resp.status_code == 200


# ======================================================================
# POST API routes — project CRUD
# ======================================================================

class TestProjectAPI:

    @pytest.mark.asyncio
    async def test_create_project(self, web_client):
        client, orch = web_client
        resp = await client.post("/api/projects", data={
            "name": "api-proj",
            "working_dir": ".",
            "description": "Created via API",
        })
        # Should redirect to project detail on success
        assert resp.status_code in (200, 303), resp.text

    @pytest.mark.asyncio
    async def test_create_project_duplicate(self, web_client):
        client, orch = web_client
        await _create_project(orch, "dup-proj")
        resp = await client.post(
            "/api/projects",
            data={"name": "dup-proj", "working_dir": "."},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200  # htmx gets 200 with error div
        assert "error" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_delete_project(self, web_client):
        client, orch = web_client
        await _create_project(orch, "del-proj")
        resp = await client.delete("/api/projects/del-proj")
        assert resp.status_code in (200, 303)

    @pytest.mark.asyncio
    async def test_create_phase(self, web_client):
        client, orch = web_client
        await _create_project(orch, "phase-proj")
        resp = await client.post(
            "/api/projects/phase-proj/phases",
            data={"name": "New Phase", "goal": "Test"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code in (200, 303)


# ======================================================================
# POST API routes — task operations
# ======================================================================

class TestTaskAPI:

    @pytest.mark.asyncio
    async def test_create_task(self, web_client):
        client, orch = web_client
        project = await _create_project(orch, "tapi-proj")
        phase = await orch.create_phase(
            project_name="tapi-proj", name="P1", goal="G",
        )
        resp = await client.post(
            "/api/projects/tapi-proj/tasks",
            data={
                "prompt": "Do the thing",
                "phase_number": str(phase["phase_number"]),
                "name": "Test Task",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code in (200, 303)

    @pytest.mark.asyncio
    async def test_start_task_not_found(self, web_client):
        client, orch = web_client
        resp = await client.post("/api/tasks/nonexistent/start")
        assert resp.status_code in (200, 400)

    @pytest.mark.asyncio
    async def test_stop_task_not_found(self, web_client):
        client, orch = web_client
        resp = await client.post("/api/tasks/nonexistent/stop")
        assert resp.status_code in (200, 400)

    @pytest.mark.asyncio
    async def test_send_input_no_message(self, web_client):
        client, orch = web_client
        resp = await client.post(
            "/api/tasks/fake123/input",
            data={"message": ""},
        )
        assert resp.status_code == 400
        assert "No input" in resp.text or "error" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_continue_no_message(self, web_client):
        client, orch = web_client
        resp = await client.post(
            "/api/tasks/fake123/continue",
            data={"message": ""},
        )
        assert resp.status_code == 400


# ======================================================================
# POST API routes — expert CRUD
# ======================================================================

class TestExpertAPI:

    @pytest.mark.asyncio
    async def test_create_expert(self, web_client):
        client, orch = web_client
        resp = await client.post(
            "/api/experts",
            data={
                "name": "test-expert",
                "description": "A test expert",
                "system_prompt": "You are a test expert.",
                "category": "testing",
            },
        )
        assert resp.status_code in (200, 303)

    @pytest.mark.asyncio
    async def test_delete_expert_not_found(self, web_client):
        client, orch = web_client
        resp = await client.post("/api/experts/nonexistent/delete")
        assert resp.status_code in (200, 400, 404)


# ======================================================================
# API misc routes
# ======================================================================

class TestMiscAPI:

    @pytest.mark.asyncio
    async def test_interrupted_work(self, web_client):
        client, orch = web_client
        resp = await client.get("/api/interrupted")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_stop_all(self, web_client):
        client, orch = web_client
        resp = await client.post("/api/stop-all")
        assert resp.status_code in (200, 303)
