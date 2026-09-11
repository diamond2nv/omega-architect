"""反向断言的**判据**：问「失败是否消失」，不要问「错误是否变少」。

实测背景（真 Lean，2026-09-11，`scripts/counterfactual_labeled_eval.py`）：
默认指标是错误**条数**（``1 + len(errors)``）。而删掉任何一条战术都可能让错误
变少（错误搬家 / 上下文变化），于是旧口径 ``violated = drop >= min_drop`` 把
**15/15** 例无害擦除全判成串扰，归因器于是拒绝给出任何靶点。

Qin 2012 的 RBC 问的是「沿候选方向擦除后**症状是否消失**」——不是「症状是否变轻」。
所以默认判据改为 ``metric_after <= 0``（真消失），旧口径保留为可选项以便对照。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.engine.counterfactual import (  # noqa: E402
    CounterfactualAttributor,
    render_by_block,
)

HEADER = "theorem t : P"


def _tactics_of(code: str) -> list[str]:
    lines = code.splitlines()
    for idx, ln in enumerate(lines):
        if ln.rstrip().endswith("by"):
            return [x.strip() for x in lines[idx + 1:] if x.strip()]
    return []


class ErrorShrinkFake:
    """擦掉任何一条战术都让错误**少一条**，但**从不**修好。

    模拟真 Lean 的常见形态：基线 2 条错，删一条剩 1 条 —— 按「条数回落」看像是
    找到了元凶，其实证明依旧失败（错误搬家）。
    """

    def __call__(self, code: str):
        ts = _tactics_of(code)
        n = 1 + len(ts)
        return {
            "success": False,
            "exit_code": 1,
            "errors": [f"e{i}" for i in range(n)],
            "error_class": "other",
        }


class CulpritOmegafake:
    """只有 ``omega`` 是真元凶：含它必失败，去掉它必通过。"""

    def __call__(self, code: str):
        if "omega" in _tactics_of(code):
            return {"success": False, "exit_code": 1,
                    "errors": ["unsolved goals\n⊢ P"], "error_class": "unsolved_goal"}
        return {"success": True, "exit_code": 0, "errors": [], "error_class": "no_error"}


class TestReverseCriterion:
    def test_error_count_shrinkage_is_not_smearing(self):
        """默认判据：没修好就不算串扰（这是 15/15 误报的修复）。"""
        rep = CounterfactualAttributor(ErrorShrinkFake(), render=render_by_block).attribute(
            HEADER, ["a", "b", "c"], candidates=[0])
        assert rep.baseline_metric > 0
        assert rep.smearing_detected is False
        assert rep.targets, "候选擦除确实让指标回落 ⇒ 仍应作为缓解靶点报出"
        assert rep.targets[0].is_fix is False, "回落 ≠ 修复"

    def test_legacy_drop_criterion_reproduces_the_false_alarm(self):
        """旧口径（drop >= min_drop）在同一个假编译器上把无害擦除全判成串扰。"""
        rep = CounterfactualAttributor(
            ErrorShrinkFake(), render=render_by_block, reverse_uses_fix=False
        ).attribute(HEADER, ["a", "b", "c"], candidates=[0])
        assert rep.smearing_detected is True

    def test_real_fix_on_a_non_candidate_is_still_smearing(self):
        """反向断言的核心能力不能丢：非候选擦除能让失败**消失** ⇒ 必须检出。"""
        rep = CounterfactualAttributor(CulpritOmegafake(), render=render_by_block).attribute(
            HEADER, ["simp", "omega", "ring"], candidates=[2])
        assert rep.smearing_detected is True
        assert rep.targets == []

    def test_correct_candidate_is_confirmed_under_the_new_criterion(self):
        rep = CounterfactualAttributor(CulpritOmegafake(), render=render_by_block).attribute(
            HEADER, ["simp", "omega", "ring"], candidates=[1])
        assert [t.index for t in rep.targets] == [1]
        assert rep.targets[0].is_fix is True
        assert rep.smearing_detected is False
        assert rep.confirmed is True
