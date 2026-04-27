"""Regression test: interactive task continuation must write RESULT files.

When an interactive task reaches ===CONFIRMED=== in ``_execute_continuation``,
it transitions from ``awaiting_input`` to ``completed``. Before this test was
added, the result-file write was only done in the scheduler's ``_on_complete``
(which fires on the initial ``awaiting_input`` exit and skips interactive
tasks), so the final result was never persisted to
``.taktis/phases/{N}/RESULT_{task_id}.md`` — breaking downstream context.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.events import EventBus


def _make_session_ctx(mock_session: MagicMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.return_value = cm
    return cm


@pytest.mark.asyncio
async def test_interactive_continuation_confirmed_writes_result_file(
    tmp_path: Path,
) -> None:
    """When an interactive task completes via ===CONFIRMED===,
    ``RESULT_{task_id}.md`` and the ``.summary.md`` must be written under
    ``.taktis/phases/{N}/``.
    """
    from taktis.core.execution_service import ExecutionService

    working_dir = str(tmp_path)
    task_id = "abcd1234"
    phase_number = 2

    captured: dict = {}

    async def _fake_continue_task(
        *,
        task_id: str,
        process,
        message: str,
        session_id: str,
        on_output=None,
        on_complete=None,
    ):
        captured["on_output"] = on_output
        captured["on_complete"] = on_complete
        return process

    orch = MagicMock()
    orch._event_bus = EventBus()
    orch._manager = MagicMock()
    orch._manager.continue_task = _fake_continue_task
    orch._handle_pipeline_task_complete = AsyncMock()

    _make_session_ctx(orch._session_factory)

    task_row = {
        "id": task_id,
        "phase_id": "phase-xyz",
        "interactive": True,
        "task_type": "question-asker",
        "name": "Ask about pivot",
        "wave": 1,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "num_turns": 0,
        "peak_input_tokens": 0,
    }
    phase_row = {"id": "phase-xyz", "phase_number": phase_number}

    with (
        patch("taktis.core.execution_service.repo") as mock_repo,
        patch("taktis.core.sdk_process.SDKProcess") as mock_sdk_cls,
    ):
        mock_repo.get_task = AsyncMock(return_value=task_row)
        mock_repo.get_phase_by_id = AsyncMock(return_value=phase_row)
        mock_repo.update_task = AsyncMock()
        mock_repo.create_task_output = AsyncMock()
        mock_sdk_cls.return_value = MagicMock()

        await ExecutionService._execute_continuation(
            orch,
            task_id=task_id,
            message="pivot",
            session_id="sess-1",
            project={
                "id": "proj-1",
                "working_dir": working_dir,
                "default_model": "sonnet",
            },
        )

        on_output = captured["on_output"]
        on_complete = captured["on_complete"]
        assert on_output is not None and on_complete is not None, (
            "Expected continue_task to receive on_output/on_complete"
        )

        confirmed_result = "pivot\n===CONFIRMED==="
        await on_output(
            task_id,
            {"type": "result", "result": confirmed_result, "cost_usd": 0.0,
             "input_tokens": 1, "output_tokens": 1, "num_turns": 1},
        )
        await on_complete(task_id, 0, "")

    result_path = tmp_path / ".taktis" / "phases" / str(phase_number) / f"RESULT_{task_id}.md"
    summary_path = tmp_path / ".taktis" / "phases" / str(phase_number) / f"RESULT_{task_id}.summary.md"

    assert result_path.exists(), (
        f"Expected RESULT file at {result_path} — interactive task result "
        "was not persisted after ===CONFIRMED==="
    )
    assert summary_path.exists(), f"Expected summary file at {summary_path}"

    body = result_path.read_text(encoding="utf-8")
    assert "===CONFIRMED===" in body, "Result file should contain final result text"


@pytest.mark.asyncio
async def test_interactive_continuation_awaiting_input_skips_write(
    tmp_path: Path,
) -> None:
    """If the interactive turn does NOT contain ===CONFIRMED===, the task is
    still ``awaiting_input`` — no RESULT file should be written yet.
    """
    from taktis.core.execution_service import ExecutionService

    working_dir = str(tmp_path)
    task_id = "deadbeef"
    phase_number = 2

    captured: dict = {}

    async def _fake_continue_task(*, task_id, process, message, session_id,
                                  on_output=None, on_complete=None):
        captured["on_output"] = on_output
        captured["on_complete"] = on_complete
        return process

    orch = MagicMock()
    orch._event_bus = EventBus()
    orch._manager = MagicMock()
    orch._manager.continue_task = _fake_continue_task
    orch._handle_pipeline_task_complete = AsyncMock()

    _make_session_ctx(orch._session_factory)

    task_row = {
        "id": task_id,
        "phase_id": "phase-abc",
        "interactive": True,
        "task_type": "question-asker",
        "name": "Interactive",
        "wave": 1,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "num_turns": 0,
        "peak_input_tokens": 0,
    }

    with (
        patch("taktis.core.execution_service.repo") as mock_repo,
        patch("taktis.core.sdk_process.SDKProcess") as mock_sdk_cls,
    ):
        mock_repo.get_task = AsyncMock(return_value=task_row)
        mock_repo.get_phase_by_id = AsyncMock(
            return_value={"id": "phase-abc", "phase_number": phase_number},
        )
        mock_repo.update_task = AsyncMock()
        mock_repo.create_task_output = AsyncMock()
        mock_sdk_cls.return_value = MagicMock()

        await ExecutionService._execute_continuation(
            orch,
            task_id=task_id,
            message="tell me more",
            session_id="sess-1",
            project={
                "id": "proj-1",
                "working_dir": working_dir,
                "default_model": "sonnet",
            },
        )

        await captured["on_output"](
            task_id,
            {"type": "result", "result": "still thinking, not done",
             "cost_usd": 0.0, "input_tokens": 1, "output_tokens": 1, "num_turns": 1},
        )
        await captured["on_complete"](task_id, 0, "")

    result_path = tmp_path / ".taktis" / "phases" / str(phase_number) / f"RESULT_{task_id}.md"
    assert not result_path.exists(), (
        "RESULT file must not be written while task is still awaiting_input"
    )
