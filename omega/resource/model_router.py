#!/usr/bin/env python3
"""ModelRouter — intelligent model selection for theorem proving.

Routes each theorem to the most appropriate model based on:
- Theorem complexity (from ``analyze_theorem_pattern``)
- GPU memory availability
- API availability (API key present / local model running)
- History: per-model success rates for similar complexity levels

Usage::

    from omega.resource.model_router import ModelRouter

    router = ModelRouter()
    model_id = router.select(theorem_header="theorem t (n : ℕ) : n + 0 = n :=")
    # Returns: "goedel/goedel-v2-8b" (simple) or "deepseek/deepseek-v4-flash" (hard)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from omega.research.training.feature_extraction import extract_hc
from omega.resource.pricing import (
    compute_savings,
    lookup_price,
    prompt_hint_locale,
)
from omega.resource.routing_flags import (
    TIER_HARD,
    TIER_MEDIUM,
    TIER_NAMES,
    TIER_SIMPLE,
    TheoremFlags,
    apply_postprocess,
    compute_theorem_flags,
)
from omega.search.proposer import analyze_theorem_pattern

# ── Complexity tiers ──────────────────────────────────────────

TIER_SIMPLE = "simple"
TIER_MEDIUM = "medium"
TIER_HARD = "hard"

# Int versions for _estimate_complexity comparisons
_TIER_SIMPLE_INT = 0
_TIER_MEDIUM_INT = 1
_TIER_HARD_INT = 2

# Thresholds: confidence from analyze_theorem_pattern
_COMPLEXITY_MAP: dict[float, int] = {
    0.15: 2,      # unknown pattern → hard
    0.30: 2,      # low confidence → hard
    0.50: 1,      # medium confidence
    0.70: 0,      # induction pattern → simple
    0.90: 0,      # trivial/rfl → simple
}

_TIER_TO_STR: dict[int, str] = {0: TIER_SIMPLE, 1: TIER_MEDIUM, 2: TIER_HARD}
_STR_TO_TIER: dict[str, int] = {v: k for k, v in _TIER_TO_STR.items()}


def _estimate_complexity(
    header: str,
    history: list[dict] | None = None,
    fallback_count: int = 0,
) -> tuple[str, TheoremFlags, int]:
    """Estimate theorem complexity using pattern analysis + flag-based post-processing.

    Returns
    -------
    tuple[str, TheoremFlags, int]
        (tier_string, flags, raw_tier_before_postprocess)
    """
    pattern = analyze_theorem_pattern(header)
    conf = pattern["confidence"]
    strategy = pattern["strategy"]

    # Baseline tier from pattern analysis
    base_tier = _TIER_SIMPLE_INT  # default
    if strategy in ("trivial", "rfl") or strategy == "simp" and conf >= 0.7:
        base_tier = _TIER_SIMPLE_INT  # 0
    elif "induction" in strategy:
        base_tier = _TIER_MEDIUM_INT  # 1
    else:
        # Fallback complexity mapping by confidence
        for threshold, tier in sorted(_COMPLEXITY_MAP.items()):
            if conf <= threshold:
                base_tier = tier
                break

    # Also check for compound structure indicators
    if "theorem" in header and "(" in header and ")" in header:
        if "ℕ" in header or "ℤ" in header or "ℝ" in header or "List" in header:
            base_tier = max(base_tier, _TIER_MEDIUM_INT)

    # Compute runtime flags from text + history
    flags = compute_theorem_flags(header, history=history)

    # Apply post-processing pipeline (safety, overrides, escalation, sticky)
    final_tier = apply_postprocess(
        base_tier=base_tier,
        flags=flags,
        history=history,
        fallback_count=fallback_count,
    )

    return _TIER_TO_STR[final_tier], flags, base_tier


# ── Model configuration ───────────────────────────────────────

@dataclass
class ModelConfig:
    """Configuration for a single model in the router."""
    model_id: str
    preferred_tiers: list[str]  # Which complexity tiers this model handles
    cost_per_call: float = 0.0  # USD estimate (0 = free/local)
    requires_api_key: bool = False
    api_key_env: str = ""
    requires_local_server: bool = False
    health_check_url: str = ""


_DEFAULT_MODELS: list[ModelConfig] = [
    ModelConfig(
        model_id="goedel/goedel-v2-8b",
        preferred_tiers=[TIER_SIMPLE, TIER_MEDIUM],
        cost_per_call=0.0,
        requires_local_server=True,
        health_check_url="http://localhost:8001/health",
    ),
    ModelConfig(
        model_id="deepseek/deepseek-v4-flash",
        preferred_tiers=[TIER_MEDIUM, TIER_HARD],
        cost_per_call=0.0005,  # ~500 input tokens
        requires_api_key=True,
        api_key_env="DEEPSEEK_API_KEY",
    ),
    ModelConfig(
        model_id="local/qwen3-coder:30b",
        preferred_tiers=[TIER_SIMPLE],
        cost_per_call=0.0,
        requires_local_server=True,
        health_check_url="http://localhost:11434/api/tags",
    ),
]

# ── History store ─────────────────────────────────────────────

_HISTORY_PATH = Path.home() / ".omega" / "model_router_history.json"


@dataclass
class UsageRecord:
    """Record of a single model usage."""
    model_id: str
    complexity: str
    theorem_header: str
    succeeded: bool
    elapsed_s: float


@dataclass
class RouterStats:
    """Router statistics per model per complexity tier."""
    total: int = 0
    succeeded: int = 0
    total_elapsed_s: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.succeeded / max(1, self.total)

    @property
    def avg_elapsed_s(self) -> float:
        return self.total_elapsed_s / max(1, self.total)


# ── ModelRouter ───────────────────────────────────────────────


class ModelRouter:
    """Intelligent model selection for theorem proving.

    Parameters
    ----------
    history_path : str or Path
        Path to persist usage history (default: ``~/.omega/model_router_history.json``).
    prefer_cost : bool
        Prefer cheaper models when tiers overlap (default: ``True``).
    """

    def __init__(
        self,
        history_path: str | Path | None = None,
        prefer_cost: bool = True,
        enable_ml: bool = True,
    ):
        self._history_path = Path(history_path or _HISTORY_PATH)
        self._prefer_cost = prefer_cost
        self._models = list(_DEFAULT_MODELS)
        self._records: list[UsageRecord] = []
        self._last_flags: TheoremFlags | None = None
        self._last_base_tier: int = 0
        self._last_savings: dict = {}
        self._last_locale: str = "en"
        self._enable_ml = enable_ml
        self._ml_route: MLRoute | None = None
        self._last_ml_pred: MLPrediction | None = None
        self._load_history()

    # ── Public API ─────────────────────────────────────────────

    def select(
        self,
        theorem_header: str,
        plan_snapshot: dict | None = None,
    ) -> str:
        """Select the best model for a given theorem.

        Decision flow:
        1. Analyze theorem complexity
        2. Filter models by preferred tier
        3. Check availability: API key present, local server running
        4. Prefer cheaper model if tiers overlap
        5. **Plan-aware downgrade**: if ``plan_snapshot`` shows
           budget is running low, hard theorems may be downgraded
           to cheaper models (Goedel/Flash instead of Pro).
        6. Fall back to template-only if no model available

        Parameters
        ----------
        theorem_header : str
            Lean 4 theorem header to analyze.
        plan_snapshot : dict or None
            Optional budget snapshot (e.g. from ``BudgetPlan.snapshot()``)
            with keys ``remaining_cost_usd``, ``remaining_time_s``,
            ``remaining_attempts``.  When provided, the router may
            downgrade models under budget pressure.

        Returns
        -------
        str
            Model ID to use (e.g. ``"goedel/goedel-v2-8b"``).
            Falls back to ``"local/default"`` (template-only) when
            no suitable model is available.
        """
        complexity, self._last_flags, self._last_base_tier = _estimate_complexity(theorem_header)
        self._last_ml_pred = None

        # ML override: if confident, prefer ML tier over rule-based estimate
        if self._enable_ml:
            ml_pred = self._ml_predict(theorem_header)
            if ml_pred is not None:
                self._last_ml_pred = ml_pred
                # Only override if ML disagrees and is confident
                if ml_pred.tier != complexity and ml_pred.confidence >= _ML_CONFIDENCE_THRESHOLD:
                    complexity = ml_pred.tier

        candidates = self._rank(theorem_header, complexity)

        if not candidates:
            return "local/default"

        # ── Plan-aware budget downgrade ────────────────────────
        if plan_snapshot is not None:
            candidates = self._apply_plan_snapshot(candidates, complexity, plan_snapshot)

        # Pick best candidate
        best = candidates[0]

        # Compute cost savings vs most expensive model
        model_prices = [
            (m.model_id, lookup_price(m.model_id)["input_per_mtok"])
            for m in self._models
        ]
        self._last_savings = compute_savings(best.model_id, model_prices)
        self._last_locale = prompt_hint_locale(theorem_header)
        return best.model_id

    def record_outcome(
        self,
        model_id: str,
        theorem_header: str,
        succeeded: bool,
        elapsed_s: float,
    ) -> None:
        """Record the outcome of a theorem attempt for future routing.

        Persisted to ``~/.omega/model_router_history.json``.
        """
        complexity, _, _ = _estimate_complexity(theorem_header)
        self._records.append(UsageRecord(
            model_id=model_id,
            complexity=complexity,
            theorem_header=theorem_header,
            succeeded=succeeded,
            elapsed_s=elapsed_s,
        ))
        self._save_history()

    def stats(self) -> dict[str, dict[str, RouterStats]]:
        """Return per-model per-tier success statistics.

        Returns
        -------
        dict of ``{model_id: {complexity: RouterStats}}``
        """
        result: dict[str, dict[str, RouterStats]] = {}
        for rec in self._records:
            model_stats = result.setdefault(rec.model_id, {})
            tier_stats = model_stats.setdefault(rec.complexity, RouterStats())
            tier_stats.total += 1
            if rec.succeeded:
                tier_stats.succeeded += 1
            tier_stats.total_elapsed_s += rec.elapsed_s

        # Compute rates
        for model_stats in result.values():
            for stats in model_stats.values():
                pass  # properties compute on demand

        return result

    def health_report(self) -> str:
        """Return a human-readable health report of all configured models."""
        lines = ["Model Router Health:", "-" * 40]
        for m in self._models:
            available = self._check_available(m)
            icon = "✅" if available else "❌"
            price = lookup_price(m.model_id)
            inp = price["input_per_mtok"]
            out = price["output_per_mtok"]
            lines.append(
                f"  {icon} {m.model_id} "
                f"(in=${inp:.4f}/M, out=${out:.4f}/M)"
            )
        # Last-select savings
        if self._last_savings.get("savings_pct", 0) > 0:
            lines.append("")
            lines.append(f"  Last select: {self._last_savings['savings_pct']:.1f}% savings "
                         f"(max=${self._last_savings['max_price_per_m']:.4f}/M)")
            lines.append(f"  Locale: {self._last_locale}")
        return "\n".join(lines)

    # ── Internal ───────────────────────────────────────────────

    def _apply_plan_snapshot(
        self,
        candidates: list[ModelConfig],
        complexity: str,
        plan_snapshot: dict,
    ) -> list[ModelConfig]:
        """Apply plan-aware budget downgrade to candidate list.

        When budget is tight, hard theorems get downgraded to cheaper
        models.  The original candidate ordering (by tier priority) is
        preserved where possible.

        ``plan_snapshot`` keys used:
        - ``remaining_cost_usd`` — remaining budget
        - ``remaining_time_s`` — remaining wall time
        - ``remaining_attempts`` — remaining attempt count
        """
        remaining_cost = plan_snapshot.get("remaining_cost_usd", float("inf"))
        remaining_time = plan_snapshot.get("remaining_time_s", float("inf"))

        # Only downgrade under meaningful pressure
        if remaining_cost > 1.0 or remaining_time > 300:
            return candidates  # plenty of headroom

        # Cost-per-call lookup from model config
        cost_by_model: dict[str, float] = {
            m.model_id: m.cost_per_call for m in self._models
        }

        # Determine downgrade threshold: how many cheap calls can we afford?
        cheap_model_cost = min(
            (cost for cost in cost_by_model.values() if cost > 0),
            default=0.0005,
        )
        affordable_calls = remaining_cost / max(cheap_model_cost, 0.0001)

        # Hard theorem downgrade: if we can afford < 5 hard calls,
        # remove expensive models for hard tier
        if complexity == "hard" or complexity == "research":
            if remaining_cost < 0.01 or affordable_calls < 3:
                # Remove expensive (cost > flash) models for hard theorems
                expensive_ids = {
                    m_id for m_id, c in cost_by_model.items()
                    if c > 0.001  # more expensive than flash
                }
                candidates = [
                    c for c in candidates
                    if c.model_id not in expensive_ids
                ]

        # Time pressure: if time is very tight, prefer faster models
        if remaining_time < 60 and remaining_time > 0:
            # Score by speed: tok/s heuristic from pricing
            def speed_key(m: ModelConfig) -> float:
                # Free/local models are slower, flash/pro are fast
                if "flash" in m.model_id or "pro" in m.model_id:
                    return 0.0  # fast → rank first
                return 10.0  # local → rank after
            candidates.sort(key=speed_key)

        # If we filtered everything out, return original
        if not candidates:
            return [
                m for m in self._models
                if m.cost_per_call <= cheap_model_cost
            ] or self._models[:1]

        return candidates

    def _rank(self, theorem_header: str, complexity: str) -> list[ModelConfig]:
        """Rank models by suitability for a given complexity."""
        # Filter by tier match
        tier_candidates = [
            m for m in self._models if complexity in m.preferred_tiers
        ]

        if not tier_candidates:
            # No tier match — use any model
            tier_candidates = list(self._models)

        # Filter by availability
        available = [m for m in tier_candidates if self._check_available(m)]

        if not available:
            return []

        # Sort: preferred tier match (primary), then cost (if prefer_cost)
        def sort_key(m: ModelConfig) -> tuple:
            tier_priority = m.preferred_tiers.index(complexity) if complexity in m.preferred_tiers else 99
            return (tier_priority, m.cost_per_call if self._prefer_cost else 0)

        available.sort(key=sort_key)
        return available

    def _check_available(self, model: ModelConfig) -> bool:
        """Check if a model is currently available."""
        if model.requires_api_key:
            from dotenv import load_dotenv
            load_dotenv(os.path.expanduser("~/.hermes/.env"))
            key = os.environ.get(model.api_key_env, "")
            if not key or key == "***":
                return False
        if model.requires_local_server and model.health_check_url:
            try:
                import urllib.request
                urllib.request.urlopen(model.health_check_url, timeout=2)
            except Exception:
                return False
        return True

    def _ml_predict(self, theorem_header: str) -> MLPrediction | None:
        """Run ML tier prediction, lazily loading the model on first call.

        Returns ``None`` if ML is unavailable or the model can't load.
        """
        if self._ml_route is None:
            try:
                self._ml_route = MLRoute()
            except Exception:
                self._enable_ml = False
                return None
        if not self._ml_route.available():
            return None
        return self._ml_route.predict_if_confident(theorem_header)

    def _load_history(self) -> None:
        """Load usage history from disk."""
        if self._history_path.exists():
            try:
                data = json.loads(self._history_path.read_text())
                self._records = [UsageRecord(**r) for r in data.get("records", [])]
            except (json.JSONDecodeError, KeyError):
                self._records = []

    def _save_history(self) -> None:
        """Save usage history to disk."""
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "records": [
                {
                    "model_id": r.model_id,
                    "complexity": r.complexity,
                    "theorem_header": r.theorem_header[:100],
                    "succeeded": r.succeeded,
                    "elapsed_s": round(r.elapsed_s, 2),
                }
                for r in self._records[-1000:]  # Keep last 1000
            ]
        }
        self._history_path.write_text(json.dumps(data, indent=2))


# ═══════════════════════════════════════════════════════════════
# ML Route — optional ML-based tier prediction
# ═══════════════════════════════════════════════════════════════

_ML_CONFIDENCE_THRESHOLD = 0.6
"""Minimum confidence to override rule-based tier with ML prediction."""


@dataclass
class MLPrediction:
    """Prediction result from the ML route model.

    Attributes
    ----------
    tier : str
        Predicted tier (``"simple"``, ``"medium"``, or ``"hard"``).
    confidence : float
        Max softmax probability (0-1).
    probabilities : list[float]
        Full probability distribution [p_simple, p_medium, p_hard].
    model_type : str
        Backend used (``"lightgbm"`` or ``"onnx"``).
    """
    tier: str
    confidence: float
    probabilities: list[float]
    model_type: str = "lightgbm"


class MLRoute:
    """Optional ML-based tier predictor for theorem complexity.

    Loads the trained LightGBM (or ONNX) model + feature pipeline
    from ``omega/resource/models/`` and provides online predictions
    for single theorem headers.

    Parameters
    ----------
    model_dir : str or Path
        Directory containing the model files.  Defaults to
        ``omega/resource/models/`` relative to this file.
    prefer_onnx : bool
        Try ONNX runtime first (requires onnxruntime).  Falls back to
        LightGBM Booster if ONNX fails.
    confidence_threshold : float
        Predictions below this confidence return ``None`` from
        ``predict_if_confident()``.

    Examples
    --------
    >>> route = MLRoute()
    >>> pred = route.predict("theorem t (n : ℕ) : n + 0 = n :=")
    >>> pred.tier
    'medium'
    """

    def __init__(
        self,
        model_dir: str | Path | None = None,
        prefer_onnx: bool = False,
        confidence_threshold: float = _ML_CONFIDENCE_THRESHOLD,
    ):
        if model_dir is None:
            model_dir = Path(__file__).resolve().parent / "models"
        self._model_dir = Path(model_dir)
        self._threshold = confidence_threshold
        self._model = None
        self._sess = None
        self._vectorizer = None
        self._svd = None
        self._model_type: str = "none"
        self._loaded = False

    def _lazy_load(self) -> bool:
        """Load model + pipeline on first use.  Returns True if OK."""
        if self._loaded:
            return self._model is not None or self._sess is not None

        self._loaded = True

        # Load feature pipeline
        pipe_path = self._model_dir / "feature_pipeline.joblib"
        if not pipe_path.exists():
            return False
        try:
            import joblib
            pipeline = joblib.load(str(pipe_path))
            self._vectorizer = pipeline["vectorizer"]
            self._svd = pipeline["svd"]
        except Exception:
            return False

        # Load model — try ONNX first if requested
        if self._prefer_onnx():
            if self._try_load_onnx():
                return True

        # Fall back to LightGBM
        return self._try_load_lightgbm()

    def _prefer_onnx(self) -> bool:
        """Check if ONNX should be attempted."""
        onnx_path = self._model_dir / "router_lgbm_v1.onnx"
        return onnx_path.exists()

    def _try_load_onnx(self) -> bool:
        """Try loading ONNX model.  Returns True on success."""
        onnx_path = self._model_dir / "router_lgbm_v1.onnx"
        if not onnx_path.exists():
            return False
        try:
            import onnxruntime as ort
            self._sess = ort.InferenceSession(str(onnx_path))
            self._model_type = "onnx"
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def _try_load_lightgbm(self) -> bool:
        """Try loading LightGBM model.  Returns True on success."""
        lgbm_path = self._model_dir / "router_lgbm_v1.txt"
        if not lgbm_path.exists():
            return False
        try:
            import lightgbm as lgb
            self._model = lgb.Booster(model_file=str(lgbm_path))
            self._model_type = "lightgbm"
            return True
        except ImportError:
            return False
        except Exception:
            return False

    # ── Prediction API ────────────────────────────────────────

    def predict(self, header: str) -> MLPrediction | None:
        """Predict tier for a single theorem header.

        Parameters
        ----------
        header : str
            Lean 4 theorem header text.

        Returns
        -------
        MLPrediction or None
            ``None`` if the model is unavailable.
        """
        if not self._lazy_load():
            return None

        try:
            features = self._featurize(header)
        except Exception:
            return None

        probs = self._run_model(features)
        if probs is None:
            return None

        pred_idx = int(np.argmax(probs))
        tier = TIER_NAMES[pred_idx] if pred_idx < len(TIER_NAMES) else TIER_MEDIUM
        conf = float(probs[pred_idx])

        return MLPrediction(
            tier=tier,
            confidence=conf,
            probabilities=[float(p) for p in probs],
            model_type=self._model_type,
        )

    def predict_if_confident(self, header: str) -> MLPrediction | None:
        """Predict only if confidence exceeds threshold.

        Returns ``None`` when the model is unavailable or confidence
        is below ``confidence_threshold``.
        """
        pred = self.predict(header)
        if pred is None or pred.confidence < self._threshold:
            return None
        return pred

    def predict_batch(
        self, headers: list[str]
    ) -> list[MLPrediction | None]:
        """Predict tiers for multiple headers.

        Parameters
        ----------
        headers : list[str]
            Theorem header texts.

        Returns
        -------
        list[MLPrediction | None]
            Predictions in order, ``None`` for failures.
        """
        return [self.predict(h) for h in headers]

    def available(self) -> bool:
        """Check if the ML model is loaded and ready."""
        self._lazy_load()
        return self._model is not None or self._sess is not None

    # ── Internal ──────────────────────────────────────────────

    def _featurize(self, header: str) -> np.ndarray:
        """Extract 153-dim feature vector from a theorem header."""
        hc = extract_hc(header)

        tfidf_raw = self._vectorizer.transform([header])
        tfidf_svd = self._svd.transform(tfidf_raw)[0]

        return np.concatenate([hc, tfidf_svd]).astype(np.float32)

    def _run_model(self, features: np.ndarray) -> np.ndarray | None:
        """Run model inference on a single feature vector."""
        X = features.reshape(1, -1)

        if self._model is not None:
            return self._model.predict(X)[0]

        if self._sess is not None:
            outputs = self._sess.run(None, {"float_input": X})
            # ONNX output: [label, probabilities_seq]
            if len(outputs) >= 2:
                prob_map = outputs[1][0]  # first element of sequence
                # prob_map is dict[int, float]
                probs = [prob_map.get(i, 0.0) for i in range(3)]
                return np.array(probs, dtype=np.float32)
            return None

        return None
