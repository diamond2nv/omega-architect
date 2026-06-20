"""Mode Router — automatic selection of proof search strategy.

The Mode Router is the **strategy decision layer** in the Multi-Path
Trajectory Exploration framework. It selects the optimal search strategy
based on:

1. **Theorem characteristics**: difficulty signal, domain hints, statement length
2. **Resource availability**: API vs local GPU, budget remaining
3. **Execution history**: previous failures, convergence patterns

Decision Logic
--------------
```
theorem → difficulty estimation
  │
  ├── easy + API available   → DFS (fast single trajectory)
  ├── easy + local GPU only  → Beam (parallel candidates, free)
  ├── medium + API           → DFS (medium theorems pass in 5-20 rounds)
  ├── hard + high budget     → Hybrid (multi-path exploration)
  ├── previously failed *N*  → Hybrid (try different approach)
  └── unknown                → DFS (default, most reliable)
```

Design
------
The router is a **scoring-based** classifier, not a hard-coded switch.
Each strategy gets a score based on multiple signals, and the highest
score wins. This allows for continuous improvement via weight tuning
and future ML-based routing.

Current weights are hand-tuned from MiniF2F experiment data:
- DFS: 2/4 (50%) on 4-test set, ~60s avg for wins
- Beam: Same pass rate as DFS but higher latency
- Hybrid: Same pass rate, 2× latency — only justified when DFS is failing
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omega.engine.difficulty_spectrum import DifficultySpectrum
from omega.engine.trajectory import (
    SearchStrategy,
    get_strategy,
)

logger = logging.getLogger("omega.engine.router")


# ═══════════════════════════════════════════════════════════════════
# Enums & Config
# ═══════════════════════════════════════════════════════════════════


class DifficultyLevel(Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    UNKNOWN = "unknown"


class ResourceProfile(Enum):
    """Available inference resources."""
    API_ONLY = "api_only"          # Remote DeepSeek API, no local GPU
    LOCAL_GPU = "local_gpu"        # Local vLLM/Ollama, no API
    HYBRID_RESOURCE = "hybrid"     # Both API and local GPU available
    API_WITH_CACHE = "api_cache"   # API available + cached results


@dataclass
class RouterConfig:
    """Configuration for the Mode Router.

    Parameters
    ----------
    dfs_max_rounds : int
        Max rounds for DFS when selected (default 50).
    beam_num_samples : int
        Beam width when selected (default 6).
    hybrid_phase1_rounds : int
        DFS rounds before triggering beam in Hybrid (default 20).
    previous_failure_threshold : int
        Number of previous failures before forcing Hybrid (default 2).
    budget_hybrid_threshold_usd : float
        Minimum remaining budget to consider Hybrid (default 0.30).
    """

    dfs_max_rounds: int = 50
    beam_num_samples: int = 6
    hybrid_phase1_rounds: int = 20
    previous_failure_threshold: int = 2
    budget_hybrid_threshold_usd: float = 0.30


# ═══════════════════════════════════════════════════════════════════
# Router State
# ═══════════════════════════════════════════════════════════════════


@dataclass
class RoutingContext:
    """Context for a single routing decision.

    Parameters
    ----------
    theorem_header : str
        The theorem to prove.
    resource_profile : ResourceProfile
        Available compute resources.
    budget_remaining_usd : float
        Remaining budget in USD.
    previous_failures : int
        How many times this theorem has failed before.
    history_hint : str or None
        Optional hint from previous runs ("stuck", "timeout", etc.).
    domain : str or None
        Detected domain (e.g. "algebra", "number_theory", "combinatorics").
    """

    theorem_header: str = ""
    resource_profile: ResourceProfile = ResourceProfile.API_ONLY
    budget_remaining_usd: float = 0.50
    previous_failures: int = 0
    history_hint: str | None = None
    domain: str | None = None


@dataclass
class RoutingDecision:
    """Result of a routing decision.

    Parameters
    ----------
    strategy_name : str
        Selected strategy: "dfs", "beam", or "hybrid".
    strategy : SearchStrategy
        Configured strategy instance.
    confidence : float
        Confidence in this decision (0.0-1.0).
    reasoning : str
        Human-readable explanation of the decision.
    scores : dict[str, float]
        Raw scores for each strategy.
    """

    strategy_name: str = "dfs"
    strategy: SearchStrategy | None = None
    confidence: float = 0.5
    reasoning: str = ""
    scores: dict[str, float] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# Difficulty Estimation
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# Difficulty Estimation via NLP Spectrum
# ═══════════════════════════════════════════════════════════════════
_DOMAIN_PATTERNS: dict[str, list[str]] = {
    "algebra": [r"\+", r"\*", r"ring", r"group", r"field", r"polynomial", r"matrix", r"comm", r"mul_", r"add_"],
    "number_theory": [r"prime", r"dvd", r"mod", r"congruence", r"gcd", r"lcm", r"∣", r"Prime"],
    "combinatorics": [r"choose", r"factorial", r"permutation", r"graph", r"count", r"Nat\."],
    "analysis": [r"limit", r"continuous", r"derivative", r"integral", r"sup", r"inf", r"Real\."],
    "geometry": [r"triangle", r"circle", r"angle", r"distance", r"area"],
}


def estimate_difficulty(theorem: str) -> DifficultyLevel:
    """Estimate theorem difficulty using NLP spectrum voting.

    Uses the same multi-algorithm approach as the Layer 2 error classifier:
    TokenJaccard + EditDistance + BM25Retrieval + EmbeddingSimilarity,
    voting on a labeled corpus of known theorems.
    """
    _spectrum = DifficultySpectrum()
    est = _spectrum.estimate(theorem)
    level_map = {
        "easy": DifficultyLevel.EASY,
        "medium": DifficultyLevel.MEDIUM,
        "hard": DifficultyLevel.HARD,
    }
    return level_map.get(est.level.lower(), DifficultyLevel.UNKNOWN)


def detect_domain(theorem: str) -> str | None:
    """Detect the mathematical domain of a theorem."""
    scores: dict[str, int] = {}
    for domain, patterns in _DOMAIN_PATTERNS.items():
        scores[domain] = sum(1 for pat in patterns if re.search(pat, theorem, re.IGNORECASE))
    if not scores or max(scores.values()) == 0:
        return None
    return max(scores, key=scores.get)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
# Strategy Scoring
# ═══════════════════════════════════════════════════════════════════


def _score_dfs(difficulty: DifficultyLevel, ctx: RoutingContext) -> float:
    """Score for DFS strategy (0.0-10.0)."""
    score = 5.0  # default baseline

    # DFS excels at easy/medium
    if difficulty == DifficultyLevel.EASY:
        score += 3.0
    elif difficulty == DifficultyLevel.MEDIUM:
        score += 1.0

    # DFS needs API (it's the main consumer)
    if ctx.resource_profile in (ResourceProfile.API_ONLY, ResourceProfile.API_WITH_CACHE):
        score += 2.0

    # Previous failures penalize DFS (it already failed)
    if ctx.previous_failures > 0:
        score -= min(4.0, ctx.previous_failures * 2.0)

    # Stuck history is a strong negative signal
    if ctx.history_hint in ("stuck", "dead_loop", "diverging"):
        score -= 3.0

    return max(0.0, score)


def _score_beam(difficulty: DifficultyLevel, ctx: RoutingContext) -> float:
    """Score for Beam strategy (0.0-10.0)."""
    score = 3.0  # lower baseline — generally less effective than DFS

    # Beam works best with local GPU (parallel compilation)
    if ctx.resource_profile == ResourceProfile.LOCAL_GPU:
        score += 3.0

    # Beam for easy theorems with cheap local inference
    if difficulty == DifficultyLevel.EASY and ctx.resource_profile == ResourceProfile.LOCAL_GPU:
        score += 3.0

    # Beam for problems where exploration helps
    if difficulty == DifficultyLevel.MEDIUM:
        score += 1.0

    # Budget signal: beam is cheaper per attempt with local GPU
    if ctx.budget_remaining_usd < 0.10 and ctx.resource_profile == ResourceProfile.LOCAL_GPU:
        score += 2.0

    return max(0.0, score)


def _score_hybrid(difficulty: DifficultyLevel, ctx: RoutingContext, hybrid_budget_threshold: float = 0.30) -> float:
    """Score for Hybrid strategy (0.0-10.0)."""
    score = 2.0  # higher cost baseline

    # Hybrid only pays off for hard theorems
    if difficulty == DifficultyLevel.HARD:
        score += 3.0

    # Previous failures signal that simpler strategies won't work
    if ctx.previous_failures >= 2:
        score += 4.5
    elif ctx.previous_failures >= 1:
        score += 2.0

    # Stuck history is the #1 signal for Hybrid
    if ctx.history_hint in ("stuck", "dead_loop", "diverging"):
        score += 5.0

    # Budget constraint
    if ctx.budget_remaining_usd < hybrid_budget_threshold:
        score -= 3.0

    # Resource: need API for dialogue phase
    if ctx.resource_profile not in (ResourceProfile.API_ONLY, ResourceProfile.HYBRID_RESOURCE, ResourceProfile.API_WITH_CACHE):
        score -= 2.0

    return max(0.0, score)


def _score_leap(
    difficulty: DifficultyLevel,
    ctx: RoutingContext,
    leap_budget_threshold: float = 0.30,
) -> float:
    """Score for LEAP Orchestrator strategy (0.0-10.0).

    ⚠️ NOT calibrated: weights are initial estimates, not tuned from
    experiment data. See docs/leap-codegraph-analysis.md for validation
    plan.

    Expected pattern: LEAP should be chosen for hard theorems where
    DFS/Beam/Hybrid have already failed. Domain bias (geometry -1) is
    a placeholder and may be incorrect.
    """
    score = 1.0
    if difficulty == DifficultyLevel.HARD:
        score += 4.0
    if ctx.previous_failures >= 3:
        score += 5.0
    elif ctx.previous_failures >= 1:
        score += 2.0
    if ctx.history_hint in ("stuck", "dead_loop", "diverging", "timeout"):
        score += 4.0
    if ctx.budget_remaining_usd < leap_budget_threshold:
        score -= 4.0
    if ctx.resource_profile not in (ResourceProfile.API_ONLY, ResourceProfile.HYBRID_RESOURCE, ResourceProfile.API_WITH_CACHE):
        score -= 3.0
    if ctx.domain in ("algebra", "number_theory"):
        score += 1.0
    elif ctx.domain == "geometry":
        score -= 1.0  # placeholder: may need adjustment
    return max(0.0, score)


# ═══════════════════════════════════════════════════════════════════
# Main Router
# ═══════════════════════════════════════════════════════════════════


class ModeRouter:
    """Selects the optimal proof search strategy for a given context.

    Usage
    -----
        from omega.engine.router import ModeRouter, RoutingContext

        router = ModeRouter()
        decision = router.route(RoutingContext(
            theorem_header="theorem t : 1+1=2 := by ...",
            resource_profile=ResourceProfile.API_ONLY,
            budget_remaining_usd=0.50,
        ))
        trajectory = decision.strategy.run(theorem)
    """

    def __init__(self, config: RouterConfig | None = None) -> None:
        self._config = config or RouterConfig()

    def route(self, ctx: RoutingContext) -> RoutingDecision:
        """Select the best search strategy for a given context.

        Parameters
        ----------
        ctx : RoutingContext
            The routing context with theorem, resources, and history.

        Returns
        -------
        RoutingDecision
            The selected strategy with confidence and reasoning.
        """
        difficulty = estimate_difficulty(ctx.theorem_header)
        domain = detect_domain(ctx.theorem_header)
        ctx.domain = domain

        # Score each strategy
        scores = {
            "dfs": _score_dfs(difficulty, ctx),
            "beam": _score_beam(difficulty, ctx),
            "hybrid": _score_hybrid(difficulty, ctx, self._config.budget_hybrid_threshold_usd),
            "leap": _score_leap(difficulty, ctx),
        }

        # Select winner
        winner = max(scores, key=scores.get)  # type: ignore[arg-type]
        max_score = scores[winner]
        total = sum(scores.values()) or 1.0
        confidence = min(1.0, max_score / total * 2.0)

        # Configure the selected strategy
        if winner == "dfs":
            strategy = get_strategy("dfs")
            reasoning = self._reason_dfs(difficulty, domain, ctx)
        elif winner == "beam":
            strategy = get_strategy("beam")
            reasoning = self._reason_beam(difficulty, domain, ctx)
        elif winner == "leap":
            strategy = get_strategy("leap")
            reasoning = self._reason_leap(difficulty, domain, ctx)
        else:
            strategy = get_strategy("hybrid")
            reasoning = self._reason_hybrid(difficulty, domain, ctx)

        return RoutingDecision(
            strategy_name=winner,
            strategy=strategy,
            confidence=confidence,
            reasoning=reasoning,
            scores=scores,
        )

    @staticmethod
    def _reason_dfs(difficulty: DifficultyLevel, domain: str | None, ctx: RoutingContext) -> str:
        parts = [f"DFS selected for {difficulty.value} theorem"]
        if domain:
            parts.append(f"(domain: {domain})")
        if ctx.previous_failures == 0:
            parts.append("— first attempt, single trajectory is cheapest")
        else:
            parts.append(f"— {ctx.previous_failures} previous failures, but budget is low")
        return " ".join(parts)

    @staticmethod
    def _reason_beam(difficulty: DifficultyLevel, domain: str | None, ctx: RoutingContext) -> str:
        parts = [f"Beam selected for {difficulty.value} theorem"]
        if ctx.resource_profile == ResourceProfile.LOCAL_GPU:
            parts.append("— local GPU available for fast parallel compilation")
        else:
            parts.append("— parallel trajectory exploration")
        return " ".join(parts)

    @staticmethod
    def _reason_hybrid(difficulty: DifficultyLevel, domain: str | None, ctx: RoutingContext) -> str:
        parts = [f"Hybrid selected for {difficulty.value} theorem"]
        if ctx.history_hint:
            parts.append(f"(previous termination: {ctx.history_hint})")
        if ctx.previous_failures >= 2:
            parts.append(f"— multi-path needed after {ctx.previous_failures} failures")
        if domain:
            parts.append(f"(domain: {domain})")
        return " ".join(parts)

    @staticmethod
    def _reason_leap(difficulty: DifficultyLevel, domain: str | None, ctx: RoutingContext) -> str:
        parts = [f"LEAP selected for {difficulty.value} theorem"]
        if ctx.previous_failures >= 3:
            parts.append(f"— {ctx.previous_failures} previous failures, need decomposition")
        elif ctx.previous_failures >= 1:
            parts.append(f"— {ctx.previous_failures} failures, DAG-based exploration")
        if ctx.history_hint:
            parts.append(f"(hint: {ctx.history_hint})")
        if ctx.domain in ("algebra", "number_theory"):
            parts.append("— decomposition-friendly domain")
        return " ".join(parts)
