"""Tests for taktis.core.phase_review — the phase review system.

Covers:
- extract_critical_items (pure function, thorough edge cases)
- _get_result_text (async, mock scheduler + repo)
- spawn_phase_review (async, heavily mocked)
- _fix_and_re_review (async, fix/re-review cycle)
- _re_review_phase (async, re-review after fix)
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from taktis.core.phase_review import (
    extract_critical_items,
    _get_result_text,
    spawn_phase_review,
    _fix_and_re_review,
    _re_review_phase,
    _MAX_REVIEW_ATTEMPTS,
)
# Expert IDs used in test assertions — these are the stable IDs from the .md frontmatter
_TEST_REVIEWER_ID = "4e4e016e2d5a59019e18035167c0a07d"
_TEST_IMPLEMENTER_ID = "dc868af81906568a97458c1f8ee709a4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduler(
    *,
    experts: list[dict] | None = None,
    task_outputs: list[dict] | None = None,
    wait_statuses: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock WaveScheduler with the session factory pattern."""
    mock_conn = AsyncMock()

    @asynccontextmanager
    async def _mock_session():
        yield mock_conn

    scheduler = MagicMock()
    scheduler._session_factory = _mock_session
    scheduler.execute_task = AsyncMock()
    scheduler._wait_for_tasks = AsyncMock(
        return_value=wait_statuses or {},
    )

    # These will be controlled via the repo patch, but we keep the conn
    # accessible for assertions.
    scheduler._mock_conn = mock_conn
    return scheduler


def _make_phase(
    *,
    phase_id: str = "phase-001",
    phase_number: int = 1,
    name: str = "Setup",
    goal: str = "Set up project scaffolding",
) -> dict:
    return {
        "id": phase_id,
        "phase_number": phase_number,
        "name": name,
        "goal": goal,
    }


def _make_project(
    *,
    project_id: str = "proj-001",
    name: str = "TestProject",
    working_dir: str = "/tmp/test-project",
) -> dict:
    return {
        "id": project_id,
        "name": name,
        "working_dir": working_dir,
    }


# ===========================================================================
# extract_critical_items — pure function tests
# ===========================================================================


