"""Tests for recover_unprocessed_reviews and report_interrupted_work.

These two functions in ``taktis.core.crash_recovery`` had zero test
coverage.  ``recover_stale_tasks`` is already covered by test_recovery.py.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.crash_recovery import (
    recover_unprocessed_reviews,
    report_interrupted_work,
)
from taktis.core.events import EVENT_SYSTEM_INTERRUPTED_WORK, EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_review_row(
    task_id: str = "rev01",
    phase_id: str = "ph01",
    project_id: str = "proj01",
) -> dict:
    return {"task_id": task_id, "phase_id": phase_id, "project_id": project_id}


def _make_task(
    task_id: str,
    task_type: str = "implement",
    status: str = "completed",
) -> dict:
    return {"id": task_id, "task_type": task_type, "status": status}


def _make_result_output(review_text: str) -> dict:
    """Create a task_output dict whose content is a JSON-encoded result."""
    return {
        "content": json.dumps({"type": "result", "result": review_text}),
    }


def _review_text_with_criticals(items: list[str] | None = None) -> str:
    if items is None:
        items = ["Missing auth check on /api/data", "SQL injection in query builder"]
    lines = ["## CRITICAL — Must Fix\n"]
    for item in items:
        lines.append(f"- {item}")
    lines.append("\n## Warnings\n- Minor style issue")
    return "\n".join(lines)


def _review_text_no_criticals() -> str:
    return (
        "## CRITICAL — Must Fix\nNone found.\n\n"
        "## Warnings\n- Consider adding more tests"
    )


# ---------------------------------------------------------------------------
# Session factory helper — yields the mock connection
# ---------------------------------------------------------------------------


def _session_factory(mock_conn):
    @asynccontextmanager
    async def factory():
        yield mock_conn

    return factory


# ======================================================================
# recover_unprocessed_reviews
# ======================================================================


class TestRecoverUnprocessedReviews:
    """Tests for the recover_unprocessed_reviews function."""

    @pytest.mark.asyncio
    async def test_no_reviews_found(self):
        """When there are no completed reviews on complete phases, does nothing."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[]
            )
            await recover_unprocessed_reviews(
                session_factory, project_service, scheduler
            )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_phase_with_no_review_text(self):
        """If a review task has no result output, the phase is skipped."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_task = _make_task("rev01", task_type="phase_review", status="completed")

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=[review_task])
            mock_repo.update_task = AsyncMock()
            # No result outputs
            mock_repo.get_task_outputs = AsyncMock(return_value=[])

            await recover_unprocessed_reviews(
                session_factory, project_service, scheduler
            )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_phase_with_no_criticals(self):
        """If the review text has no CRITICALs, the phase is skipped."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_task = _make_task("rev01", task_type="phase_review", status="completed")
        review_text = _review_text_no_criticals()

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=[review_task])
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=[],
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_triggers_fix_loop_for_phase_with_criticals(self):
        """Phases with unresolved CRITICALs trigger _fix_and_re_review."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(
            return_value={"id": "proj01", "name": "Test Project", "phases": []}
        )
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_task = _make_task("rev01", task_type="phase_review", status="completed")
        review_text = _review_text_with_criticals()
        critical_items = [
            "Missing auth check on /api/data",
            "SQL injection in query builder",
        ]

        phase_dict = {"id": "ph01", "name": "Phase 1", "status": "complete"}
        project_dict = {"id": "proj01", "name": "Test Project"}

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=[review_task])
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase_dict)
            mock_repo.get_project_by_id = AsyncMock(return_value=project_dict)

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        scheduler._fix_and_re_review.assert_called_once()
        call_args = scheduler._fix_and_re_review.call_args
        assert call_args[0][0] == phase_dict  # phase
        assert call_args[0][2] == review_text  # review_text
        assert call_args[0][3] == critical_items  # critical_items
        assert call_args[1]["attempt"] == 1  # no prior fix tasks → attempt=1

    @pytest.mark.asyncio
    async def test_attempt_counts_completed_fix_tasks(self):
        """Attempt number accounts for completed phase_review_fix tasks."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(
            return_value={"id": "proj01", "name": "Test"}
        )
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_text = _review_text_with_criticals()
        critical_items = ["Missing auth check"]

        # One completed review + two completed fix tasks
        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
            _make_task("fix01", task_type="phase_review_fix", status="completed"),
            _make_task("fix02", task_type="phase_review_fix", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        # 2 completed fixes → attempt = 3
        assert scheduler._fix_and_re_review.call_args[1]["attempt"] == 3

    @pytest.mark.asyncio
    async def test_cleans_up_pending_review_and_fix_tasks(self):
        """Pending review/fix tasks are marked failed before re-triggering."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(return_value={"id": "proj01"})
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_text = _review_text_with_criticals()
        critical_items = ["Critical issue"]

        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
            _make_task("pend_rev", task_type="phase_review", status="pending"),
            _make_task("pend_fix", task_type="phase_review_fix", status="pending"),
            _make_task("impl01", task_type="implement", status="pending"),  # not a review task
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        # Pending review/fix tasks (pend_rev, pend_fix) marked failed
        update_calls = mock_repo.update_task.call_args_list
        failed_ids = [
            c[0][1] for c in update_calls if c[1].get("status") == "failed"
            or (len(c[0]) > 2 and c[0][2] == "failed")
        ]
        # update_task(conn, task_id, status="failed") — positional conn + task_id, kw status
        failed_ids = []
        for call in update_calls:
            args, kwargs = call
            if kwargs.get("status") == "failed":
                # args[1] is the task_id (args[0] is conn)
                failed_ids.append(args[1])
        assert "pend_rev" in failed_ids
        assert "pend_fix" in failed_ids
        # impl01 should NOT be marked failed (it's not a review task type)
        assert "impl01" not in failed_ids

    @pytest.mark.asyncio
    async def test_deduplicates_phases(self):
        """Multiple review rows for the same phase only process once."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(return_value={"id": "proj01"})
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_text = _review_text_with_criticals(["Critical bug"])
        critical_items = ["Critical bug"]

        # Two review rows for the same phase
        review_rows = [
            _make_review_row(task_id="rev01", phase_id="ph01"),
            _make_review_row(task_id="rev02", phase_id="ph01"),
        ]

        # The second (rev02) is the latest since reversed() processes last-first
        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
            _make_task("rev02", task_type="phase_review", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=review_rows
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        # Only called once despite two review rows for same phase
        assert scheduler._fix_and_re_review.call_count == 1

    @pytest.mark.asyncio
    async def test_multiple_phases_processed(self):
        """Reviews from different phases each get processed independently."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(
            return_value={"id": "proj01", "name": "Test"}
        )
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_text = _review_text_with_criticals(["Critical bug"])
        critical_items = ["Critical bug"]

        review_rows = [
            _make_review_row(task_id="rev01", phase_id="ph01"),
            _make_review_row(task_id="rev02", phase_id="ph02"),
        ]

        # Each phase has its own review task
        phase1_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]
        phase2_tasks = [
            _make_task("rev02", task_type="phase_review", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=review_rows
            )
            # Return different task lists per phase
            mock_repo.get_tasks_by_phase = AsyncMock(
                side_effect=[phase2_tasks, phase1_tasks]  # reversed order
            )
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                side_effect=lambda conn, pid: {
                    "ph01": {"id": "ph01", "name": "Phase 1"},
                    "ph02": {"id": "ph02", "name": "Phase 2"},
                }[pid]
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        assert scheduler._fix_and_re_review.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_phase_not_found(self):
        """If get_phase_by_id returns None, the phase is skipped."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_text = _review_text_with_criticals()
        critical_items = ["Critical issue"]

        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(return_value=None)
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_project_not_found(self):
        """If get_project_by_id returns None, the phase is skipped."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_text = _review_text_with_criticals()
        critical_items = ["Critical issue"]

        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(return_value=None)

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_fix_loop_logs_and_continues(self, caplog):
        """If _fix_and_re_review raises, it is logged and execution continues."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(return_value={"id": "proj01"})
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock(
            side_effect=RuntimeError("SDK crash")
        )

        review_text = _review_text_with_criticals()
        critical_items = ["Critical issue"]
        review_row = _make_review_row()

        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                with caplog.at_level(logging.ERROR, logger="taktis.core.crash_recovery"):
                    # Should NOT raise — exception is caught internally
                    await recover_unprocessed_reviews(
                        session_factory, project_service, scheduler
                    )

        assert "Fix loop failed" in caplog.text

    @pytest.mark.asyncio
    async def test_review_text_from_json_string_content(self):
        """Content stored as a JSON string is properly parsed."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(return_value={"id": "proj01"})
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_text = _review_text_with_criticals(["Auth bypass"])
        critical_items = ["Auth bypass"]
        review_row = _make_review_row()

        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]

        # Content is a JSON string (not a dict) — code does json.loads on it
        output = {"content": json.dumps({"type": "result", "result": review_text})}

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(return_value=[output])
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        scheduler._fix_and_re_review.assert_called_once()
        assert scheduler._fix_and_re_review.call_args[0][2] == review_text

    @pytest.mark.asyncio
    async def test_content_not_json_is_skipped(self):
        """Non-JSON content in output is skipped gracefully."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]

        # Content is a non-JSON string — json.loads will fail, continue
        output = {"content": "this is not json"}

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(return_value=[output])

            await recover_unprocessed_reviews(
                session_factory, project_service, scheduler
            )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_dict_without_result_type_skipped(self):
        """Output content dict with type != 'result' does not yield review_text."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]

        # type is "assistant" not "result"
        output = {
            "content": json.dumps({"type": "assistant", "text": "thinking..."}),
        }

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(return_value=[output])

            await recover_unprocessed_reviews(
                session_factory, project_service, scheduler
            )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_last_completed_review_task(self):
        """When multiple completed reviews exist, uses the last one's output."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(return_value={"id": "proj01"})
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        review_text = _review_text_with_criticals(["Latest critical"])
        critical_items = ["Latest critical"]

        # Two completed reviews — code picks the last one (rev02)
        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="completed"),
            _make_task("rev02", task_type="phase_review", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                return_value={"id": "ph01", "name": "Phase 1"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                await recover_unprocessed_reviews(
                    session_factory, project_service, scheduler
                )

        # Verify get_task_outputs was called with "rev02" (the last review)
        output_call = mock_repo.get_task_outputs.call_args
        assert output_call[0][1] == "rev02"

    @pytest.mark.asyncio
    async def test_no_completed_review_tasks_in_phase(self):
        """Phase with no completed review tasks is skipped entirely."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        scheduler = MagicMock()
        scheduler._fix_and_re_review = AsyncMock()

        review_row = _make_review_row()
        # Only a pending review, no completed one
        phase_tasks = [
            _make_task("rev01", task_type="phase_review", status="pending"),
            _make_task("impl01", task_type="implement", status="completed"),
        ]

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=[review_row]
            )
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=phase_tasks)
            mock_repo.update_task = AsyncMock()

            await recover_unprocessed_reviews(
                session_factory, project_service, scheduler
            )

        scheduler._fix_and_re_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_does_not_prevent_processing_next_phase(self, caplog):
        """Exception on one phase does not prevent the next phase from running."""
        mock_conn = MagicMock()
        session_factory = _session_factory(mock_conn)
        project_service = MagicMock()
        project_service._enrich_project = AsyncMock(return_value={"id": "proj01"})
        scheduler = MagicMock()

        review_text = _review_text_with_criticals(["Critical"])
        critical_items = ["Critical"]

        # Two different phases
        review_rows = [
            _make_review_row(task_id="rev01", phase_id="ph01", project_id="proj01"),
            _make_review_row(task_id="rev02", phase_id="ph02", project_id="proj01"),
        ]

        tasks_ph1 = [
            _make_task("rev01", task_type="phase_review", status="completed"),
        ]
        tasks_ph2 = [
            _make_task("rev02", task_type="phase_review", status="completed"),
        ]

        # First call raises, second succeeds
        call_count = [0]

        async def fix_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Boom on phase 2")

        scheduler._fix_and_re_review = AsyncMock(side_effect=fix_side_effect)

        with patch("taktis.core.crash_recovery.repo") as mock_repo:
            mock_repo.get_completed_reviews_on_complete_phases = AsyncMock(
                return_value=review_rows
            )
            mock_repo.get_tasks_by_phase = AsyncMock(
                side_effect=[tasks_ph2, tasks_ph1]  # reversed order
            )
            mock_repo.update_task = AsyncMock()
            mock_repo.get_task_outputs = AsyncMock(
                return_value=[_make_result_output(review_text)]
            )
            mock_repo.get_phase_by_id = AsyncMock(
                side_effect=lambda conn, pid: {"id": pid, "name": f"Phase {pid}"}
            )
            mock_repo.get_project_by_id = AsyncMock(
                return_value={"id": "proj01", "name": "Test"}
            )

            with patch(
                "taktis.core.scheduler.WaveScheduler._extract_critical_items",
                return_value=critical_items,
            ):
                with caplog.at_level(logging.ERROR, logger="taktis.core.crash_recovery"):
                    await recover_unprocessed_reviews(
                        session_factory, project_service, scheduler
                    )

        # Both phases were attempted
        assert scheduler._fix_and_re_review.call_count == 2
        assert "Fix loop failed" in caplog.text


# ======================================================================
# report_interrupted_work
# ======================================================================


class TestReportInterruptedWork:
    """Tests for the report_interrupted_work function."""

    @pytest.mark.asyncio
    async def test_no_interrupted_work(self):
        """When no interrupted work exists, returns early without publishing."""
        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(
            return_value={"phases": [], "pipelines": []}
        )
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        await report_interrupted_work(project_service, event_bus)

        event_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_warning_for_interrupted_phases(self, caplog):
        """Each interrupted phase produces a warning log."""
        phases = [
            {"name": "Phase 1", "id": "ph01", "project_name": "proj-a"},
            {"name": "Phase 2", "id": "ph02", "project_name": "proj-b"},
        ]
        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(
            return_value={"phases": phases, "pipelines": []}
        )
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="taktis.core.crash_recovery"):
            await report_interrupted_work(project_service, event_bus)

        assert "Phase 1" in caplog.text
        assert "ph01" in caplog.text
        assert "Phase 2" in caplog.text
        assert "ph02" in caplog.text

    @pytest.mark.asyncio
    async def test_logs_warning_for_interrupted_pipelines(self, caplog):
        """Each interrupted pipeline produces a warning log."""
        pipelines = [
            {"project_name": "my-project", "project_id": "proj01"},
        ]
        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(
            return_value={"phases": [], "pipelines": pipelines}
        )
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="taktis.core.crash_recovery"):
            await report_interrupted_work(project_service, event_bus)

        assert "my-project" in caplog.text
        assert "proj01" in caplog.text

    @pytest.mark.asyncio
    async def test_publishes_event_with_full_data(self):
        """Publishes EVENT_SYSTEM_INTERRUPTED_WORK with the interrupted data."""
        phases = [
            {"name": "Phase 1", "id": "ph01", "project_name": "proj-a"},
        ]
        pipelines = [
            {"project_name": "proj-a", "project_id": "proj01"},
        ]
        interrupted = {"phases": phases, "pipelines": pipelines}

        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(return_value=interrupted)
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        await report_interrupted_work(project_service, event_bus)

        event_bus.publish.assert_called_once_with(
            EVENT_SYSTEM_INTERRUPTED_WORK, interrupted
        )

    @pytest.mark.asyncio
    async def test_publishes_with_real_event_bus(self):
        """Works correctly with an actual EventBus instance."""
        phases = [
            {"name": "Phase 1", "id": "ph01", "project_name": "proj-a"},
        ]
        interrupted = {"phases": phases, "pipelines": []}

        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(return_value=interrupted)
        event_bus = EventBus()

        # Subscribe to capture the event
        queue = event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)

        await report_interrupted_work(project_service, event_bus)

        # Verify event was received
        event = queue.get_nowait()
        assert event["event_type"] == EVENT_SYSTEM_INTERRUPTED_WORK
        assert event["data"] == interrupted

        event_bus.unsubscribe(EVENT_SYSTEM_INTERRUPTED_WORK, queue)

    @pytest.mark.asyncio
    async def test_phases_only_no_pipelines(self, caplog):
        """Publishes when there are phases but no pipelines."""
        phases = [
            {"name": "Build API", "id": "ph99", "project_name": "web-app"},
        ]
        interrupted = {"phases": phases, "pipelines": []}

        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(return_value=interrupted)
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="taktis.core.crash_recovery"):
            await report_interrupted_work(project_service, event_bus)

        event_bus.publish.assert_called_once()
        assert "Build API" in caplog.text

    @pytest.mark.asyncio
    async def test_pipelines_only_no_phases(self, caplog):
        """Publishes when there are pipelines but no phases."""
        pipelines = [
            {"project_name": "new-project", "project_id": "projXY"},
        ]
        interrupted = {"phases": [], "pipelines": pipelines}

        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(return_value=interrupted)
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="taktis.core.crash_recovery"):
            await report_interrupted_work(project_service, event_bus)

        event_bus.publish.assert_called_once()
        assert "new-project" in caplog.text
        assert "projXY" in caplog.text

    @pytest.mark.asyncio
    async def test_log_includes_resume_instructions(self, caplog):
        """Log messages include the resume command hint."""
        phases = [
            {"name": "Phase 1", "id": "abc123", "project_name": "demo"},
        ]
        pipelines = [
            {"project_name": "demo", "project_id": "proj42"},
        ]
        interrupted = {"phases": phases, "pipelines": pipelines}

        project_service = MagicMock()
        project_service.get_interrupted_work = AsyncMock(return_value=interrupted)
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="taktis.core.crash_recovery"):
            await report_interrupted_work(project_service, event_bus)

        assert "resume phase abc123" in caplog.text
        assert "resume pipeline proj42" in caplog.text
