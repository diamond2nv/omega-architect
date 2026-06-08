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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omega.resource.pricing import (
    compute_savings,
    lookup_price,
    prompt_hint_locale,
    select_localized_hint,
)
from omega.resource.routing_flags import (
    TIER_HARD,
    TIER_MEDIUM,
    TIER_SIMPLE,
    TIER_NAMES,
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
    if strategy in ("trivial", "rfl"):
        base_tier = _TIER_SIMPLE_INT  # 0
    elif strategy == "simp" and conf >= 0.7:
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
    ):
        self._history_path = Path(history_path or _HISTORY_PATH)
        self._prefer_cost = prefer_cost
        self._models = list(_DEFAULT_MODELS)
        self._records: list[UsageRecord] = []
        self._last_flags: TheoremFlags | None = None
        self._last_base_tier: int = 0
        self._last_savings: dict = {}
        self._last_locale: str = "en"
        self._load_history()

    # ── Public API ─────────────────────────────────────────────

    def select(self, theorem_header: str) -> str:
        """Select the best model for a given theorem.

        Decision flow:
        1. Analyze theorem complexity
        2. Filter models by preferred tier
        3. Check availability: API key present, local server running
        4. Prefer cheaper model if tiers overlap
        5. Fall back to template-only if no model available

        Parameters
        ----------
        theorem_header : str
            Lean 4 theorem header to analyze.

        Returns
        -------
        str
            Model ID to use (e.g. ``"goedel/goedel-v2-8b"``).
            Falls back to ``"local/default"`` (template-only) when
            no suitable model is available.
        """
        complexity, self._last_flags, self._last_base_tier = _estimate_complexity(theorem_header)
        candidates = self._rank(theorem_header, complexity)

        if not candidates:
            return "local/default"

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
