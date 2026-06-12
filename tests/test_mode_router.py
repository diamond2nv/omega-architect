"""Tests for ModeRouter — automatic search strategy selection.

Covers:
1. Difficulty estimation via NLP spectrum
2. Domain detection via regex
3. Strategy scoring (DFS/Beam/Hybrid)
4. Full routing decisions
5. Edge cases (empty theorem, unknown difficulty, multiple failures)
"""
from __future__ import annotations

import pytest

from omega.engine.router import (
    DifficultyLevel,
    ModeRouter,
    ResourceProfile,
    RoutingContext,
    RoutingDecision,
    detect_domain,
    estimate_difficulty,
    _score_dfs,
    _score_beam,
    _score_hybrid,
)
from omega.engine.difficulty_spectrum import DifficultySpectrum


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

_EASY_THEOREMS = [
    "theorem t : 1 + 1 = 2 := by",
    "theorem t : a + 0 = a := by",
    "theorem mathd_algebra_478 : (2 : ℝ)^3 = 8 := by",
]

_MEDIUM_THEOREMS = [
    "theorem add_comm (a b : ℕ) : a + b = b + a := by",
    "theorem t (h : a = b) (h2 : b = c) : a = c := by",
    "theorem triangle_ineq (a b : ℝ) : |a + b| ≤ |a| + |b| := by",
]

_HARD_THEOREMS = [
    "theorem imo_1959_p1 : ∀ n : ℕ, 21*n + 4 / 14*n + 3 は既約分数 := by",
    "theorem putnam_2000_a1 : ∀ (A : Set ℝ), A ≠ ∅ → A は有界 → sup A ∈ closure A := by",
    "theorem t (h : Prime p) (h2 : p ∣ a*b) : p ∣ a ∨ p ∣ b := by",
]


@pytest.fixture
def router() -> ModeRouter:
    return ModeRouter()


@pytest.fixture
def default_ctx() -> RoutingContext:
    return RoutingContext()


# ═══════════════════════════════════════════════════════════════════
# 1. Difficulty Estimation
# ═══════════════════════════════════════════════════════════════════


class TestDifficultyEstimation:
    """Difficulty Spectrum should correctly classify known theorems."""

    def test_easy_theorems(self):
        for thm in _EASY_THEOREMS:
            level = estimate_difficulty(thm)
            assert level in (DifficultyLevel.EASY, DifficultyLevel.UNKNOWN), (
                f"{thm[:50]} → expected EASY or UNKNOWN, got {level}"
            )

    def test_medium_theorems(self):
        """Medium theorems should be MEDIUM or EASY (boundary)."""
        for thm in _MEDIUM_THEOREMS:
            level = estimate_difficulty(thm)
            assert level in (DifficultyLevel.MEDIUM, DifficultyLevel.EASY), (
                f"{thm[:50]} → expected MEDIUM or EASY, got {level}"
            )

    def test_hard_theorems(self):
        """Hard theorems should be HARD or MEDIUM."""
        for thm in _HARD_THEOREMS:
            level = estimate_difficulty(thm)
            assert level in (DifficultyLevel.HARD, DifficultyLevel.MEDIUM, DifficultyLevel.UNKNOWN), (
                f"{thm[:50]} → expected HARD/MEDIUM/UNKNOWN, got {level}"
            )

    def test_hard_is_not_easy(self):
        for thm in _HARD_THEOREMS:
            level = estimate_difficulty(thm)
            assert level != DifficultyLevel.EASY, (
                f"{thm[:50]} should NOT be classified as EASY"
            )

    def test_unknown_empty_string(self):
        level = estimate_difficulty("")
        assert level == DifficultyLevel.UNKNOWN

    def test_unknown_gibberish(self):
        level = estimate_difficulty("xyzabc qwerty 12345 !@#$%")
        assert level in (DifficultyLevel.UNKNOWN, DifficultyLevel.EASY)


