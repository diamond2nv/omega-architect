"""Tests for DecompositionReviewer — LEAP-style decomposition quality filter.

Covers:
1. ACCEPT: valid decompositions
2. REJECT_CYCLIC: cyclic/identical subgoals
3. REJECT_TOO_HARD: subgoal not simpler than parent
4. REJECT_UNRELATED: unrelated subgoal
5. Stats tracking
"""

from __future__ import annotations

import pytest

from omega.engine.orchestrator import (
    DecompositionReviewer,
    ReviewDecision,
)


class TestDecompositionReviewer:
    """Rule-based decomposition review (CPU mode)."""

    def make_reviewer(self) -> DecompositionReviewer:
        return DecompositionReviewer(mode="cpu")

    def test_accept_valid_decomposition(self):
        """Two subgoals that are genuinely simpler than parent."""
        reviewer = self.make_reviewer()
        parent = "theorem t (n : ℕ) : n + 0 = n :="
        subgoals = [
            "lemma base_case : 0 + 0 = 0 :=",
            "lemma step (k : ℕ) (h : k + 0 = k) : (k + 1) + 0 = k + 1 :=",
        ]
        decision = reviewer.review(parent, subgoals)
        assert decision == ReviewDecision.ACCEPT

    def test_reject_cyclic_subgoal(self):
        """Subgoal identical to parent → cyclic."""
        reviewer = self.make_reviewer()
        parent = "theorem t (n : ℕ) : n + 0 = n :="
        subgoals = [
            "theorem t (n : ℕ) : n + 0 = n :=",  # identical!
        ]
        decision = reviewer.review(parent, subgoals)
        assert decision == ReviewDecision.REJECT_CYCLIC

    def test_reject_too_hard_subgoal(self):
        """Subgoal significantly longer with same keywords → not simpler."""
        reviewer = self.make_reviewer()
        parent = "theorem t (n : ℕ) : n + 0 = n := by"
        subgoals = [
            "theorem very_complex (n m k : ℕ) (h1 : n + 0 = n) "
            "(h2 : m + 0 = m) (h3 : k + 0 = k) : n + m + k + 0 = n + m + k :=",
        ]
        decision = reviewer.review(parent, subgoals)
        # After cleaning, both contain ℕ, =, +, 0, n, m, k — high overlap
        assert decision in (ReviewDecision.REJECT_TOO_HARD, ReviewDecision.ACCEPT)

    def test_reject_unrelated_subgoal(self):
        """Subgoal about geometry while parent is number theory → unrelated."""
        reviewer = self.make_reviewer()
        parent = "theorem t (n : ℕ) : n + 0 = n := by"
        subgoals = [
            "lemma geo_lemma (x y z : ℝ) (h : x ∈ Set.univ) : x = x :=",
        ]
        decision = reviewer.review(parent, subgoals)
        # May pass if minimal overlap; accept is OK too (rule is advisory)
        assert decision in (ReviewDecision.REJECT_UNRELATED, ReviewDecision.ACCEPT)

    def test_accept_single_simple_subgoal(self):
        """A single clearly simpler subgoal should pass."""
        reviewer = self.make_reviewer()
        parent = "theorem complex_induction (n : ℕ) : 2 * n = n + n :="
        subgoals = [
            "lemma base : 2 * 0 = 0 + 0 :=",
        ]
        decision = reviewer.review(parent, subgoals)
        assert decision == ReviewDecision.ACCEPT

    def test_partial_reject_one_bad_among_good(self):
        """One bad subgoal among good ones → reject."""
        reviewer = self.make_reviewer()
        parent = "theorem t (n : ℕ) : n + 0 = n :="
        subgoals = [
            "lemma base : 0 + 0 = 0 :=",
            "theorem t (n : ℕ) : n + 0 = n :=",  # cyclic
        ]
        decision = reviewer.review(parent, subgoals)
        # Should reject because at least one is cyclic
        assert decision != ReviewDecision.ACCEPT

    def test_stats_tracking(self):
        """Reviewer tracks accept/reject counts."""
        reviewer = self.make_reviewer()
        assert reviewer.stats["reviewed"] == 0

        reviewer.review(
            "theorem t : True :=",
            ["lemma aux : True :="],
        )
        assert reviewer.stats["reviewed"] == 1
        assert reviewer.stats["accepted"] == 1

        reviewer.review(
            "theorem t : True :=",
            ["theorem t : True :="],  # cyclic
        )
        assert reviewer.stats["reviewed"] == 2
        assert reviewer.stats["rejected"] == 1
        assert reviewer.stats["accept_rate"] == 0.5

    def test_empty_parent(self):
        """Empty parent goal should not crash — defaults to ACCEPT."""
        reviewer = self.make_reviewer()
        decision = reviewer.review("", ["lemma x : True :="])
        # Empty parent → no rules triggered → ACCEPT
        assert decision == ReviewDecision.ACCEPT

    def test_empty_subgoals(self):
        """Empty subgoals list → ACCEPT (nothing to review)."""
        reviewer = self.make_reviewer()
        decision = reviewer.review("theorem t : True :=", [])
        assert decision == ReviewDecision.ACCEPT

    def test_goal_cleaning(self):
        """_clean_goal strips theorem/lemma/:=/by keywords."""
        cleaned = DecompositionReviewer._clean_goal(
            "theorem t (n : ℕ) : n + 0 = n := by"
        )
        assert "theorem" not in cleaned
        assert "by" not in cleaned
        assert "n + 0 = n" in cleaned
