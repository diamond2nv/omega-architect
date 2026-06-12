#!/usr/bin/env python3
"""PlanManager — Ω-Architect 统一计划管理层。

三层计划的统一入口和 resolve() 管线：
  PathPlan    → 全资源路径
  GPUPlan     → GPU 资源模式
  BudgetPlan  → 预算层级
  ExecutionPlan → 三层集成 + 交叉约束

用法:
    >>> from omega.plan import PlanManager
    >>> mgr = PlanManager.resolve(
    ...     budget_tier="development",
    ...     gpu_mode="gpu-minimal",
    ... )
    >>> mgr.pre_flight()
    [Paths] Lean 4.30.0 ✅ | Mathlib ✅ (8109 oleans) | Bench ✅ | Cache ~/.omega/cache
    [GPU]   ✅ NVIDIA RTX 4500 Ada (vllm, 24.4 tok/s, 21334/24564 MB free)
    [Budget] ✅ Budget OK: cost $0.00/$0.50, time 0.0s/120.0s, attempts 0/10
    ✅ Pre-flight PASSED
    >>> mgr.estimate(50)
    {'n_proofs': 50, 'estimated_time_s': 102.5, 'estimated_success_rate': 0.6}

    # 传给 inner_loop
    >>> from omega.loop.inner import inner_loop
    >>> result = inner_loop(theorem="...", plan=mgr.execution)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from omega.plan.allocator import AllocationSummary, Assignment, ProofAllocator
from omega.plan.budget_plan import BudgetPlan
from omega.plan.execution import ExecutionPlan
from omega.plan.gpu_plan import GPUPlan
from omega.plan.path_plan import PathPlan, resolve_path_plan

logger = logging.getLogger("omega.plan")


@dataclass
class PlanManager:
    """Ω-Architect 统一计划管理器。

    持有三个子计划 + 集成执行计划。
    """

    paths: PathPlan
    gpu: GPUPlan
    budget: BudgetPlan
    execution: ExecutionPlan

    # ── 工厂 ──────────────────────────────────────────────

    @classmethod
    def resolve(
        cls,
        budget_tier: str = "production",
        gpu_mode: str = "gpu-minimal",
        preferred_model: str | None = None,
        **path_overrides,
    ) -> PlanManager:
        """一次调用解析全部三层计划。

        Args:
            budget_tier: "development" | "production" | "exhaustive"
            gpu_mode: "cpu-only" | "gpu-minimal" | "gpu-batch"
            preferred_model: 偏好模型 ID（如 "deepseek/deepseek-v4-flash"）。
                设置后 ProofAllocator 分配时只考虑该模型。
            **path_overrides: 覆盖 PathPlan 的特定路径（如 mcp_server="..."）

        Returns:
            完全初始化的 PlanManager
        """
        paths = resolve_path_plan(**path_overrides)
        gpu = GPUPlan.for_mode(gpu_mode)
        budget = BudgetPlan.for_tier(budget_tier)
        execution = ExecutionPlan(budget=budget, gpu=gpu, paths=paths,
                                  preferred_model=preferred_model)

        logger.info(
            "PlanManager resolved: budget=%s, gpu=%s, lean=%s",
            budget_tier, gpu_mode, paths.version,
        )

        return cls(paths=paths, gpu=gpu, budget=budget, execution=execution)

    # ── 便捷方法 ──────────────────────────────────────────

    def pre_flight(self, model_id: str = "deepseek/deepseek-v4-flash") -> str:
        """运行前全量检查（委托给 ExecutionPlan.pre_flight）。"""
        return self.execution.pre_flight(model_id=model_id)

    def estimate(self, n_proofs: int = 50) -> dict[str, Any]:
        """估算运行时间/成本（委托给 ExecutionPlan.estimate）。"""
        return self.execution.estimate(n_proofs=n_proofs)

    def validate(self):
        """交叉约束验证。"""
        return self.execution.validate()

    def allocate(
        self,
        theorems: list[str],
        available_models: list[str] | None = None,
    ) -> list[Assignment]:
        """运筹优化：在预算/时间/尝试次数约束下，为批量定理分配最佳模型。

        Args:
            theorems: 定理列表。
            available_models: 候选模型列表。默认使用 ``ProofAllocator``
                的内置能力表（goedel, flash, pro, local）。

        Returns:
            每个定理的 ``Assignment`` 列表（与输入顺序一致）。
        """
        alloc = ProofAllocator()
        return alloc.allocate(theorems, self.execution, available_models=available_models)

    def allocation_summary(self, theorems: list[str]) -> str:
        """分析并报告批量定理的最优分配方案。

        不执行实际证明，只做"如果运行会怎样"的规划分析。
        """
        alloc = ProofAllocator()
        assignments = alloc.allocate(theorems, self.execution)
        return alloc.summary(assignments, self.execution).report()

    def to_dict(self) -> dict[str, Any]:
        return self.execution.to_dict()


# ── 模块级便捷函数 ────────────────────────────────────────


def quick_plan() -> PlanManager:
    """快速创建开发用计划（development tier, gpu-minimal mode）。

    适合交互式调试：
        >>> from omega.plan import quick_plan
        >>> plan = quick_plan()
        >>> print(plan.pre_flight())
    """
    return PlanManager.resolve(budget_tier="development", gpu_mode="gpu-minimal")