class TestExtractCriticalItems:
    """Thorough tests for the CRITICAL section parser."""

    def test_single_critical_item(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "- Missing error handling in main loop\n"
            "## Warnings\n"
            "- Some minor issue\n"
        )
        items = extract_critical_items(text)
        assert items == ["- Missing error handling in main loop"]

    def test_multiple_critical_items(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "- Missing error handling in main loop\n"
            "- No input validation on user data\n"
            "- SQL injection vulnerability in query builder\n"
            "## Warnings\n"
        )
        items = extract_critical_items(text)
        assert len(items) == 3
        assert "- Missing error handling in main loop" in items
        assert "- No input validation on user data" in items
        assert "- SQL injection vulnerability in query builder" in items

    def test_none_found_in_critical_section(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "None found\n"
            "## Warnings\n"
        )
        items = extract_critical_items(text)
        assert items == []

    def test_na_in_critical_section(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "N/A\n"
            "## Warnings\n"
        )
        items = extract_critical_items(text)
        assert items == []

    def test_none_lowercase_in_critical_section(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "none\n"
            "## Warnings\n"
        )
        items = extract_critical_items(text)
        assert items == []

    def test_no_critical_section(self):
        text = (
            "## Summary\n"
            "Everything looks fine.\n"
            "## Warnings\n"
            "- Minor style issue\n"
        )
        items = extract_critical_items(text)
        assert items == []

    def test_empty_string(self):
        items = extract_critical_items("")
        assert items == []

    def test_case_insensitive_critical_and_must_fix(self):
        text = (
            "## critical / must fix\n"
            "- Issue one\n"
            "---\n"
        )
        items = extract_critical_items(text)
        assert items == ["- Issue one"]

    def test_mixed_case_critical(self):
        text = (
            "## Critical / Must Fix\n"
            "- Found a problem\n"
            "## Next section\n"
        )
        items = extract_critical_items(text)
        assert items == ["- Found a problem"]

    def test_critical_section_ends_at_heading(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "- Bug A\n"
            "- Bug B\n"
            "## Recommendations\n"
            "- Suggestion 1\n"
        )
        items = extract_critical_items(text)
        assert len(items) == 2
        assert "- Bug A" in items
        assert "- Bug B" in items

    def test_critical_section_ends_at_horizontal_rule(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "- Bug A\n"
            "---\n"
            "Some other content\n"
        )
        items = extract_critical_items(text)
        assert items == ["- Bug A"]

    def test_critical_section_ends_at_end_of_text(self):
        """Items collected through to end of text when no terminator."""
        text = (
            "## CRITICAL / Must Fix\n"
            "- Bug A\n"
            "- Bug B\n"
        )
        items = extract_critical_items(text)
        assert len(items) == 2

    def test_empty_lines_in_critical_section_are_skipped(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "- Bug A\n"
            "\n"
            "- Bug B\n"
            "## End\n"
        )
        items = extract_critical_items(text)
        assert len(items) == 2

    def test_none_found_stops_section(self):
        """'None found' in the CRITICAL section should stop parsing (no items after)."""
        text = (
            "## CRITICAL / Must Fix\n"
            "None found\n"
            "- This should not be captured\n"
            "## End\n"
        )
        items = extract_critical_items(text)
        assert items == []

    def test_na_case_insensitive(self):
        text = (
            "## CRITICAL / Must Fix\n"
            "n/a\n"
            "## Warnings\n"
        )
        items = extract_critical_items(text)
        assert items == []

    def test_items_without_bullet_prefix(self):
        """Non-empty, non-heading lines in the critical section are captured."""
        text = (
            "## CRITICAL / Must Fix\n"
            "Missing error handling in the parser module\n"
            "## Warnings\n"
        )
        items = extract_critical_items(text)
        assert items == ["Missing error handling in the parser module"]

    def test_heading_inside_critical_section_terminates(self):
        """A sub-heading (#) inside the critical section terminates it."""
        text = (
            "## CRITICAL / Must Fix\n"
            "- Issue 1\n"
            "### Details\n"
            "This detail should not be captured\n"
        )
        items = extract_critical_items(text)
        assert items == ["- Issue 1"]

    def test_critical_must_fix_as_inline_text(self):
        """The trigger line must contain both 'critical' and 'must fix'."""
        text = (
            "**CRITICAL / MUST FIX**\n"
            "- Serious bug\n"
            "## Next\n"
        )
        items = extract_critical_items(text)
        assert items == ["- Serious bug"]

    def test_critical_without_must_fix_does_not_trigger(self):
        """A line with 'critical' but without 'must fix' does not start the section."""
        text = (
            "## CRITICAL Issues\n"
            "- Should not be captured\n"
            "## End\n"
        )
        items = extract_critical_items(text)
        assert items == []


# ===========================================================================
# _get_result_text — async tests
# ===========================================================================


