"""Tests for Proposer — tactic generation primitives, error classifier, and cache integration.

Verifies the 8 AGENTS.md primitives, Lean error classification,
and ProofCache wrapping in GoedelProver.
"""

from omega.search.proposer import (
    Proposer,
    TacticSuggestion,
    GoalState,
    analyze_theorem_pattern,
    apply_lemma_suggestions,
    rewrite_goal_suggestions,
    suggest_trivial_tactics,
    error_based_suggestions,
    default_proposer,
)
from omega.search.error_classifier import LeanErrorClassifier, ErrorCategory


# ── Helpers ─────────────────────────────────────────────────────


def _goal(target: str, hypotheses: list[str] | None = None) -> GoalState:
    """Create a minimal GoalState for testing."""
    return GoalState(
        goal_text=f"theorem t : {target} :=",
        target_type=target,
        hypotheses=hypotheses or [],
    )


# ── Theorem Pattern Analysis ───────────────────────────────────


class TestAnalyzePattern:
    def test_trivial_true(self):
        r = analyze_theorem_pattern("theorem t : True :=")
        assert r["strategy"] == "trivial"
        assert r["confidence"] >= 0.8

    def test_rfl_reflexive(self):
        r = analyze_theorem_pattern("theorem t (a : ℕ) : a = a :=")
        assert r["strategy"] == "rfl"
        assert r["confidence"] >= 0.9

    def test_induction_nat(self):
        r = analyze_theorem_pattern("theorem add_zero (n : ℕ) : n + 0 = n :=")
        assert r["strategy"] == "induction"
        assert r["confidence"] >= 0.6

    def test_calc_chain(self):
        r = analyze_theorem_pattern("theorem chain (a b c : Type) : a = b → b = c → a = c :=")
        assert r["strategy"] in ("calc", "simp")

    def test_conjunction(self):
        r = analyze_theorem_pattern("theorem and_comm : True ∧ False :=")
        assert r["strategy"] == "conjunction"


# ── apply_lemma (Primitive 1) ──────────────────────────────────


class TestApplyLemma:
    def test_conjunction_goal(self):
        goal = _goal("True ∧ False")
        suggestions = apply_lemma_suggestions(goal)
        assert any("constructor" in s.tactic for s in suggestions)

    def test_disjunction_goal(self):
        goal = _goal("True ∨ False")
        suggestions = apply_lemma_suggestions(goal)
        tactics = {s.tactic for s in suggestions}
        assert "left" in tactics or "right" in tactics

    def test_existential_goal(self):
        goal = _goal("∃ x : ℕ, x = 0")
        suggestions = apply_lemma_suggestions(goal)
        assert any("use" in s.tactic for s in suggestions)

    def test_negation_goal(self):
        goal = _goal("¬ True")
        suggestions = apply_lemma_suggestions(goal)
        assert any("intro" in s.tactic for s in suggestions)

    def test_implication_goal(self):
        goal = _goal("True → False")
        suggestions = apply_lemma_suggestions(goal)
        assert any("intro" in s.tactic for s in suggestions)

    def test_ordering_goal(self):
        goal = _goal("n ≤ m")
        suggestions = apply_lemma_suggestions(goal)
        assert any("omega" in s.tactic for s in suggestions)

    def test_empty_goal(self):
        suggestions = apply_lemma_suggestions(_goal(""))
        assert len(suggestions) == 0

    def test_returns_max_4(self):
        goal = _goal("True ∨ False ∧ ¬ True → False ≤ 0")
        suggestions = apply_lemma_suggestions(goal)
        assert len(suggestions) <= 4


# ── rewrite_goal (Primitive 2) ─────────────────────────────────


class TestRewriteGoal:
    def test_reflexive_equality(self):
        goal = _goal("a = a")
        suggestions = rewrite_goal_suggestions(goal)
        assert any(s.tactic == "rfl" for s in suggestions)
        assert any(s.is_complete for s in suggestions)

    def test_arithmetic_equality(self):
        goal = _goal("a + b = b + a")
        suggestions = rewrite_goal_suggestions(goal)
        tactics = {s.tactic for s in suggestions}
        assert "simp" in tactics or "omega" in tactics

    def test_calc_chain(self):
        goal = _goal("a = b = c")
        suggestions = rewrite_goal_suggestions(goal)
        assert any("calc" in s.tactic for s in suggestions)

    def test_function_application(self):
        goal = _goal("f (x) = g (x)")
        suggestions = rewrite_goal_suggestions(goal)
        assert any("simp" in s.tactic for s in suggestions)

    def test_no_equality(self):
        suggestions = rewrite_goal_suggestions(_goal("True"))
        assert len(suggestions) == 0

    def test_returns_max_3(self):
        goal = _goal("a + b + c = d + e + f")
        suggestions = rewrite_goal_suggestions(goal)
        assert len(suggestions) <= 3

    def test_same_side_reflexive(self):
        goal = _goal("a + b = a + b")
        suggestions = rewrite_goal_suggestions(goal)
        assert any(s.tactic == "rfl" for s in suggestions)


