"""runtime — 运行时自适应层。

监测硬件事件并自动降级/升级策略。在 PlanManager 启动后运行，
持续监控 API/GPU/内存状态，在异常时触发降级。

用法:
    >>> from omega.gpu_layer.runtime import RuntimeAdapter
    >>> from omega.plan import PlanManager
    >>> plan = PlanManager.resolve()
    >>> adapter = RuntimeAdapter(plan)
    >>> warnings = adapter.pre_flight()
    >>> for w in warnings:
    ...     print(f"⚠ {w}")
    >>> # 运行中: API timeout → 自动降级
    >>> adapter.on_api_error("timeout connecting to DeepSeek API")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omega.gpu_layer.detector import HardwareProfile
    from omega.plan import PlanManager

logger = logging.getLogger("omega.gpu_layer.runtime")


class RuntimeAdapter:
    """运行时自适应 — 硬件事件监测 → 策略降级/升级。

    RuntimeAdapter 在 PlanManager 之上运行，不改变 PlanManager 的接口。
    它只做两件事:
        1. pre_flight(): 运行前检查，返回 warnings 列表
        2. 事件驱动降级: API/GPU/内存事件 → 自动调整策略

    Attributes:
        plan: 当前关联的 PlanManager（原地降级会替换其内部计划）
        hw: 初始硬件画像（启动时检测，运行中不刷新）
        capability: 初始能力等级
    """

    def __init__(self, plan: PlanManager) -> None:
        self._plan = plan
        self._original_profile: HardwareProfile | None = None
        self._current_capability: str = ""
        self._calls_since_last_check = 0

    # ── 属性 ──────────────────────────────────────────────────

    @property
    def plan(self) -> PlanManager:
        return self._plan

    @property
    def capability(self) -> str:
        return self._current_capability

    # ── 运行前检查 ────────────────────────────────────────────

    def pre_flight(self) -> list[str]:
        """运行前全面检查，返回 warnings 列表。

        检查项:
            - 如果是 cpu-only 模式，确认无 GPU 依赖未声明
            - 内存不足时发出预警
            - 记录初始硬件画像
        """
        from omega.gpu_layer.detector import HardwareProfile

        self._original_profile = HardwareProfile.detect()
        assert self._original_profile is not None
        self._current_capability = self._original_profile.capability

        warnings: list[str] = []

        # CPU-only 警告
        if self._current_capability == "cpu-only":
            warnings.append(
                f"[HW] CPU-only 模式: {self._original_profile.cpu_cores_physical}物理核心, "
                f"GPU 不可用, DeepSeek API 为唯一推理后端"
            )
            if self._plan.gpu.mode not in ("cpu-only",):
                warnings.append(
                    f"[HW] PlanManager 的 gpu_mode='{self._plan.gpu.mode}' 与检测结果 "
                    f"'{self._current_capability}' 不匹配 — 建议使用 resolve_auto()"
                )

        # 内存预警
        if self._original_profile.ram_available_gb < 2.0:
            warnings.append(
                f"[MEM] 可用内存仅 {self._original_profile.ram_available_gb:.1f}GB, "
                f"Lean 编译可能需要 >2GB, 建议关闭其他程序"
            )

        # 打印摘要
        logger.info(
            "Hardware profile: %s — capability=%s",
            self._original_profile.summary(),
            self._current_capability,
        )

        return warnings

    # ── 事件驱动降级 ─────────────────────────────────────────

    def on_api_error(self, error: str) -> None:
        """DeepSeek API 故障时的降级策略。

        只在 cpu-only 模式下有意义（无本地 GPU 可回退），
        所以主要是 retry 层面处理。这里仅记录日志。
        """
        error_lower = error.lower()

        if "timeout" in error_lower:
            logger.warning("API timeout — 依赖 retry 层自动重试")
            return

        if "quota" in error_lower or "rate limit" in error_lower:
            logger.warning("API quota/rate limit — 建议降低并发或等待")
            return

        if "auth" in error_lower or "key" in error_lower:
            logger.error("API auth failure — DeepSeek API key 可能无效")
            return

        logger.warning("API error (unclassified): %s", error[:200])

    def on_gpu_lost(self) -> None:
        """GPU 被抢占时的紧急降级。

        从 gpu-full/gpu-light → cpu-only，用 DeepSeek API 兜底。
        仅在 GPU 可用模式下有意义。
        """
        if self._current_capability in ("cpu-only", "mps-only"):
            return  # 本来就无 GPU

        logger.error(
            "GPU lost! 从 %s 紧急降级到 cpu-only (DeepSeek API 兜底)",
            self._current_capability,
        )
        self._downgrade("cpu-only")

    def on_memory_pressure(self, available_gb: float) -> None:
        """可用内存不足时预警。

        Args:
            available_gb: 当前可用内存 (GB)
        """
        if available_gb < 1.0:
            logger.warning(
                "严重内存不足 (%.1f GB)，建议减小 batch size 或关闭其他程序",
                available_gb,
            )

    def _downgrade(self, target_mode: str) -> None:
        """原地降级 PlanManager 的 GPU 模式。"""
        from omega.plan import PlanManager

        old_mode = self._plan.gpu.mode
        self._plan = PlanManager.resolve(
            budget_tier=self._plan.budget.tier,
            gpu_mode=target_mode,
        )
        self._current_capability = target_mode
        logger.info("Downgrade: %s → %s", old_mode, target_mode)
