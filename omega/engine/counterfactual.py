"""L3 反事实验证 —— 擦除-重编译归因 (counterfactual attribution)。

为什么需要这一层
----------------
wiki `concepts/diagnosis-technique-spectrum-2026-09` 把诊断栈分成「四层 + 金标准」，
并明写：**反事实验证是唯一「确认」；没有它的诊断只是相关性报告**。

回源审计 `concepts/zero-token-diagnosis-layer-audit-2026` §4/§9.1 实测：本仓
``engine/mcts_diagnosis.py`` 的产出（``stuck_nodes`` / ``blind_spots`` / ``error_heat`` /
``visit_entropy``）**全部落在诊断栈 L0「只产线索」**，另 ``failure_chain`` 部分落在
L2 动态建模；而 **L1 不确定度 / L2 机理白箱 / L2 因果定位 / L3 反事实验证全部缺失**
（当时 ``grep counterfactual|intervention|rollback`` = 0 命中）。本模块补 **L3**。

算法来源（母页 §9 对 Qin 2012 的精读）
-------------------------------------
Qin 2012（Annu. Rev. Control, ``10.1016/j.arcontrol.2012.09.004``）的 **RBC 重构贡献**：

    「沿候选方向擦除重构、**令指标回落者即元凶**」= 反事实式抗串扰归因；
    并明说**贡献图有 smearing 串扰缺陷（可指错）**。

套到 omega：

    现状（L0 探针）: 统计 error_class@depth 频次得「热区」   ← 贡献图式，有 smearing
    本模块（L3）   : 从证明体擦除候选 tactic → 重编译 → 看**失败是否真消失**
                     「失败消失的那个」= 元凶，而不是「出现最多的那个」

为什么在 Lean 侧特别划算
------------------------
L3 在一般系统里是最贵的一层（需可重放环境 + 干预接口）。但 Lean 是纯函数式编译器，
且 ``CompileGate`` 自带 SHA 缓存（热态实测 ~2.1 s/次，见
``docs/experiments/mcts-lean-e2e-2026-09-10.md`` §5）
⇒ **L3 在 Lean 侧近乎零边际成本**。

设计约束
--------
- **0-token**：只调用编译回调，不碰 LLM。
- **compile_fn 可注入**：测试可传假实现，**无需真 Lean 工具链**（CI 友好）。
- **指标可插拔**：默认「错误条数」，可换成功率 / 误差类严重度等。
- 编译结果同时兼容 ``omega.loop.compile_gate.CompileResult``（dataclass）与
  ``make_real_compile_callback`` 的 dict 形状（契约见 commit ``0abeda3``）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 编译回调：Lean 代码 → 结果。结果可以是 CompileResult(dataclass) 或 dict。
CompileFn = Callable[[str], Any]


# ═══════════════════════════════════════════════════════════════════
# 编译结果容错读取（dataclass / dict 两种形状）
# ═══════════════════════════════════════════════════════════════════

def _get(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def is_success(result: Any) -> bool:
    """编译是否通过。优先显式 ``success``，退化到 ``exit_code == 0``。"""
    s = _get(result, "success", None)
    if s is None:
        return _get(result, "exit_code", None) == 0
    return bool(s)


def errors_of(result: Any) -> list[str]:
    """错误消息列表（dict 形状下从 ``diagnostics`` 里挑 severity==error）。"""
    errs = _get(result, "errors", None)
    if errs:
        return [str(e) for e in errs]
    diags = _get(result, "diagnostics", None) or []
    out: list[str] = []
    for d in diags:
        if isinstance(d, dict) and d.get("severity") == "error":
            out.append(str(d.get("message", "")))
    return out


def error_class_of(result: Any) -> str:
    """主导错误类（枚举取 ``.value``，字符串原样）。"""
    ec = _get(result, "error_class", None)
    if ec is None:
        return ""
    return getattr(ec, "value", None) or str(ec)


# ═══════════════════════════════════════════════════════════════════
# 默认失败指标
# ═══════════════════════════════════════════════════════════════════

def error_count_metric(result: Any) -> float:
    """默认失败指标：``0.0`` = 编译通过；否则 ``1.0 + 错误条数``。

    ``+1.0`` 保证「通过」**严格小于任何失败**——否则「错误更少但仍在失败」会被
    误判为回落，那正是 L0 贡献图指错的一种形式。
    """
    if is_success(result):
        return 0.0
    return 1.0 + len(errors_of(result))


# ═══════════════════════════════════════════════════════════════════
# 渲染
# ═══════════════════════════════════════════════════════════════════

def render_by_block(theorem_header: str, tactics: Sequence[str], indent: str = "  ") -> str:
    """把战术序列渲染成 Lean 代码（``theorem … := by`` + 缩进战术块）。

    空序列 ⇒ ``by`` + ``sorry`` —— 保证**始终可编译**，便于比较基线。
    """
    header = theorem_header.strip()
    if header.endswith(":="):
        header = header[:-2].rstrip()
    body = [f"{indent}{t.strip()}" for t in tactics if t and t.strip()]
    if not body:
        body = [f"{indent}sorry"]
    return header + " := by\n" + "\n".join(body)


# ═══════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RepairTarget:
    """**可执行修复靶点** = (位置, 动作)。

    对应 wiki 母页对「诊断」的定义里「产出**可执行修复靶点**」一项：
    不是"热区"这种描述性统计，而是**能直接拿去做干预的 (下标, 战术文本)**。
    """

    index: int
    tactic: str
    metric_before: float
    metric_after: float
    error_before: str = ""
    error_after: str = ""

    @property
    def drop(self) -> float:
        """指标回落量（>0 才算回落）。注意：**回落 ≠ 修复**。"""
        return self.metric_before - self.metric_after

    @property
    def is_fix(self) -> bool:
        """擦除后**完全**编译通过 ⇒ 真修复；否则只是"错误变少"（缓解）。

        这个区分很重要：Qin 2012 的 RBC 只看"指标回落"，而回落也可能是
        「错误搬家」（例如 unsolved_goal 变成 type_mismatch，条数减少但没修好）。
        """
        return self.metric_after <= 0.0

    @property
    def action(self) -> str:
        """人类可读的干预动作。"""
        return f"erase/repair tactic #{self.index}: {self.tactic!r}"


@dataclass(frozen=True)
class ReverseCheck:
    """反向断言：擦除**非**候选 tactic 应**不**引起回落（排除 smearing 串扰）。

    这是 RBC 归因最关键的一步——没有它，"令指标回落"可能只是**擦掉了任何东西**
    都会让错误搬家（母页警告的 smearing）。
    """

    index: int
    tactic: str
    drop: float
    violated: bool  # True ⇒ 该非候选也令指标回落 ⇒ 串扰嫌疑


@dataclass
class CounterfactualReport:
    """L3 反事实验证的产出。"""

    baseline_metric: float
    baseline_error: str = ""
    targets: list[RepairTarget] = field(default_factory=list)
    reverse_checks: list[ReverseCheck] = field(default_factory=list)
    n_candidates: int = 0
    n_tactics: int = 0
    n_compiles: int = 0
    note: str = ""

    @property
    def smearing_detected(self) -> bool:
        """是否检出串扰：存在**非候选**擦除也令指标回落。"""
        return any(c.violated for c in self.reverse_checks)

    @property
    def confirmed(self) -> bool:
        """L3「确认」判据。

        三条同时成立才算确认：
        1. 有靶点（至少一个候选擦除后指标回落）；
        2. **没有**检出串扰；
        3. 至少做过一次反向断言（否则无法排除"擦哪都回落"）。

        第 3 条意味着：当 ``candidates`` 覆盖了全部 tactic 时，
        ``confirmed`` 会（诚实地）为 ``False`` —— 此时没有非候选可校验。
        """
        return bool(self.targets) and not self.smearing_detected and bool(self.reverse_checks)

    def summary(self) -> str:
        lines = [
            "Counterfactual Report (L3)",
            f"  baseline: metric={self.baseline_metric} "
            f"class={self.baseline_error or '-'}",
            f"  candidates={self.n_candidates}/{self.n_tactics} tactics, "
            f"compiles={self.n_compiles}",
        ]
        if not self.targets:
            lines.append("  targets: (none — 无候选擦除引起指标回落)")
        else:
            lines.append(f"  targets: {len(self.targets)}")
            for t in self.targets:
                kind = "FIX" if t.is_fix else "mitigate"
                lines.append(
                    f"    - #{t.index} {t.tactic!r} [{kind}]: metric {t.metric_before}"
                    f"→{t.metric_after} (drop={t.drop})"
                )
        if self.reverse_checks:
            bad = [c for c in self.reverse_checks if c.violated]
            lines.append(
                f"  reverse checks: {len(self.reverse_checks)} "
                f"({len(bad)} violated ⇒ smearing={'YES' if bad else 'no'})"
            )
        else:
            lines.append("  reverse checks: (none — 无法排除擦哪都回落)")
        if self.note:
            lines.append(f"  note: {self.note}")
        lines.append(f"  CONFIRMED: {self.confirmed}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 归因器
# ═══════════════════════════════════════════════════════════════════

class CounterfactualAttributor:
    """擦除-重编译归因器（Qin 2012 RBC 模板的 Lean 化）。

    用法::

        attr = CounterfactualAttributor(compile_gate.compile)
        report = attr.attribute(theorem_header, ["simp", "omega", "ring"],
                                candidates=[0, 2])
        if report.confirmed:
            for t in report.targets:
                print(t.action)

    Parameters
    ----------
    compile_fn : callable
        Lean 代码 → 编译结果（0-token；测试可注入假实现）。
    metric : callable
        编译结果 → 失败指标（越小越好）。默认 :func:`error_count_metric`。
    min_drop : float
        判定"回落"的最小阈值。默认 ``1.0``（至少少 1 条错误）。
    render : callable
        ``(theorem_header, tactics) -> str``。默认 :func:`render_by_block`。
    max_reverse : int
        最多做几次反向断言（每次都要编译，故设上限）。
    """

    def __init__(
        self,
        compile_fn: CompileFn,
        metric: Callable[[Any], float] = error_count_metric,
        min_drop: float = 1.0,
        render: Callable[[str, Sequence[str]], str] = render_by_block,
        max_reverse: int = 3,
    ) -> None:
        self.compile_fn = compile_fn
        self.metric = metric
        self.min_drop = min_drop
        self.render = render
        self.max_reverse = max_reverse

    # ── 内部 ────────────────────────────────────────────────────────

    def _compile(self, code: str, memo: dict[str, Any]) -> Any:
        """带 memo 的编译（同一份代码只编译一次）。"""
        if code not in memo:
            memo[code] = self.compile_fn(code)
        return memo[code]

    def attribute(
        self,
        theorem_header: str,
        tactics: Sequence[str],
        candidates: Iterable[int] | None = None,
        *,
        max_reverse: int | None = None,
    ) -> CounterfactualReport:
        """对 ``tactics`` 做擦除-重编译归因。

        Parameters
        ----------
        theorem_header : str
            定理头（可含 ``:=``；渲染时会剥掉）。
        tactics : Sequence[str]
            证明体战术序列。
        candidates : Iterable[int] | None
            候选下标（通常来自 L0 探针的热区，如 ``error_heat``）。
            ``None`` ⇒ 全部下标都是候选（此时**无反向断言可做**，
            ``report.confirmed`` 会诚实地为 False）。
        max_reverse : int | None
            覆盖实例默认的反向断言次数。
        """
        tactics = list(tactics)
        n = len(tactics)
        memo: dict[str, Any] = {}
        report = CounterfactualReport(
            baseline_metric=0.0, n_tactics=n, n_candidates=0
        )

        baseline_code = self.render(theorem_header, tactics)
        baseline = self._compile(baseline_code, memo)
        report.baseline_metric = self.metric(baseline)
        report.baseline_error = error_class_of(baseline)

        # 通过 ⇒ 没有失败可归因
        if report.baseline_metric <= 0.0:
            report.note = "基线已编译通过 —— 无失败可归因"
            report.n_compiles = len(memo)
            return report

        cand = sorted({i for i in (candidates if candidates is not None else range(n))
                       if 0 <= i < n})
        report.n_candidates = len(cand)

        for i in cand:
            reduced = tactics[:i] + tactics[i + 1:]
            res = self._compile(self.render(theorem_header, reduced), memo)
            report.targets.append(RepairTarget(
                index=i,
                tactic=tactics[i],
                metric_before=report.baseline_metric,
                metric_after=self.metric(res),
                error_before=report.baseline_error,
                error_after=error_class_of(res),
            ))
        report.targets = [t for t in report.targets if t.drop >= self.min_drop]
        report.targets.sort(key=lambda t: (-t.drop, t.index))

        # ── 反向断言：擦除非候选不应引起回落 ────────────────────────
        limit = self.max_reverse if max_reverse is None else max_reverse
        non_cand = [i for i in range(n) if i not in set(cand)]
        for i in non_cand[:max(0, limit)]:
            reduced = tactics[:i] + tactics[i + 1:]
            res = self._compile(self.render(theorem_header, reduced), memo)
            drop = report.baseline_metric - self.metric(res)
            report.reverse_checks.append(ReverseCheck(
                index=i, tactic=tactics[i], drop=drop,
                violated=drop >= self.min_drop,
            ))

        if not non_cand:
            report.note = ("候选覆盖全部 tactic ⇒ 无非候选可校验，"
                           "无法排除 smearing（confirmed 必为 False）")
        elif report.smearing_detected:
            report.note = ("检出串扰：非候选擦除也令指标回落 ⇒ "
                           "热区可能指错（Qin 2012 警告的贡献图 smearing）")

        report.n_compiles = len(memo)
        logger.debug("counterfactual: %s", report.summary())
        return report
