#!/usr/bin/env python3
"""ProofAllocator — 多目标约束分配器（运筹优化层）。

将 N 个定理在预算/时间/尝试次数约束下，贪心地分配给最合适的模型。
属于 **plan 层**（不是 resource 层），因为这是跨多定理的批规划，
不是单定理的逐次升级（后者由 resource.ModelAllocator 负责）。

与 ModelAllocator 的区别：
  - ModelAllocator：单定理 escalation（本地→远程），用于 inner_loop 内
  - ProofAllocator：批量定理在 4 项约束下贪心分配，用于 prove-batch/benchmark

与 ModelRouter 的区别：
  - ModelRouter：单定理的短期路由决策（看几秒内的健康状况）
  - ProofAllocator：批量定理的中期规划（看整个 batch 的预算）

运筹框架：
  目标:  max  Σ(success_prob_i)        ← 最大化期望通过数
  约束:  Σ(cost_i)           ≤ budget   ← 预算上限
         Σ(time_i)           ≤ deadline ← 时间上限
         Σ(attempts_i)       ≤ max_n    ← 尝试次数上限
         model_id_i ∈ available_models  ← 可用模型集

求解策略：贪心（非精确求解），因为 cost/time/prob 是概率性的。

Usage:
    >>> from omega.plan.allocator import ProofAllocator
    >>> alloc = ProofAllocator()
    >>> plan = PlanManager.resolve()
    >>> assignments = alloc.allocate(theorems=[...], plan=plan.execution)
    >>> for a in assignments:
    ...     print(f"{a.tier:7s} → {a.model_id:30s} ({a.reason})")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from omega.plan.execution import ExecutionPlan
from omega.resource.model_router import _estimate_complexity

logger = logging.getLogger("omega.plan.allocator")

# ── Model capability table ──────────────────────────────────────
# These are domain-agnostic hand-estimates.  The MLRoute tier predictor
# (ModelRouter._ml_predict) provides more accurate per-theorem estimates
# when the trained model exists; these are used as fallback.

_MODEL_CAPS: dict[str, dict[str, Any]] = {
    "goedel/goedel-v2-8b": {
        "success_prob": {"simple": 0.85, "medium": 0.65, "hard": 0.30},
        "time_per_proof_s": {"simple": 5.0, "medium": 12.0, "hard": 25.0},
        "cost_per_call": 0.0,
    },
    "deepseek/deepseek-v4-flash": {
        "success_prob": {"simple": 0.70, "medium": 0.75, "hard": 0.60},
        "time_per_proof_s": {"simple": 2.0, "medium": 4.0, "hard": 10.0},
        "cost_per_call": 0.0005,
    },
    "deepseek/deepseek-v4-pro": {
        "success_prob": {"simple": 0.80, "medium": 0.85, "hard": 0.75},
        "time_per_proof_s": {"simple": 3.0, "medium": 6.0, "hard": 15.0},
        "cost_per_call": 0.002,
    },
    "local/qwen3-coder:30b": {
        "success_prob": {"simple": 0.75, "medium": 0.40, "hard": 0.10},
        "time_per_proof_s": {"simple": 8.0, "medium": 20.0, "hard": 40.0},
        "cost_per_call": 0.0,
    },
}


# ── Data types ──────────────────────────────────────────────────


@dataclass
class Assignment:
    """Single theorem-to-model assignment result."""

    theorem_header: str
    tier: str
    model_id: str
    reason: str
    estimated_cost: float
    estimated_time_s: float
    estimated_success_prob: float


@dataclass
class AllocationSummary:
    """Summary of a batch allocation."""

    n_theorems: int
    n_by_tier: dict[str, int]
    n_by_model: dict[str, int]
    total_estimated_cost: float
    total_estimated_time_s: float
    expected_passes: float
    constraints_met: bool
    violations: list[str]

    def report(self) -> str:
        lines = [
            "┌─ ProofAllocator Summary ─────────────────────────────",
            f"│  {self.n_theorems} theorems across {len(self.n_by_model)} models",
        ]
        for tier, count in sorted(self.n_by_tier.items()):
            lines.append(f"│  {tier:8s}: {count}")
        lines.append("│")
        for model, count in sorted(self.n_by_model.items()):
            name = model.split("/")[-1] if "/" in model else model
            lines.append(f"│  → {name:30s} × {count}")
        lines.append("│")
        lines.append(f"│  Est. cost       ${self.total_estimated_cost:.4f}")
        lines.append(f"│  Est. time       {self.total_estimated_time_s:.0f}s")
        lines.append(f"│  Expected passes {self.expected_passes:.1f}/{self.n_theorems}")
        if self.constraints_met:
            lines.append("│  ✅ Constraints met")
        else:
            for v in self.violations:
                lines.append(f"│  ❌ {v}")
        lines.append("└────────────────────────────────────────────────")
        return "\n".join(lines)


# ── Allocator ──────────────────────────────────────────────────


class ProofAllocator:
    """Multi-theorem proof allocator with greedy constraint optimization.

    Greedy algorithm:
    1. Estimate tier for each theorem.
    2. Score each (theorem, model) pair by ROI = success_prob / cost.
    3. Pick the best model per theorem.
    4. If total exceeds a constraint, downgrade lowest-ROI assignments.
    """

    def __init__(self, model_caps: dict | None = None):
        self._caps = model_caps or _MODEL_CAPS

    # ── Public API ─────────────────────────────────────────────

    def allocate(
        self,
        theorems: list[str],
        plan: ExecutionPlan,
        available_models: list[str] | None = None,
    ) -> list[Assignment]:
        """Greedy allocation of theorems to models.

        Args:
            theorems: Theorem headers to allocate.
            plan: ExecutionPlan with budget/gpu snapshot.
            available_models: Subset of model IDs to consider.
                Defaults to all models in capability table.

        Returns:
            List of ``Assignment``, one per theorem, in input order.
        """
        if available_models is None:
            available_models = list(self._caps.keys())

        # ── Respect preferred_model from ExecutionPlan ──
        self._preferred_model = None
        if plan.preferred_model and plan.preferred_model not in available_models:
            if plan.preferred_model in self._caps:
                available_models = [plan.preferred_model]
                self._preferred_model = plan.preferred_model
            else:
                logger.warning(
                    "preferred_model=%s not in capability table, falling back to all models",
                    plan.preferred_model,
                )
        elif plan.preferred_model and plan.preferred_model in available_models:
            # Move preferred_model to front so ROI ranks it first for free models
            available_models = [plan.preferred_model] + [
                m for m in available_models if m != plan.preferred_model
            ]
            self._preferred_model = plan.preferred_model

        budget_snap = plan.budget.snapshot()
        remaining_cost = budget_snap.remaining_cost_usd
        remaining_time = budget_snap.remaining_time_s
        remaining_attempts = budget_snap.remaining_attempts

        # Phase 1: score & assign each theorem independently
        assignments: list[Assignment] = []
        for header in theorems:
            tier, _flags, _base_tier = _estimate_complexity(header)
            candidates = self._score_candidates(header, tier, available_models)
            if not candidates:
                assignments.append(Assignment(
                    theorem_header=header,
                    tier=tier,
                    model_id="local/default",
                    reason="no_suitable_model",
                    estimated_cost=0.0,
                    estimated_time_s=30.0,
                    estimated_success_prob=0.05,
                ))
                continue
            best = candidates[0]
            assignments.append(Assignment(
                theorem_header=header,
                tier=tier,
                model_id=best["model_id"],
                reason=best["reason"],
                estimated_cost=best["cost"],
                estimated_time_s=best["time"],
                estimated_success_prob=best["prob"],
            ))

        # Phase 2: rebalance if constraints exceeded
        self._rebalance(
            assignments, available_models,
            remaining_cost, remaining_time, remaining_attempts,
        )

        return assignments

    def summary(
        self,
        assignments: list[Assignment],
        plan: ExecutionPlan | None = None,
    ) -> AllocationSummary:
        """Produce a human-readable summary from allocations."""
        n_by_tier: dict[str, int] = {}
        n_by_model: dict[str, int] = {}
        total_cost = 0.0
        total_time = 0.0
        expected = 0.0
        for a in assignments:
            n_by_tier[a.tier] = n_by_tier.get(a.tier, 0) + 1
            n_by_model[a.model_id] = n_by_model.get(a.model_id, 0) + 1
            total_cost += a.estimated_cost
            total_time += a.estimated_time_s
            expected += a.estimated_success_prob

        violations: list[str] = []
        constraints_met = True
        if plan is not None:
            budget_snap = plan.budget.snapshot()
            if total_cost > budget_snap.remaining_cost_usd:
                violations.append(
                    f"Cost ${total_cost:.4f} exceeds remaining ${budget_snap.remaining_cost_usd:.4f}"
                )
                constraints_met = False
            if total_time > budget_snap.remaining_time_s:
                violations.append(
                    f"Time {total_time:.0f}s exceeds remaining {budget_snap.remaining_time_s:.0f}s"
                )
                constraints_met = False
            if len(assignments) > budget_snap.remaining_attempts:
                violations.append(
                    f"Attempts {len(assignments)} exceeds remaining {budget_snap.remaining_attempts}"
                )
                constraints_met = False

        return AllocationSummary(
            n_theorems=len(assignments),
            n_by_tier=n_by_tier,
            n_by_model=n_by_model,
            total_estimated_cost=round(total_cost, 4),
            total_estimated_time_s=round(total_time, 1),
            expected_passes=round(expected, 1),
            constraints_met=constraints_met,
            violations=violations,
        )

    # ── Internal ───────────────────────────────────────────────

    def _score_candidates(
        self,
        _header: str,
        tier: str,
        available_models: list[str],
    ) -> list[dict]:
        """Rank available models for a given theorem by ROI.

        Returns empty list if no model has nonzero success probability.
        """
        candidates: list[dict] = []
        for model_id in available_models:
            caps = self._caps.get(model_id, {})
            sp = caps.get("success_prob", {}).get(tier, 0.0)
            if sp < 0.01:
                continue
            tp = caps.get("time_per_proof_s", {}).get(tier, 10.0)
            cost = caps.get("cost_per_call", 0.0)

            # ROI = expected success / cost (inverted for free models)
            roi = sp / max(cost, 0.0001) if cost > 0 else sp * 10_000.0

            if cost == 0:
                reason = f"free_local (p={sp:.2f})"
            elif roi > 1_000:
                reason = f"high_roi (p={sp:.2f}, ${cost:.4f}/call)"
            else:
                reason = f"capability (p={sp:.2f}, ${cost:.4f}/call)"

            candidates.append({
                "model_id": model_id,
                "prob": sp,
                "cost": cost,
                "time": tp,
                "roi": roi,
                "reason": reason,
            })

        # Sort: preferred model first (if set), then by ROI descending
        # If no preferred model, sort purely by ROI
        preferred = getattr(self, '_preferred_model', None)
        if preferred:
            candidates.sort(key=lambda c: (
                0 if c["model_id"] == preferred else 1,
                -c["roi"],
            ))
        else:
            candidates.sort(key=lambda c: -c["roi"])
        return candidates

    def _rebalance(
        self,
        assignments: list[Assignment],
        available_models: list[str],
        remaining_cost: float,
        remaining_time: float,
        remaining_attempts: int,
    ) -> None:
        """Greedy rebalance: downgrade lowest-ROI assignments.

        Iteratively finds the assignment with the worst success/time or
        success/cost ratio and downgrades it to a cheaper model, until
        all constraints are met or no further downgrade is possible.
        """
        n = len(assignments)
        if n == 0:
            return
        if n > remaining_attempts:
            # Can't fix too-many-attempts without dropping theorems
            logger.warning(
                "Allocation: %d theorems > %d remaining attempts — constraints will be violated",
                n, remaining_attempts,
            )
            return

        max_iter = n * 2  # safety limit
        for _iter in range(max_iter):
            cost_ok = remaining_cost >= sum(a.estimated_cost for a in assignments)
            time_ok = remaining_time >= sum(a.estimated_time_s for a in assignments)
            if cost_ok and time_ok:
                break

            # Find worst assignment by ROI = prob / (cost + time_penalty)
            def roi_key(i: int) -> float:
                a = assignments[i]
                denom = max(a.estimated_cost, 0.0001) + a.estimated_time_s / 60.0
                return a.estimated_success_prob / denom

            worst_idx = min(range(n), key=roi_key)
            a = assignments[worst_idx]

            cheaper = self._find_cheaper(a.tier, a.model_id, available_models)
            if cheaper is None:
                # Already at cheapest — can't improve this iteration
                continue

            caps = self._caps.get(cheaper, {})
            sp = caps.get("success_prob", {}).get(a.tier, 0.1)
            tp = caps.get("time_per_proof_s", {}).get(a.tier, 15.0)
            cost = caps.get("cost_per_call", 0.0)

            a.model_id = cheaper
            a.estimated_cost = cost
            a.estimated_time_s = tp
            a.estimated_success_prob = sp
            a.reason = f"downgraded_from_{a.model_id.split('/')[-1]}"

    def _find_cheaper(
        self,
        tier: str,
        current_model_id: str,
        available_models: list[str],
    ) -> str | None:
        """Find a cheaper model with nonzero success probability for this tier."""
        current_cost = self._caps.get(current_model_id, {}).get(
            "cost_per_call", float("inf")
        )
        candidates = [
            m for m in available_models
            if self._caps.get(m, {}).get("cost_per_call", float("inf")) < current_cost
            and self._caps.get(m, {}).get("success_prob", {}).get(tier, 0.0) > 0.05
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda m: self._caps.get(m, {}).get("cost_per_call", float("inf")))
        return candidates[0]
