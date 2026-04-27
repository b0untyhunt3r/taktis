"""Tests for the supersession-marker feature in taktis/core/context.py.

Covers the explicit ===SUPERSEDE:...=== opt-in emitted by tasks (notably
question-asker pivots) that invalidates prior-phase context artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taktis.core import context as ctx_mod


def _init(tmp_path: Path) -> str:
    wd = str(tmp_path)
    ctx_mod.init_context(wd, "SuperTest", "desc")
    (tmp_path / ".taktis").mkdir(exist_ok=True)
    return wd


@pytest.mark.asyncio
async def test_no_marker_no_change(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    (tmp_path / ".taktis" / "REQUIREMENTS.md").write_text("req body", encoding="utf-8")
    modified = await ctx_mod.apply_supersession_if_marked(
        wd, "task0001", 1, "A normal result with no marker."
    )
    assert modified == []
    assert (tmp_path / ".taktis" / "REQUIREMENTS.md").read_text(encoding="utf-8") == "req body"


@pytest.mark.asyncio
async def test_marker_prepends_banner_to_listed_files(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    (tmp_path / ".taktis" / "REQUIREMENTS.md").write_text("old reqs\n", encoding="utf-8")
    (tmp_path / ".taktis" / "ROADMAP.md").write_text("old roadmap\n", encoding="utf-8")
    phase1 = tmp_path / ".taktis" / "phases" / "1"
    phase1.mkdir(parents=True)
    (phase1 / "PLAN.md").write_text("old plan\n", encoding="utf-8")

    result = (
        "user picked python\n"
        "===CONFIRMED===\n"
        "===SUPERSEDE: REQUIREMENTS.md, ROADMAP.md, phases/1/PLAN.md===\n"
    )
    modified = await ctx_mod.apply_supersession_if_marked(wd, "abcd1234", 2, result)
    assert set(modified) == {"REQUIREMENTS.md", "ROADMAP.md", "phases/1/PLAN.md"}

    for rel, original_tail in [
        ("REQUIREMENTS.md", "old reqs"),
        ("ROADMAP.md", "old roadmap"),
        ("phases/1/PLAN.md", "old plan"),
    ]:
        content = (tmp_path / ".taktis" / rel).read_text(encoding="utf-8")
        assert content.startswith("> **SUPERSEDED**")
        assert "task `abcd1234`" in content
        assert "phases/2/RESULT_abcd1234.md" in content
        assert original_tail in content


@pytest.mark.asyncio
async def test_idempotent_second_apply_skips(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    target = tmp_path / ".taktis" / "REQUIREMENTS.md"
    target.write_text("body\n", encoding="utf-8")

    marker_result = "===SUPERSEDE: REQUIREMENTS.md===\n"
    first = await ctx_mod.apply_supersession_if_marked(wd, "t1", 1, marker_result)
    assert first == ["REQUIREMENTS.md"]
    after_first = target.read_text(encoding="utf-8")

    second = await ctx_mod.apply_supersession_if_marked(wd, "t2", 1, marker_result)
    assert second == []
    assert target.read_text(encoding="utf-8") == after_first
    assert after_first.count("> **SUPERSEDED**") == 1


@pytest.mark.asyncio
async def test_missing_files_silently_skipped(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    (tmp_path / ".taktis" / "REQUIREMENTS.md").write_text("present\n", encoding="utf-8")

    result = "===SUPERSEDE: REQUIREMENTS.md, NOPE.md, phases/9/PLAN.md===\n"
    modified = await ctx_mod.apply_supersession_if_marked(wd, "tid", 1, result)
    assert modified == ["REQUIREMENTS.md"]
    assert not (tmp_path / ".taktis" / "NOPE.md").exists()


@pytest.mark.asyncio
async def test_path_escape_rejected(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")

    result = "===SUPERSEDE: ../outside.md===\n"
    modified = await ctx_mod.apply_supersession_if_marked(wd, "tid", 1, result)
    assert modified == []
    assert outside.read_text(encoding="utf-8") == "secret\n"


@pytest.mark.asyncio
async def test_close_but_not_exact_marker_ignored(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    (tmp_path / ".taktis" / "REQUIREMENTS.md").write_text("body\n", encoding="utf-8")

    for bogus in [
        "===supersede: REQUIREMENTS.md===",
        "==SUPERSEDE: REQUIREMENTS.md==",
        "===SUPERSEDE REQUIREMENTS.md===",
    ]:
        modified = await ctx_mod.apply_supersession_if_marked(wd, "tid", 1, bogus)
        assert modified == [], f"should not match: {bogus!r}"


@pytest.mark.asyncio
async def test_empty_marker_list_no_op(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    modified = await ctx_mod.apply_supersession_if_marked(
        wd, "tid", 1, "===SUPERSEDE:   ==="
    )
    assert modified == []


@pytest.mark.asyncio
async def test_phase_none_uses_root_result_path(tmp_path: Path) -> None:
    wd = _init(tmp_path)
    (tmp_path / ".taktis" / "REQUIREMENTS.md").write_text("body\n", encoding="utf-8")
    modified = await ctx_mod.apply_supersession_if_marked(
        wd, "zz99", None, "===SUPERSEDE: REQUIREMENTS.md==="
    )
    assert modified == ["REQUIREMENTS.md"]
    content = (tmp_path / ".taktis" / "REQUIREMENTS.md").read_text(encoding="utf-8")
    assert "RESULT_zz99.md" in content
