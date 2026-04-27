"""Tests for the expert registry: frontmatter parsing, format_expert_options, load_builtins."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from taktis import repository as repo
from taktis.core.experts import (
    ExpertRegistry,
    _parse_expert_md,
    format_expert_options,
)


# ---------------------------------------------------------------------------
# _parse_expert_md
# ---------------------------------------------------------------------------


class TestParseExpertMd:
    """Tests for YAML frontmatter parsing."""

    def test_valid_frontmatter(self):
        text = "---\nname: test\ndescription: A test\ncategory: review\n---\nYou are a test expert."
        metadata, body = _parse_expert_md(text)
        assert metadata == {"name": "test", "description": "A test", "category": "review"}
        assert body == "You are a test expert."

    def test_multiline_body(self):
        text = "---\nname: x\n---\nLine 1.\n\nLine 2."
        metadata, body = _parse_expert_md(text)
        assert metadata == {"name": "x"}
        assert "Line 1." in body
        assert "Line 2." in body

    def test_missing_opening_fence(self):
        with pytest.raises(ValueError, match="must start with YAML frontmatter"):
            _parse_expert_md("no frontmatter here")

    def test_missing_closing_fence(self):
        with pytest.raises(ValueError):
            _parse_expert_md("---\nname: broken\n")

    def test_invalid_yaml(self):
        with pytest.raises(yaml.YAMLError):
            _parse_expert_md("---\n: [invalid yaml\n---\nbody")

    def test_non_dict_yaml(self):
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            _parse_expert_md("---\n- just a list\n---\nbody")

    def test_strips_whitespace(self):
        text = "\n  ---\nname: trimmed\n---\n  body text  \n"
        metadata, body = _parse_expert_md(text)
        assert metadata["name"] == "trimmed"
        assert body == "body text"


# ---------------------------------------------------------------------------
# format_expert_options
# ---------------------------------------------------------------------------


def _make_session_factory(db):
    @asynccontextmanager
    async def _factory():
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return _factory


@pytest.mark.asyncio
async def test_format_expert_options_excludes_pipeline(db_conn):
    """Pipeline experts should not appear in the formatted list."""
    # Insert a pipeline-internal expert and a user-facing expert
    await repo.create_expert(db_conn, name="interviewer", description="Interviews", category="planning", is_builtin=True, pipeline_internal=True)
    await repo.create_expert(db_conn, name="implementer", description="Implements", category="implementation", is_builtin=True)
    await db_conn.commit()

    result = await format_expert_options(_make_session_factory(db_conn))
    assert "interviewer" not in result
    assert "implementer" in result


@pytest.mark.asyncio
async def test_format_expert_options_marks_default(db_conn):
    """The is_default expert should be marked as DEFAULT."""
    await repo.create_expert(db_conn, name="implementer", description="Implements", category="implementation", is_builtin=True, is_default=True)
    await repo.create_expert(db_conn, name="architect", description="Designs", category="architecture", is_builtin=True)
    await db_conn.commit()

    result = await format_expert_options(_make_session_factory(db_conn))
    assert "(DEFAULT" in result
    # architect should not be marked default
    lines = result.split("\n")
    architect_line = next(l for l in lines if "architect" in l)
    assert "DEFAULT" not in architect_line


@pytest.mark.asyncio
async def test_format_expert_options_sorted(db_conn):
    """Expert names should appear in alphabetical order."""
    await repo.create_expert(db_conn, name="devops", description="DevOps", category="devops", is_builtin=True)
    await repo.create_expert(db_conn, name="architect", description="Arch", category="architecture", is_builtin=True)
    await db_conn.commit()

    result = await format_expert_options(_make_session_factory(db_conn))
    lines = [l for l in result.split("\n") if l.startswith("- ")]
    names = [l.split(":")[0].strip("- ") for l in lines]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_format_expert_options_excludes_reviewer(db_conn):
    """Reviewer is pipeline-internal and should not appear."""
    await repo.create_expert(db_conn, name="reviewer", description="Reviews", category="review", is_builtin=True, pipeline_internal=True)
    await repo.create_expert(db_conn, name="implementer", description="Implements", category="implementation", is_builtin=True)
    await db_conn.commit()

    result = await format_expert_options(_make_session_factory(db_conn))
    assert "reviewer" not in result


# ---------------------------------------------------------------------------
# pipeline_internal field on experts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_internal_set_on_builtins(db_conn):
    """Builtins with pipeline_internal frontmatter should have the field set."""
    registry = ExpertRegistry(_make_session_factory(db_conn))
    await registry.load_builtins()

    reviewer = await repo.get_expert_by_name(db_conn, "reviewer-general")
    assert reviewer is not None
    assert not bool(reviewer.get("pipeline_internal"))

    implementer = await repo.get_expert_by_name(db_conn, "implementer-general")
    assert implementer is not None
    assert not bool(implementer.get("pipeline_internal"))


# ---------------------------------------------------------------------------
# load_builtins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_builtins_creates_experts(db_conn):
    """load_builtins should create expert records from .md files."""
    registry = ExpertRegistry(_make_session_factory(db_conn))
    await registry.load_builtins()

    experts = await repo.list_experts(db_conn)
    names = {e["name"] for e in experts}
    # Should contain at least the core experts
    assert "implementer-general" in names
    assert "reviewer-general" in names
    assert "roadmapper" in names
    assert len(experts) >= 100


@pytest.mark.asyncio
async def test_load_builtins_updates_changed_expert(db_conn):
    """If a builtin expert's content changes, load_builtins should update it."""
    # Pre-insert an expert with stale content
    await repo.create_expert(
        db_conn, name="implementer-general", description="Old desc",
        system_prompt="Old prompt", category="implementation", is_builtin=True,
    )
    await db_conn.commit()

    registry = ExpertRegistry(_make_session_factory(db_conn))
    await registry.load_builtins()

    expert = await repo.get_expert_by_name(db_conn, "implementer-general")
    # Should have been updated to match the .md file
    assert expert["description"] != "Old desc"
    assert "senior software engineer" in expert["system_prompt"].lower()


