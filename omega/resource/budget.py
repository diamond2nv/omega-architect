"""BudgetTracker — track and enforce per-proof-attempt budget limits.

Pure Python stdlib; no external pricing database or LLM library required.
"""

from __future__ import annotations

import copy
from typing import Any

from omega.resource.config import DEFAULT_BUDGET


class BudgetTracker:
    """Track token, cost, time, and attempt budgets for a proof attempt.

    Usage:
        >>> tracker = BudgetTracker()
        >>> tracker.check_token(100, 50, "deepseek/deepseek-chat")
        True
        >>> tracker.consume(100, 50, "deepseek/deepseek-chat", elapsed_s=2.5)
        {'tokens': 150, 'cost': 4.9e-05, 'time': ...}
        >>> tracker.remaining()
        {'tokens': ..., 'cost': ..., 'time': ..., 'attempts': ...}
    """

    def __init__(self, config_dict: dict[str, Any] | None = None) -> None:
        self._config = copy.deepcopy(config_dict or DEFAULT_BUDGET)

        # Remaining budgets
        b = self._config["budget"]
        self._remaining_tokens: float = float(b["max_tokens"])
        self._remaining_cost: float = float(b["max_cost_usd"])
        self._remaining_time: float = float(b["max_time_s"])
        self._remaining_attempts: int = int(b["max_attempts"])

        # Running totals for summary
        self._total_tokens: float = 0.0
        self._total_cost: float = 0.0
        self._total_time: float = 0.0
        self._total_attempts: int = 0

    # ------------------------------------------------------------------
    # Budget checks (non-consuming)
    # ------------------------------------------------------------------

    def check_token(self, input_tokens: int, output_tokens: int, model_id: str = "") -> bool:
        """Return True if consuming *input_tokens + output_tokens* stays within budget.

        Token budget applies to ALL models (local ollama and paid alike)
        to prevent excessive generation.
        """
        total = input_tokens + output_tokens
        return total <= self._remaining_tokens

    def check_cost(self, input_tokens: int, output_tokens: int, model_id: str = "") -> bool:
        """Return True if the estimated cost stays within budget.

        Free models (ollama, local) return True silently — no cost budget.
        """
        if self.is_free_model(model_id):
            return True
        cost = self.estimate_cost(model_id, input_tokens, output_tokens)
        return cost <= self._remaining_cost

    def check_time(self, elapsed_s: float) -> bool:
        """Return True if *elapsed_s* stays within remaining time."""
        return elapsed_s <= self._remaining_time

    def check_attempts(self, n: int = 1) -> bool:
        """Return True if *n* more attempts stay within the attempt budget."""
        return (self._remaining_attempts - n) >= 0

    # ------------------------------------------------------------------
    # Free-model detection
    # ------------------------------------------------------------------

    def is_free_model(self, model_id: str) -> bool:
        """Check if *model_id* matches a free-model prefix (e.g. ``ollama/``)."""
        prefixes: list[str] = self._config.get("models", {}).get("free", [])
        return any(model_id.startswith(p) for p in prefixes)

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def estimate_cost(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost for the given token counts and model (model_id first)."""
        if self.is_free_model(model_id):
            return 0.0

        model_cfg = self._config.get("models", {}).get(model_id)
        if model_cfg is None:
            # Unknown model — return 0 (can't price it)
            return 0.0

        input_rate: float = model_cfg.get("input_per_token", 0.0)
        output_rate: float = model_cfg.get("output_per_token", 0.0)

        return input_tokens * input_rate + output_tokens * output_rate

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------

    def consume(
        self,
        input_tokens: int,
        output_tokens: int,
        model_id: str,
        elapsed_s: float,
    ) -> dict[str, Any]:
        """Record consumption of tokens, cost, time, and an attempt.

        Returns a snapshot dict with keys ``tokens``, ``cost``, ``time``,
        ``attempts`` representing the *consumed* amounts for this call.
        """
        total_tok = input_tokens + output_tokens
        cost = self.estimate_cost(model_id, input_tokens, output_tokens)

        if not self.is_free_model(model_id):
            self._remaining_tokens -= total_tok
            self._remaining_cost -= cost
        self._remaining_time -= elapsed_s
        self._remaining_attempts -= 1

        self._total_tokens += total_tok
        self._total_cost += cost
        self._total_time += elapsed_s
        self._total_attempts += 1

        return {
            "tokens": total_tok,
            "cost": cost,
            "time": elapsed_s,
            "attempts": 1,
        }

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def remaining(self) -> dict[str, float | int]:
        """Return remaining budget as ``{tokens, cost, time, attempts}``."""
        return {
            "tokens": max(self._remaining_tokens, 0),
            "cost": max(self._remaining_cost, 0.0),
            "time": max(self._remaining_time, 0.0),
            "attempts": max(self._remaining_attempts, 0),
        }

    def summary(self) -> str:
        """Return a human-readable budget summary."""
        r = self.remaining()
        b = self._config["budget"]
        pct_tok = _pct(self._total_tokens, b["max_tokens"])
        pct_cost = _pct(self._total_cost, b["max_cost_usd"])
        pct_time = _pct(self._total_time, b["max_time_s"])
        pct_att = _pct(self._total_attempts, b["max_attempts"])

        return (
            f"BudgetTracker — used / remaining\n"
            f"  tokens:    {self._total_tokens:>10,.0f} / {r['tokens']:>10,.0f}  ({pct_tok}% used)\n"
            f"  cost (USD): {self._total_cost:>10.6f} / {r['cost']:>10.6f}  ({pct_cost}% used)\n"
            f"  time (s):   {self._total_time:>10.2f} / {r['time']:>10.2f}  ({pct_time}% used)\n"
            f"  attempts:   {self._total_attempts:>10} / {r['attempts']:>10}  ({pct_att}% used)"
        )

    def reset(self) -> None:
        """Reset all tracked consumption to initial values."""
        self.__init__(self._config)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        r = self.remaining()
        return (
            f"BudgetTracker(tokens_rem={r['tokens']}, cost_rem={r['cost']:.6f}, "
            f"time_rem={r['time']:.1f}s, attempts_rem={r['attempts']})"
        )


def _pct(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(used / total * 100, 1)
