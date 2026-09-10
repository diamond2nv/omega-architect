"""goal_extract (机理白箱) 测试 —— 从 Lean 免费信号解析目标态。

背景 (审计页 §5.3): 缺 L2 机理白箱不是成本问题, 是**漏读** —— Lean 的
`unsolved goals` 诊断自带 ``⊢`` 目标与假设上下文, 而 omega 此前只读
`error_class` 字符串。本文件把"读得对"变成可跑断言。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.engine.goal_extract import (  # noqa: E402
    ParsedGoal,
    extract_goal_texts,
    format_goal_context,
    parse_goal_state,
    parse_goals,
)

# ── 真实形状的 Lean 输出 ────────────────────────────────────────

ONE_GOAL = """error: unsolved goals
case succ
n : Nat
ih : n + 0 = n
⊢ n.succ + 0 = n.succ
"""

BARE_GOAL = "unsolved goals\n⊢ 2 + 2 = 5"

TWO_GOALS = """error: unsolved goals
h : P
⊢ Q
⊢ R
"""

TYPE_MISMATCH = """error: type mismatch
  Nat
has type
  Type 1
but is expected to have type
  Bool
"""


class TestParseGoals:
    def test_single_goal_with_hypotheses(self):
        goals = parse_goals(ONE_GOAL)
        assert len(goals) == 1
        g = goals[0]
        assert g.target == "n.succ + 0 = n.succ"
        assert g.case == "succ"
        assert g.hypotheses == ["n : Nat", "ih : n + 0 = n"]

    def test_bare_goal_no_hypotheses(self):
        g = parse_goal_state(BARE_GOAL)
        assert g is not None
        assert g.target == "2 + 2 = 5"
        assert g.hypotheses == []

    def test_multiple_goals(self):
        goals = parse_goals(TWO_GOALS)
        assert [g.target for g in goals] == ["Q", "R"]
        assert goals[0].hypotheses == ["h : P"]
        assert goals[1].hypotheses == []      # 后续目标不重复打印上下文

    def test_no_goal_returns_empty(self):
        """type mismatch 没有 ``⊢`` ⇒ 不该硬造出目标态。"""
        assert parse_goals(TYPE_MISMATCH) == []
        assert parse_goal_state(TYPE_MISMATCH) is None

    def test_empty_and_garbage_input(self):
        assert parse_goals("") == []
        assert parse_goals("error: whatever") == []
        assert parse_goal_state("") is None

    def test_noise_prefixes_not_treated_as_hypotheses(self):
        msg = "error: unsolved goals\nwarning: unused variable h\nx : Nat\n⊢ P"
        g = parse_goal_state(msg)
        assert g is not None
        assert g.hypotheses == ["x : Nat"]

    def test_ascii_turnstile_fallback(self):
        g = parse_goal_state("unsolved goals\n|- P")
        assert g is not None and g.target == "P"

    def test_case_label_only_applies_to_its_goal(self):
        msg = "unsolved goals\ncase zero\n⊢ a\ncase succ\nn : Nat\n⊢ b"
        goals = parse_goals(msg)
        assert [g.case for g in goals] == ["zero", "succ"]
        assert goals[1].hypotheses == ["n : Nat"]

    def test_hypothesis_with_multiple_names(self):
        g = parse_goal_state("unsolved goals\nh₁ h₂ : P\n⊢ Q")
        assert g is not None
        assert g.hypotheses == ["h₁ h₂ : P"]

    def test_rendering_round_trips_shape(self):
        g = parse_goal_state(ONE_GOAL)
        assert g is not None
        r = g.rendering()
        assert r.startswith("case succ")
        assert r.splitlines()[-1] == "⊢ n.succ + 0 = n.succ"

    def test_is_valid_rejects_empty_target(self):
        assert ParsedGoal(target="").is_valid is False
        assert ParsedGoal(target="P").is_valid is True


class TestExtractGoalTexts:
    def test_dedupes_across_messages(self):
        assert extract_goal_texts([BARE_GOAL, BARE_GOAL]) == ["2 + 2 = 5"]

    def test_preserves_order(self):
        msgs = ["unsolved goals\n⊢ A", "unsolved goals\n⊢ B", "unsolved goals\n⊢ A"]
        assert extract_goal_texts(msgs) == ["A", "B"]

    def test_empty_input(self):
        assert extract_goal_texts([]) == []
        assert extract_goal_texts([TYPE_MISMATCH]) == []


class TestFormatGoalContext:
    def test_includes_hypotheses_and_target(self):
        ctx = format_goal_context([ONE_GOAL])
        assert "⊢ n.succ + 0 = n.succ" in ctx
        assert "ih : n + 0 = n" in ctx

    def test_respects_limit(self):
        msgs = ["unsolved goals\n⊢ A", "unsolved goals\n⊢ B", "unsolved goals\n⊢ C"]
        ctx = format_goal_context(msgs, limit=2)
        assert "A" in ctx and "B" in ctx and "C" not in ctx

    def test_empty_when_no_goals(self):
        assert format_goal_context([TYPE_MISMATCH]) == ""
        assert format_goal_context([]) == ""


class TestProofStateWiring:
    """接线回归 —— 防止"已实现但未接线"(审计页对 MCTSStrategy 的批评正是这个)。"""

    @staticmethod
    def _state(errors):
        from omega.engine.trajectory import ProofState
        return ProofState(theorem="theorem t : P", errors=list(errors))

    def test_goal_state_property_reads_errors(self):
        st = self._state([ONE_GOAL])
        gs = st.goal_state
        assert gs is not None
        assert gs.target == "n.succ + 0 = n.succ"
        assert gs.hypotheses == ["n : Nat", "ih : n + 0 = n"]

    def test_goal_state_none_when_no_turnstile(self):
        assert self._state([TYPE_MISMATCH]).goal_state is None
        assert self._state([]).goal_state is None

    def test_goal_state_picks_first_parseable_error(self):
        st = self._state([TYPE_MISMATCH, BARE_GOAL])
        gs = st.goal_state
        assert gs is not None and gs.target == "2 + 2 = 5"

    def test_extract_goals_fills_the_goals_field(self):
        st = self._state([TWO_GOALS])
        assert st.goals == []                 # 默认空 —— 正是"漏读"的状态
        st.goals = st.extract_goals()         # 显式填充
        assert st.goals == ["Q", "R"]