class TestGetResultText:

    @pytest.mark.asyncio
    async def test_returns_result_from_dict_content(self):
        """Dict content with type=result returns the result field."""
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": {"type": "result", "result": "All looks good."}}
            ])
            result = await _get_result_text(scheduler, "task-123")
        assert result == "All looks good."

    @pytest.mark.asyncio
    async def test_returns_result_from_json_string_content(self):
        """String content that is valid JSON with type=result."""
        scheduler = _make_scheduler()
        content_str = json.dumps({"type": "result", "result": "Review passed."})
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": content_str}
            ])
            result = await _get_result_text(scheduler, "task-123")
        assert result == "Review passed."

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_outputs(self):
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[])
            result = await _get_result_text(scheduler, "task-123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_string_for_non_result_event(self):
        """Content dicts without type=result are skipped."""
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": {"type": "text", "text": "some text"}}
            ])
            result = await _get_result_text(scheduler, "task-123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_handles_invalid_json_string_gracefully(self):
        """Invalid JSON string content is skipped (continue)."""
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": "not valid json {{{"}
            ])
            result = await _get_result_text(scheduler, "task-123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_uses_last_result_event(self):
        """When multiple result events exist, the last one wins."""
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": {"type": "result", "result": "First result."}},
                {"content": {"type": "result", "result": "Final result."}},
            ])
            result = await _get_result_text(scheduler, "task-123")
        assert result == "Final result."

    @pytest.mark.asyncio
    async def test_skips_none_content(self):
        """Rows with content=None are skipped."""
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": None}
            ])
            result = await _get_result_text(scheduler, "task-123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_passes_event_types_filter(self):
        """Ensure the call filters for 'result' event types."""
        scheduler = _make_scheduler()
        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.get_task_outputs = AsyncMock(return_value=[])
            await _get_result_text(scheduler, "task-123")
            mock_repo.get_task_outputs.assert_called_once()
            _, kwargs = mock_repo.get_task_outputs.call_args
            assert kwargs.get("event_types") == ["result"]


# ===========================================================================
# spawn_phase_review — async integration tests (heavily mocked)
# ===========================================================================


class TestSpawnPhaseReview:

    @pytest.mark.asyncio
    async def test_creates_review_task_with_correct_attributes(self):
        """Review task should have wave=999, task_type=phase_review, model=opus."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[
                {"id": "exp-reviewer", "name": "reviewer"},
            ])
            mock_repo.get_expert_by_role = AsyncMock(return_value={"id": _TEST_REVIEWER_ID, "name": "reviewer"})
            mock_repo.create_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(return_value=[])

            await spawn_phase_review(scheduler, phase, project)

            mock_repo.create_task.assert_called_once()
            _, kwargs = mock_repo.create_task.call_args
            assert kwargs["wave"] == 999
            assert kwargs["task_type"] == "phase_review"
            assert kwargs["model"] == "opus"
            assert kwargs["expert_id"] == _TEST_REVIEWER_ID
            assert kwargs["id"] == "abcd1234"
            assert kwargs["phase_id"] == "phase-001"
            assert kwargs["project_id"] == "proj-001"
            assert "Review Phase 1" in kwargs["name"]

    @pytest.mark.asyncio
    async def test_uses_reviewer_expert_when_available(self):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[
                {"id": "exp-other", "name": "implementer"},
                {"id": "exp-reviewer", "name": "reviewer"},
            ])
            mock_repo.get_expert_by_role = AsyncMock(return_value={"id": _TEST_REVIEWER_ID, "name": "reviewer"})
            mock_repo.create_task = AsyncMock()

            await spawn_phase_review(scheduler, phase, project)

            _, kwargs = mock_repo.create_task.call_args
            assert kwargs["expert_id"] == _TEST_REVIEWER_ID

    @pytest.mark.asyncio
    async def test_logs_warning_when_reviewer_not_found(self, caplog):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])  # no experts
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            import logging
            with caplog.at_level(logging.WARNING, logger="taktis.core.phase_review"):
                await spawn_phase_review(scheduler, phase, project)

            assert any("reviewer" in r.message and "not found" in r.message for r in caplog.records)
            _, kwargs = mock_repo.create_task.call_args
            assert kwargs["expert_id"] is None

    @pytest.mark.asyncio
    async def test_returns_early_when_review_task_fails(self, caplog):
        """If the review task itself crashes, skip CRITICAL extraction."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "failed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            import logging
            with caplog.at_level(logging.WARNING, logger="taktis.core.phase_review"):
                await spawn_phase_review(scheduler, phase, project)

            # Should warn and return without writing REVIEW.md or checking CRITICALs
            assert any("failed" in r.message and "skipping review" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_calls_fix_and_re_review_when_criticals_found(self):
        """When CRITICALs are found, _fix_and_re_review is called."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        review_text = (
            "## CRITICAL / Must Fix\n"
            "- Missing error handling\n"
            "## Warnings\n"
        )

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=review_text),
            patch("taktis.core.phase_review._fix_and_re_review", new_callable=AsyncMock) as mock_fix,
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await spawn_phase_review(scheduler, phase, project)

            mock_fix.assert_called_once()
            args, kwargs = mock_fix.call_args
            assert args[0] is scheduler
            assert args[1] is phase
            assert args[2] is project
            assert args[3] == review_text
            assert args[4] == ["- Missing error handling"]
            assert kwargs.get("attempt") == 1

    @pytest.mark.asyncio
    async def test_writes_review_md_when_review_passes(self):
        """When no CRITICALs, REVIEW.md should be written."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        review_text = (
            "## CRITICAL / Must Fix\n"
            "None found\n"
            "## Warnings\n"
            "- Some minor thing\n"
        )

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=review_text),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock) as mock_write,
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await spawn_phase_review(scheduler, phase, project)

            mock_write.assert_called_once_with(
                "/tmp/test-project", 1, review_text,
            )

    @pytest.mark.asyncio
    async def test_no_review_md_written_when_review_text_empty(self):
        """When review_text is empty, REVIEW.md should not be written."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock) as mock_write,
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await spawn_phase_review(scheduler, phase, project)

            mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_info_when_no_criticals(self, caplog):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value="All good."),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            import logging
            with caplog.at_level(logging.INFO, logger="taktis.core.phase_review"):
                await spawn_phase_review(scheduler, phase, project)

            assert any("passed" in r.message and "no CRITICALs" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_executes_and_waits_for_task(self):
        """scheduler.execute_task and _wait_for_tasks are called correctly."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await spawn_phase_review(scheduler, phase, project)

            scheduler.execute_task.assert_called_once_with("abcd1234", project)
            scheduler._wait_for_tasks.assert_called_once_with(["abcd1234"])