# ═══════════════════════════════════════════════════════════════════
# 2. Domain Detection
# ═══════════════════════════════════════════════════════════════════


class TestDomainDetection:
    """Regex-based domain detection should identify mathematical domains."""

    def test_detect_algebra(self):
        dom = detect_domain("theorem t (a b : ℕ) : a + b = b + a := by")
        assert dom == "algebra"

    def test_detect_number_theory(self):
        dom = detect_domain("theorem t (h : Prime p) (h2 : p ∣ a*b) : p ∣ a := by")
        assert dom == "number_theory"

    def test_detect_combinatorics(self):
        dom = detect_domain("theorem rust_petersen_count : Nat.choose 5 2 = 10 := by")
        assert dom == "combinatorics", f"Expected combinatorics, got {dom}"

    def test_detect_analysis(self):
        dom = detect_domain("theorem t (h : a ≤ b) (h2 : x + ε ≤ y) : Real.sup s := by")
        assert dom == "analysis"

    def test_unknown_domain(self):
        dom = detect_domain("theorem t : True := by trivial")
        assert dom is None

    def test_mixed_domain_prefers_first_match(self):
        dom = detect_domain("theorem t : Prime p ∧ a + b = b + a := by")
        assert dom is not None  # Should match something


# ═══════════════════════════════════════════════════════════════════
# 3. Strategy Scoring (Unit Tests)
# ═══════════════════════════════════════════════════════════════════


class TestStrategyScoring:
    """Individual strategy scoring functions should return 0-10 range."""

    def test_dfs_baseline(self):
        ctx = RoutingContext(resource_profile=ResourceProfile.LOCAL_GPU)
        score = _score_dfs(DifficultyLevel.UNKNOWN, ctx)
        assert 0.0 <= score <= 10.0
        assert score == 5.0  # default baseline (no API, unknown difficulty)

    def test_dfs_easy_boost(self):
        ctx = RoutingContext(resource_profile=ResourceProfile.API_ONLY)
        score = _score_dfs(DifficultyLevel.EASY, ctx)
        assert score > 5.0  # easy + API = boost
        assert score == 10.0  # 5 + 3 (easy) + 2 (API)

    def test_dfs_penalty_failures(self):
        ctx = RoutingContext(previous_failures=3)
        score = _score_dfs(DifficultyLevel.UNKNOWN, ctx)
        assert score < 5.0  # failures penalize

    def test_dfs_penalty_stuck(self):
        ctx = RoutingContext(history_hint="stuck")
        score = _score_dfs(DifficultyLevel.UNKNOWN, ctx)
        assert score < 5.0  # stuck = strong negative

    def test_beam_baseline(self):
        ctx = RoutingContext()
        score = _score_beam(DifficultyLevel.UNKNOWN, ctx)
        assert 0.0 <= score <= 10.0

    def test_beam_local_gpu_boost(self):
        ctx = RoutingContext(resource_profile=ResourceProfile.LOCAL_GPU)
        score = _score_beam(DifficultyLevel.EASY, ctx)
        # Local GPU + easy → beam is attractive
        assert score > 3.0

    def test_beam_budget_low(self):
        ctx = RoutingContext(
            budget_remaining_usd=0.05,
            resource_profile=ResourceProfile.LOCAL_GPU,
        )
        score = _score_beam(DifficultyLevel.UNKNOWN, ctx)
        assert score > 3.0  # cheap local = attractive when budget low

    def test_hybrid_baseline(self):
        ctx = RoutingContext()
        score = _score_hybrid(DifficultyLevel.UNKNOWN, ctx)
        assert 0.0 <= score <= 10.0

    def test_hybrid_hard_boost(self):
        ctx = RoutingContext()
        score = _score_hybrid(DifficultyLevel.HARD, ctx)
        assert score > 2.0  # hard = boost

    def test_hybrid_previous_failures_strong(self):
        ctx = RoutingContext(previous_failures=3)
        score = _score_hybrid(DifficultyLevel.HARD, ctx)
        assert score > 5.0  # multiple failures + hard = strong signal

    def test_hybrid_stuck_strong(self):
        ctx = RoutingContext(history_hint="diverging")
        score = _score_hybrid(DifficultyLevel.UNKNOWN, ctx)
        assert score > 2.0  # stuck = strongest signal

    def test_hybrid_low_budget_penalty(self):
        ctx = RoutingContext(budget_remaining_usd=0.01)
        score = _score_hybrid(DifficultyLevel.HARD, ctx, hybrid_budget_threshold=0.30)
        assert score < 5.0  # low budget penalizes expensive hybrid