# ── Error Classifier ────────────────────────────────────────────


class TestErrorClassifier:
    def test_syntax_error(self):
        cls = LeanErrorClassifier()
        r = cls.classify("syntax error: unexpected token")
        assert r.category == ErrorCategory.SYNTAX_ERROR

    def test_type_error(self):
        cls = LeanErrorClassifier()
        r = cls.classify("type mismatch: expected ℕ, got Bool")
        assert r.category == ErrorCategory.TYPE_ERROR

    def test_unsolved_goal(self):
        cls = LeanErrorClassifier()
        r = cls.classify("unsolved goals: n + 0 = n")
        assert r.category == ErrorCategory.UNSOLVED_GOAL

    def test_missing_lemma(self):
        cls = LeanErrorClassifier()
        r = cls.classify("unknown identifier: add_comm")
        assert r.category == ErrorCategory.MISSING_LEMMA

    def test_tactic_error(self):
        cls = LeanErrorClassifier()
        r = cls.classify("tactic 'omega' failed")
        assert r.category == ErrorCategory.TACTIC_ERROR
        assert "omega" in r.detail

    def test_timeout(self):
        cls = LeanErrorClassifier()
        r = cls.classify("maximum number of heartbeats reached")
        assert r.category == ErrorCategory.TIMEOUT

    def test_unknown_module(self):
        cls = LeanErrorClassifier()
        r = cls.classify("unknown module 'FooBar'")
        assert r.category == ErrorCategory.UNKNOWN_MODULE

    def test_empty_message(self):
        cls = LeanErrorClassifier()
        r = cls.classify("")
        assert r.category == ErrorCategory.UNKNOWN_ERROR

    def test_one_liner_detail(self):
        cls = LeanErrorClassifier()
        r = cls.classify("tactic 'simp' failed, goal not simplified")
        assert r.category == ErrorCategory.TACTIC_ERROR
        assert len(r.detail) > 0

    def test_classify_many(self):
        cls = LeanErrorClassifier()
        errs = [
            "syntax error at line 5",
            "unsolved goals: n = 0",
            "unknown identifier: foobar",
        ]
        groups = cls.classify_many(errs)
        assert ErrorCategory.SYNTAX_ERROR in groups
        assert ErrorCategory.UNSOLVED_GOAL in groups
        assert ErrorCategory.MISSING_LEMMA in groups
        assert len(groups[ErrorCategory.SYNTAX_ERROR]) == 1

    def test_suggestion_tactics_unsolved(self):
        tactics = LeanErrorClassifier.suggestion_tactics(ErrorCategory.UNSOLVED_GOAL)
        assert len(tactics) >= 4
        names = {t[0] for t in tactics}
        assert "omega" in names
        assert "simp" in names

    def test_suggestion_tactics_excludes_tried(self):
        tactics = LeanErrorClassifier.suggestion_tactics(
            ErrorCategory.UNSOLVED_GOAL,
            tried_tactics={"omega", "simp"},
        )
        names = {t[0] for t in tactics}
        assert "omega" not in names
        assert "simp" not in names

    def test_strategy_for_category(self):
        strategy = LeanErrorClassifier.strategy(ErrorCategory.UNSOLVED_GOAL)
        assert "omega" in strategy or "simp" in strategy or "arith" in strategy


# ── error_based_suggestions (with classifier integration) ──────


class TestErrorBasedSuggestions:
    def test_empty_errors_returns_empty(self):
        suggestions = error_based_suggestions(_goal("True"), [])
        assert len(suggestions) == 0

    def test_unsolved_goal_suggests_alternatives(self):
        suggestions = error_based_suggestions(
            _goal("n + 0 = n"),
            ["unsolved goals: n + 0 = n"],
        )
        assert len(suggestions) > 0
        assert any(s.confidence > 0 for s in suggestions)

    def test_limited_to_8_suggestions(self):
        many_errors = ["unsolved goals: goal"] * 20
        suggestions = error_based_suggestions(_goal("n + 0 = n"), many_errors)
        assert len(suggestions) <= 8


# ── suggest_trivial_tactics ────────────────────────────────────


class TestSuggestTrivial:
    def test_trivial_for_true(self):
        suggestions = suggest_trivial_tactics(_goal("True"))
        assert any("trivial" in s.tactic for s in suggestions)

    def test_rfl_for_reflexive(self):
        suggestions = suggest_trivial_tactics(_goal("a = a"))
        assert any("rfl" in s.tactic for s in suggestions)

    def test_induction_for_nat(self):
        goal = GoalState(goal_text="theorem t (n : ℕ) : n + 0 = n :=", target_type="n + 0 = n")
        suggestions = suggest_trivial_tactics(goal)
        assert any("induction" in s.tactic for s in suggestions)

    def test_skips_tried_tactics(self):
        suggestions = suggest_trivial_tactics(
            _goal("a = a"),
            previous_errors=["tactic 'rfl' failed"],
        )
        assert not any("rfl" in s.tactic for s in suggestions)