# ===========================================================================
# _fix_and_re_review — async tests
# ===========================================================================


class TestFixAndReReview:

    @pytest.mark.asyncio
    async def test_creates_fix_task_with_implementer_expert(self):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"fix01234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._re_review_phase", new_callable=AsyncMock) as mock_rr,
        ):
            mock_uuid.return_value = MagicMock(hex="fix01234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[
                {"id": "exp-impl", "name": "implementer"},
            ])
            mock_repo.get_expert_by_role = AsyncMock(return_value={"id": _TEST_IMPLEMENTER_ID, "name": "implementer"})
            mock_repo.create_task = AsyncMock()

            await _fix_and_re_review(
                scheduler, phase, project,
                review_text="Some review",
                critical_items=["- Bug A"],
                attempt=1,
            )

            mock_repo.create_task.assert_called_once()
            _, kwargs = mock_repo.create_task.call_args
            assert kwargs["task_type"] == "phase_review_fix"
            assert kwargs["wave"] == 999
            assert kwargs["expert_id"] == _TEST_IMPLEMENTER_ID
            assert "Fix CRITICALs (attempt 1)" in kwargs["name"]
            assert kwargs["phase_id"] == "phase-001"
            assert kwargs["project_id"] == "proj-001"

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded_marks_phase_failed(self):
        """When attempt > _MAX_REVIEW_ATTEMPTS, phase is marked failed."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler()

        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.update_phase = AsyncMock()

            await _fix_and_re_review(
                scheduler, phase, project,
                review_text="Review text",
                critical_items=["- Bug"],
                attempt=_MAX_REVIEW_ATTEMPTS + 1,
            )

            mock_repo.update_phase.assert_called_once()
            _, kwargs = mock_repo.update_phase.call_args
            assert kwargs.get("status", args[2] if len(args := mock_repo.update_phase.call_args[0]) > 2 else None) is not None

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded_marks_phase_failed_correctly(self, caplog):
        """When attempt > _MAX_REVIEW_ATTEMPTS, phase status is set to 'failed'."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler()

        with patch("taktis.core.phase_review.repo") as mock_repo:
            mock_repo.update_phase = AsyncMock()

            import logging
            with caplog.at_level(logging.WARNING, logger="taktis.core.phase_review"):
                await _fix_and_re_review(
                    scheduler, phase, project,
                    review_text="Review text",
                    critical_items=["- Bug"],
                    attempt=_MAX_REVIEW_ATTEMPTS + 1,
                )

            # Verify phase marked failed
            mock_repo.update_phase.assert_called_once()
            call_args = mock_repo.update_phase.call_args
            assert call_args[1].get("status") == "failed" or (
                len(call_args[0]) > 2 and call_args[0][2] == "failed"
            )
            # Verify warning logged
            assert any("CRITICALs" in r.message and "failed" in r.message for r in caplog.records)
            # Verify no tasks were created
            scheduler.execute_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_to_re_review_even_if_fix_task_fails(self):
        """Even if the fix task fails, re-review is still attempted."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"fix01234": "failed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._re_review_phase", new_callable=AsyncMock) as mock_rr,
        ):
            mock_uuid.return_value = MagicMock(hex="fix01234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await _fix_and_re_review(
                scheduler, phase, project,
                review_text="Review text",
                critical_items=["- Bug A"],
                attempt=1,
            )

            # _re_review_phase should still be called
            mock_rr.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_correct_attempt_to_re_review(self):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"fix01234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._re_review_phase", new_callable=AsyncMock) as mock_rr,
        ):
            mock_uuid.return_value = MagicMock(hex="fix01234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await _fix_and_re_review(
                scheduler, phase, project,
                review_text="Review text",
                critical_items=["- Bug A", "- Bug B"],
                attempt=2,
            )

            mock_rr.assert_called_once_with(
                scheduler, phase, project, 2,
                prior_critical_items=["- Bug A", "- Bug B"],
            )

    @pytest.mark.asyncio
    async def test_logs_warning_when_implementer_not_found(self, caplog):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"fix01234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._re_review_phase", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="fix01234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])  # no experts
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            import logging
            with caplog.at_level(logging.WARNING, logger="taktis.core.phase_review"):
                await _fix_and_re_review(
                    scheduler, phase, project,
                    review_text="Review text",
                    critical_items=["- Bug"],
                    attempt=1,
                )

            assert any("implementer" in r.message and "not found" in r.message for r in caplog.records)
            _, kwargs = mock_repo.create_task.call_args
            assert kwargs["expert_id"] is None

    @pytest.mark.asyncio
    async def test_fix_prompt_contains_critical_issues(self):
        """The fix task prompt should contain the critical items."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"fix01234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._re_review_phase", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="fix01234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await _fix_and_re_review(
                scheduler, phase, project,
                review_text="Full review text here",
                critical_items=["- Missing error handling", "- SQL injection"],
                attempt=1,
            )

            _, kwargs = mock_repo.create_task.call_args
            prompt = kwargs["prompt"]
            assert "Missing error handling" in prompt
            assert "SQL injection" in prompt


