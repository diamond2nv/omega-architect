"""CounterfactualAttributor (L3 反事实验证) 测试 —— 全部用假编译器, 无需真 Lean。

覆盖 wiki 母页「诊断」定义里的三项门槛 + Qin 2012 RBC 的反串扰要求:

    定位到机制  ← RepairTarget = (位置, 动作)
    可证伪验证  ← 擦除→重编译, 断言指标**真的**回落
    排除 smearing ← 反向断言: 擦除非候选**不应**回落

关键设计: ``compile_fn`` 可注入 ⇒ 本文件不需要 Lean 工具链, CI 可跑。
"""
import os
import sys
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.engine.counterfactual import (  # noqa: E402
    CounterfactualAttributor,
    RepairTarget,
    error_class_of,
    error_count_metric,
    errors_of,
    is_success,
    render_by_block,
)

HEADER = "theorem t : P"

# ══════════════════════════════════════════════════════════════
# 假编译器
# ══════════════════════════════════════════════════════════════


def _tactics_of(code: str) -> list[str]:
    """取出 ``:= by`` 之后的战术行。"""
    lines = code.splitlines()
    for idx, ln in enumerate(lines):
        if ln.rstrip().endswith("by"):
            return [x.strip() for x in lines[idx + 1:] if x.strip()]
    return []


class FakeLean:
    """含 ``omega`` 即报 unsolved_goal; 否则编译通过。

    模拟真实情形: 3 个 tactic 里只有 1 个是元凶。
    """

    def __init__(self, culprit: str = "omega") -> None:
        self.culprit = culprit
        self.calls: list[str] = []

    def __call__(self, code: str):
        self.calls.append(code)
        if self.culprit in _tactics_of(code):
            return {
                "success": False, "exit_code": 1,
                "errors": ["unsolved goals\n⊢ P"],
                "error_class": "unsolved_goal",
            }
        return {"success": True, "exit_code": 0, "errors": [],
                "error_class": "no_error"}


