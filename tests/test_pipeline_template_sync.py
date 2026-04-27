"""Tests for pipeline template auto-sync (seed + update built-ins on startup).

Verifies that ``_seed_pipeline_templates()`` in ``taktis/db.py``:
1. Inserts new templates that don't exist in the DB.
2. Updates built-in templates (``is_default=1``) when JSON content changes.
3. Skips user-created templates (``is_default=0``) — never overwrites them.
4. Is idempotent — calling it twice with the same JSON files is a no-op.
"""
from __future__ import annotations

import json
import os
import uuid

import aiosqlite
import pytest
import pytest_asyncio

from taktis.db import _CREATE_TABLES_SQL, _seed_pipeline_templates


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB with pipeline_templates table created."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(_CREATE_TABLES_SQL)
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.close()


def _write_json_template(directory, filename, name, description, flow_json, is_default=True):
    """Write a pipeline template JSON file into *directory*."""
    path = os.path.join(directory, filename)
    data = {
        "name": name,
        "description": description,
        "flow_json": flow_json,
        "is_default": is_default,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineTemplateSync:
    """Seed + sync behaviour for built-in pipeline templates."""

    async def test_seeds_new_template(self, db, tmp_path, monkeypatch):
        """A template not in the DB is inserted."""
        templates_dir = tmp_path / "pipeline_templates"
        templates_dir.mkdir()
        _write_json_template(
            str(templates_dir), "test-pipeline.json",
            name="Test Pipeline",
            description="A test pipeline",
            flow_json={"drawflow": {"Home": {"data": {}}}},
        )
        monkeypatch.setattr(
            "taktis.db._get_defaults_dir",
            lambda: tmp_path,
        )

        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT name, description, is_default FROM pipeline_templates")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Test Pipeline"
        assert rows[0][1] == "A test pipeline"
        assert rows[0][2] == 1  # is_default

    async def test_updates_builtin_template_when_content_changes(self, db, tmp_path, monkeypatch):
        """A built-in template (is_default=1) is updated when JSON changes."""
        templates_dir = tmp_path / "pipeline_templates"
        templates_dir.mkdir()
        monkeypatch.setattr(
            "taktis.db._get_defaults_dir",
            lambda: tmp_path,
        )

        # Seed initial version
        _write_json_template(
            str(templates_dir), "my-pipeline.json",
            name="My Pipeline",
            description="Version 1",
            flow_json={"drawflow": {"Home": {"data": {"1": {"id": 1}}}}},
        )
        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT description, flow_json FROM pipeline_templates WHERE name = 'My Pipeline'")
        row = await cur.fetchone()
        assert row[0] == "Version 1"
        original_flow = json.loads(row[1])
        assert "1" in original_flow["drawflow"]["Home"]["data"]

        # Update the JSON file with new content
        _write_json_template(
            str(templates_dir), "my-pipeline.json",
            name="My Pipeline",
            description="Version 2 — improved prompts",
            flow_json={"drawflow": {"Home": {"data": {"1": {"id": 1}, "2": {"id": 2}}}}},
        )
        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT description, flow_json FROM pipeline_templates WHERE name = 'My Pipeline'")
        row = await cur.fetchone()
        assert row[0] == "Version 2 — improved prompts"
        updated_flow = json.loads(row[1])
        assert "2" in updated_flow["drawflow"]["Home"]["data"]

    async def test_skips_user_created_template(self, db, tmp_path, monkeypatch):
        """A user-created template (is_default=0) is never overwritten."""
        templates_dir = tmp_path / "pipeline_templates"
        templates_dir.mkdir()
        monkeypatch.setattr(
            "taktis.db._get_defaults_dir",
            lambda: tmp_path,
        )

        # Manually insert a user-created template with the same name
        user_desc = "My custom description"
        user_flow = json.dumps({"drawflow": {"Home": {"data": {"custom": True}}}})
        await db.execute(
            """INSERT INTO pipeline_templates (id, name, description, flow_json, is_default, created_at)
               VALUES (?, ?, ?, ?, 0, datetime('now'))""",
            (uuid.uuid4().hex, "User Pipeline", user_desc, user_flow),
        )
        await db.commit()

        # Put a JSON file with the same name but is_default=false
        _write_json_template(
            str(templates_dir), "user-pipeline.json",
            name="User Pipeline",
            description="Built-in version trying to overwrite",
            flow_json={"drawflow": {"Home": {"data": {"builtin": True}}}},
            is_default=False,
        )
        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT description, flow_json FROM pipeline_templates WHERE name = 'User Pipeline'")
        row = await cur.fetchone()
        # Must remain the user's original
        assert row[0] == user_desc
        assert json.loads(row[1]) == {"drawflow": {"Home": {"data": {"custom": True}}}}

    async def test_idempotent_no_change(self, db, tmp_path, monkeypatch):
        """Calling sync twice with identical JSON files does not update timestamps."""
        templates_dir = tmp_path / "pipeline_templates"
        templates_dir.mkdir()
        monkeypatch.setattr(
            "taktis.db._get_defaults_dir",
            lambda: tmp_path,
        )

        _write_json_template(
            str(templates_dir), "stable.json",
            name="Stable Pipeline",
            description="Never changes",
            flow_json={"drawflow": {"Home": {"data": {}}}},
        )

        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT updated_at FROM pipeline_templates WHERE name = 'Stable Pipeline'")
        first_updated = (await cur.fetchone())[0]

        # Call again — should be a no-op
        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT updated_at FROM pipeline_templates WHERE name = 'Stable Pipeline'")
        second_updated = (await cur.fetchone())[0]

        assert first_updated == second_updated

    async def test_upgrades_existing_to_builtin_when_json_says_is_default(self, db, tmp_path, monkeypatch):
        """An existing template with is_default=0 gets upgraded to is_default=1
        when the JSON file has is_default=true, enabling future syncs."""
        templates_dir = tmp_path / "pipeline_templates"
        templates_dir.mkdir()
        monkeypatch.setattr(
            "taktis.db._get_defaults_dir",
            lambda: tmp_path,
        )

        # Insert a template that was previously seeded without is_default=1
        old_flow = json.dumps({"drawflow": {"Home": {"data": {"old": True}}}})
        await db.execute(
            """INSERT INTO pipeline_templates (id, name, description, flow_json, is_default, created_at)
               VALUES (?, ?, ?, ?, 0, datetime('now'))""",
            (uuid.uuid4().hex, "Legacy Pipeline", "Old desc", old_flow),
        )
        await db.commit()

        # JSON file says is_default=true — this should upgrade the DB record
        _write_json_template(
            str(templates_dir), "legacy.json",
            name="Legacy Pipeline",
            description="Updated desc",
            flow_json={"drawflow": {"Home": {"data": {"new": True}}}},
            is_default=True,
        )
        await _seed_pipeline_templates(db)

        cur = await db.execute(
            "SELECT is_default, description, flow_json FROM pipeline_templates WHERE name = 'Legacy Pipeline'"
        )
        row = await cur.fetchone()
        assert row[0] == 1  # upgraded to built-in
        assert row[1] == "Updated desc"
        assert json.loads(row[2]) == {"drawflow": {"Home": {"data": {"new": True}}}}

    async def test_multiple_templates_mixed(self, db, tmp_path, monkeypatch):
        """Multiple JSON files: one new, one updated, one unchanged."""
        templates_dir = tmp_path / "pipeline_templates"
        templates_dir.mkdir()
        monkeypatch.setattr(
            "taktis.db._get_defaults_dir",
            lambda: tmp_path,
        )

        # Pre-seed two templates in DB
        flow_a = json.dumps({"drawflow": {"Home": {"data": {"a": 1}}}})
        flow_b = json.dumps({"drawflow": {"Home": {"data": {"b": 1}}}})
        await db.execute(
            """INSERT INTO pipeline_templates (id, name, description, flow_json, is_default, created_at)
               VALUES (?, ?, ?, ?, 1, datetime('now'))""",
            (uuid.uuid4().hex, "Template A", "Desc A", flow_a),
        )
        await db.execute(
            """INSERT INTO pipeline_templates (id, name, description, flow_json, is_default, created_at)
               VALUES (?, ?, ?, ?, 1, datetime('now'))""",
            (uuid.uuid4().hex, "Template B", "Desc B", flow_b),
        )
        await db.commit()

        # JSON files: A unchanged, B updated, C new
        _write_json_template(
            str(templates_dir), "a.json",
            name="Template A", description="Desc A",
            flow_json={"drawflow": {"Home": {"data": {"a": 1}}}},
        )
        _write_json_template(
            str(templates_dir), "b.json",
            name="Template B", description="Desc B v2",
            flow_json={"drawflow": {"Home": {"data": {"b": 2}}}},
        )
        _write_json_template(
            str(templates_dir), "c.json",
            name="Template C", description="Brand new",
            flow_json={"drawflow": {"Home": {"data": {"c": 1}}}},
        )

        await _seed_pipeline_templates(db)

        cur = await db.execute("SELECT name, description FROM pipeline_templates ORDER BY name")
        rows = {row[0]: row[1] for row in await cur.fetchall()}

        assert rows["Template A"] == "Desc A"  # unchanged
        assert rows["Template B"] == "Desc B v2"  # updated
        assert rows["Template C"] == "Brand new"  # new


class TestBuiltInJsonFilesHaveIsDefault:
    """Verify that all shipped JSON template files have is_default=true."""

    def test_all_json_files_have_is_default_true(self):
        from taktis.db import _get_defaults_dir

        defaults_dir = _get_defaults_dir() / "pipeline_templates"
        assert defaults_dir.is_dir(), f"Missing directory: {defaults_dir}"

        for json_path in sorted(defaults_dir.glob("*.json")):
            data = json.loads(json_path.read_text(encoding="utf-8"))
            assert data.get("is_default") is True, (
                f"{json_path.name} has is_default={data.get('is_default')!r}, expected true"
            )
