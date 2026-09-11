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

    多行战术块**逐行**缩进（相对缩进保留），与
    ``CompileGateTransition.append_tactic`` 同一规则；否则嵌套块（如
    ``induction n with | zero => …``）会在第二行起掉到第 0 列而报语法错。
    """
    header = theorem_header.strip()
    if header.endswith(":="):
        header = header[:-2].rstrip()
    body = _indent_blocks(tactics, indent)
    if not body:
        body = [f"{indent}sorry"]
    return header + " := by\n" + "\n".join(body)


def _indent_blocks(tactics: Sequence[str], indent: str = "  ", mode: str = "block") -> list[str]:
    """战术文本 → 缩进后的行列表（多行块**逐行**加缩进，空行保持空）。

    ``mode="block"`` 先把整块两端空白去掉（``render_by_block`` 的历史行为：
    单行 ``"  norm_num"`` 仍渲染成 ``"  norm_num"``）；
    ``mode="append"`` 只去首尾换行，与 ``CompileGateTransition.append_tactic``
    逐字节一致（用于复刻搜索里真实被编译的那份源码）。
    """
    lines: list[str] = []
    for t in tactics:
        raw = t or ""
        body = raw.strip() if mode == "block" else raw.strip("\n")
        if not body.strip():
            continue
        lines.extend(f"{indent}{ln}" if ln.strip() else "" for ln in body.splitlines())
    return lines


def render_by_append(theorem_source: str, tactics: Sequence[str], indent: str = "  ") -> str:
    """按**追加**语义渲染：源（已含 ``:= by``）+ 逐个缩进的战术块。

    与 :func:`render_by_block` 的分工：

    * ``render_by_block`` 要求 header **不含** ``:= by``（渲染时自己补上），
      适合「定理头 + 战术列表」这种干净输入；
    * ``render_by_append`` 复刻搜索里**真实发生**的写法 —— 源文本已带 ``:= by``，
      战术块被逐条追加（``CompileGateTransition.append_tactic``）。

    后者在 MCTS 接线上更重要：``compile_fn`` 当初编译的就是这份逐字节相同的源码，
    所以擦除-重编译的**基线**就是被诊断的那个产物本身，而不是它的一个近似渲染。
    """
    code = theorem_source.rstrip()
    for block in _indent_blocks(tactics, indent, mode="append"):
        code = f"{code}\n{block}" if code.strip() else block
    return code


def reconstruct_theorem_source(
    code: str,
    tactics: Sequence[str],
    render: Callable[[str, Sequence[str]], str],
    indent: str = "  ",
) -> str | None:
    """从「源码 + 战术序列」反推定理源（header），**用 render 实测校验**。

    实现上是把 ``tactics`` 按追加语义合成后缀，从 ``code`` 尾部剥掉，
    再对候选 header 逐一调用 ``render`` 并断言能**逐字节复现** ``code``。
    能复现才返回，否则返回 ``None``（调用方应放弃归因并如实记录原因）。

    为什么必须实测校验：``render`` 是可注入的（``render_by_block`` /
    ``render_by_append`` / 自定义），且 ``code`` 可能来自任何 transition。
    只按字符串尾部猜 header 会让**基线 ≠ 被诊断的产物** —— 那时算出的
    「指标回落」是另一个东西的性质，属于本模块最要防的假确认。
    """
    if not tactics:
        return None
    suffix = "\n".join(_indent_blocks(tactics, indent, mode="append"))
    if not suffix:
        return None
    stripped = code.rstrip()
    if not stripped.endswith(suffix):
        return None

    base = stripped[: len(stripped) - len(suffix)].rstrip()
    candidates = [base]
    # ``render_by_block`` 会自己补 ":= by" ⇒ 候选 header 需剥掉原尾部
    trimmed = base
    for tail in ("by", ":="):
        if trimmed.rstrip().endswith(tail):
            trimmed = trimmed.rstrip()[: -len(tail)].rstrip()
    candidates.append(trimmed)

    for candidate in candidates:
        try:
            if render(candidate, list(tactics)).rstrip() == stripped:
                return candidate
        except Exception:  # noqa: BLE001 - 注入的 render 可能任意行为
            logger.debug("reconstruct: render 抛出异常", exc_info=True)
    return None


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
    violated: bool  # True ⇒ 擦掉该**非候选**也让失败消失（或按旧口径：指标回落）⇒ 串扰嫌疑


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
    reverse_uses_fix : bool
        反向断言的判据。``True``（默认）＝「擦掉非候选后**失败是否消失**」
        （``metric_after <= 0``）；``False``＝旧口径「指标是否回落
        ``drop >= min_drop``」。**为什么默认前者**：默认指标是错误**条数**，
        而删掉任何一条战术都可能让错误变少（错误搬家 / 上下文变化），
        于是几乎每次无害擦除都会被判成串扰 —— 实测在 15/15 例真 Lean 用例上
        全部误报（见 docs/experiments/counterfactual-labeled-eval-2026-09-11.md）。
        Qin 2012 的 RBC 问的也是「干预后症状是否消失」，不是「症状是否变轻」。
    """

    def __init__(
        self,
        compile_fn: CompileFn,
        metric: Callable[[Any], float] = error_count_metric,
        min_drop: float = 1.0,
        render: Callable[[str, Sequence[str]], str] = render_by_block,
        max_reverse: int = 3,
        reverse_uses_fix: bool = True,
    ) -> None:
        self.compile_fn = compile_fn
        self.metric = metric
        self.min_drop = min_drop
        self.render = render
        self.max_reverse = max_reverse
        self.reverse_uses_fix = reverse_uses_fix

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
            metric_after = self.metric(res)
            drop = report.baseline_metric - metric_after
            violated = (metric_after <= 0.0) if self.reverse_uses_fix else (drop >= self.min_drop)
            report.reverse_checks.append(ReverseCheck(
                index=i, tactic=tactics[i], drop=drop, violated=violated,
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
