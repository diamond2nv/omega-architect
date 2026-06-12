#!/usr/bin/env python3
"""ExecutionPlan — 三层预算/GPU/路径计划的集成与交叉约束。

组合 BudgetPlan + GPUPlan + PathPlan 为一个统一的 ExecutionPlan，
并在运行前执行交叉约束验证。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from omega.plan.budget_plan import BudgetPlan
from omega.plan.gpu_plan import GPUPlan
from omega.plan.path_plan import PathPlan

logger = logging.getLogger("omega.plan.execution")


@dataclass
class ConstraintIssue:
    """交叉约束违反。"""

    severity: str  # "error" | "warning"
    layer: str     # "budget" | "gpu" | "path" | "cross"
    message: str


@dataclass
class ValidationResult:
    """验证结果。"""

    passed: bool
    issues: list[ConstraintIssue]

    def summary(self) -> str:
        if self.passed:
            return "✅ All plan constraints validated"
        errors = [i for i in self.issues if i.severity == "error"]
        warnings = [i for i in self.issues if i.severity == "warning"]
        parts = []
        if errors:
            parts.append(f"❌ {len(errors)} error(s)")
        if warnings:
            parts.append(f"⚠️ {len(warnings)} warning(s)")
        return f"{', '.join(parts)}: {self.issues[0].message if self.issues else 'unknown'}"


class ExecutionPlan:
    """三层集成执行计划。

    创建后调用 validate() 检查交叉约束，通过后传给 inner_loop()。

    ``preferred_model`` 用于 ProofAllocator 分配时只考虑该模型，
    与 inner_loop 的实际 cfg.model 保持一致。
    """

    def __init__(
        self,
        budget: BudgetPlan,
        gpu: GPUPlan,
        paths: PathPlan,
        preferred_model: str | None = None,
    ) -> None:
        self.budget = budget
        self.gpu = gpu
        self.paths = paths
        self.preferred_model = preferred_model

    # ── 工厂 ──────────────────────────────────────────────

    @classmethod
    def resolve(
        cls,
        budget_tier: str = "production",
        gpu_mode: str = "gpu-minimal",
        preferred_model: str | None = None,
    ) -> ExecutionPlan:
        """从零解析完整的三层计划。

        等价于 PlanManager.resolve() 的轻量版，适合快速测试。

        Args:
            budget_tier: "development" | "production" | "exhaustive"
            gpu_mode: "cpu-only" | "gpu-minimal" | "gpu-batch"
            preferred_model: 偏好模型 ID（如 "deepseek/deepseek-v4-flash"）。
                设置后 ProofAllocator 分配时只考虑该模型。
        """
        from omega.plan.path_plan import resolve_path_plan
        return cls(
            budget=BudgetPlan.for_tier(budget_tier),
            gpu=GPUPlan.for_mode(gpu_mode),
            paths=resolve_path_plan(),
            preferred_model=preferred_model,
        )

    # ── 验证 ──────────────────────────────────────────────

    def validate(self) -> ValidationResult:
        """交叉约束验证。"""
        issues: list[ConstraintIssue] = []

        # ── 单层检查 ──

        # 路径
        if not self.paths.binaries_ok():
            issues.append(ConstraintIssue(
                severity="error", layer="path",
                message=f"Lean binaries not found: lean={self.paths.lean_bin}, lake={self.paths.lake_bin}",
            ))
        if self.paths.mathlib_ok() < 100:
            issues.append(ConstraintIssue(
                severity="warning", layer="path",
                message=f"Mathlib cache low ({self.paths.olean_count} oleans): T2 compile may be slow",
            ))
        if not self.paths.mcp_ok():
            issues.append(ConstraintIssue(
                severity="warning", layer="path",
                message=f"MCP server not configured (current: {self.paths.mcp_server})",
            ))

        # GPU
        gpu_snap = self.gpu.snapshot()
        if not gpu_snap.available and self.gpu.mode != "cpu-only":
            issues.append(ConstraintIssue(
                severity="error", layer="gpu",
                message=f"GPU not available in mode '{self.gpu.mode}'",
            ))

        # 预算 — 只做轻量检查，实际预算在运行时由 BudgetPlan.assert_available() 执行

        # ── 交叉约束 ──

        # GPU mode ↔ budget tier: gpu-batch 要求 production 及以上
        if self.gpu.mode == "gpu-batch" and self.budget.tier == "development":
            issues.append(ConstraintIssue(
                severity="warning", layer="cross",
                message="GPU mode 'gpu-batch' with budget tier 'development' may exhaust budget quickly. "
                        "Consider 'production' or 'exhaustive'.",
            ))

        # GPU gpu-minimal ↔ lean mathlib: T2 compile 需要 lean binary
        if self.gpu.mode == "cpu-only" and not self.paths.binaries_ok():
            issues.append(ConstraintIssue(
                severity="error", layer="cross",
                message="CPU-only mode requires Lean binaries for T2 compile gate",
            ))

        return ValidationResult(
            passed=not any(i.severity == "error" for i in issues),
            issues=issues,
        )

    # ── 估算 ──────────────────────────────────────────────

    def estimate(self, n_proofs: int = 50) -> dict[str, Any]:
        """估算时间、成本、成功率。"""
        gpu_time = self.gpu.estimate_time(n_proofs)
        budget_snap = self.budget.snapshot()

        # 简化的成功率估算
        success_rate = 0.5  # 基线 50%
        if "goedel" in str(self.paths.lean_bin):
            success_rate += 0.1  # Goedel 加成
        if self.gpu.mode == "gpu-batch":
            success_rate += 0.15  # 批量采样加成
        success_rate = min(success_rate, 0.95)

        return {
            "n_proofs": n_proofs,
            "estimated_time_s": round(gpu_time, 1),
            "estimated_cost_usd": round(budget_snap.cost_usd, 4),
            "estimated_success_rate": round(success_rate, 2),
            "budget_remaining": {
                "time_s": budget_snap.remaining_time_s,
                "attempts": budget_snap.remaining_attempts,
                "cost_usd": budget_snap.remaining_cost_usd,
            },
        }

    # ── 运行前入口 — 一次调用完成全部检查 ────────────────

    def pre_flight(self, model_id: str = "deepseek/deepseek-v4-flash") -> str:
        """运行前全量检查。返回 summary 字符串。"""
        parts: list[str] = []

        # 1. 路径检查
        parts.append(f"[Paths] {self.paths.summary()}")

        # 2. GPU 检查
        gpu_snap = self.gpu.assert_available()
        parts.append(f"[GPU]   {gpu_snap.summary()}")

        # 3. 预算检查
        budget_snap = self.budget.assert_available(model_id)
        parts.append(f"[Budget] {budget_snap.summary()}")

        # 4. 交叉约束
        validation = self.validate()
        if not validation.passed:
            for issue in validation.issues:
                level = "❌" if issue.severity == "error" else "⚠️"
                parts.append(f"[{level}] {issue.layer}: {issue.message}")
            parts.append("❌ Pre-flight FAILED")
        else:
            parts.append("✅ Pre-flight PASSED")

        return "\n".join(parts)

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget.to_dict(),
            "gpu": self.gpu.to_dict(),
            "paths": self.paths.to_dict(),
        }
