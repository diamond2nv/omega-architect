"""Tests for DEC (edit distance) + EA-GRPO reward + fixp@k metric."""

from __future__ import annotations

import pytest

from omega.search.dec import (
    FixPCandidate,
    FixPReport,
    compute_dec,
    compute_ea_grpo_reward,
    compute_fixp_candidates,
    levenshtein_lines,
)


class TestLevenshteinLines:
    """Tests for line-level Levenshtein distance."""

    def test_identical(self):
        """Identical code → DEC = 0."""
        code = "theorem t : True := by\n  trivial"
        assert levenshtein_lines(code, code) == 0.0

    def test_one_line_change(self):
        """One line changed → DEC = 1/N."""
        src = "theorem t : True := by\n  sorry"
        tgt = "theorem t : True := by\n  trivial"
        # 2 lines, 1 substitution → 1/2 = 0.5
        assert levenshtein_lines(src, tgt) == 0.5

    def test_insert_line(self):
        """Insert a line → DEC = 1/N."""
        src = "theorem t : 1 = 1 :="
        tgt = "theorem t : 1 = 1 :=\n  rfl"
        # 1 line → 2 lines: insertion → 1/1 = 1.0
        assert levenshtein_lines(src, tgt) == 1.0

    def test_delete_line(self):
        """Delete a line → DEC = 1/N."""
        src = "theorem t : 1 = 1 :=\n  rfl\n  done"
        tgt = "theorem t : 1 = 1 :=\n  rfl"
        # 3 lines → 2 lines: deletion → 1/3 ≈ 0.333
        assert levenshtein_lines(src, tgt) == pytest.approx(1.0 / 3.0)

    def test_empty_source(self):
        """Empty source → DEC = 0."""
        assert levenshtein_lines("", "some code") == 0.0

    def test_empty_both(self):
        """Both empty → DEC = 0."""
        assert levenshtein_lines("", "") == 0.0

    def test_large_change(self):
        """Completely different code — DEC can exceed 1.0 for insertions."""
        src = "theorem t1 : True := trivial"
        tgt = "theorem t2 (n : ℕ) : n = n := by\n  rfl"
        result = levenshtein_lines(src, tgt)
        assert result >= 0.0  # valid range (upper bound not guaranteed)


class TestComputeDec:
    """Tests for the compute_dec alias."""

    def test_alias(self):
        assert compute_dec("a\nb", "a\nc") == 0.5


class TestComputeEaGrpoReward:
    """Tests for EA-GRPO reward computation."""

    def test_all_correct_no_penalty(self):
        """All candidates correct and identical → no penalty."""
        code = "theorem t : True := trivial"
        rewards = compute_ea_grpo_reward(
            candidate_codes=[code, code, code],
            source_code=code,
            compile_results=[True, True, True],
            group_accuracy_threshold=0.5,
            edit_penalty_beta=0.3,
        )
        # All correct, DEC=0 → z = -mean/std = negative → sigmoid ≈ 0 → reward ≈ 1.0
        for r in rewards:
            assert r == pytest.approx(1.0, abs=0.01)

    def test_all_incorrect(self):
        """All incorrect → reward = 0 for all."""
        code = "theorem t : True := trivial"
        rewards = compute_ea_grpo_reward(
            candidate_codes=[code, code],
            source_code=code,
            compile_results=[False, False],
        )
        assert rewards == [0.0, 0.0]

    def test_mixed_correctness_penalty_applied(self):
        """Some correct with varying edit costs → penalty varies."""
        src = "theorem t : True := by\n  sorry"
        # Candidate 1: minimal fix (1 line change)
        minimal = "theorem t : True := by\n  trivial"
        # Candidate 2: larger edit (rewrites whole thing)
        larger = "theorem t : True :=\n  by\n    trivial"
        # Candidate 3: incorrect
        wrong = "theorem t : False := by\n  trivial"

        rewards = compute_ea_grpo_reward(
            candidate_codes=[minimal, larger, wrong],
            source_code=src,
            compile_results=[True, True, False],
            group_accuracy_threshold=0.3,  # low threshold → penalty active
            edit_penalty_beta=0.3,
        )

        # Wrong → 0
        assert rewards[2] == 0.0
        # Both correct → positive reward, but minimal should be higher
        assert rewards[0] > rewards[1], (
            f"Minimal edit should have higher reward: "
            f"{rewards[0]:.4f} vs {rewards[1]:.4f}"
        )

    def test_penalty_inactive_low_accuracy(self):
        """Low group accuracy (< threshold) → no penalty."""
        src = "theorem t : True := by\n  sorry"
        code = "theorem t : True := by\n  trivial"

        # Accuracy = 1/4 = 0.25, threshold = 0.5 → penalty inactive
        rewards = compute_ea_grpo_reward(
            candidate_codes=[code, code, code, "wrong"],
            source_code=src,
            compile_results=[True, True, True, False],
            group_accuracy_threshold=0.5,
            edit_penalty_beta=0.3,
        )

        # All correct candidates should get ≈1.0 (no penalty)
        for r in rewards[:3]:
            assert r == pytest.approx(1.0, abs=0.01)

    def test_empty_input(self):
        """Empty candidates → empty rewards."""
        assert compute_ea_grpo_reward([], "", []) == []


class TestFixPMetric:
    """Tests for fixp@k computation."""

    def test_perfect_fix(self):
        """Golden fix at top → fixp@1 = 1."""
        src = "theorem t : 1 = 2 :="
        golden = "theorem t : 1 = 1 :="

        report = compute_fixp_candidates(
            source_code=src,
            candidates=[golden],
            compiler_results=[True],
            golden_fix=golden,
            k=1, p=1.0,
        )

        assert report.pass_at_k == 1.0
        assert report.fixp_at_k == 1.0  # DEC ratio <= 1.0 means precise

    def test_over_edit(self):
        """Over-edited fix (DEC ratio > p) → fixp@1 = 0."""
        src = "theorem t : 1 = 2 :="
        over_edit = "theorem t : 1 = 1 := by\n  -- completely rewritten\n  rfl"
        golden = "theorem t : 1 = 1 :="

        report = compute_fixp_candidates(
            source_code=src,
            candidates=[over_edit],
            compiler_results=[True],
            golden_fix=golden,
            k=1, p=1.0,
        )

        # pass@1 = 1 (compiles), fixp@1 = 0 (DEC ratio > 1.0)
        assert report.pass_at_k == 1.0
        assert report.fixp_at_k == 0.0

    def test_multiple_candidates(self):
        """Multiple candidates, fixp@k selects correct + precise ones."""
        src = "theorem t : 1 = 2 :="
        golden = "theorem t : 1 = 1 :="
        wrong = "theorem t : 2 = 2 :="

        report = compute_fixp_candidates(
            source_code=src,
            candidates=[golden, wrong],
            compiler_results=[True, False],
            golden_fix=golden,
            k=2, p=1.5,
        )

        assert report.pass_at_k == 1.0
        # k=2, and golden is correct + DEC ratio ≤ 1.5
        assert report.fixp_at_k == 1.0
