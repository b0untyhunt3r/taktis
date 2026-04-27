"""Unit tests for taktis/core/context.py — file I/O error handling.

Every new except block introduced in context.py (ERR-04) is exercised here.
Tests use tmp_path so no real filesystem state leaks between runs, and
unittest.mock.patch to simulate I/O failures without needing real broken
filesystems.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taktis.core import context as ctx_mod
from taktis.exceptions import ContextError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> str:
    """Return a working_dir string whose .taktis/ subtree already exists."""
    wd = str(tmp_path)
    ctx_mod.init_context(wd, "TestProject", "A test project")
    return wd


# ---------------------------------------------------------------------------
# init_context — mkdir / write_text failures
# ---------------------------------------------------------------------------


class TestInitContext:
    def test_happy_path_creates_files(self, tmp_path: Path) -> None:
        wd = str(tmp_path)
        ctx_mod.init_context(wd, "Proj", "Desc")
        assert (tmp_path / ".taktis" / "PROJECT.md").exists()
        assert (tmp_path / ".taktis" / "phases").is_dir()

    def test_ctx_mkdir_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = str(tmp_path)
        with patch.object(Path, "mkdir", side_effect=OSError("no space")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(wd, "P", "")
        err = exc_info.value
        assert ".taktis" in str(err)
        assert err.cause is not None

    def test_project_write_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = str(tmp_path)
        # Let mkdir succeed; fail only on the first write_text call.
        original_write = Path.write_text
        call_count = [0]

        def _fail_first(self, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("disk full")
            return original_write(self, *args, **kwargs)

        with patch.object(Path, "write_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(wd, "P", "")
        assert "PROJECT.md" in str(exc_info.value)


# ---------------------------------------------------------------------------
# read_context — read_text failures
# ---------------------------------------------------------------------------


class TestReadContext:
    def test_happy_path_returns_content(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        data = ctx_mod.read_context(wd)
        assert "project" in data

    def test_project_read_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        # Patch read_text only on PROJECT.md (it's the first .exists() check).
        original_read = Path.read_text
        call_count = [0]

        def _fail_first(self, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("permission denied")
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_context(wd)
        assert "PROJECT.md" in str(exc_info.value)


# ---------------------------------------------------------------------------
# init_phase_dir — mkdir failure
# ---------------------------------------------------------------------------


class TestInitPhaseDir:
    def test_happy_path_creates_directory(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 3)
        assert (tmp_path / ".taktis" / "phases" / "3").is_dir()

    def test_mkdir_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        with patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_phase_dir(wd, 7)
        assert "phase 7" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# write_phase_plan — write_text failure
# ---------------------------------------------------------------------------


class TestWritePhasePlan:
    def test_happy_path_creates_plan(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_phase_plan(wd, 1, "Init", "Get going", [
            {"wave": 1, "expert": "arch", "prompt": "Do the thing"},
        ])
        plan = (tmp_path / ".taktis" / "phases" / "1" / "PLAN.md").read_text()
        assert "Do the thing" in plan

    def test_write_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        with patch.object(Path, "write_text", side_effect=OSError("no space")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_phase_plan(wd, 1, "Init", "Go", [])
        assert "PLAN.md" in str(exc_info.value)



# ---------------------------------------------------------------------------
# write_research_file / read_research_files
# ---------------------------------------------------------------------------


class TestResearchFiles:
    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_research_file(wd, "notes.md", "# Notes\nSome notes.")
        files = ctx_mod.read_research_files(wd)
        assert "notes.md" in files
        assert "Some notes." in files["notes.md"]

    def test_read_returns_empty_when_no_research_dir(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        files = ctx_mod.read_research_files(wd)
        assert files == {}

    def test_write_mkdir_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        with patch.object(Path, "mkdir", side_effect=OSError("ro fs")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_research_file(wd, "x.md", "content")
        assert "research" in str(exc_info.value).lower()

    def test_write_file_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        # mkdir succeeds (research dir is created), write_text fails
        original_mkdir = Path.mkdir

        def _mkdir_ok(self, *a, **kw):
            return original_mkdir(self, *a, **kw)

        with patch.object(Path, "mkdir", _mkdir_ok):
            with patch.object(Path, "write_text", side_effect=OSError("full")):
                with pytest.raises(ContextError) as exc_info:
                    ctx_mod.write_research_file(wd, "x.md", "data")
        assert "x.md" in str(exc_info.value)

    def test_read_file_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_research_file(wd, "notes.md", "data")
        with patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_research_files(wd)
        assert "notes.md" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Simple write helpers (requirements, roadmap, verification, phase docs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("func,filename", [
    (ctx_mod.write_requirements, "REQUIREMENTS.md"),
    (ctx_mod.write_roadmap, "ROADMAP.md"),
    (ctx_mod.write_verification, "VERIFICATION.md"),
])
class TestSimpleWriteHelpers:
    def test_happy_path(self, tmp_path: Path, func, filename: str) -> None:
        wd = _make_ctx(tmp_path)
        func(wd, "# Content")
        assert (tmp_path / ".taktis" / filename).read_text() == "# Content"

    def test_write_failure_raises_os_error(
        self, tmp_path: Path, func, filename: str,
    ) -> None:
        wd = _make_ctx(tmp_path)
        with patch.object(Path, "write_text", side_effect=OSError("full")):
            with pytest.raises(OSError):
                func(wd, "x")


@pytest.mark.parametrize("func,filename,extra_args", [
    (ctx_mod.write_task_discuss, "DISCUSS_abc123.md", ("abc123",)),
    (ctx_mod.write_task_research, "RESEARCH_abc123.md", ("abc123",)),
    (ctx_mod.write_phase_review, "REVIEW.md", ()),
])
class TestPhaseWriteHelpers:
    def test_happy_path(self, tmp_path: Path, func, filename: str, extra_args) -> None:
        wd = _make_ctx(tmp_path)
        func(wd, 2, *extra_args, "# Content")
        assert (tmp_path / ".taktis" / "phases" / "2" / filename).read_text() == "# Content"

    def test_write_failure_raises_context_error(
        self, tmp_path: Path, func, filename: str, extra_args,
    ) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 2)
        with patch.object(Path, "write_text", side_effect=OSError("full")):
            with pytest.raises(ContextError) as exc_info:
                func(wd, 2, *extra_args, "x")
        assert filename in str(exc_info.value)


# ---------------------------------------------------------------------------
# write_task_result
# ---------------------------------------------------------------------------


class TestWriteTaskResult:
    def test_happy_path_creates_result_file(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_task_result(wd, 1, "abc123", "Task output here", task_name="Do stuff", wave=2)
        result_path = tmp_path / ".taktis" / "phases" / "1" / "RESULT_abc123.md"
        assert result_path.exists()
        content = result_path.read_text(encoding="utf-8")
        assert content.startswith("## Wave 2: Do stuff\n")
        assert "Task output here" in content

    def test_defaults_wave_zero_and_empty_name(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_task_result(wd, 3, "def456", "result text")
        result_path = tmp_path / ".taktis" / "phases" / "3" / "RESULT_def456.md"
        content = result_path.read_text(encoding="utf-8")
        assert "## Wave 0: \n" in content

    def test_write_failure_raises_context_error(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_task_result(wd, 1, "abc123", "x")
        assert "abc123" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# get_phase_context — graceful degradation
# ---------------------------------------------------------------------------


class TestGetPhaseContext:
    def test_happy_path_returns_full_context(self, tmp_path: Path) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_requirements(wd, "## Requirements")
        ctx_mod.update_context_manifest(wd, "REQUIREMENTS.md", "P2 — medium")
        ctx_mod.write_roadmap(wd, "## Roadmap")
        ctx_mod.update_context_manifest(wd, "ROADMAP.md", "P2 — medium")
        ctx_mod.write_phase_plan(wd, 1, "Phase One", "Goal", [
            {"wave": 1, "expert": "arch", "prompt": "Do it"},
        ])
        result, manifest = ctx_mod.get_phase_context(wd, 1)
        assert "<project_context>" in result
        assert "<REQUIREMENTS_md>" in result
        assert "<ROADMAP_md>" in result
        assert "<current_phase_plan>" in result
        assert isinstance(manifest, list)

    def test_returns_empty_when_context_dir_missing(self, tmp_path: Path) -> None:
        result, manifest = ctx_mod.get_phase_context(str(tmp_path), None)
        assert result == ""
        assert manifest == []

    def test_read_failure_skips_section_and_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_requirements(wd, "## Requirements")
        ctx_mod.update_context_manifest(wd, "REQUIREMENTS.md", "P2 — medium")

        original_read = Path.read_text
        fail_path = str(tmp_path / ".taktis" / "PROJECT.md")

        def _fail_project(self, *args, **kwargs):
            if str(self) == fail_path:
                raise OSError("permission denied")
            return original_read(self, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_project):
                result, _ = ctx_mod.get_phase_context(wd, None)

        # PROJECT.md section is absent but other sections survive
        assert "<project_context>" not in result
        assert "<REQUIREMENTS_md>" in result
        # A warning was emitted
        assert any("project context" in r.message for r in caplog.records)

    def test_multiple_read_failures_return_partial_context(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        wd = _make_ctx(tmp_path)
        ctx_mod.write_phase_plan(wd, 1, "Phase One", "Goal", [])

        original_read = Path.read_text
        project_path = str(tmp_path / ".taktis" / "PROJECT.md")

        def _fail_project(self, *args, **kwargs):
            if str(self) == project_path:
                raise OSError("bad sector")
            return original_read(self, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_project):
                result, _ = ctx_mod.get_phase_context(wd, 1)

        # Phase plan content survives despite PROJECT.md failure
        assert result != ""
        assert "<current_phase_plan>" in result
        # A warning was emitted
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1

    def test_no_crash_when_all_reads_fail(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        wd = _make_ctx(tmp_path)
        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", side_effect=OSError("all broken")):
                result, _ = ctx_mod.get_phase_context(wd, None)
        # Graceful degradation: empty string, no exception raised
        assert result == ""

    def test_context_error_carries_cause(self, tmp_path: Path) -> None:
        """ContextError.__cause__ must be set so tracebacks chain correctly."""
        wd = _make_ctx(tmp_path)
        original_read = Path.read_text
        original_err = OSError("disk error")

        def _raise(self, *args, **kwargs):
            raise original_err

        # Use read_context (raises, not degrades) to verify __cause__ chaining.
        with patch.object(Path, "read_text", _raise):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_context(wd)
        assert exc_info.value.__cause__ is original_err


# ---------------------------------------------------------------------------
# Phase context caching (Change 1)
# ---------------------------------------------------------------------------


class TestPhaseContextCache:
    """Tests for phase context caching."""

    def setup_method(self):
        """Clear cache before each test."""
        from taktis.core.context import clear_phase_context_cache
        clear_phase_context_cache()

    def test_ctx_cache_01_second_call_uses_cache(self, tmp_path: Path) -> None:
        """CTX-CACHE-01: Second call uses cache (no disk re-reads for base content)."""
        wd = _make_ctx(tmp_path)
        ctx_mod.write_requirements(wd, "## Requirements")
        ctx_mod.update_context_manifest(wd, "REQUIREMENTS.md", "P2 — medium")
        ctx_mod.write_roadmap(wd, "## Roadmap")
        ctx_mod.update_context_manifest(wd, "ROADMAP.md", "P2 — medium")

        # First call — populates cache
        result1, manifest1 = ctx_mod.get_phase_context(wd, None)
        assert "<REQUIREMENTS_md>" in result1

        # Count read_text calls on the second invocation
        original_read = Path.read_text
        read_count = [0]

        def _counting_read(self, *args, **kwargs):
            read_count[0] += 1
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", _counting_read):
            result2, manifest2 = ctx_mod.get_phase_context(wd, None)

        # Second call should return identical content with zero base reads
        assert result2 == result1
        assert manifest2 == manifest1
        assert read_count[0] == 0, (
            f"Expected 0 read_text calls on cached hit, got {read_count[0]}"
        )

    def test_ctx_cache_02_different_phase_is_cache_miss(self, tmp_path: Path) -> None:
        """CTX-CACHE-02: Different phase_number is a cache miss."""
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        ctx_mod.init_phase_dir(wd, 2)
        ctx_mod.write_phase_plan(wd, 1, "P1", "Goal 1", [])
        ctx_mod.write_phase_plan(wd, 2, "P2", "Goal 2", [])

        result_p1, _ = ctx_mod.get_phase_context(wd, 1)
        result_p2, _ = ctx_mod.get_phase_context(wd, 2)

        # Both should have content, but different phase plans
        assert "Phase 1" in result_p1
        assert "Phase 2" in result_p2
        assert result_p1 != result_p2

    def test_ctx_cache_03_task_specific_files_never_cached(self, tmp_path: Path) -> None:
        """CTX-CACHE-03: Task-specific files are never cached."""
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        ctx_mod.write_task_discuss(wd, 1, "task1", "# Discuss task1")
        ctx_mod.write_task_discuss(wd, 1, "task2", "# Discuss task2")

        result1, _ = ctx_mod.get_phase_context(wd, 1, task_id="task1")
        result2, _ = ctx_mod.get_phase_context(wd, 1, task_id="task2")

        assert "<task_decisions>" in result1
        assert "Discuss task1" in result1
        assert "<task_decisions>" in result2
        assert "Discuss task2" in result2
        # Task1's discuss content should NOT appear in task2's result
        assert "Discuss task1" not in result2

    def test_ctx_cache_04_ttl_expiry_triggers_fresh_read(self, tmp_path: Path) -> None:
        """CTX-CACHE-04: TTL expiry triggers fresh read."""
        wd = _make_ctx(tmp_path)

        # First call — populates cache
        result1, _ = ctx_mod.get_phase_context(wd, None)
        assert result1 != ""

        # Simulate TTL expiry by patching time.monotonic
        import time as time_mod
        original_monotonic = time_mod.monotonic
        expired_time = original_monotonic() + ctx_mod._PHASE_CONTEXT_TTL + 10.0

        original_read = Path.read_text
        read_count = [0]

        def _counting_read(self, *args, **kwargs):
            read_count[0] += 1
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", _counting_read):
            with patch("taktis.core.context.time.monotonic", return_value=expired_time):
                result2, _ = ctx_mod.get_phase_context(wd, None)

        # After TTL expiry, files should be re-read
        assert result2 == result1
        assert read_count[0] > 0, "Expected file re-reads after TTL expiry"

    def test_ctx_cache_05_invalidate_clears_specific_phase(self, tmp_path: Path) -> None:
        """CTX-CACHE-05: invalidate_phase_context clears specific phase."""
        from taktis.core.context import invalidate_phase_context

        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        ctx_mod.init_phase_dir(wd, 2)

        # Populate cache for both phases
        ctx_mod.get_phase_context(wd, 1)
        ctx_mod.get_phase_context(wd, 2)

        # Invalidate only phase 1
        invalidate_phase_context(wd, 1)

        # Phase 1 should be a cache miss (re-reads files)
        original_read = Path.read_text
        read_count = [0]

        def _counting_read(self, *args, **kwargs):
            read_count[0] += 1
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", _counting_read):
            ctx_mod.get_phase_context(wd, 1)
        assert read_count[0] > 0, "Phase 1 should have been re-read after invalidation"

        # Phase 2 should still be cached
        read_count[0] = 0
        with patch.object(Path, "read_text", _counting_read):
            ctx_mod.get_phase_context(wd, 2)
        assert read_count[0] == 0, "Phase 2 should still be cached"

    def test_ctx_cache_06_write_task_result_triggers_invalidation(self, tmp_path: Path) -> None:
        """CTX-CACHE-06: write_task_result triggers invalidation."""
        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)

        # Populate cache
        ctx_mod.get_phase_context(wd, 1)

        # Write a task result — should invalidate the cache
        ctx_mod.write_task_result(wd, 1, "abc123", "Result here", task_name="Test", wave=1)

        # Next call should re-read (and include the result)
        original_read = Path.read_text
        read_count = [0]

        def _counting_read(self, *args, **kwargs):
            read_count[0] += 1
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", _counting_read):
            result, _ = ctx_mod.get_phase_context(wd, 1)

        assert read_count[0] > 0, "Files should be re-read after write_task_result"
        assert "Result here" in result

    def test_ctx_cache_07_clear_cache_clears_everything(self, tmp_path: Path) -> None:
        """CTX-CACHE-07: clear_phase_context_cache clears everything."""
        from taktis.core.context import clear_phase_context_cache

        wd = _make_ctx(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)

        # Populate cache
        ctx_mod.get_phase_context(wd, 1)
        ctx_mod.get_phase_context(wd, None)

        # Clear entire cache
        clear_phase_context_cache()

        # Both should be cache misses now
        original_read = Path.read_text
        read_count = [0]

        def _counting_read(self, *args, **kwargs):
            read_count[0] += 1
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", _counting_read):
            ctx_mod.get_phase_context(wd, 1)
        assert read_count[0] > 0, "Phase 1 should re-read after clear"

        read_count[0] = 0
        clear_phase_context_cache()
        with patch.object(Path, "read_text", _counting_read):
            ctx_mod.get_phase_context(wd, None)
        assert read_count[0] > 0, "None-phase should re-read after clear"
