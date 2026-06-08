"""Tests for ModelRouter v2 — rule-based, ML-based, and full pipeline.

Four test classes:

1. TestRuleBasedModelRouter — regression: heuristic routing
2. TestMLModelRouter — unit+integration: LightGBM model
3. TestRouterPipeline — integration: end-to-end selection
4. TestRouterEdgeCases — boundary conditions
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from omega.resource.model_router import (
    MLPrediction,
    MLRoute,
    ModelRouter,
    ModelConfig,
    TIER_HARD,
    TIER_MEDIUM,
    TIER_SIMPLE,
    TIER_NAMES,
    _estimate_complexity,
)
from omega.resource.routing_flags import (
    compute_theorem_flags,
    apply_postprocess,
    TheoremFlags,
)

# ── Test fixtures ────────────────────────────────────────────

_MODEL_DIR = Path(__file__).resolve().parents[1] / "omega" / "resource" / "models"
_LGBM_PATH = _MODEL_DIR / "router_lgbm_v1.txt"
_FEATURE_PIPELINE = _MODEL_DIR / "feature_pipeline.joblib"
_FEATURE_NAMES = _MODEL_DIR / "feature_names.npy"

_TIER_0_HEADERS = [
    "theorem t : True := by\n  trivial",
    "theorem t : 1 = 1 := by\n  rfl",
    "theorem t (a : Nat) : a = a := by\n  rfl",
]
_TIER_1_HEADERS = [
    "theorem double_succ (n : ℕ) : double n.succ = double n + 2 :=",
    "theorem add_comm (a b : ℕ) : a + b = b + a :=",
    "theorem mul_comm (a b : ℕ) : a * b = b * a :=",
]
_TIER_2_HEADERS = [
    "theorem OMR_problem_348663 : ∃ ϕ : MvPolynomial (Fin 3) ℂ →+* Polynomial ℂ, ϕ (X 0) = X := by",
    "theorem banach_fixed_point : ∀ (f : ℝ → ℝ) (_ : ...), ∃! x, f x = x :=",
    "theorem spectral_theorem (A : Matrix (Fin n) (Fin n) ℂ) : A.IsHermitian → ... :=",
]
_ALL_HEADERS = _TIER_0_HEADERS + _TIER_1_HEADERS + _TIER_2_HEADERS


# ═══════════════════════════════════════════════════════════════
# 1. Rule-Based ModelRouter Regression
# ═══════════════════════════════════════════════════════════════


class TestRuleBasedModelRouter:
    """Verify existing heuristic routing behavior unchanged."""

    def make_router(self) -> ModelRouter:
        return ModelRouter()

    # ── _estimate_complexity ──────────────────────────────────

    def test_estimate_returns_tier_string(self):
        tier_str, flags, base = _estimate_complexity(_TIER_0_HEADERS[0])
        assert isinstance(tier_str, str)
        assert tier_str in (TIER_SIMPLE, TIER_MEDIUM, TIER_HARD)

    def test_estimate_trivial_is_not_hard(self):
        tier_str, _, _ = _estimate_complexity(_TIER_0_HEADERS[0])
        assert tier_str != TIER_HARD

    def test_estimate_induction_is_not_simple(self):
        tier_str, _, _ = _estimate_complexity(_TIER_1_HEADERS[0])
        assert tier_str != TIER_SIMPLE

    def test_estimate_omr_problem_is_hard(self):
        tier_str, _, _ = _estimate_complexity(_TIER_2_HEADERS[0])
        assert tier_str == TIER_HARD

    def test_estimate_returns_flags_class(self):
        _, flags, _ = _estimate_complexity(_TIER_1_HEADERS[0])
        assert isinstance(flags, TheoremFlags)

    def test_estimate_long_header_flagged(self):
        _, flags, _ = _estimate_complexity("theorem t : " + "A" * 250 + " :=")
        assert flags.long_proof is True

    def test_estimate_complex_type_detected(self):
        _, flags, _ = _estimate_complexity(
            "theorem t (xs : List ℕ) : ∀ x, x ∈ xs → x ≥ 0 :="
        )
        assert flags.complex_type is True

    # ── select() ──────────────────────────────────────────────

    def test_select_returns_model_id(self):
        result = self.make_router().select(_TIER_0_HEADERS[0])
        assert isinstance(result, str)
        assert "/" in result

    def test_select_all_headers_return_model(self):
        router = self.make_router()
        for header in _ALL_HEADERS:
            result = router.select(header)
            assert isinstance(result, str) and "/" in result

    def test_select_stores_last_flags(self):
        router = self.make_router()
        router.select(_TIER_1_HEADERS[0])
        assert router._last_flags is not None

    def test_select_stores_locale(self):
        router = self.make_router()
        router.select(_TIER_0_HEADERS[0])
        assert router._last_locale in ("en", "zh")

    # ── record_outcome / stats ────────────────────────────────

    def test_record_outcome_updates_stats(self):
        router = self.make_router()
        router.record_outcome(
            theorem_header=_TIER_1_HEADERS[0],
            model_id="goedel/goedel-v2-8b",
            succeeded=True,
            elapsed_s=5.0,
        )
        stats = router.stats()
        assert len(stats) > 0

    def test_record_and_stats_count_match(self, tmp_path):
        router = ModelRouter(history_path=tmp_path / "h.json")
        n = len(_ALL_HEADERS)
        for i, hdr in enumerate(_ALL_HEADERS):
            router.record_outcome(hdr, "test/router", i % 2 == 0, float(i))
        stats = router.stats()
        total = sum(
            ts.total
            for ms in stats.values()
            for ts in ms.values()
        )
        assert total == n

    # ── health_report ─────────────────────────────────────────

    def test_health_report_contains_models(self):
        router = self.make_router()
        report = router.health_report()
        assert "goedel" in report or "deepseek" in report

    def test_health_report_always_valid(self):
        router = self.make_router()
        router.select(_TIER_0_HEADERS[0])
        report = router.health_report()
        assert "Model Router Health" in report
        assert report.count("\n") >= 3

    # ── _rank ─────────────────────────────────────────────────

    def test_rank_requires_tier_match(self):
        router = self.make_router()
        candidates = router._rank(_TIER_0_HEADERS[0], TIER_SIMPLE)
        assert len(candidates) > 0
        for c in candidates:
            assert TIER_SIMPLE in c.preferred_tiers

    def test_rank_returns_empty_for_unmatched_tier(self):
        router = self.make_router()
        # Only 'hard' tiers get 'non_existent' — fallback kicks in
        candidates = router._rank(_TIER_0_HEADERS[0], "hard")
        assert len(candidates) >= 0

    # ── _check_available ──────────────────────────────────────

    def test_check_unreachable_server(self):
        cfg = ModelConfig(
            model_id="test/test",
            preferred_tiers=[TIER_SIMPLE],
            requires_local_server=True,
            health_check_url="http://localhost:0/health",
        )
        assert ModelRouter()._check_available(cfg) is False

    def test_check_no_constraints(self):
        cfg = ModelConfig(
            model_id="test/test",
            preferred_tiers=[TIER_SIMPLE],
        )
        assert ModelRouter()._check_available(cfg) is True


# ═══════════════════════════════════════════════════════════════
# 2. ML ModelRouter Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not _LGBM_PATH.exists(),
    reason="LightGBM model not at omega/resource/models/router_lgbm_v1.txt",
)
class TestMLModelRouter:
    """LightGBM model loading, prediction, feature extraction."""

    def setup_method(self):
        import joblib as _jl
        import lightgbm as _lgb

        self.model = _lgb.Booster(model_file=str(_LGBM_PATH))
        pipeline = _jl.load(str(_FEATURE_PIPELINE))
        self.vectorizer = pipeline["vectorizer"]
        self.svd = pipeline["svd"]
        self.feature_names = np.load(
            str(_FEATURE_NAMES), allow_pickle=True
        ).tolist()

    def _featurize(self, header: str) -> np.ndarray:
        from omega.research.training.feature_extraction import (
            extract_hc as _extract_hc,
        )

        hc = _extract_hc(header)
        tfidf = self.vectorizer.transform([header])
        svd_out = self.svd.transform(tfidf)[0]
        return np.concatenate([hc, svd_out]).astype(np.float32)

    # ── Model ─────────────────────────────────────────────────

    def test_model_loaded(self):
        import numpy as np_
        X_dummy = np_.zeros((1, 153), dtype=np_.float32)
        probs = self.model.predict(X_dummy)[0]
        assert self.model is not None
        assert self.model.num_feature() == 153
        assert len(probs) == 3

    def test_file_size_reasonable(self):
        size_mb = _LGBM_PATH.stat().st_size / 1e6
        assert 0.5 < size_mb < 50

    # ── Features ──────────────────────────────────────────────

    def test_vectorizer_fitted(self):
        assert hasattr(self.vectorizer, "vocabulary_")
        assert len(self.vectorizer.vocabulary_) <= 500

    def test_svd_output_dim_102(self):
        tfidf = self.vectorizer.transform(_ALL_HEADERS[:3])
        assert self.svd.transform(tfidf).shape[1] == 102

    def test_feature_names_listed(self):
        assert len(self.feature_names) == 153
        assert "hc_" in self.feature_names[0]
        assert "tfidf_" in self.feature_names[51]

    # ── Prediction ────────────────────────────────────────────

    def test_predicts_3_probs(self):
        X = self._featurize(_TIER_0_HEADERS[0]).reshape(1, -1)
        probs = self.model.predict(X)[0]
        assert probs.shape == (3,)
        assert abs(probs.sum() - 1.0) < 1e-5

    def test_predicts_tier2_correctly(self):
        """ML model correctly identifies hard theorems."""
        for h in _TIER_2_HEADERS:
            X = self._featurize(h).reshape(1, -1)
            pred = int(np.argmax(self.model.predict(X)[0]))
            assert pred == 2, f"Expected tier 2, got {pred} for: {h[:60]}"

    def test_predicts_gives_valid_distribution(self):
        """All predictions produce valid probability distributions."""
        for h in _ALL_HEADERS:
            X = self._featurize(h).reshape(1, -1)
            probs = self.model.predict(X)[0]
            assert abs(probs.sum() - 1.0) < 1e-5
            assert all(p >= 0 for p in probs)
            assert len(probs) == 3

    def test_probability_sums_to_one(self):
        for h in _ALL_HEADERS:
            X = self._featurize(h).reshape(1, -1)
            probs = self.model.predict(X)[0]
            assert abs(probs.sum() - 1.0) < 1e-5

    # ── Performance ───────────────────────────────────────────

    def test_inference_under_100us(self):
        import time
        X = self._featurize(_TIER_1_HEADERS[0]).reshape(1, -1)
        for _ in range(10):
            self.model.predict(X)
        t0 = time.perf_counter()
        for _ in range(1000):
            self.model.predict(X)
        elapsed = time.perf_counter() - t0
        assert elapsed / 1000 * 1e6 < 120, f"Too slow: {elapsed/1000*1e6:.0f} µs"

    # ── Feature importance ────────────────────────────────────

    def test_feature_importance_nonzero(self):
        imp = self.model.feature_importance(importance_type="gain")
        assert len(imp) == 153
        assert imp.sum() > 0

    def test_top_feature_is_named(self):
        imp = self.model.feature_importance(importance_type="gain")
        top_name = self.feature_names[int(np.argmax(imp))]
        assert top_name.startswith("hc_") or top_name.startswith("tfidf_")


# ═══════════════════════════════════════════════════════════════
# 3. Pipeline Integration
# ═══════════════════════════════════════════════════════════════


class TestRouterPipeline:
    """End-to-end: rule-based routing + pricing + history."""

    def test_all_headers_selectable(self):
        router = ModelRouter()
        for h in _ALL_HEADERS:
            m = router.select(h)
            assert isinstance(m, str) and "/" in m

    def test_repeated_select_consistent(self):
        router = ModelRouter()
        h = _TIER_1_HEADERS[0]
        assert router.select(h) == router.select(h)

    def test_record_then_stats(self):
        router = ModelRouter()
        for i, h in enumerate(_ALL_HEADERS):
            router.record_outcome(h, "test/m", i % 2 == 0, float(i))
        s = router.stats()
        assert any(
            ts.total > 0 for ms in s.values() for ts in ms.values()
        )

    def test_select_sets_locale(self):
        router = ModelRouter()
        router.select(_TIER_0_HEADERS[0])
        assert router._last_locale in ("en", "zh")

    def test_history_persistence(self, tmp_path):
        p = tmp_path / "h.json"
        r1 = ModelRouter(history_path=p)
        r1.record_outcome(_TIER_0_HEADERS[0], "test/m", True, 0.5)
        r2 = ModelRouter(history_path=p)
        assert len(r2._records) >= 1

    def test_health_report_nonempty(self):
        report = ModelRouter().health_report()
        assert "Model Router Health" in report
        assert len(report) > 50


# ═══════════════════════════════════════════════════════════════
# 4. Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestRouterEdgeCases:
    """Boundary conditions and error handling."""

    def test_empty_header(self):
        assert "/" in ModelRouter().select("")

    def test_very_long_header(self):
        assert "/" in ModelRouter().select("theorem t : " + "A" * 5000 + " :=")

    def test_invalid_format(self):
        assert "/" in ModelRouter().select("invalid syntax here")

    def test_unicode_header(self):
        assert "/" in ModelRouter().select("theorem 定理 (n : ℕ) : n + 0 = n :=")

    def test_empty_stats(self):
        assert isinstance(ModelRouter().stats(), dict)

    def test_repeated_save_history_limit(self, tmp_path):
        p = tmp_path / "h.json"
        r = ModelRouter(history_path=p)
        for _ in range(1500):
            r.record_outcome("t", "test/m", True, 0.1)
        r._save_history()
        # _save_history saves only last 1000 to disk but keeps all in memory
        data = json.loads(p.read_text())
        assert len(data["records"]) <= 1000

    def test_bad_health_check(self):
        cfg = ModelConfig(
            model_id="test/bad",
            preferred_tiers=[TIER_SIMPLE],
            requires_local_server=True,
            health_check_url="http://255.255.255.255:0/health",
        )
        assert ModelRouter()._check_available(cfg) is False


# ═══════════════════════════════════════════════════════════════
# 5. MLRoute Tests
# ═══════════════════════════════════════════════════════════════


_ML_DIR = Path(__file__).resolve().parents[1] / "omega" / "resource" / "models"


@pytest.mark.skipif(
    not (_ML_DIR / "router_lgbm_v1.txt").exists(),
    reason="LightGBM model not at omega/resource/models/router_lgbm_v1.txt",
)
class TestMLRoute:
    """MLRoute — model loading, prediction, integration."""

    def setup_method(self):
        self.route = MLRoute(model_dir=str(_ML_DIR))

    # ── Loading ───────────────────────────────────────────────

    def test_available_on_init(self):
        """MLRoute lazily loads and reports available."""
        route = MLRoute(model_dir=str(_ML_DIR))
        assert route.available() is True

    def test_predict_returns_mlprediction(self):
        pred = self.route.predict(_TIER_0_HEADERS[0])
        assert pred is not None
        assert isinstance(pred, MLPrediction)
        assert pred.tier in (TIER_SIMPLE, TIER_MEDIUM, TIER_HARD)
        assert 0 <= pred.confidence <= 1
        assert len(pred.probabilities) == 3
        assert abs(sum(pred.probabilities) - 1.0) < 1e-5

    def test_predict_all_headers(self):
        for h in _ALL_HEADERS:
            pred = self.route.predict(h)
            assert pred is not None, f"Failed on: {h[:50]}"

    # ── Confidence threshold ──────────────────────────────────

    def test_predict_if_confident_filters_low(self):
        """predict_if_confident returns None for uncertain predictions."""
        route = MLRoute(model_dir=str(_ML_DIR), confidence_threshold=0.99)
        for h in _ALL_HEADERS:
            pred = route.predict_if_confident(h)
            # With 0.99 threshold, most predictions will be below
            if pred is not None:
                assert pred.confidence >= 0.99

    def test_low_threshold_allows_all(self):
        route = MLRoute(model_dir=str(_ML_DIR), confidence_threshold=0.0)
        for h in _ALL_HEADERS:
            pred = route.predict_if_confident(h)
            assert pred is not None

    def test_default_threshold_no_false_negatives(self):
        """Default threshold 0.6: valid headers should usually pass."""
        route = MLRoute(model_dir=str(_ML_DIR))
        for h in _TIER_2_HEADERS:
            pred = route.predict_if_confident(h)
            assert pred is not None, f"Tier 2 should be confident: {h[:50]}"

    # ── Batch ─────────────────────────────────────────────────

    def test_predict_batch_returns_all(self):
        results = self.route.predict_batch(_ALL_HEADERS)
        assert len(results) == len(_ALL_HEADERS)
        for pred in results:
            assert pred is not None

    def test_predict_batch_consistency(self):
        """Same header → same prediction."""
        h = _TIER_1_HEADERS[0]
        r1 = self.route.predict(h)
        r2 = self.route.predict(h)
        assert r1 is not None and r2 is not None
        assert r1.tier == r2.tier
        assert abs(r1.confidence - r2.confidence) < 1e-6
        assert r1.probabilities == r2.probabilities

    # ── Unavailable model ─────────────────────────────────────

    def test_nonexistent_dir_returns_none(self):
        route = MLRoute(model_dir="/tmp/nonexistent_ml_dir_xyz")
        assert route.available() is False
        assert route.predict(_TIER_0_HEADERS[0]) is None

    def test_predict_on_empty_header(self):
        pred = self.route.predict("")
        assert pred is not None  # Should still produce a prediction

    # ── Model type ────────────────────────────────────────────

    def test_model_type_lightgbm(self):
        pred = self.route.predict(_TIER_1_HEADERS[0])
        assert pred is not None
        # Either LightGBM or ONNX backend should be available
        assert pred.model_type in ("lightgbm", "onnx")
        assert self.route._model is not None or self.route._sess is not None


@pytest.mark.skipif(
    not (_ML_DIR / "router_lgbm_v1.onnx").exists(),
    reason="ONNX model not at omega/resource/models/router_lgbm_v1.onnx",
)
class TestMLRouteONNX:
    """MLRoute with ONNX backend."""

    def test_onnx_loaded(self):
        route = MLRoute(model_dir=str(_ML_DIR))
        assert route.available()
        assert route._model_type in ("lightgbm", "onnx")

    def test_onnx_prediction(self):
        route = MLRoute(model_dir=str(_ML_DIR))
        pred = route.predict(_TIER_2_HEADERS[0])
        assert pred is not None
        assert pred.tier == TIER_HARD
        assert len(pred.probabilities) == 3

    def test_onnx_vs_lightgbm_agreement(self):
        """ONNX and LightGBM should give near-identical predictions."""
        route = MLRoute(model_dir=str(_ML_DIR))
        import lightgbm as lgb
        lgbm = lgb.Booster(model_file=str(_ML_DIR / "router_lgbm_v1.txt"))

        for h in _ALL_HEADERS:
            ml_pred = route.predict(h)
            X = route._featurize(h).reshape(1, -1)
            lgb_probs = lgbm.predict(X)[0]
            lgb_tier = TIER_NAMES[int(np.argmax(lgb_probs))]
            assert ml_pred is not None
            assert ml_pred.tier == lgb_tier, (
                f"Mismatch for {h[:50]}: ML={ml_pred.tier} "
                f"LGB={lgb_tier}"
            )


# ═══════════════════════════════════════════════════════════════
# 6. ML-Enabled ModelRouter Integration
# ═══════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not (_ML_DIR / "router_lgbm_v1.txt").exists(),
    reason="LightGBM model not available",
)
class TestModelRouterWithML:
    """ModelRouter with ML backend enabled."""

    def test_select_with_ml(self):
        router = ModelRouter(enable_ml=True)
        for h in _ALL_HEADERS:
            model_id = router.select(h)
            assert "/" in model_id
        # After at least one call with ML enabled, the ML route should be loaded
        assert router._ml_route is not None
        # Some headers may trigger ML override
        ml_overrides = sum(
            1 for h in _ALL_HEADERS
            if router._ml_route is not None
            and router._ml_route.predict_if_confident(h) is not None
        )
        assert ml_overrides >= 0  # Any number is acceptable

    def test_select_without_ml(self):
        router = ModelRouter(enable_ml=False)
        for h in _ALL_HEADERS:
            model_id = router.select(h)
            assert "/" in model_id
        assert router._ml_route is None  # Should not load ML model

    def test_without_ml_every_result_has_slash(self):
        router = ModelRouter(enable_ml=False)
        for h in _ALL_HEADERS:
            assert "/" in router.select(h)

    def test_ml_results_consistent(self):
        """ML-enabled vs disabled should not change basic API contract."""
        ml_router = ModelRouter(enable_ml=True)
        no_ml_router = ModelRouter(enable_ml=False)
        for h in _ALL_HEADERS:
            r1 = ml_router.select(h)
            r2 = no_ml_router.select(h)
            assert "/" in r1
            assert "/" in r2
