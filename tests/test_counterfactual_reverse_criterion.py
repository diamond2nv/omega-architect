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
sys.path.insert(0, os.path.dirname(__file__))  # 共享假件 tests/_fakes.py

from _fakes import CulpritOmegafake, ErrorShrinkFake  # noqa: E402

from omega.engine.counterfactual import (  # noqa: E402
    CounterfactualAttributor,
    render_by_block,
)

HEADER = "theorem t : P"


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