class GradedFake:
    """``bad`` 存在时失败: 有 ``helper`` 报 3 条错, 无 ``helper`` 报 1 条错。

    用于区分「指标回落」与「真修复」——擦掉 helper 让错误从 3 条降到 1 条,
    指标回落了但**并没有修好** (Qin 2012 只看回落, 会把它当元凶)。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, code: str):
        self.calls.append(code)
        ts = _tactics_of(code)
        if "bad" not in ts:
            return {"success": True, "exit_code": 0, "errors": []}
        n = 3 if "helper" in ts else 1
        return {"success": False, "exit_code": 1,
                "errors": [f"e{i}" for i in range(n)], "error_class": "other"}


@dataclass
class FakeDataclassResult:
    """模拟 omega.loop.compile_gate.CompileResult 的 dataclass 形状。"""

    success: bool = True
    errors: list = field(default_factory=list)
    error_class: str = "no_error"


# ══════════════════════════════════════════════════════════════
# 渲染与容错读取
# ══════════════════════════════════════════════════════════════


class TestRenderAndAccessors:
    def test_render_places_tactics_under_by(self):
        assert render_by_block(HEADER, ["simp"]) == "theorem t : P := by\n  simp"

    def test_render_strips_trailing_assign(self):
        assert render_by_block("theorem t : P :=", ["simp"]).startswith("theorem t : P := by")

    def test_render_empty_is_sorry(self):
        """空序列必须仍可编译 —— 否则基线比较无从谈起。"""
        assert _tactics_of(render_by_block(HEADER, [])) == ["sorry"]

    def test_is_success_prefers_explicit_field(self):
        assert is_success({"success": True, "exit_code": 1}) is True
        assert is_success({"success": False, "exit_code": 0}) is False

    def test_is_success_falls_back_to_exit_code(self):
        assert is_success({"exit_code": 0}) is True
        assert is_success({"exit_code": 1}) is False

    def test_errors_of_reads_diagnostics_when_no_errors_key(self):
        r = {"diagnostics": [
            {"severity": "error", "message": "boom"},
            {"severity": "warning", "message": "meh"},
        ]}
        assert errors_of(r) == ["boom"]

    def test_error_class_of_handles_enum_like_object(self):
        class _EC:
            value = "unsolved_goal"

        assert error_class_of({"error_class": _EC()}) == "unsolved_goal"
        assert error_class_of({"error_class": None}) == ""

    def test_metric_success_strictly_below_any_failure(self):
        """'错误更少但仍在失败' 不得被误判为回落。"""
        assert error_count_metric({"success": True}) == 0.0
        assert error_count_metric({"success": False, "errors": ["a"]}) == 2.0
        assert (error_count_metric({"success": False, "errors": ["a"]})
                > error_count_metric({"success": True}))

    def test_dataclass_shape_supported(self):
        assert is_success(FakeDataclassResult())
        assert error_count_metric(FakeDataclassResult(success=False, errors=["x", "y"])) == 3.0


# ══════════════════════════════════════════════════════════════
# 归因主路径
# ══════════════════════════════════════════════════════════════

TACTICS = ["intro h", "omega", "ring"]  # 元凶在 index 1


class TestAttribution:
    def test_confirmed_attribution(self):
        """候选=[1] ⇒ 定位到 #1, 无串扰, 反向断言通过 ⇒ CONFIRMED。"""
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(HEADER, TACTICS, candidates=[1])
        assert rep.baseline_metric == 2.0
        assert [t.index for t in rep.targets] == [1]
        assert rep.targets[0].tactic == "omega"
        assert rep.targets[0].is_fix is True
        assert len(rep.reverse_checks) == 2          # #0 与 #2
        assert rep.smearing_detected is False
        assert rep.confirmed is True

    def test_l0_hot_zone_pointing_wrong(self):
        """L0 热区指向 #2, 真元凶却是 #1 ⇒ 应检出串扰而非给假靶点。

        这正是模块存在的理由: error_heat 是**贡献图式**统计, Qin 2012 明说它有
        smearing 缺陷、可指错。L3 用"擦掉后失败是否真消失"把它纠正过来。
        """
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(HEADER, TACTICS, candidates=[2])
        assert rep.targets == []                      # 擦 #2 毫无用处 ⇒ 不给靶点
        assert rep.smearing_detected is True          # 非候选 #1 一擦就好 ⇒ 串扰
        assert rep.confirmed is False
        assert "串扰" in rep.note

    def test_all_candidates_means_no_reverse_check(self):
        """候选覆盖全部 ⇒ 无非候选可校验 ⇒ confirmed 必须诚实地为 False。"""
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(HEADER, TACTICS, candidates=None)
        assert [t.index for t in rep.targets] == [1]
        assert rep.reverse_checks == []
        assert rep.confirmed is False
        assert "无法排除 smearing" in rep.note

    def test_baseline_already_passing(self):
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(HEADER, ["intro h", "ring"])
        assert rep.baseline_metric == 0.0
        assert rep.targets == []
        assert "无失败可归因" in rep.note
        assert rep.confirmed is False

    def test_mitigation_is_not_a_fix(self):
        """指标回落 ≠ 修复: 擦 helper 让错误 3→1, 回落了但没修好。"""
        rep = CounterfactualAttributor(GradedFake()).attribute(
            HEADER, ["helper", "bad"], candidates=[0])
        assert len(rep.targets) == 1
        t = rep.targets[0]
        assert t.metric_before == 4.0 and t.metric_after == 2.0
        assert t.drop == 2.0
        assert t.is_fix is False                      # ← 关键区分

    def test_min_drop_filters_weak_drops(self):
        """提高 min_drop ⇒ 2.0 的回落不再算靶点。"""
        rep = CounterfactualAttributor(GradedFake(), min_drop=3.0).attribute(
            HEADER, ["helper", "bad"], candidates=[0])
        assert rep.targets == []

    def test_targets_sorted_by_drop(self):
        class TwoCulprits:
            def __call__(self, code):
                ts = _tactics_of(code)
                errs = [x for x in ("a", "b") if x in ts]
                if errs:
                    return {"success": False, "exit_code": 1, "errors": errs}
                return {"success": True, "exit_code": 0, "errors": []}

        rep = CounterfactualAttributor(TwoCulprits()).attribute(
            HEADER, ["a", "b", "c"], candidates=[0, 1])
        # 擦 a 后还剩 b (1 错, metric 2.0); 擦 b 后还剩 a (metric 2.0) —— 同 drop, 按 index 排
        assert [t.index for t in rep.targets] == [0, 1]

    def test_memoization_compiles_each_variant_once(self):
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(HEADER, TACTICS, candidates=[1])
        # 基线 + 擦#1 + 擦#0 + 擦#2 = 4 种不同代码
        assert rep.n_compiles == 4
        assert len(fake.calls) == 4
        assert len(set(fake.calls)) == 4

    def test_max_reverse_limits_compiles(self):
        fake = FakeLean()
        rep = CounterfactualAttributor(fake, max_reverse=1).attribute(
            HEADER, TACTICS, candidates=[1])
        assert len(rep.reverse_checks) == 1

    def test_out_of_range_candidates_ignored(self):
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(
            HEADER, TACTICS, candidates=[1, 99, -1])
        assert [t.index for t in rep.targets] == [1]

    def test_summary_is_human_readable(self):
        fake = FakeLean()
        rep = CounterfactualAttributor(fake).attribute(HEADER, TACTICS, candidates=[1])
        s = rep.summary()
        assert "L3" in s and "FIX" in s and "CONFIRMED: True" in s


class TestRepairTarget:
    def test_action_names_position_and_tactic(self):
        t = RepairTarget(index=2, tactic="ring", metric_before=2.0, metric_after=0.0)
        assert "#2" in t.action and "ring" in t.action

    def test_drop_and_is_fix(self):
        assert RepairTarget(0, "x", 2.0, 1.0).drop == 1.0
        assert RepairTarget(0, "x", 2.0, 1.0).is_fix is False
        assert RepairTarget(0, "x", 2.0, 0.0).is_fix is True


@pytest.mark.parametrize("n", [0, 1, 3])
def test_render_never_produces_empty_body(n):
    tactics = ["simp"] * n
    assert _tactics_of(render_by_block(HEADER, tactics)) or n == 0
