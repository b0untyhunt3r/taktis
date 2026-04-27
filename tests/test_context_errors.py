"""Focused I/O error-path tests for taktis/core/context.py.

Scope
-----
This module tests *only* the file I/O error paths in context.py using three
specific failure modes:

1. **PermissionError** – the OS denies read/write access (EACCES).
2. **OSError / generic** – disk-full, bad sector, or other I/O error.
3. **Missing-file scenarios** – a file that should exist is absent (covered
   via both FileNotFoundError and by removing files from tmp_path before the
   call under test).

Why this module exists alongside ``test_context.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``test_context.py`` covers the happy path and basic ``OSError`` injection.
The gaps it leaves are:

- No ``PermissionError``-specific tests (``PermissionError`` is a subclass
  of ``OSError``; some OS checks test ``errno.EACCES`` explicitly).
- ``write_task_result`` **write** failure path (context.py):
  the file exists, the read succeeds, *then* the write of the appended
  content fails.  This branch is completely absent from test_context.py.
- ``init_context`` **phases subdirectory** mkdir failure (L38-45): tested
  as a distinct site from the ctx-dir mkdir failure.
- ``get_phase_context`` **review-file** graceful-degrade path.
- ``__cause__`` object identity — every I/O path must set ``__cause__`` to
  the *exact same* exception object, not a copy.

Testing conventions
-------------------
- ``unittest.mock.patch`` / ``patch.object`` keep tests hermetic.
- ``tmp_path`` (pytest built-in) provides a real but temporary filesystem
  so that successful operations actually touch disk.
- No ``sleep``, no wall-clock time, no network calls.
- Each test verifies exactly one behaviour; compound assertions have been
  split where practical.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from taktis.core import context as ctx_mod
from taktis.exceptions import ContextError


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _init(tmp_path: Path) -> str:
    """Return a working_dir whose .taktis/ tree already exists."""
    wd = str(tmp_path)
    ctx_mod.init_context(wd, "ErrorTestProject", "Testing error paths")
    return wd


def _ctx(tmp_path: Path) -> Path:
    """Return the absolute .taktis/ path for inspection."""
    return tmp_path / ".taktis"


# ---------------------------------------------------------------------------
# TC-CTX-001 … TC-CTX-012  init_context — directory creation failures
# ---------------------------------------------------------------------------


class TestInitContextDirectoryErrors:
    """mkdir failures in init_context propagate as ContextError."""

    # -- ctx (top-level) directory ------------------------------------------

    def test_permission_error_on_ctx_mkdir_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-001: PermissionError on ctx mkdir → ContextError."""
        with patch.object(Path, "mkdir", side_effect=PermissionError("EACCES")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        assert exc_info.value.cause is not None

    def test_oserror_on_ctx_mkdir_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-002: Generic OSError on ctx mkdir → ContextError."""
        with patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        err = exc_info.value
        assert isinstance(err, ContextError)
        assert ".taktis" in str(err)

    def test_ctx_mkdir_cause_is_exact_exception(self, tmp_path: Path) -> None:
        """TC-CTX-003: __cause__ identity — ctx mkdir."""
        original = PermissionError("EACCES")
        with patch.object(Path, "mkdir", side_effect=original):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        assert exc_info.value.__cause__ is original
        assert exc_info.value.cause is original

    # -- phases subdirectory -------------------------------------------------

    def test_permission_error_on_phases_mkdir_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-004: PermissionError on phases/ mkdir → ContextError."""
        original_mkdir = Path.mkdir
        call_count = [0]

        def _fail_second(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 2:
                raise PermissionError("cannot create phases/")
            return original_mkdir(self_, *args, **kwargs)

        with patch.object(Path, "mkdir", _fail_second):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        err = exc_info.value
        assert "phases" in str(err).lower()
        assert isinstance(err.cause, PermissionError)

    def test_oserror_on_phases_mkdir_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-005: Generic OSError on phases/ mkdir → ContextError."""
        original_mkdir = Path.mkdir
        call_count = [0]

        def _fail_second(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("no inodes left")
            return original_mkdir(self_, *args, **kwargs)

        with patch.object(Path, "mkdir", _fail_second):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        assert isinstance(exc_info.value, ContextError)
        assert "phases" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TC-CTX-013 … TC-CTX-020  init_context — file-write failures
# ---------------------------------------------------------------------------


class TestInitContextWriteErrors:
    """write_text failures in init_context propagate as ContextError."""

    def test_permission_error_on_project_md_write(self, tmp_path: Path) -> None:
        """TC-CTX-013: PermissionError writing PROJECT.md → ContextError."""
        original_write = Path.write_text
        call_count = [0]

        def _fail_first(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("EACCES on PROJECT.md")
            return original_write(self_, *args, **kwargs)

        with patch.object(Path, "write_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        err = exc_info.value
        assert "PROJECT.md" in str(err)
        assert isinstance(err.cause, PermissionError)

    def test_project_write_cause_identity(self, tmp_path: Path) -> None:
        """TC-CTX-015: __cause__ is the exact PermissionError that was raised."""
        original_write = Path.write_text
        original_exc = PermissionError("exact identity")
        call_count = [0]

        def _fail_first(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 1:
                raise original_exc
            return original_write(self_, *args, **kwargs)

        with patch.object(Path, "write_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_context(str(tmp_path), "P", "")
        assert exc_info.value.__cause__ is original_exc
        assert exc_info.value.cause is original_exc


# ---------------------------------------------------------------------------
# TC-CTX-021 … TC-CTX-028  read_context — read failures
# ---------------------------------------------------------------------------


class TestReadContextErrors:
    """PermissionError and FileNotFoundError in read_context → ContextError."""

    def test_permission_error_reading_project_md(self, tmp_path: Path) -> None:
        """TC-CTX-021: PermissionError on PROJECT.md read → ContextError."""
        wd = _init(tmp_path)
        original_read = Path.read_text
        call_count = [0]

        def _fail_first(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("no read permission")
            return original_read(self_, *args, **kwargs)

        with patch.object(Path, "read_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_context(wd)
        err = exc_info.value
        assert "PROJECT.md" in str(err)
        assert isinstance(err.cause, PermissionError)

    def test_file_not_found_simulated_via_oserror(self, tmp_path: Path) -> None:
        """TC-CTX-022: FileNotFoundError on PROJECT.md read → ContextError."""
        wd = _init(tmp_path)
        original_read = Path.read_text
        call_count = [0]

        def _fail_first(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 1:
                raise FileNotFoundError("file gone after exists() check")
            return original_read(self_, *args, **kwargs)

        with patch.object(Path, "read_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_context(wd)
        err = exc_info.value
        assert isinstance(err.cause, FileNotFoundError)

    def test_cause_identity_on_read_failure(self, tmp_path: Path) -> None:
        """TC-CTX-024: __cause__ is the exact exception that was raised."""
        wd = _init(tmp_path)
        original_exc = PermissionError("exact identity check")
        original_read = Path.read_text
        call_count = [0]

        def _fail_first(self_: Path, *args, **kwargs):  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 1:
                raise original_exc
            return original_read(self_, *args, **kwargs)

        with patch.object(Path, "read_text", _fail_first):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_context(wd)
        assert exc_info.value.__cause__ is original_exc

    def test_missing_files_return_empty_dict(self, tmp_path: Path) -> None:
        """TC-CTX-025: When .taktis/ has no .md files, read_context returns {}."""
        wd = _init(tmp_path)
        # Remove PROJECT.md; it should simply be absent
        (_ctx(tmp_path) / "PROJECT.md").unlink()
        result = ctx_mod.read_context(wd)
        assert result == {}


# ---------------------------------------------------------------------------
# TC-CTX-030 … TC-CTX-034  init_phase_dir — mkdir failures
# ---------------------------------------------------------------------------


class TestInitPhaseDirErrors:
    """PermissionError and OSError in init_phase_dir."""

    def test_permission_error_raises_context_error(self, tmp_path: Path) -> None:
        """TC-CTX-030: PermissionError on mkdir → ContextError."""
        wd = _init(tmp_path)
        with patch.object(Path, "mkdir", side_effect=PermissionError("EACCES")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_phase_dir(wd, 5)
        err = exc_info.value
        assert isinstance(err.cause, PermissionError)

    def test_error_message_identifies_phase(self, tmp_path: Path) -> None:
        """TC-CTX-031: ContextError message names the phase number."""
        wd = _init(tmp_path)
        with patch.object(Path, "mkdir", side_effect=OSError("no space")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_phase_dir(wd, 99)
        assert "99" in str(exc_info.value)

    def test_cause_identity(self, tmp_path: Path) -> None:
        """TC-CTX-032: __cause__ is the exact exception raised."""
        wd = _init(tmp_path)
        original_exc = PermissionError("identity test")
        with patch.object(Path, "mkdir", side_effect=original_exc):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.init_phase_dir(wd, 1)
        assert exc_info.value.__cause__ is original_exc


# ---------------------------------------------------------------------------
# TC-CTX-035 … TC-CTX-042  write_phase_plan — write failures
# ---------------------------------------------------------------------------


class TestWritePhasePlanErrors:
    """PermissionError and OSError in write_phase_plan."""

    def test_permission_error_raises_context_error(self, tmp_path: Path) -> None:
        """TC-CTX-035: PermissionError writing PLAN.md → ContextError."""
        wd = _init(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        with patch.object(Path, "write_text", side_effect=PermissionError("EACCES")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_phase_plan(wd, 1, "Name", "Goal", [])
        err = exc_info.value
        assert "PLAN.md" in str(err)
        assert isinstance(err.cause, PermissionError)

    def test_oserror_raises_context_error(self, tmp_path: Path) -> None:
        """TC-CTX-036: Generic OSError writing PLAN.md → ContextError."""
        wd = _init(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_phase_plan(wd, 1, "Name", "Goal", [])
        assert isinstance(exc_info.value, ContextError)

    def test_cause_identity(self, tmp_path: Path) -> None:
        """TC-CTX-037: __cause__ is the exact PermissionError raised."""
        wd = _init(tmp_path)
        ctx_mod.init_phase_dir(wd, 1)
        original_exc = PermissionError("exact")
        with patch.object(Path, "write_text", side_effect=original_exc):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_phase_plan(wd, 1, "Name", "Goal", [])
        assert exc_info.value.__cause__ is original_exc



# ---------------------------------------------------------------------------
# TC-CTX-063 … TC-CTX-073  write_research_file / read_research_files errors
# ---------------------------------------------------------------------------


class TestResearchFileErrors:
    """PermissionError in research I/O paths."""

    def test_permission_error_on_mkdir_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-063: PermissionError creating research/ dir → ContextError."""
        wd = _init(tmp_path)
        with patch.object(Path, "mkdir", side_effect=PermissionError("EACCES")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.write_research_file(wd, "notes.md", "content")
        assert "research" in str(exc_info.value).lower()
        assert isinstance(exc_info.value.cause, PermissionError)

    def test_permission_error_on_file_write_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-064: PermissionError writing research file → ContextError."""
        wd = _init(tmp_path)
        original_mkdir = Path.mkdir

        def _mkdir_ok(self_: Path, *args, **kwargs):  # type: ignore[override]
            return original_mkdir(self_, *args, **kwargs)

        with patch.object(Path, "mkdir", _mkdir_ok):
            with patch.object(
                Path, "write_text", side_effect=PermissionError("EACCES on write"),
            ):
                with pytest.raises(ContextError) as exc_info:
                    ctx_mod.write_research_file(wd, "notes.md", "data")
        assert "notes.md" in str(exc_info.value)
        assert isinstance(exc_info.value.cause, PermissionError)

    def test_write_cause_identity(self, tmp_path: Path) -> None:
        """TC-CTX-065: __cause__ identity on research file write."""
        wd = _init(tmp_path)
        original_mkdir = Path.mkdir
        original_exc = PermissionError("identity")

        def _mkdir_ok(self_: Path, *args, **kwargs):  # type: ignore[override]
            return original_mkdir(self_, *args, **kwargs)

        with patch.object(Path, "mkdir", _mkdir_ok):
            with patch.object(Path, "write_text", side_effect=original_exc):
                with pytest.raises(ContextError) as exc_info:
                    ctx_mod.write_research_file(wd, "notes.md", "data")
        assert exc_info.value.__cause__ is original_exc

    def test_permission_error_on_read_raises_context_error(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-066: PermissionError reading a research file → ContextError."""
        wd = _init(tmp_path)
        ctx_mod.write_research_file(wd, "notes.md", "some data")
        with patch.object(Path, "read_text", side_effect=PermissionError("locked")):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_research_files(wd)
        assert "notes.md" in str(exc_info.value)
        assert isinstance(exc_info.value.cause, PermissionError)

    def test_read_cause_identity(self, tmp_path: Path) -> None:
        """TC-CTX-067: __cause__ identity on research file read."""
        wd = _init(tmp_path)
        ctx_mod.write_research_file(wd, "notes.md", "data")
        original_exc = PermissionError("exact")
        with patch.object(Path, "read_text", side_effect=original_exc):
            with pytest.raises(ContextError) as exc_info:
                ctx_mod.read_research_files(wd)
        assert exc_info.value.__cause__ is original_exc

    def test_missing_research_dir_returns_empty(self, tmp_path: Path) -> None:
        """TC-CTX-068: Absent research/ directory → read_research_files returns {}."""
        wd = _init(tmp_path)
        result = ctx_mod.read_research_files(wd)
        assert result == {}

    def test_non_md_files_in_research_dir_are_ignored(
        self, tmp_path: Path,
    ) -> None:
        """TC-CTX-069: Non-.md files in research/ are silently skipped."""
        wd = _init(tmp_path)
        research_dir = _ctx(tmp_path) / "research"
        research_dir.mkdir()
        (research_dir / "notes.txt").write_text("should be ignored")
        (research_dir / "notes.md").write_text("should appear")
        result = ctx_mod.read_research_files(wd)
        assert "notes.md" in result
        assert "notes.txt" not in result


# ---------------------------------------------------------------------------
# TC-CTX-074 … TC-CTX-080  Simple write helpers — PermissionError paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("func,filename", [
    (ctx_mod.write_requirements, "REQUIREMENTS.md"),
    (ctx_mod.write_roadmap, "ROADMAP.md"),
    (ctx_mod.write_verification, "VERIFICATION.md"),
])
class TestSimpleWriteHelperPermissionErrors:
    """TC-CTX-074 … TC-CTX-076: PermissionError for each simple write helper."""

    def test_permission_error_raises(
        self, tmp_path: Path, func, filename: str,
    ) -> None:
        """PermissionError propagates from write helpers."""
        wd = _init(tmp_path)
        with patch.object(Path, "write_text", side_effect=PermissionError("EACCES")):
            with pytest.raises(PermissionError):
                func(wd, "# Content")

    def test_cause_identity(self, tmp_path: Path, func, filename: str) -> None:
        """The exact PermissionError raised is the one that propagates."""
        wd = _init(tmp_path)
        original_exc = PermissionError("exact identity")
        with patch.object(Path, "write_text", side_effect=original_exc):
            with pytest.raises(PermissionError) as exc_info:
                func(wd, "# Content")
        assert exc_info.value is original_exc


@pytest.mark.parametrize("func,filename,extra_args", [
    (ctx_mod.write_task_discuss, "DISCUSS_abc123.md", ("abc123",)),
    (ctx_mod.write_task_research, "RESEARCH_abc123.md", ("abc123",)),
    (ctx_mod.write_phase_review, "REVIEW.md", ()),
])
class TestPhaseWriteHelperPermissionErrors:
    """TC-CTX-077 … TC-CTX-080: PermissionError for each phase write helper."""

    def test_permission_error_raises_context_error(
        self, tmp_path: Path, func, filename: str, extra_args,
    ) -> None:
        """PermissionError → ContextError with filename in message."""
        wd = _init(tmp_path)
        ctx_mod.init_phase_dir(wd, 3)
        with patch.object(Path, "write_text", side_effect=PermissionError("EACCES")):
            with pytest.raises(ContextError) as exc_info:
                func(wd, 3, *extra_args, "# Phase Content")
        err = exc_info.value
        assert filename in str(err)
        assert isinstance(err.cause, PermissionError)

    def test_cause_identity(self, tmp_path: Path, func, filename: str, extra_args) -> None:
        """__cause__ is the exact PermissionError raised."""
        wd = _init(tmp_path)
        ctx_mod.init_phase_dir(wd, 3)
        original_exc = PermissionError("exact")
        with patch.object(Path, "write_text", side_effect=original_exc):
            with pytest.raises(ContextError) as exc_info:
                func(wd, 3, *extra_args, "# Content")
        assert exc_info.value.__cause__ is original_exc


# ---------------------------------------------------------------------------
# TC-CTX-081 … TC-CTX-093  get_phase_context — graceful degradation
# ---------------------------------------------------------------------------


class TestGetPhaseContextGracefulDegradation:
    """get_phase_context must degrade gracefully on any read failure.

    All failures here emit a WARNING and return partial (or empty) context
    rather than propagating the exception.
    """

    def test_permission_error_on_project_md_skips_section(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-081: PermissionError on PROJECT.md → section skipped, warning logged."""
        wd = _init(tmp_path)
        original_read = Path.read_text
        fail_path = str(_ctx(tmp_path) / "PROJECT.md")

        def _fail_project(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) == fail_path:
                raise PermissionError("no access")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_project):
                result, _ = ctx_mod.get_phase_context(wd, None)

        assert "<project_context>" not in result
        assert any("project context" in r.message for r in caplog.records)

    def test_permission_error_on_review_file_skips_section(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-083: PermissionError reading prior-phase REVIEW.md → skipped.

        This specific path (review file in prior phase) is absent from
        test_context.py and is a genuine untested branch.
        """
        wd = _init(tmp_path)
        # Create phase 1 with a review
        ctx_mod.write_phase_review(wd, 1, "# Phase 1 Review\nAll good.")

        review_path = str(_ctx(tmp_path) / "phases" / "1" / "REVIEW.md")
        original_read = Path.read_text

        def _fail_review(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) == review_path:
                raise PermissionError("no read on review")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_review):
                # phase_number=2 means phase 1 is a "prior phase"
                result, _ = ctx_mod.get_phase_context(wd, 2)

        assert "<prior_phase_review" not in result
        assert any("phase 1 review" in r.message for r in caplog.records)

    def test_file_not_found_on_requirements_skips_section(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-084: FileNotFoundError on REQUIREMENTS.md → section skipped."""
        wd = _init(tmp_path)
        ctx_mod.write_requirements(wd, "# Reqs")
        ctx_mod.update_context_manifest(wd, "REQUIREMENTS.md", "P2 — medium")

        req_path = str(_ctx(tmp_path) / "REQUIREMENTS.md")
        original_read = Path.read_text

        def _fail_req(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) == req_path:
                raise FileNotFoundError("gone between exists() and read()")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_req):
                result, _ = ctx_mod.get_phase_context(wd, None)

        assert "<REQUIREMENTS_md>" not in result
        assert any("REQUIREMENTS.md" in r.message for r in caplog.records)

    def test_file_not_found_on_roadmap_skips_section(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-085: FileNotFoundError on ROADMAP.md → section skipped."""
        wd = _init(tmp_path)
        ctx_mod.write_roadmap(wd, "# Roadmap")
        ctx_mod.update_context_manifest(wd, "ROADMAP.md", "P2 — medium")

        roadmap_path = str(_ctx(tmp_path) / "ROADMAP.md")
        original_read = Path.read_text

        def _fail_roadmap(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) == roadmap_path:
                raise FileNotFoundError("race condition")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_roadmap):
                result, _ = ctx_mod.get_phase_context(wd, None)

        assert "<ROADMAP_md>" not in result
        assert any("ROADMAP.md" in r.message for r in caplog.records)

    def test_permission_error_on_current_plan_skips_section(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-086: PermissionError on current PLAN.md → section skipped."""
        wd = _init(tmp_path)
        ctx_mod.write_phase_plan(wd, 2, "Phase Two", "Goal", [])

        plan_path = str(_ctx(tmp_path) / "phases" / "2" / "PLAN.md")
        original_read = Path.read_text

        def _fail_plan(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) == plan_path:
                raise PermissionError("plan locked")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_plan):
                result, _ = ctx_mod.get_phase_context(wd, 2)

        assert "<current_phase_plan>" not in result
        assert any("plan" in r.message.lower() for r in caplog.records)

    def test_all_reads_fail_returns_empty_string_no_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-087: When every read fails, return '' and never raise."""
        wd = _init(tmp_path)
        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(
                Path, "read_text", side_effect=PermissionError("all denied"),
            ):
                result, _ = ctx_mod.get_phase_context(wd, None)
        assert result == ""

    def test_missing_ctx_dir_returns_empty_string(self, tmp_path: Path) -> None:
        """TC-CTX-088: Entirely absent .taktis/ → returns '' immediately."""
        result, _ = ctx_mod.get_phase_context(str(tmp_path), 1)
        assert result == ""

    def test_partial_failures_preserve_surviving_sections(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-089: Only failed sections are absent; others survive."""
        wd = _init(tmp_path)
        ctx_mod.write_requirements(wd, "## Requirements")
        ctx_mod.update_context_manifest(wd, "REQUIREMENTS.md", "P2 — medium")

        # Fail only PROJECT.md; everything else should come through
        original_read = Path.read_text
        fail_path = str(_ctx(tmp_path) / "PROJECT.md")

        def _fail_project(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) == fail_path:
                raise PermissionError("project denied")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_project):
                result, _ = ctx_mod.get_phase_context(wd, None)

        # requirements survived
        assert "<REQUIREMENTS_md>" in result
        # project_context is absent
        assert "<project_context>" not in result

    def test_warning_logged_for_each_failed_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TC-CTX-090: One WARNING per failed file is emitted to the logger."""
        wd = _init(tmp_path)
        ctx_mod.write_requirements(wd, "# Reqs")
        ctx_mod.update_context_manifest(wd, "REQUIREMENTS.md", "P2 — medium")
        ctx_mod.write_roadmap(wd, "# Roadmap")
        ctx_mod.update_context_manifest(wd, "ROADMAP.md", "P2 — medium")

        original_read = Path.read_text
        fail_paths = {
            str(_ctx(tmp_path) / "PROJECT.md"),
            str(_ctx(tmp_path) / "REQUIREMENTS.md"),
        }

        def _fail_targeted(self_: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_) in fail_paths:
                raise PermissionError("permission denied")
            return original_read(self_, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="taktis.core.context"):
            with patch.object(Path, "read_text", _fail_targeted):
                ctx_mod.get_phase_context(wd, None)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 2


# ---------------------------------------------------------------------------
# TC-CTX-094 … TC-CTX-096  Regression tests for previously identified gaps
# ---------------------------------------------------------------------------


class TestRegressionCauseChaining:
    """Regression tests: __cause__ must be set on every I/O error path.

    These tests were written AFTER auditing the full set of except blocks
    in context.py to ensure every 'raise ContextError(…) from exc' actually
    threads the original exception through correctly.
    """

    @pytest.mark.parametrize("func_and_setup", [
        # (callable, setup_callable_or_None)
        ("init_context", None),
        ("read_context", "init"),
        ("init_phase_dir", "init"),
    ])
    def test_context_error_always_wraps_original_exception(
        self, tmp_path: Path, func_and_setup,
    ) -> None:
        """TC-CTX-094: Every ContextError carries a non-None .cause."""
        label, setup = func_and_setup
        wd = str(tmp_path)

        if setup == "init":
            ctx_mod.init_context(wd, "P", "")

        original_exc = PermissionError("permission denied")

        if label == "init_context":
            with patch.object(Path, "mkdir", side_effect=original_exc):
                with pytest.raises(ContextError) as exc_info:
                    ctx_mod.init_context(wd, "P", "")

        elif label == "read_context":
            with patch.object(Path, "read_text", side_effect=original_exc):
                with pytest.raises(ContextError) as exc_info:
                    ctx_mod.read_context(wd)

        elif label == "init_phase_dir":
            with patch.object(Path, "mkdir", side_effect=original_exc):
                with pytest.raises(ContextError) as exc_info:
                    ctx_mod.init_phase_dir(wd, 1)

        assert exc_info.value.cause is not None, (
            f"{label}: expected .cause to be set"
        )
        assert exc_info.value.__cause__ is not None, (
            f"{label}: expected __cause__ to be set"
        )
        assert exc_info.value.__cause__ is original_exc, (
            f"{label}: expected __cause__ to be the exact original exception"
        )