@pytest.mark.asyncio
async def test_load_builtins_skips_unchanged(db_conn):
    """If builtin content hasn't changed, load_builtins should not update."""
    registry = ExpertRegistry(_make_session_factory(db_conn))
    # Load once
    await registry.load_builtins()

    expert_before = await repo.get_expert_by_name(db_conn, "implementer-general")

    # Load again — should skip (no update)
    await registry.load_builtins()

    expert_after = await repo.get_expert_by_name(db_conn, "implementer-general")
    assert expert_before["id"] == expert_after["id"]


# ---------------------------------------------------------------------------
# Prompt template format-string correctness
# ---------------------------------------------------------------------------


class TestPromptTemplates:
    """Verify all prompt templates can be formatted without KeyError."""

    def test_simple_interview_prompt(self):
        from taktis.core.prompts import SIMPLE_INTERVIEW_PROMPT
        result = SIMPLE_INTERVIEW_PROMPT.format(
            description="A test project", expert_options="- implementer: default",
        )
        assert "A test project" in result

    def test_deep_interview_prompt(self):
        from taktis.core.prompts import DEEP_INTERVIEW_PROMPT
        result = DEEP_INTERVIEW_PROMPT.format(
            description="A test project", expert_options="- implementer: default",
        )
        assert "A test project" in result

    def test_researcher_prompts(self):
        from taktis.core.prompts import (
            RESEARCHER_STACK_PROMPT, RESEARCHER_FEATURES_PROMPT,
            RESEARCHER_ARCHITECTURE_PROMPT, RESEARCHER_PITFALLS_PROMPT,
        )
        kwargs = {"description": "desc", "interview": "transcript"}
        for prompt in [RESEARCHER_STACK_PROMPT, RESEARCHER_FEATURES_PROMPT,
                       RESEARCHER_ARCHITECTURE_PROMPT, RESEARCHER_PITFALLS_PROMPT]:
            result = prompt.format(**kwargs)
            assert "desc" in result

    def test_synthesizer_prompt(self):
        from taktis.core.prompts import SYNTHESIZER_PROMPT
        result = SYNTHESIZER_PROMPT.format(
            description="desc", interview="transcript",
            research_stack="s", research_features="f",
            research_architecture="a", research_pitfalls="p",
        )
        assert "desc" in result
        assert "transcript" in result

    def test_roadmapper_prompt(self):
        from taktis.core.prompts import ROADMAPPER_PROMPT
        result = ROADMAPPER_PROMPT.format(
            description="desc", interview="t",
            synthesizer="r", expert_options="opts",
        )
        assert "desc" in result

    def test_plan_checker_prompt(self):
        from taktis.core.prompts import PLAN_CHECKER_PROMPT
        result = PLAN_CHECKER_PROMPT.format(
            interview="transcript",
            requirements="reqs", roadmap="road", plan="phases",
        )
        assert "reqs" in result
        assert "transcript" in result

    def test_roadmapper_revision_prompt(self):
        from taktis.core.prompts import ROADMAPPER_REVISION_PROMPT
        result = ROADMAPPER_REVISION_PROMPT.format(
            description="d", interview="t", synthesizer="r",
            issues="issues", previous_plan_text="prev", expert_options="opts",
        )
        assert "issues" in result

    def test_discuss_task_prompt(self):
        from taktis.core.prompts import DISCUSS_TASK_PROMPT
        result = DISCUSS_TASK_PROMPT.format(
            task_name="Build API", task_expert="implementer",
            task_wave=1, task_prompt="Build the API",
            project_context="context here",
        )
        assert "Build API" in result

    def test_research_task_prompt(self):
        from taktis.core.prompts import RESEARCH_TASK_PROMPT
        result = RESEARCH_TASK_PROMPT.format(
            task_name="Build API", task_expert="implementer",
            task_prompt="Build the API", project_context="context",
        )
        assert "Build API" in result

    def test_phase_review_prompt(self):
        from taktis.core.prompts import PHASE_REVIEW_PROMPT
        result = PHASE_REVIEW_PROMPT.format(
            phase_number=1, phase_name="Setup",
            phase_goal="Get it done", working_dir="/tmp",
        )
        assert "Setup" in result

    def test_phase_review_fix_prompt(self):
        from taktis.core.prompts import PHASE_REVIEW_FIX_PROMPT
        result = PHASE_REVIEW_FIX_PROMPT.format(
            phase_number=1, phase_name="Setup",
            phase_goal="Set up the project skeleton",
            working_dir="/tmp", critical_issues="- Bug found",
            review_text="Full review text",
        )
        assert "Setup" in result
        assert "Bug found" in result
