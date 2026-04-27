"""Tests for ContextBudget priority-based context assembly and write-time summaries."""
from __future__ import annotations
import pytest
from taktis.core.context import ContextBudget, _extract_summary


class TestContextBudgetPriority:

    def test_priority_ordering(self):
        """P0 sections included before P4; P4 omitted when budget is tight."""
        # Budget floors at 1000, so use content large enough to exceed it
        budget = ContextBudget(budget_chars=1000)
        budget.add(ContextBudget.P4_TRIM, "low", "x" * 800, source_path="low.md")
        budget.add(ContextBudget.P0_MUST, "high", "y" * 800, source_path="high.md")
        text, manifest = budget.assemble()
        assert "high" in text  # P0 always included (even if truncated)
        modes = {m["tag"]: m["mode"] for m in manifest}
        assert modes["high"] in ("full", "truncated")
        assert modes["low"] == "omitted"

    def test_exact_fit(self):
        """Section at exact boundary should fit as 'full'."""
        content = "a" * 100
        # Budget = header + XML tags overhead + content
        # Header is ~85 chars, XML tags ~25 chars, so budget needs to cover all
        budget = ContextBudget(budget_chars=5000)
        budget.add(ContextBudget.P1_HIGH, "sec", content, source_path="f.md")
        text, manifest = budget.assemble()
        assert manifest[0]["mode"] == "full"
        assert manifest[0]["chars_used"] == 100

    def test_summary_fallback(self):
        """When full content exceeds budget but summary fits, use summary."""
        # Budget floors at 1000; content must exceed 1000 so full doesn't fit
        budget = ContextBudget(budget_chars=1000)
        budget.add(ContextBudget.P2_MEDIUM, "big", "x" * 2000,
                   source_path="big.md", summary="short summary here")
        text, manifest = budget.assemble()
        assert manifest[0]["mode"] == "summary"
        assert "short summary here" in text
        assert "[Full content in big.md]" in text

    def test_p0_always_included(self):
        """Even with tiny budget, P0 section is truncated not omitted."""
        budget = ContextBudget(budget_chars=1000)  # floor
        budget.add(ContextBudget.P0_MUST, "critical", "z" * 5000, source_path="c.md")
        text, manifest = budget.assemble()
        assert manifest[0]["mode"] == "truncated"
        assert manifest[0]["chars_used"] > 0
        assert "[... truncated" in text

    def test_manifest_accuracy(self):
        """Manifest entries have correct chars_full, chars_used, mode."""
        budget = ContextBudget(budget_chars=10000)
        budget.add(ContextBudget.P1_HIGH, "a", "hello world", source_path="a.md")
        budget.add(ContextBudget.P2_MEDIUM, "b", "x" * 200, source_path="b.md")
        _, manifest = budget.assemble()
        assert len(manifest) == 2
        assert manifest[0]["tag"] == "a"  # P1 comes first
        assert manifest[0]["chars_full"] == 11
        assert manifest[0]["chars_used"] == 11
        assert manifest[0]["mode"] == "full"

    def test_empty_content_skipped(self):
        """Empty or whitespace-only content is not added."""
        budget = ContextBudget(budget_chars=10000)
        budget.add(ContextBudget.P0_MUST, "empty", "", source_path="e.md")
        budget.add(ContextBudget.P0_MUST, "spaces", "   \n  ", source_path="s.md")
        budget.add(ContextBudget.P1_HIGH, "real", "content", source_path="r.md")
        _, manifest = budget.assemble()
        assert len(manifest) == 1
        assert manifest[0]["tag"] == "real"

    def test_budget_floor(self):
        """Budget floors at 1000 even if 0 is passed."""
        budget = ContextBudget(budget_chars=0)
        budget.add(ContextBudget.P0_MUST, "x", "a" * 500, source_path="x.md")
        text, manifest = budget.assemble()
        assert manifest[0]["mode"] in ("full", "truncated")
        assert manifest[0]["chars_used"] > 0

    def test_multiple_p0_all_included(self):
        """Multiple P0 sections are all included (possibly truncated)."""
        budget = ContextBudget(budget_chars=1000)
        budget.add(ContextBudget.P0_MUST, "proj", "a" * 400, source_path="p.md")
        budget.add(ContextBudget.P0_MUST, "plan", "b" * 400, source_path="pl.md")
        text, manifest = budget.assemble()
        tags = [m["tag"] for m in manifest]
        assert "proj" in tags
        assert "plan" in tags
        # Both should be included (full or truncated), not omitted
        for m in manifest:
            assert m["mode"] != "omitted"

    def test_xml_structure(self):
        """Assembled text has proper XML tag wrapping."""
        budget = ContextBudget(budget_chars=10000)
        budget.add(ContextBudget.P1_HIGH, "my_section", "hello", source_path="f.md")
        text, _ = budget.assemble()
        assert "<my_section>" in text
        assert "</my_section>" in text
        assert "hello" in text
        assert "# Project Context" in text


class TestExtractSummary:

    def test_first_paragraph(self):
        """Extracts first substantive paragraph (must be >50 chars)."""
        text = ("The authentication module has three critical security vulnerabilities "
                "that need immediate attention.\n\nMore details about each issue follow below.")
        result = _extract_summary(text)
        assert result.startswith("The authentication module")
        assert "More details" not in result

    def test_skips_preamble(self):
        """Skips common LLM preamble patterns (paragraphs must be >50 chars)."""
        text = ("I'll analyze the codebase now and look for any potential issues that stand out.\n\n"
                "The authentication module has three critical security issues that need fixing.")
        result = _extract_summary(text)
        assert "authentication module" in result
        assert "I'll analyze" not in result

    def test_skips_multiple_preambles(self):
        """Skips multiple preamble paragraphs."""
        text = "Sure, let me help.\n\nI'll look into this.\n\nThe database layer uses connection pooling."
        result = _extract_summary(text)
        assert "database layer" in result

    def test_short_text(self):
        """Text shorter than max_chars returned as-is."""
        text = "Short result."
        assert _extract_summary(text, max_chars=500) == "Short result."

    def test_empty_returns_empty(self):
        assert _extract_summary("") == ""
        assert _extract_summary(None) == ""  # type: ignore

    def test_truncation_at_max_chars(self):
        """Long paragraphs truncated to max_chars."""
        text = "x" * 1000
        result = _extract_summary(text, max_chars=500)
        assert len(result) <= 500

    def test_fallback_to_preamble_if_only_option(self):
        """If all paragraphs are preamble, use first one > 50 chars."""
        text = "I'll analyze the complex authentication system in detail to find issues."
        result = _extract_summary(text)
        assert len(result) > 0  # Falls back to the preamble
