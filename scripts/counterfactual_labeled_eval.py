#!/usr/bin/env python3
"""L3 反事实验证 —— 在**真实 Lean**上测靶点正确率（带机器标注集）。

为什么需要这个脚本
------------------
审计页 `concepts/zero-token-diagnosis-layer-audit-2026` §9.6/§9.7 留的未达成项：

    「``confirmed=True`` 仅表示『做过反向断言且未检出串扰』，**不等于诊断质量达标** ——
      靶点正确率需在真实不可解定理上有标注集才能谈。」

本脚本把「标注集」做出来，并把它跑在真 Lean 上（不是假编译器）。

标注集怎么来 —— 🟢 机器标注，不是人猜
-------------------------------------
::

    base   = 一个**真编译通过**的 Lean 脚本（core Lean，无 Mathlib 依赖）
    inject = 在位置 i 插入一条在该位置**真编译失败**的战术 w
    verify = 擦掉 w 后**真恢复通过**（第二次编译实测）
    ground = i            ← 由编译器两次实测钉死，不依赖任何启发式

⇒ 缺陷位置是构造出来的、可机器复核的；归因器看不到标签，必须自己把它找出来。

三个队列
--------
A **单缺陷可擦除**（半合成）：测 top-1 命中率 / confirmed 率 / 串扰率，
  并与 **L0 热区候选集**对比（热区是贡献图式统计，Qin 2012 说它会 smearing）。
B **不可擦除缺陷**（负例对照）：正确修复是**换序**或**补一条**，单条擦除表达不了
  ⇒ 正确答案是「不给靶点」。假阳性在这里会被照出来。
C **端到端接线**（真 Lean 全流程）：缺陷在链首 ⇒ 走真正的
  ``MCTSStrategy.run_diagnosed(attributor=…)``，验证 L0→L3 在真编译器上跑通。

诚实边界（必须与数字一起引用）
------------------------------
* 队列 A/B 是**半合成**：真 Lean、真错误、真编译，但缺陷是**注入**的。
  它度量「定位能力」，**不是**「organic Mathlib 失败上的靶点正确率」——
  后者需要人工标注的语料，目前没有。
* 每个 base 都由编译器实测；不通过的 base **丢弃并计入报告**，绝不静默跳过。
* 0-LLM：全程只用编译器与纯 Python（含候选生成、指标、串扰断言）。

用法::

    python3 scripts/counterfactual_labeled_eval.py                 # 全队列
    python3 scripts/counterfactual_labeled_eval.py --probe         # 只验证 base 语料
    python3 scripts/counterfactual_labeled_eval.py --json-out /tmp/eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omega.engine.counterfactual import (  # noqa: E402
    CounterfactualAttributor,
    render_by_append,
)
from omega.engine.lean_adapters import (  # noqa: E402
    CompileDistanceEvaluator,
    CompileGateTransition,
    transition_from_source,
)
from omega.engine.mcts_diagnosis import (  # noqa: E402
    HEAT_DEPTH_BUCKET,
    DiagnosisCollector,
    DiagnosisView,
    candidates_from_heat,
)
from omega.engine.strategy_mcts import MCTSStrategy  # noqa: E402
from omega.engine.trajectory import ProofAction, ProofState  # noqa: E402
from omega.loop.compile_gate import CompileGate  # noqa: E402
from omega.loop.errors import CompileErrorClass  # noqa: E402

# ══════════════════════════════════════════════════════════════════
# 语料：真编译通过的核心 Lean 证明（2026-09-11 在本机 Lean 4.30.0 实测）
# ══════════════════════════════════════════════════════════════════

BASES: list[tuple[str, str, list[str]]] = [
    ("nat_add_zero", "theorem b_nat_add_zero (n : Nat) : n + 0 = n := by", ["simp"]),
    ("zero_add_nat", "theorem b_zero_add_nat (n : Nat) : 0 + n = n := by", ["simp"]),
    ("add_comm_nat", "theorem b_add_comm_nat (n m : Nat) : n + m = m + n := by", ["omega"]),
    ("le_succ_self", "theorem b_le_succ_self (n : Nat) : n ≤ n + 1 := by", ["omega"]),
    ("mul_lit", "theorem b_mul_lit : 2 * 3 = 6 := by", ["decide"]),
    ("modus_ponens", "theorem b_modus_ponens (p q : Prop) (h1 : p) (h2 : p → q) : q := by",
     ["exact h2 h1"]),
    ("and_left", "theorem b_and_left (p q : Prop) (h : p ∧ q) : p := by", ["exact h.left"]),
    ("lt_succ_self", "theorem b_lt_succ_self (n : Nat) : n < n + 1 := by", ["omega"]),
    ("and_self", "theorem b_and_self (p : Prop) (h : p) : p ∧ p := by", ["exact ⟨h, h⟩"]),
    ("sub_zero", "theorem b_sub_zero (n : Nat) : n - 0 = n := by", ["simp"]),
    ("succ_eq", "theorem b_succ_eq (n : Nat) : n + 1 = n.succ := by", ["rfl"]),
    ("and_conj", "theorem b_and_conj : 1 + 1 = 2 ∧ 2 + 2 = 4 := by",
     ["constructor", "decide", "decide"]),
    ("rw_both", "theorem b_rw_both (n : Nat) : n + 0 = 0 + n := by",
     ["rw [Nat.add_zero, Nat.zero_add]"]),
    ("intro_exact", "theorem b_intro_exact (p : Prop) : p → p := by", ["intro h", "exact h"]),
    ("le_refl", "theorem b_le_refl (n : Nat) : n ≤ n := by", ["omega"]),
]

#: 注入用「错战术」池（按顺序试，取第一条*在该位置真的报错*的）。
WRONG_POOL: list[str] = [
    "exact Nat.zero_le 0",
    "simp_all",
    "ring",
    "norm_num",
    "linarith",
    "aesop",
    "contradiction",
    "assumption",
    "rw [Nat.add_comm]",
    "exact absurd",
    "induction n",
]

#: 队列 C 的候选 base（缺陷由验证器注入到**链首**，并强制每个前缀都失败）。
CHAIN_BASES: list[tuple[str, str, list[str]]] = [
    ("chain_type_mismatch", "theorem c_type_mismatch : 1 + 1 = 2 := by", ["decide"]),
    ("chain_unknown_tactic", "theorem c_conj : 1 + 1 = 2 ∧ 2 + 2 = 4 := by",
     ["constructor", "decide", "decide"]),
    ("chain_wrong_rewrite", "theorem c_rewrite (n : Nat) : n + 0 = 0 + n := by",
     ["rw [Nat.add_zero, Nat.zero_add]"]),
    ("chain_add_comm", "theorem c_add_comm (n m : Nat) : n + m = m + n := by", ["omega"]),
]

#: 队列 B 由 ``build_negative_cases`` **机器生成**（见该函数）。
#: ⚠️ 曾手写一条「缺一条战术」负例（``["constructor","decide"]``），被编译器证伪：
#: ``decide`` 能直接闭合成对合取目标 ⇒ 擦掉 constructor 真的修好 ⇒ L3 给的靶点是对的，
#: 是**标签错了**。手写标签会被自己证伪，故改为自动生成 + 机器校验。
NEGATIVE_PREMISE = "正确修复**不是**单条擦除（换序 / 补一条），故正确输出是「不给靶点」"


# ══════════════════════════════════════════════════════════════════
# 编译 / 度量工具
# ══════════════════════════════════════════════════════════════════


class LeanTool:
    """真 Lean 编译 + 度量（0-token；带独立的 SHA 缓存目录）。"""

    def __init__(self, cache_dir: str | None = None, fresh_cache: bool = False) -> None:
        if fresh_cache:
            cache_dir = tempfile.mkdtemp(prefix="omega-eval-cache-")
        self.gate = CompileGate(cache_dir=cache_dir or "/tmp/omega-eval-cache/")
        self.compiles = 0
        self.false_no_error = 0  # 失败却标成 NO_ERROR 的次数（§4.1 那类缺陷的探针）
        self.false_no_error_samples: list[dict] = []

    def compile(self, code: str):
        r = self.gate.compile(code)
        self.compiles += 1
        if not r.success and r.error_class is CompileErrorClass.NO_ERROR:
            self.false_no_error += 1
            if len(self.false_no_error_samples) < 5:
                self.false_no_error_samples.append({
                    "code": code, "errors": list(r.errors),
                    "diagnostics": list(r.diagnostics)[:4], "cached": r.cached,
                })
        return r

    def class_of(self, r) -> str:
        return r.error_class.value if r.error_class else ""

    def heat_from_prefixes(self, source: str, tactics: list[str]):
        """按搜索的建树方式复刻 L0：逐前缀真编译 → (classes, depths, heat)。

        与 `DiagnosisCollector.collect` 同构：bucket = ``class@(depth // 3)``，
        depth 从 1 开始（节点深度）。
        """
        classes: list[str] = []
        depths: list[int] = []
        heat: dict[str, int] = {}
        for i in range(1, len(tactics) + 1):
            r = self.compile(render_by_append(source, tactics[:i]))
            cls = self.class_of(r) if not r.success else ""
            classes.append(cls)
            depths.append(i)
            if cls:
                key = f"{cls}@{i // HEAT_DEPTH_BUCKET}"
                heat[key] = heat.get(key, 0) + 1
        return classes, depths, heat


def is_erasable(tool: LeanTool, source: str, tactics: list[str], idx: int) -> bool:
    """擦掉 ``idx`` 后真编译通过？—— 标注集的机器判据。"""
    r = tool.compile(render_by_append(source, [t for j, t in enumerate(tactics) if j != idx]))
    return bool(r.success)


def all_prefixes_fail(tool: LeanTool, source: str, tactics: list[str]) -> bool:
    """每个真前缀都失败 ⇒ 链式搜索能一路走到脚本末尾（够得到缺陷）。"""
    return all(
        not tool.compile(render_by_append(source, tactics[:i])).success
        for i in range(1, len(tactics) + 1)
    )


def build_chain_case(tool: LeanTool, base) -> CaseResult | None:
    """把缺陷注入链首，并要求 (a) 全体前缀失败 (b) 整脚本失败 (c) 擦掉后真恢复。"""
    name, header, working = base
    for wrong in WRONG_POOL:
        tactics = [wrong] + list(working)
        if not all_prefixes_fail(tool, header, tactics):
            continue
        if not is_erasable(tool, header, tactics, 0):
            continue
        case = CaseResult(name=name, cohort="C", source=header, tactics=tactics,
                          ground_truth=0, ground_truth_tactic=wrong)
        case.defect_class = tool.class_of(tool.compile(render_by_append(header, tactics)))
        return case
    return None


def build_negative_cases(tool: LeanTool, bases, max_cases: int = 3) -> list[CaseResult]:
    """负例：**失败且任何单条擦除都不能恢复**（正确修复是换序/补条，不是擦除）。

    机器生成 + 机器校验：遍历 working 脚本的排列，保留 (a) 真编译失败、
    (b) 任一条被擦掉后仍失败 的那些。⇒ 期望 L3 **不给靶点**（给了就是假阳性）。
    """
    from itertools import permutations

    out: list[CaseResult] = []
    for name, header, working in bases:
        if len(working) < 2:
            continue
        for perm in permutations(range(len(working))):
            tactics = [working[i] for i in perm]
            if tactics == list(working):
                continue
            if tool.compile(render_by_append(header, tactics)).success:
                continue  # 该排列其实能过 ⇒ 不是缺陷
            if any(is_erasable(tool, header, tactics, i) for i in range(len(tactics))):
                continue  # 单条可擦除 ⇒ 属队列 A，不是负例
            case = CaseResult(name=f"neg_{name}_perm", cohort="B", source=header,
                              tactics=tactics, ground_truth=None, note=NEGATIVE_PREMISE)
            case.defect_class = tool.class_of(tool.compile(render_by_append(header, tactics)))
            out.append(case)
            break
        if len(out) >= max_cases:
            break
    return out


def first_successful_prefix(tool: LeanTool, source: str, tactics: list[str]) -> int | None:
    """搜索会在哪个前缀处终止（该前缀已编译通过）？None = 全程失败。

    诚实性用：若在缺陷之前就终止，链式搜索**够不到**缺陷（队列 C 因此把缺陷放链首）。
    """
    for i in range(1, len(tactics) + 1):
        if tool.compile(render_by_append(source, tactics[:i])).success:
            return i
    return None


# ══════════════════════════════════════════════════════════════════
# 队列 A：单缺陷可擦除
# ══════════════════════════════════════════════════════════════════


@dataclass
class CaseResult:
    name: str
    cohort: str
    source: str
    tactics: list[str]
    ground_truth: int | None
    ground_truth_tactic: str = ""
    defect_class: str = ""
    # L0 基线：热区候选集
    l0_candidates: list[int] = field(default_factory=list)
    l0_has_culprit: bool | None = None
    l0_set_size: int = 0
    # L3 产出
    attributed: bool = False
    attribution_error: str = ""
    targets: list[dict] = field(default_factory=list)
    top1_index: int | None = None
    top1_correct: bool | None = None
    confirmed: bool = False
    smearing: bool = False
    is_fix: bool | None = None
    # 消融：候选集换成「上帝视角」（只给元凶）时，反向断言在真 Lean 上会不会误报
    oracle_confirmed: bool | None = None
    oracle_smearing: bool | None = None
    oracle_targets: list[dict] = field(default_factory=list)
    # 附注
    search_stops_at_prefix: int | None = None
    note: str = ""


def build_injected_cases(tool: LeanTool, base) -> list[CaseResult]:
    """注入真报错的战术，用**两次真编译**钉死 ground truth。

    两种缺陷位置都造（这是**定位能力**的测试能否成立的前提 —— 若标签恒为 0，
    「top-1 命中率」就没有信息量）：

    * ``i = len(working)`` **追加型**：证明本身完整，末尾多一条 ⇒ 该位置之前
      的前缀全部**通过**（L0 热区只覆盖它，反而能定位）；
    * ``i = 0`` **前置型**：缺陷在首位 ⇒ 每个前缀都失败（热区覆盖全部战术，
      L0 无法定位，只有 L3 能挑出元凶）。
    """
    name, header, working = base
    out: list[CaseResult] = []
    for idx in (len(working), 0):
        for wrong in WRONG_POOL:
            tactics = working[:idx] + [wrong] + working[idx:]
            if tool.compile(render_by_append(header, tactics)).success:
                continue  # 注入后仍通过 ⇒ 不是缺陷
            if not is_erasable(tool, header, tactics, idx):
                continue  # 擦掉也不恢复 ⇒ 不是「单条可擦除」缺陷
            case = CaseResult(
                name=f"{name}@i{idx}",
                cohort="A",
                source=header,
                tactics=tactics,
                ground_truth=idx,
                ground_truth_tactic=wrong,
            )
            case.defect_class = tool.class_of(tool.compile(render_by_append(header, tactics)))
            case.search_stops_at_prefix = first_successful_prefix(tool, header, tactics)
            out.append(case)
            break
    return out


def evaluate_case(tool: LeanTool, case: CaseResult, attributor: CounterfactualAttributor) -> None:
    """走**生产入口**：L0 热区 → 候选 → L3 擦除归因 → 写回 DiagnosisView。"""
    classes, depths, heat = tool.heat_from_prefixes(case.source, case.tactics)
    view = DiagnosisView(theorem=case.source, error_heat=heat)
    case.l0_candidates = candidates_from_heat(heat, classes, depths)
    case.l0_set_size = len(case.l0_candidates)
    case.l0_has_culprit = (
        case.ground_truth in case.l0_candidates if case.ground_truth is not None else None
    )

    report = DiagnosisCollector.attribute(
        view,
        attributor=attributor,
        tactics=case.tactics,
        theorem_source=case.source,
        code=render_by_append(case.source, case.tactics),
        classes=classes,
        depths=depths,
    )
    case.attributed = report is not None
    case.attribution_error = view.attribution_error
    if report is None:
        return
    case.targets = [
        {"index": t.index, "tactic": t.tactic, "drop": t.drop, "is_fix": t.is_fix}
        for t in report.targets
    ]
    if report.targets:
        case.top1_index = report.targets[0].index
        case.is_fix = report.targets[0].is_fix
        case.top1_correct = case.top1_index == case.ground_truth
    case.confirmed = report.confirmed
    case.smearing = report.smearing_detected

    # ── 消融：把候选集换成「上帝视角」（只给元凶）→ 反向断言在真 Lean 上会不会误报
    if case.ground_truth is not None:
        oracle_view = DiagnosisView(theorem=case.source, error_heat=heat)
        oracle = DiagnosisCollector.attribute(
            oracle_view,
            attributor=attributor,
            tactics=case.tactics,
            theorem_source=case.source,
            code=render_by_append(case.source, case.tactics),
            candidates=[case.ground_truth],
            classes=classes,
            depths=depths,
        )
        if oracle is not None:
            case.oracle_confirmed = oracle.confirmed
            case.oracle_smearing = oracle.smearing_detected
            case.oracle_targets = [
                {"index": t.index, "tactic": t.tactic, "drop": t.drop, "is_fix": t.is_fix}
                for t in oracle.targets
            ]


# ══════════════════════════════════════════════════════════════════
# 队列 B：不可擦除（负例对照）
# ══════════════════════════════════════════════════════════════════


def run_negative(tool: LeanTool, res: CaseResult,
                 attributor: CounterfactualAttributor) -> CaseResult:
    res.search_stops_at_prefix = first_successful_prefix(tool, res.source, res.tactics)
    evaluate_case(tool, res, attributor)
    # 负例判据：允许「有候选但都不回落」；不允许给出修复靶点
    fixes = [t for t in res.targets if t["is_fix"]]
    mitigations = [t for t in res.targets if not t["is_fix"]]
    res.note += (
        f" | 实测：修复靶点={len(fixes)}"
        + ("" if not fixes else f" ❌ 假修复 {fixes}")
        + ("" if not mitigations else
           f" | 缓解靶点(非修复，另计)={mitigations} —— 默认指标按**错误条数**，"
           f"删任何一条都可能让错误变少，故会报缓解；这不算假修复")
    )
    return res


# ══════════════════════════════════════════════════════════════════
# 队列 C：端到端（真 Lean + 真搜索 + 真归因）
# ══════════════════════════════════════════════════════════════════


#: 注：``tests/_fakes.py`` 有同一实现（供单测用）。评测脚本不 import tests/，
#: 故此处保留一份 —— 但**生产侧**的对应抽象是 ``lean_adapters.transition_from_source``。
def chain_generator(tactics: list[str]):
    """按**位置**逐条给候选（模拟逐条服务）。

    ⚠️ 绝不能用 ``set`` 去重：脚本里可能**重复同一条战术**（如 ``decide`` 出现两次），
    去重会让生成器提前返回空 ⇒ 链在脚本末尾前一条就断，诊断到的路径比脚本短一条。
    实测踩到过：4 条战术的脚本只走到 3 条，于是「擦掉首条后仍失败」被判无靶点 ——
    归因没错，是harness错了。
    """

    def _gen(state: ProofState) -> list[ProofAction]:
        i = int(state.metadata.get("chain_index") or 0)
        if i >= len(tactics):
            return []
        state.metadata["chain_index"] = i + 1
        return [ProofAction(type="tactic", content=tactics[i], confidence=0.5)]

    return _gen


def transition_over(compile_fn, source: str) -> CompileGateTransition:
    """代码为空时从 ``source`` 起追加 —— 直接复用生产适配器（含 smoke/测试共 4 处调用点）。"""
    return transition_from_source(source, compile_fn)


def run_chain(tool: LeanTool, res: CaseResult, attributor: CounterfactualAttributor) -> CaseResult:
    source, tactics = res.source, res.tactics
    strategy = MCTSStrategy(
        action_generator=chain_generator(tactics),
        state_transition=transition_over(tool.gate.compile, source),
        evaluator=CompileDistanceEvaluator(),
        max_iterations=12,
    )
    trajectory, diagnosis = strategy.run_diagnosed(source, attributor=attributor)
    res.attributed = diagnosis.attribution is not None
    res.attribution_error = diagnosis.attribution_error
    res.confirmed = diagnosis.targets_confirmed
    res.smearing = bool(diagnosis.attribution and diagnosis.attribution.smearing_detected)
    res.targets = [
        {"index": t.index, "tactic": t.tactic, "drop": t.drop, "is_fix": t.is_fix}
        for t in diagnosis.repair_targets
    ]
    if res.targets:
        res.top1_index = res.targets[0]["index"]
        res.is_fix = res.targets[0]["is_fix"]
        res.top1_correct = res.top1_index == res.ground_truth
    res.note = (
        f"trajectory.success={trajectory.success} steps={len(trajectory.steps)} "
        f"iterations={diagnosis.iterations} | {diagnosis.summary()}"
    )
    return res


# ══════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════


def _rate(num: int, den: int) -> str:
    return f"{num}/{den}" + (f" = {num / den:.0%}" if den else "")


def build_report(a: list[CaseResult], b: list[CaseResult], c: list[CaseResult],
                 dropped: list[str], tool: LeanTool, elapsed: float) -> dict:
    attributed = [r for r in a if r.attributed]
    with_target = [r for r in attributed if r.top1_index is not None]
    top1_ok = [r for r in with_target if r.top1_correct]
    cycle = [r for r in attributed if r.smearing]
    # L0 基线：热区候选集是否包含元凶 / 候选集大小
    l0_hits = [r for r in a if r.l0_has_culprit]
    l0_full = [r for r in a if r.l0_set_size == len(r.tactics)]
    return {
        "cohort_a": {
            "n": len(a), "attributed": len(attributed),
            "with_target": len(with_target),
            "top1_correct": len(top1_ok),
            "top1_accuracy": f"{_rate(len(top1_ok), len(with_target))}",
            "confirmed": len([r for r in attributed if r.confirmed]),
            "is_fix": len([r for r in with_target if r.is_fix]),
            "smearing": len(cycle),
            # 消融：候选集=上帝视角（只给元凶）
            "oracle_runs": len([r for r in a if r.oracle_confirmed is not None]),
            "oracle_confirmed": len([r for r in a if r.oracle_confirmed]),
            "oracle_false_smearing": len([r for r in a if r.oracle_smearing]),
            "oracle_target_is_culprit": f"{_rate(len([r for r in a if r.oracle_targets and r.oracle_targets[0]['index'] == r.ground_truth]), len([r for r in a if r.oracle_targets]))}",
        },
        "l0_baseline": {
            "candidate_set_contains_culprit": f"{_rate(len(l0_hits), len(a))}",
            "candidate_set_covers_every_tactic": f"{_rate(len(l0_full), len(a))}",
            "candidate_set_size_avg": round(
                sum(r.l0_set_size for r in a) / len(a), 2) if a else 0,
            "note": "热区候选集=『错误类@深度桶』命中者；覆盖全部战术 ⇒ 无法定位",
        },
        "cohort_b": {
            "n": len(b),
            # 假阳性只有一种：声称擦除能**修好**（is_fix）。缓解靶点另计 ——
            # 默认指标是错误条数，删任何一条都可能让条数变少，故缓解不是错误。
            "false_fix_targets": len([r for r in b if any(t["is_fix"] for t in r.targets)]),
            "mitigation_only": len([r for r in b if r.targets
                                    and not any(t["is_fix"] for t in r.targets)]),
            "detail": {r.name: r.targets for r in b},
        },
        "cohort_c": {
            "n": len(c),
            "attributed": len([r for r in c if r.attributed]),
            "confirmed_on_culprit": len([r for r in c if r.confirmed and r.top1_correct]),
            "errors": {r.name: r.attribution_error for r in c if r.attribution_error},
        },
        "health": {
            "compiles": tool.compiles,
            "false_no_error_on_failure": tool.false_no_error,
            "false_no_error_samples": tool.false_no_error_samples,
            "bases_dropped": dropped,
            "elapsed_s": round(elapsed, 1),
        },
        "cases": {"A": [asdict(r) for r in a], "B": [asdict(r) for r in b],
                  "C": [asdict(r) for r in c]},
    }


def print_report(rep: dict) -> None:
    a, c = rep["cohort_a"], rep["cohort_c"]
    print("\n" + "=" * 78)
    print("队列 A：单缺陷可擦除（半合成，真 Lean）")
    print(f"  用例 {a['n']} | 完成归因 {a['attributed']} | 给出靶点 {a['with_target']}")
    print(f"  ★ top-1 命中元凶 : {a['top1_accuracy']}")
    print(f"  真修复(is_fix)   : {a['is_fix']} | confirmed : {a['confirmed']} "
          f"| 检出串扰 : {a['smearing']}")
    print(f"  消融(候选=元凶)  : 靶点=元凶 {a['oracle_target_is_culprit']} "
          f"| confirmed {a['oracle_confirmed']}/{a['oracle_runs']} "
          f"| 反向断言误报串扰 {a['oracle_false_smearing']}")
    print(f"  L0 基线：候选集含元凶 {rep['l0_baseline']['candidate_set_contains_culprit']}"
          f" | 候选集=全部战术 {rep['l0_baseline']['candidate_set_covers_every_tactic']}")
    print("\n队列 B：不可擦除缺陷（负例对照，期望不给靶点）")
    print(f"  用例 {rep['cohort_b']['n']} | 假修复靶点 {rep['cohort_b']['false_fix_targets']}"
          f" | 仅缓解靶点 {rep['cohort_b']['mitigation_only']}")
    for name, targets in rep["cohort_b"]["detail"].items():
        print(f"    {name}: targets={targets}")
    print("\n队列 C：端到端接线（真 Lean + 真搜索 + 真归因）")
    print(f"  用例 {c['n']} | 完成归因 {c['attributed']} | 命中且 confirmed "
          f"{c['confirmed_on_culprit']}")
    for name, err in c["errors"].items():
        print(f"    ⚠️ {name}: {err}")
    h = rep["health"]
    print(f"\n编译次数 {h['compiles']} | 失败却标 NO_ERROR 的次数 {h['false_no_error_on_failure']}"
          f" | 丢弃 base {h['bases_dropped']} | {h['elapsed_s']}s")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-out", default="", help="结果写入 JSON 的路径")
    ap.add_argument("--probe", action="store_true", help="只验证 base 语料是否真编译通过")
    ap.add_argument("--fresh-cache", action="store_true", help="每次用新的编译缓存目录")
    ap.add_argument("--smoke", action="store_true", help="只跑队列 C（最少编译次数）")
    args = ap.parse_args(argv)

    t0 = time.time()
    tool = LeanTool(fresh_cache=args.fresh_cache)
    attributor = CounterfactualAttributor(tool.gate.compile, render=render_by_append)

    # ── base 语料实测：不通过的**丢弃并报告** ─────────────────────
    verified, dropped = [], []
    for base in BASES:
        name, header, working = base
        if tool.compile(render_by_append(header, working)).success:
            verified.append(base)
        else:
            dropped.append(name)
    print(f"[base] 实测通过 {len(verified)}/{len(BASES)}；丢弃 {dropped}")

    if args.probe:
        print("[probe] 只验证语料，结束（不生成标注集）")
        return 0

    # ── 队列 A：注入 + 两次真编译钉死 ground truth ─────────────────
    cases_a: list[CaseResult] = []
    if not args.smoke:
        for base in verified:
            built = build_injected_cases(tool, base)
            if not built:
                dropped.append(f"{base[0]}(无法构造单缺陷可擦除用例)")
                continue
            for case in built:
                evaluate_case(tool, case, attributor)
                cases_a.append(case)
                print(f"[A] {case.name:22s} gt={case.ground_truth} "
                      f"tactic={case.ground_truth_tactic[:20]!r} "
                      f"cls={case.defect_class:14s} top1={case.top1_index} "
                      f"confirmed={case.confirmed} smearing={case.smearing}", flush=True)

    # ── 队列 B：负例对照 ─────────────────────────────────────────
    cases_b: list[CaseResult] = []
    if not args.smoke:
        neg_cases = build_negative_cases(tool, verified)
        cases_b = [run_negative(tool, c, attributor) for c in neg_cases]
        if not neg_cases:
            dropped.append("队列B(未找到『失败且单条擦除不可恢复』的负例)")
    for r in cases_b:
        print(f"[B] {r.name:24s} cls={r.defect_class:16s} targets={r.targets}")

    # ── 队列 C：端到端 ───────────────────────────────────────────
    cases_c: list[CaseResult] = []
    for base in CHAIN_BASES:
        case = build_chain_case(tool, base)
        if case is None:
            dropped.append(f"{base[0]}(无法构造链首缺陷用例：前缀过早通过)")
            print(f"[C] {base[0]:24s} SKIP (无法构造)", flush=True)
            continue
        r = run_chain(tool, case, attributor)
        cases_c.append(r)
        print(f"[C] {r.name:22s} gt=0 top1={r.top1_index} is_fix={r.is_fix} "
              f"confirmed={r.confirmed} smearing={r.smearing} | {r.note[:70]}", flush=True)

    rep = build_report(cases_a, cases_b, cases_c, dropped, tool, time.time() - t0)
    print_report(rep)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"[out] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