# ── default_proposer (full pipeline) ────────────────────────────


class TestDefaultProposer:
    def test_true_goal(self):
        suggestions = default_proposer(_goal("True"), [], {})
        assert len(suggestions) > 0
        assert any(s.tactic == "trivial" for s in suggestions)

    def test_equality_goal(self):
        suggestions = default_proposer(_goal("a = a"), [], {})
        assert any("rfl" in s.tactic or "simp" in s.tactic for s in suggestions)

    def test_conjunction_goal(self):
        suggestions = default_proposer(_goal("True ∧ False"), [], {})
        assert any("constructor" in s.tactic for s in suggestions)

    def test_with_errors_includes_alternatives(self):
        suggestions = default_proposer(
            _goal("n + 0 = n"), [],
            {"previous_errors": ["tactic 'simp' failed"]},
        )
        assert len(suggestions) > 0

    def test_disjunction_goal(self):
        suggestions = default_proposer(_goal("True ∨ False"), [], {})
        tactics = {s.tactic for s in suggestions}
        assert "left" in tactics or "right" in tactics


# ── Proposer class ─────────────────────────────────────────────


class TestProposerClass:
    def test_goedel_strategy(self):
        p = Proposer(strategy="goedel", num_samples=3)
        assert p.strategy == "goedel"
        assert p.num_samples == 3

    def test_suggest_returns_list(self):
        p = Proposer(strategy="goedel", num_samples=1)
        suggestions = p.suggest(_goal("True"), [], **{"theorem_header": "theorem t : True :=", "round": 0})
        assert isinstance(suggestions, list)

    def test_suggest_no_llm_returns_templates(self):
        p = Proposer(strategy="goedel", generate_fn=None, num_samples=1)
        suggestions = p.suggest(
            _goal("True"), [],
            **{"theorem_header": "theorem t : True :=", "round": 0},
        )
        assert len(suggestions) > 0
        # Template tactics include analysis + trivial + rewrite + apply
        assert any(s.tactic in ("trivial", "rfl", "simp") for s in suggestions)

    def test_repr(self):
        p = Proposer(strategy="rethlas", num_samples=2)
        r = repr(p)
        assert "rethlas" in r
        assert "2" in r


# ── Cache Integration (via ProofCache) ─────────────────────────


class TestCacheIntegration:
    """Test that ProofCache integrates with GoedelProver correctly.

    These tests verify that:
    - Cache is accepted by GoedelProver.__init__
    - Cache parameters are forwarded to Proposer
    - Proposer stores cache/model_id attributes
    """

    def test_goedel_prover_accepts_cache(self):
        from omega.prover.go_prover import GoedelProver
        from omega.prover.cache import ProofCache

        cache = ProofCache()
        gp = GoedelProver(cache=cache)
        assert gp._cache is cache
        cache.close()

    def test_cache_passed_to_proposer(self):
        from omega.prover.go_prover import GoedelProver
        from omega.prover.cache import ProofCache

        cache = ProofCache()
        gp = GoedelProver(cache=cache)
        assert hasattr(gp.proposer, "_cache")
        assert gp.proposer._cache is cache
        cache.close()

    def test_proposer_has_model_id(self):
        from omega.prover.go_prover import GoedelProver

        gp = GoedelProver()
        assert hasattr(gp.proposer, "_model_id")
        assert gp.proposer._model_id == "deepseek/deepseek-v4-flash"

    def test_model_id_used_for_cache_key(self):
        from omega.prover.go_prover import GoedelProver
        from omega.prover.cache import ProofCache

        cache = ProofCache()
        key = cache.store(
            theorem_header="theorem t : True :=",
            model_id="deepseek/deepseek-v4-flash",
            llm_output="by trivial",
        )
        assert len(key) == 64  # SHA256 hex
        cache.close()

    def test_cache_hit_returns_same(self):
        from omega.prover.cache import ProofCache

        cache = ProofCache()
        cache.store(
            theorem_header="theorem t : True :=",
            model_id="deepseek/deepseek-v4-flash",
            llm_output="by trivial",
            compiled=True,
        )
        result = cache.lookup(
            theorem_header="theorem t : True :=",
            model_id="deepseek/deepseek-v4-flash",
        )
        assert result == "by trivial"
        cache.close()

    def test_cache_miss_for_different_theorem(self):
        from omega.prover.cache import ProofCache

        cache = ProofCache()
        cache.store(
            theorem_header="theorem a : True :=",
            model_id="deepseek/deepseek-v4-flash",
            llm_output="by trivial",
        )
        result = cache.lookup(
            theorem_header="theorem b : False :=",
            model_id="deepseek/deepseek-v4-flash",
        )
        assert result is None
        cache.close()