# ===========================================================================
# _re_review_phase — async tests
# ===========================================================================


class TestReReviewPhase:

    @pytest.mark.asyncio
    async def test_creates_re_review_task_with_prior_context(self):
        """Re-review prompt should reference prior CRITICALs."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value="All clear now."),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[
                {"id": "exp-reviewer", "name": "reviewer"},
            ])
            mock_repo.get_expert_by_role = AsyncMock(return_value={"id": _TEST_REVIEWER_ID, "name": "reviewer"})
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()

            await _re_review_phase(
                scheduler, phase, project, attempt=1,
                prior_critical_items=["- Missing error handling"],
            )

            mock_repo.create_task.assert_called_once()
            _, kwargs = mock_repo.create_task.call_args
            assert kwargs["task_type"] == "phase_review"
            assert kwargs["model"] == "opus"
            assert kwargs["wave"] == 999
            assert kwargs["expert_id"] == _TEST_REVIEWER_ID
            assert "Re-review Phase 1 (attempt 2)" in kwargs["name"]

            # The prompt should contain prior critical info
            prompt = kwargs["prompt"]
            assert "Missing error handling" in prompt
            assert "re-review attempt 2" in prompt.lower()

    @pytest.mark.asyncio
    async def test_failed_re_review_marks_phase_failed(self):
        """If the re-review task itself fails, phase is marked failed."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "failed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()

            await _re_review_phase(
                scheduler, phase, project, attempt=1,
                prior_critical_items=["- Bug"],
            )

            mock_repo.update_phase.assert_called_once()
            call_args = mock_repo.update_phase.call_args
            # Should mark phase as failed
            assert "failed" in str(call_args)

    @pytest.mark.asyncio
    async def test_criticals_still_present_recurses_to_fix(self):
        """If CRITICALs still exist after re-review, _fix_and_re_review is called."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "completed"})

        re_review_text = (
            "## CRITICAL / Must Fix\n"
            "- Still broken\n"
            "## End\n"
        )

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=re_review_text),
            patch("taktis.core.phase_review._fix_and_re_review", new_callable=AsyncMock) as mock_fix,
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()

            await _re_review_phase(
                scheduler, phase, project, attempt=1,
                prior_critical_items=["- Original bug"],
            )

            mock_fix.assert_called_once()
            args = mock_fix.call_args[0]
            assert args[0] is scheduler
            assert args[1] is phase
            assert args[2] is project
            assert args[3] == re_review_text
            assert args[4] == ["- Still broken"]
            # attempt should be incremented
            assert mock_fix.call_args[1].get("attempt") == 2

    @pytest.mark.asyncio
    async def test_no_criticals_marks_phase_complete(self, caplog):
        """When re-review finds no CRITICALs, phase is marked complete."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "completed"})

        clean_review = (
            "## CRITICAL / Must Fix\n"
            "None found\n"
            "## Warnings\n"
            "- Minor style issue\n"
        )

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=clean_review),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock) as mock_write,
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()

            import logging
            with caplog.at_level(logging.INFO, logger="taktis.core.phase_review"):
                await _re_review_phase(
                    scheduler, phase, project, attempt=1,
                    prior_critical_items=["- Was broken"],
                )

            # Phase should be marked complete
            mock_repo.update_phase.assert_called_once()
            call_args = mock_repo.update_phase.call_args
            assert "complete" in str(call_args)

            # REVIEW.md should be written
            mock_write.assert_called_once_with(
                "/tmp/test-project", 1, clean_review,
            )

            # Info log about passing
            assert any("passed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_review_md_written_when_re_review_text_empty(self):
        """When re-review returns empty text, REVIEW.md should not be written."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock) as mock_write,
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()

            await _re_review_phase(
                scheduler, phase, project, attempt=1,
                prior_critical_items=["- Bug"],
            )

            mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_re_review_without_prior_criticals_uses_base_prompt(self):
        """When prior_critical_items is None, uses the base prompt directly."""
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()

            await _re_review_phase(
                scheduler, phase, project, attempt=1,
                prior_critical_items=None,
            )

            _, kwargs = mock_repo.create_task.call_args
            prompt = kwargs["prompt"]
            # Should NOT contain re-review attempt language
            assert "re-review attempt" not in prompt.lower()

    @pytest.mark.asyncio
    async def test_re_review_logs_warning_when_reviewer_not_found(self, caplog):
        phase = _make_phase()
        project = _make_project()
        scheduler = _make_scheduler(wait_statuses={"rr012345": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.phase_review._get_result_text", new_callable=AsyncMock, return_value=""),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_uuid.return_value = MagicMock(hex="rr012345xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[])  # no experts
            mock_repo.get_expert_by_role = AsyncMock(return_value=None)
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()

            import logging
            with caplog.at_level(logging.WARNING, logger="taktis.core.phase_review"):
                await _re_review_phase(
                    scheduler, phase, project, attempt=1,
                    prior_critical_items=["- Bug"],
                )

            assert any("reviewer" in r.message and "not found" in r.message for r in caplog.records)


# ===========================================================================
# Integration-style: full cycle tests
# ===========================================================================


class TestFullReviewCycle:
    """Test multi-step flows without patching internal functions."""

    @pytest.mark.asyncio
    async def test_spawn_review_no_criticals_full_flow(self):
        """Full flow: spawn review -> no CRITICALs -> write REVIEW.md -> done."""
        phase = _make_phase()
        project = _make_project()

        clean_review = (
            "## CRITICAL / Must Fix\n"
            "None found\n"
            "## Warnings\n"
            "- Minor issue\n"
        )
        clean_result = json.dumps({"type": "result", "result": clean_review})

        scheduler = _make_scheduler(wait_statuses={"abcd1234": "completed"})

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4") as mock_uuid,
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock) as mock_write,
        ):
            mock_uuid.return_value = MagicMock(hex="abcd1234xxxxxxxxxxxxxxxxxxxxxxxx")
            mock_repo.list_experts = AsyncMock(return_value=[
                {"id": "exp-reviewer", "name": "reviewer"},
            ])
            mock_repo.get_expert_by_role = AsyncMock(return_value={"id": _TEST_REVIEWER_ID, "name": "reviewer"})
            mock_repo.create_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(return_value=[
                {"content": clean_result},
            ])

            await spawn_phase_review(scheduler, phase, project)

            # REVIEW.md written
            mock_write.assert_called_once_with(
                "/tmp/test-project", 1, clean_review,
            )
            # No fix tasks created (only the review task)
            assert mock_repo.create_task.call_count == 1

    @pytest.mark.asyncio
    async def test_fix_and_re_review_resolves_on_second_attempt(self):
        """Fix cycle: fix -> re-review with CRITICALs -> fix again -> re-review clean."""
        phase = _make_phase()
        project = _make_project()

        # Track uuid calls to give different IDs
        uuid_counter = iter(["fix00001", "review02", "fix00003", "review04"])

        def _next_uuid():
            hex_val = next(uuid_counter)
            mock = MagicMock()
            mock.hex = hex_val + "x" * (32 - len(hex_val))
            return mock

        still_broken_review = (
            "## CRITICAL / Must Fix\n"
            "- Still broken\n"
            "## End\n"
        )
        clean_review = (
            "## CRITICAL / Must Fix\n"
            "None found\n"
            "## End\n"
        )

        # Build a scheduler that returns 'completed' for all tasks
        scheduler = _make_scheduler()
        scheduler._wait_for_tasks = AsyncMock(
            side_effect=[
                # attempt 1 fix
                {"fix00001": "completed"},
                # attempt 1 re-review (still broken)
                {"review02": "completed"},
                # attempt 2 fix
                {"fix00003": "completed"},
                # attempt 2 re-review (clean)
                {"review04": "completed"},
            ]
        )

        # _get_result_text calls need different results for each re-review
        result_call_count = 0

        async def mock_get_result_text(sched, task_id):
            nonlocal result_call_count
            result_call_count += 1
            if result_call_count == 1:
                return still_broken_review
            return clean_review

        with (
            patch("taktis.core.phase_review.repo") as mock_repo,
            patch("taktis.core.phase_review.uuid4", side_effect=_next_uuid),
            patch("taktis.core.context.async_write_phase_review", new_callable=AsyncMock),
        ):
            mock_repo.list_experts = AsyncMock(return_value=[
                {"id": "exp-impl", "name": "implementer"},
                {"id": "exp-reviewer", "name": "reviewer"},
            ])

            async def _get_expert_by_role(conn, role):
                roles = {
                    "phase_reviewer": {"id": _TEST_REVIEWER_ID, "name": "reviewer"},
                    "phase_fixer": {"id": _TEST_IMPLEMENTER_ID, "name": "implementer"},
                }
                return roles.get(role)

            mock_repo.get_expert_by_role = AsyncMock(side_effect=_get_expert_by_role)
            mock_repo.create_task = AsyncMock()
            mock_repo.update_phase = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(return_value=[])

            # Patch _get_result_text at module level
            with patch(
                "taktis.core.phase_review._get_result_text",
                side_effect=mock_get_result_text,
            ):
                await _fix_and_re_review(
                    scheduler, phase, project,
                    review_text="Initial review",
                    critical_items=["- Initial bug"],
                    attempt=1,
                )

            # 2 fix tasks + 2 review tasks = 4 create_task calls
            assert mock_repo.create_task.call_count == 4

            # Phase should end up complete
            mock_repo.update_phase.assert_called_once()
            assert "complete" in str(mock_repo.update_phase.call_args)


class TestMaxReviewAttemptsConstant:
    """Verify the _MAX_REVIEW_ATTEMPTS constant is what we expect."""

    def test_max_review_attempts_is_three(self):
        assert _MAX_REVIEW_ATTEMPTS == 3
