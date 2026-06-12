#!/usr/bin/env python3
"""BudgetPlan — 从被动记账升级为主动预算计划。

包装 BudgetTracker 并添加：
  - 预定义 tier presets（development / production / exhaustive）
  - remaining() 快照
  - assert_available() 运行前检查
  - 与 GPUPlan 的交叉感知（本地推理 vs 远程 API）

用法:
    >>> from omega.plan.budget_plan import BudgetPlan, BudgetTier
    >>> plan = BudgetPlan.for_tier("development")
    >>> plan.assert_available("deepseek/deepseek-v4-flash")
    ✅ Budget OK: cost $0.00/$0.50, time 0.0s/120.0s
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from omega.resource.budget import BudgetTracker

logger = logging.getLogger("omega.plan.budget_plan")


# ── 预定义 Tier ──────────────────────────────────────────────


@dataclass
class TierPreset:
    """预算层级预设。"""

    label: str
    max_cost_usd: float
    max_tokens_remote: int
    max_time_s: float
    max_attempts: int
    description: str


TIER_PRESETS: dict[str, TierPreset] = {
    "development": TierPreset(
        label="development",
        max_cost_usd=0.50,
        max_tokens_remote=100_000,
        max_time_s=120.0,
        max_attempts=10,
        description="开发/调试：低成本、短时限、少尝试",
    ),
    "production": TierPreset(
        label="production",
        max_cost_usd=2.00,
        max_tokens_remote=500_000,
        max_time_s=300.0,
        max_attempts=50,
        description="正式运行：合理成本、中时限、标准尝试",
    ),
    "exhaustive": TierPreset(
        label="exhaustive",
        max_cost_usd=10.00,
        max_tokens_remote=2_000_000,
        max_time_s=3600.0,
        max_attempts=200,
        description="穷举搜索：高成本、长时限、大量尝试",
    ),
}


# ── 计划 ──────────────────────────────────────────────────────


@dataclass
class BudgetSnapshot:
    """当前预算快照。"""

    tier: str
    elapsed_s: float
    attempts_used: int
    cost_usd: float
    tokens_consumed: int
    remaining_cost_usd: float
    remaining_time_s: float
    remaining_attempts: int
    exhausted: bool

    def summary(self) -> str:
        if self.exhausted:
            return f"❌ Budget exhausted (${self.cost_usd:.2f}, {self.attempts_used} attempts)"
        return (
            f"✅ Budget OK: cost ${self.cost_usd:.2f}/${self.remaining_cost_usd + self.cost_usd:.2f}, "
            f"time {self.elapsed_s:.0f}s/{self.elapsed_s + self.remaining_time_s:.0f}s, "
            f"attempts {self.attempts_used}/{self.attempts_used + self.remaining_attempts}"
        )


class BudgetPlan:
    """预算计划 — 运行前声明意图，运行中持续检查。

    与 BudgetTracker 的不同：
    - BudgetTracker 是被动记账（记了多少花多少）
    - BudgetPlan 是主动计划（预设 tier、预先检查、预警告）
    """

    def __init__(
        self,
        tier: str = "production",
        tracker: BudgetTracker | None = None,
    ) -> None:
        self._tier_name = tier
        self._preset = TIER_PRESETS.get(tier, TIER_PRESETS["production"])
        self._tracker = tracker or BudgetTracker()

        # 运行状态
        self._start_time: datetime | None = None
        self._attempts: int = 0
        self._cost: float = 0.0
        self._tokens: int = 0

    # ── 工厂 ──────────────────────────────────────────────

    @classmethod
    def for_tier(cls, tier: str = "production") -> BudgetPlan:
        """按预设层级创建计划。"""
        return cls(tier=tier)

    # ── 运行前检查 ────────────────────────────────────────

    def assert_available(self, model_id: str) -> BudgetSnapshot:
        """运行前检查预算是否充足。抛出 ValueError 若不足。"""
        ok = self._tracker.check_token(
            0, 0, model_id=model_id
        )
        if not ok:
            raise ValueError(
                f"Budget pre-check FAILED: tier={self._tier_name} "
                f"(cost={self._preset.max_cost_usd}, tokens={self._preset.max_tokens_remote})"
            )
        return self.snapshot()

    # ── 运行时记账 ────────────────────────────────────────

    def start(self) -> None:
        """标记运行开始。"""
        self._start_time = datetime.now(UTC)

    def consume(self, input_tokens: int, output_tokens: int, model_id: str, elapsed_s: float) -> None:
        """消耗一次 LLM 调用的预算。"""
        self._attempts += 1
        self._tokens += input_tokens + output_tokens
        self._tracker.consume(input_tokens, output_tokens, model_id, elapsed_s)

    # ── 快照 ──────────────────────────────────────────────

    def snapshot(self) -> BudgetSnapshot:
        """获取当前预算快照。"""
        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(UTC) - self._start_time).total_seconds()

        remaining_time = max(0.0, self._preset.max_time_s - elapsed)
        remaining_attempts = max(0, self._preset.max_attempts - self._attempts)

        # 从 tracker 获取成本估算（简化：按远程 tier 估算）
        cost_used = self._cost
        remaining_cost = max(0.0, self._preset.max_cost_usd - cost_used)

        exhausted = (
            remaining_time <= 0
            or remaining_attempts <= 0
            or remaining_cost <= 0
        )

        return BudgetSnapshot(
            tier=self._tier_name,
            elapsed_s=elapsed,
            attempts_used=self._attempts,
            cost_usd=cost_used,
            tokens_consumed=self._tokens,
            remaining_cost_usd=remaining_cost,
            remaining_time_s=remaining_time,
            remaining_attempts=remaining_attempts,
            exhausted=exhausted,
        )

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        s = self.snapshot()
        return {
            "tier": self._tier_name,
            "preset": {
                "max_cost_usd": self._preset.max_cost_usd,
                "max_tokens_remote": self._preset.max_tokens_remote,
                "max_time_s": self._preset.max_time_s,
                "max_attempts": self._preset.max_attempts,
            },
            "snapshot": {
                "elapsed_s": s.elapsed_s,
                "attempts_used": s.attempts_used,
                "cost_usd": s.cost_usd,
                "tokens_consumed": s.tokens_consumed,
                "exhausted": s.exhausted,
            },
        }

    @property
    def tier(self) -> str:
        return self._tier_name

    @property
    def preset(self) -> TierPreset:
        return self._preset