# ═══════════════════════════════════════════════════════════════════
# 4. Full Routing Decisions
# ═══════════════════════════════════════════════════════════════════


class TestModeRouter:
    """Integrated ModeRouter decisions from context."""

    def test_easy_route_dfs(self, router):
        """Easy theorems with API available should route to DFS."""
        ctx = RoutingContext(
            theorem_header=_EASY_THEOREMS[0],
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(ctx)
        assert decision.strategy_name == "dfs", (
            f"Easy + API → expected dfs, got {decision.strategy_name} ({decision.reasoning})"
        )
        assert decision.confidence > 0.3
        assert decision.scores["dfs"] > decision.scores["hybrid"]

    def test_hard_with_failures_routes_hybrid(self, router):
        """Hard theorems with 2+ failures should route to Hybrid."""
        ctx = RoutingContext(
            theorem_header=_HARD_THEOREMS[0],
            previous_failures=2,
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(ctx)
        assert decision.strategy_name == "hybrid", (
            f"Hard + failures → expected hybrid, got {decision.strategy_name} ({decision.reasoning})"
        )
        assert decision.scores["hybrid"] > decision.scores["dfs"]

    def test_stuck_history_routes_hybrid(self, router):
        """Stuck history is the strongest signal for Hybrid."""
        ctx = RoutingContext(
            theorem_header=_HARD_THEOREMS[0],
            history_hint="stuck",
            previous_failures=2,
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(ctx)
        assert decision.strategy_name == "hybrid", (
            f"Hard + stuck + failures → expected hybrid, got {decision.strategy_name} ({decision.reasoning})"
        )

    def test_beam_when_local_gpu_easy(self, router):
        """Easy + local GPU → Beam is competitive."""
        ctx = RoutingContext(
            theorem_header=_EASY_THEOREMS[0],
            resource_profile=ResourceProfile.LOCAL_GPU,
            budget_remaining_usd=0.05,
        )
        decision = router.route(ctx)
        assert decision.strategy_name in ("dfs", "beam"), (
            f"Easy + local GPU + low budget → expected dfs or beam, got {decision.strategy_name}"
        )
        # Beam should score well
        assert decision.scores["beam"] > 1.0

    def test_unknown_rides_dfs(self, router):
        """Unknown theorems should default to DFS."""
        ctx = RoutingContext(
            theorem_header="theorem foo (x : Type) : Nonempty x := by",
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(ctx)
        assert decision.strategy_name == "dfs", (
            f"Unknown → expected dfs, got {decision.strategy_name}"
        )

    def test_routing_context_preserves_domain(self, router):
        """Domain should be detected and stored in context."""
        ctx = RoutingContext(
            theorem_header="theorem t (h : Prime p) : p ∣ a := by",
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(ctx)
        assert ctx.domain is not None
        assert "number_theory" in ctx.domain or "number" in ctx.domain.lower()

    def test_decision_has_reasoning(self, router):
        ctx = RoutingContext(theorem_header=_EASY_THEOREMS[0])
        decision = router.route(ctx)
        assert decision.reasoning and len(decision.reasoning) > 10
        assert "DFS" in decision.reasoning or "Hybrid" in decision.reasoning or "Beam" in decision.reasoning

    def test_decision_has_scores(self, router):
        ctx = RoutingContext(theorem_header=_EASY_THEOREMS[0])
        decision = router.route(ctx)
        assert "dfs" in decision.scores
        assert "beam" in decision.scores
        assert "hybrid" in decision.scores
        assert all(0.0 <= v <= 10.0 for v in decision.scores.values())

    def test_re_routes_consistently(self, router):
        """Same context should produce same decision."""
        ctx = RoutingContext(
            theorem_header=_MEDIUM_THEOREMS[0],
            resource_profile=ResourceProfile.API_ONLY,
        )
        d1 = router.route(ctx)
        d2 = router.route(RoutingContext(
            theorem_header=_MEDIUM_THEOREMS[0],
            resource_profile=ResourceProfile.API_ONLY,
        ))
        assert d1.strategy_name == d2.strategy_name


# ═══════════════════════════════════════════════════════════════════
# 5. Edge Cases
# ═══════════════════════════════════════════════════════════════════


class TestModeRouterEdgeCases:
    """Boundary conditions and edge cases."""

    def test_empty_theorem_header(self, router):
        ctx = RoutingContext(theorem_header="")
        decision = router.route(ctx)
        assert decision.strategy_name == "dfs"  # should default to DFS
        assert decision.scores["dfs"] >= 0

    def test_really_high_budget(self, router):
        """High budget should not penalize Hybrid."""
        ctx = RoutingContext(
            theorem_header=_HARD_THEOREMS[0],
            budget_remaining_usd=100.0,
            previous_failures=3,
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(ctx)
        assert decision.scores["hybrid"] > decision.scores["beam"]

    def test_zero_previous_failures_no_bias(self, router):
        ctx = RoutingContext(theorem_header=_EASY_THEOREMS[0], previous_failures=0)
        decision = router.route(ctx)
        assert decision.scores["dfs"] >= decision.scores["hybrid"]

    def test_hybrid_not_preferred_when_no_api(self, router):
        """Hybrid needs API — without it, DFS or Beam should beat it."""
        ctx = RoutingContext(
            theorem_header=_MEDIUM_THEOREMS[0],
            resource_profile=ResourceProfile.LOCAL_GPU,
            budget_remaining_usd=0.50,
        )
        decision = router.route(ctx)
        assert decision.scores["hybrid"] <= decision.scores["dfs"] + 0.1  # close but not dominant

    def test_domain_detection_adds_context(self, router):
        """Domain should be included in the reasoning."""
        ctx = RoutingContext(theorem_header=_HARD_THEOREMS[0], previous_failures=2)
        decision = router.route(ctx)
        assert decision.reasoning is not None
        assert "domain" in decision.reasoning or ctx.domain is not None


# ═══════════════════════════════════════════════════════════════════
# 6. Difficulty Spectrum Module Test
# ═══════════════════════════════════════════════════════════════════


class TestDifficultySpectrum:
    """Direct tests for DifficultySpectrum module."""

    def test_spectrum_estimates_easy(self):
        spec = DifficultySpectrum()
        result = spec.estimate("theorem t : 1 + 1 = 2 := by")
        assert result.level == "EASY" or result.level == "UNKNOWN"
        assert result.confidence >= 0.0

    def test_spectrum_estimates_hard(self):
        spec = DifficultySpectrum()
        result = spec.estimate("theorem imo_1992_p1 : ∀ a b c : ℕ, a^3 + b^3 + c^3 = 3abc → a + b + c = 0 := by")
        assert result.level == "HARD" or result.level == "UNKNOWN"
        assert result.confidence >= 0.0

    def test_spectrum_consistency(self):
        spec = DifficultySpectrum()
        r1 = spec.estimate("theorem t : 1 + 1 = 2 := by")
        r2 = spec.estimate("theorem t : 1 + 1 = 2 := by")
        assert r1.level == r2.level
