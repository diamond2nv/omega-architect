#!/usr/bin/env python3
"""BudgetTracker — dual-tier with time-based dynamic token budgets.

Automatically selects the right budget tier based on model_id.
Local models (ollama, local/...) use **time-based dynamic tokens**:
  ``effective_budget = min(max_tokens, remaining_time × measured_tok_s)``

The tok/s rates come from ``benchmark.py`` (cached hardware measurements).

Remote models (DeepSeek, Claude, ...) use cost-constrained limits with
static token caps — remote API throughput is a cost concern, not a time one.
"""

from __future__ import annotations

import logging
from typing import Any

from omega.resource.config import BudgetConfig, BudgetTier, load_config

logger = logging.getLogger("omega.resource.budget")

# Lazy import benchmark to avoid circular deps
_bench_data: Any = None


def _get_bench_tok_s(model_id: str) -> float:
    """Get measured tok/s for a model_id, or 0 if unavailable."""
    global _bench_data
    if _bench_data is None:
        try:
            from omega.resource.benchmark import load_benchmark

            _bench_data = load_benchmark()
        except Exception:
            return 0.0
    try:
        return _bench_data.get_tok_s(model_id)
    except Exception:
        return 0.0


class BudgetTracker:
    """Track token, cost, time, and attempt budgets for a proof attempt.

    Two budget tiers:
    - **local**: Applied when ``is_free_model(model_id)`` is True.
      Tokens are **time-based**: ``effective_budget = remaining_time × measured_tok_s``.
      This binds the budget to real hardware throughput.
    - **remote**: Applied for paid API models.
      Cost-constrained ($2, 5M tokens, 1h) — token caps are static.

    Usage:
        >>> tracker = BudgetTracker()
        >>> tracker.check_token(100, 50, "local/model")  # local tier
        True
        >>> tracker.consume(100, 50, "local/model", elapsed_s=2.5)
        >>> tracker.check_token(100, 50, "deepseek/deepseek-chat")  # remote tier
        True
    """

    def __init__(self, cfg: BudgetConfig | None = None) -> None:
        self._cfg = cfg or load_config()

        # Local tier budgets
        self._local_cfg: BudgetTier = self._cfg.local
        self._local_time: float = float(self._local_cfg.max_time_s)
        self._local_attempts: int = int(self._local_cfg.max_attempts)

        # If no tok_s configured, try to load from benchmark cache
        self._local_tok_s: float = self._local_cfg.tok_s
        if self._local_tok_s <= 0:
            self._local_tok_s = _get_bench_tok_s("local/default")

        # Remote tier budgets
        self._remote_cfg: BudgetTier = self._cfg.remote
        self._remote_tokens: float = float(self._remote_cfg.max_tokens)
        self._remote_cost: float = float(self._remote_cfg.max_cost_usd)
        self._remote_time: float = float(self._remote_cfg.max_time_s)
        self._remote_attempts: int = int(self._remote_cfg.max_attempts)

        # Running totals
        self._total_tokens: float = 0.0
        self._total_cost: float = 0.0
        self._total_time: float = 0.0
        self._total_api_calls: int = 0    # 每次 API consume 计数
        self._total_rounds: int = 0        # 语义轮次（由记录者标记）

        # Stuck detection: cross-dimension coordination
        # Set by caller when starting a new problem
        self._stuck_expected_time: float = 60.0   # expected completion time (s)
        self._stuck_max_rounds: int = 20           # max rounds before stuck
        self._stuck_min_attempts: int = 10         # min API calls before stuck check

        # Dynamic rate tracking (adapts based on real consumption data)
        self._dynamic_tok_s: float = self._local_tok_s  # updated from real usage
        self._dynamic_samples: list[tuple[float, float]] = []  # (tokens, time)

    # ── Active tier ──────────────────────────────────────────

    def _tier(self, model_id: str) -> str:
        """Return ``'local'`` or ``'remote'`` for the given model."""
        return "local" if self.is_free_model(model_id) else "remote"

    def _tier_cfg(self, model_id: str) -> BudgetTier:
        return self._local_cfg if self._tier(model_id) == "local" else self._remote_cfg

    # ── Per-tier remaining ──────────────────────────────────

    def _remaining_tokens(self, model_id: str, _elapsed_s: float = 0.0) -> float:
        tier = self._tier(model_id)
        if tier == "local":
            # Time-based dynamic budget
            remaining_time = max(self._local_time, 0.0)
            effective = self._local_cfg.effective_token_budget(remaining_time)
            return float(effective)
        return max(self._remote_tokens, 0.0)

    def _remaining_cost(self, model_id: str) -> float:
        if self._tier(model_id) == "local":
            return 0.0
        return max(self._remote_cost, 0.0)

    def _remaining_time(self, model_id: str) -> float:
        if self._tier(model_id) == "local":
            return max(self._local_time, 0.0)
        return max(self._remote_time, 0.0)

    def _remaining_attempts(self, model_id: str) -> int:
        if self._tier(model_id) == "local":
            return max(self._local_attempts, 0)
        return max(self._remote_attempts, 0)

    # ── Budget checks (non-consuming) ───────────────────────

    def check_token(self, input_tokens: int, output_tokens: int, model_id: str = "") -> bool:
        """Return True if consuming *input_tokens + output_tokens* stays within budget.

        For local tier, uses time-based dynamic token budget.
        For remote tier, uses static token cap.
        """
        tier = self._tier(model_id)
        if tier == "local":
            # Dynamic check: time_remaining × tok_s
            remaining_time = max(self._local_time, 0.0)
            effective = self._local_cfg.effective_token_budget(remaining_time)
            return (input_tokens + output_tokens) <= effective
        total = input_tokens + output_tokens
        return total <= self._remaining_tokens(model_id)

    def check_cost(self, input_tokens: int, output_tokens: int, model_id: str = "") -> bool:
        """Return True if estimated cost stays within budget."""
        if self.is_free_model(model_id):
            return True
        cost = self.estimate_cost(model_id, input_tokens, output_tokens)
        return cost <= self._remaining_cost(model_id)

    def check_time(self, elapsed_s: float, model_id: str = "") -> bool:
        """Return True if *elapsed_s* stays within remaining time for the tier."""
        return elapsed_s <= self._remaining_time(model_id)

    def check_attempts(self, n: int = 1, model_id: str = "") -> bool:
        """Return True if *n* more attempts stay within budget for the tier."""
        return (self._remaining_attempts(model_id) - n) >= 0

    # ── Free-model detection ────────────────────────────────

    def is_free_model(self, model_id: str) -> bool:
        """Check if *model_id* matches a free-model prefix (e.g. ``ollama/``)."""
        return any(model_id.startswith(p) for p in self._cfg.free_models)

    # ── Cost estimation ─────────────────────────────────────

    def estimate_cost(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost for the given token counts and model."""
        if self.is_free_model(model_id):
            return 0.0
        model_cfg = self._cfg.model_prices.get(model_id)
        if model_cfg is None:
            return 0.0
        return input_tokens * model_cfg.get("input_per_token", 0.0) + output_tokens * model_cfg.get(
            "output_per_token", 0.0
        )

    # ── Consumption ─────────────────────────────────────────

    def consume(
        self, input_tokens: int, output_tokens: int, model_id: str, elapsed_s: float
    ) -> dict[str, Any]:
        """Record consumption and deduct from the appropriate tier.

        For local tier, also tracks real tok/s throughput to adapt
        the dynamic budget for future checks.
        """
        total_tok = input_tokens + output_tokens
        cost = self.estimate_cost(model_id, input_tokens, output_tokens)
        tier = self._tier(model_id)

        if tier == "local":
            self._local_time -= elapsed_s
            self._local_attempts -= 1
            # Track dynamic throughput
            if elapsed_s > 0:
                self._dynamic_samples.append((total_tok, elapsed_s))
                # Weighted average (recent samples weighted heavier)
                self._dynamic_tok_s = self._compute_dynamic_tok_s()
        else:
            self._remote_tokens -= total_tok
            self._remote_cost -= cost
            self._remote_time -= elapsed_s
            self._remote_attempts -= 1

        self._total_tokens += total_tok
        self._total_cost += cost
        self._total_time += elapsed_s
        self._total_api_calls += 1

        return {"tokens": total_tok, "cost": cost, "time": elapsed_s, "api_calls": 1}

    def record_time(self, elapsed_s: float, model_id: str = "") -> dict[str, Any]:
        """Record elapsed time only (no tokens/cost/attempts).

        Use this for local operations that consume wall time but not API quota:
        MCP tool calls, local compilation (CompileGate, edit_file), VerifierAgent.

        Deducts time from the appropriate tier.
        """
        tier = self._tier(model_id) if model_id else "remote"
        if tier == "local":
            self._local_time -= elapsed_s
        else:
            self._remote_time -= elapsed_s
        self._total_time += elapsed_s
        return {"time": elapsed_s}

    def record_round(self) -> None:
        """Mark a semantic round boundary (called by inner_loop per iteration).

        Separates round-level tracking from API-call-level tracking.
        """
        self._total_rounds += 1

    def set_stuck_thresholds(self, expected_time_s: float = 60.0,
                              max_rounds: int = 20,
                              min_api_calls: int = 10) -> None:
        """Configure stuck detection thresholds for a new problem.

        Parameters
        ----------
        expected_time_s : float
            Expected completion time for this problem (used as multiplier base).
        max_rounds : int
            Rounds above which stuck detection activates.
        min_api_calls : int
            Minimum API calls needed before stuck detection kicks in.
        """
        self._stuck_expected_time = expected_time_s
        self._stuck_max_rounds = max_rounds
        self._stuck_min_attempts = min_api_calls

    def is_stuck(self) -> bool:
        """Cross-dimension stuck detection.

        Returns True when ALL of these hold:
          1. Elapsed time > 3× expected time
          2. Total API calls > min_api_calls
          3. Total rounds > max_rounds
          4. Cost used > 0 (the system made attempts, not just sitting idle)

        This prevents the system from burning all four budget dimensions
        on a single intractable problem without any progress signal.
        """
        if self._stuck_expected_time <= 0:
            return False
        time_exceeded = self._total_time > 3.0 * self._stuck_expected_time
        attempts_exceeded = self._total_api_calls > self._stuck_min_attempts
        rounds_exceeded = self._total_rounds > self._stuck_max_rounds
        made_attempts = self._total_cost > 0.0 or self._total_api_calls > 3
        return (time_exceeded and attempts_exceeded
                and rounds_exceeded and made_attempts)

    def _compute_dynamic_tok_s(self) -> float:
        """Compute adaptive tok/s from recent consumption, with fallback.

        Uses weighted average: last 5 samples get 2× weight.
        Falls back to configured tok_s if no samples.
        """
        if not self._dynamic_samples:
            return self._local_tok_s
        # Recent 5 samples weighted double
        recent = self._dynamic_samples[-5:]
        weighted_total = 0.0
        weight_sum = 0.0
        for i, (toks, ts) in enumerate(recent):
            w = 2.0 if i >= len(recent) - 3 else 1.0
            if ts > 0:
                weighted_total += (toks / ts) * w
                weight_sum += w
        if weight_sum <= 0:
            return self._local_tok_s
        return weighted_total / weight_sum

    # ── Queries ─────────────────────────────────────────────

    def remaining(self, model_id: str = "") -> dict[str, float | int]:
        """Return remaining budget as ``{tokens, cost, time, attempts}``.

        When *model_id* is empty, returns remote tier (conservative default).
        For local tier, tokens are time-based dynamic.
        """
        return {
            "tokens": self._remaining_tokens(model_id),
            "cost": self._remaining_cost(model_id),
            "time": self._remaining_time(model_id),
            "attempts": self._remaining_attempts(model_id),
        }

    def dynamic_tok_s(self) -> float:
        """Return current adaptive tok/s estimate (from benchmark or real usage)."""
        return self._dynamic_tok_s

    def summary(self, model_id: str = "") -> str:
        """Return a human-readable budget summary for the relevant tier."""
        r = self.remaining(model_id)
        tier_name = self._tier(model_id) if model_id else "remote"
        tc = self._tier_cfg(model_id) if model_id else self._remote_cfg

        if tier_name == "local" and tc.tok_s > 0:
            static_max = tc.max_tokens
            time_based = (
                int(self._local_time * self._dynamic_tok_s)
                if self._dynamic_tok_s > 0
                else r["tokens"]
            )
            tok_line = f"  tokens:    {self._total_tokens:>12,.0f} / ~{time_based:>12,}  (≤{static_max:,} cap)"
        else:
            pct_tok = _pct(self._total_tokens, tc.max_tokens)
            tok_line = (
                f"  tokens:    {self._total_tokens:>12,.0f} / {r['tokens']:>12,.0f}  ({pct_tok}%)"
            )

        pct_cost = _pct(self._total_cost, tc.max_cost_usd)
        pct_time = _pct(self._total_time, tc.max_time_s)
        pct_api = _pct(self._total_api_calls, tc.max_attempts)
        pct_round = _pct(self._total_rounds, tc.max_attempts)
        return (
            f"BudgetTracker [{tier_name}] — used / remaining\n"
            f"{tok_line}\n"
            f"  cost (USD): {self._total_cost:>12.6f} / {r['cost']:>12.6f}  ({pct_cost}%)\n"
            f"  time (s):   {self._total_time:>12.2f} / {r['time']:>12.2f}  ({pct_time}%)\n"
            f"  api_calls:  {self._total_api_calls:>12} / {r['attempts']:>12}  ({pct_api}%)\n"
            f"  rounds:     {self._total_rounds:>12}  (marked by record_round)\n"
            f"  tok/s:      {self._dynamic_tok_s:>8.1f} (adaptive)"
        )

    def reset(self) -> None:
        """Reset all tracked consumption to initial values."""
        self.__init__(self._cfg)

    def __repr__(self) -> str:
        r = self.remaining("local")
        return (
            f"BudgetTracker(local_tokens_rem=~{r['tokens']}, "
            f"local_time_rem={r['time']:.0f}s, "
            f"tok_s={self._dynamic_tok_s:.1f})"
        )


def _pct(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(used / total * 100, 1)
