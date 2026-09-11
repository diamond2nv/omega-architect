"""L3 wiring: `MCTSStrategy.run_diagnosed(attributor=…)` → `DiagnosisView.attribution`.

Closes the audit's open item (wiki `concepts/zero-token-diagnosis-layer-audit-2026`
§9.7): the counterfactual attributor was a *callable capability* but was **not wired
into the diagnosis flow** — "已实现、可注入" instead of "已接线". These tests pin the
production path down with an injected compiler, so no Lean toolchain is needed:

    search run ──▶ DiagnosisView (L0) ──▶ candidates_from_heat ──▶ attributor (L3)
                        │                                              │
                        └────────── attribution / attribution_error ◀──┘

The audit-critical cases are here, not in the standalone attributor tests:

* an L0 hot zone pointing at the **wrong** tactic must surface as ``smearing``
  through the wiring (no false repair target), and
* a baseline that cannot be reproduced byte-for-byte must be **refused**
  ("not attributed"), never silently attributed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # 共享假件 tests/_fakes.py

from _fakes import (  # noqa: E402  (共享假件，见 tests/_fakes.py)
    SOURCE,
    ChainCompiler,
    ExplodingAttributor,
    RaisingCompiler,
    Result,
    chain_generator,
    transition_over,
)
from _fakes import run_wiring as run  # noqa: E402

from omega.engine.counterfactual import (  # noqa: E402
    CounterfactualAttributor,
    reconstruct_theorem_source,
    render_by_append,
    render_by_block,
)
from omega.engine.mcts_diagnosis import (  # noqa: E402
    HEAT_DEPTH_BUCKET,
    DiagnosisView,
    candidates_from_heat,
)
from omega.engine.strategy_mcts import MCTSStrategy  # noqa: E402

# ══════════════════════════════════════════════════════════════
# Fakes: 全部搬到 tests/_fakes.py（Result / ChainCompiler / RaisingCompiler /
# ExplodingAttributor / chain_generator / transition_over / run_wiring）
# 生产侧的对应抽象：`counterfactual.tactics_of_code` +
# `lean_adapters.transition_from_source`。
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# candidates_from_heat (L0 → L3 bridge)
# ══════════════════════════════════════════════════════════════


class TestCandidatesFromHeat:
    def test_only_indices_in_the_hottest_buckets_are_proposed(self):
        heat = {"unsolved_goal@0": 5, "type_mismatch@1": 1}
        classes = ["unsolved_goal", "type_mismatch", "unsolved_goal"]
        depths = [1, 4, 2]  # //3 -> bucket 0, 1, 0
        assert candidates_from_heat(heat, classes, depths, top_k=1) == [0, 2]

    def test_ties_are_broken_by_key_so_the_result_is_deterministic(self):
        """Equal counts: the lexicographically smaller bucket key wins, repeatably."""
        heat = {"a@0": 2, "b@0": 2}
        assert candidates_from_heat(heat, ["a"], [0], top_k=1) == [0]
        assert candidates_from_heat(heat, ["b"], [0], top_k=1) == []
        assert candidates_from_heat(heat, ["b"], [0], top_k=2) == [0]

    def test_empty_class_is_never_a_candidate(self):
        """A tactic with no recorded error class carries no heat evidence."""
        assert candidates_from_heat({"unsolved_goal@0": 3}, ["", "unsolved_goal"], [0, 1]) == [1]

    def test_length_mismatch_truncates_instead_of_guessing(self):
        assert candidates_from_heat({"a@0": 1}, ["a", "a", "a"], [0]) == [0]

    def test_bucket_width_is_honoured(self):
        heat = {f"a@{1}": 1}
        assert candidates_from_heat(heat, ["a"], [1 * HEAT_DEPTH_BUCKET]) == [0]
        assert candidates_from_heat(heat, ["a"], [1 * HEAT_DEPTH_BUCKET - 1]) == []

    def test_no_heat_no_candidates(self):
        assert candidates_from_heat({}, ["a"], [0]) == []


# ══════════════════════════════════════════════════════════════
# reconstruct_theorem_source (the honesty guard)
# ══════════════════════════════════════════════════════════════


class TestReconstruct:
    def test_roundtrip_with_append_semantics(self):
        compiler = ChainCompiler()
        code = transition_over(SOURCE, compiler).append_tactic(
            transition_over(SOURCE, compiler).append_tactic(SOURCE, "alpha"), "beta"
        )
        assert reconstruct_theorem_source(code, ["alpha", "beta"], render_by_append) == SOURCE

    def test_roundtrip_with_block_semantics(self):
        code = render_by_block("theorem t : P", ["alpha", "beta"])
        assert reconstruct_theorem_source(code, ["alpha", "beta"], render_by_block) == "theorem t : P"

    def test_returns_none_when_the_tail_does_not_match(self):
        code = "theorem t : P := by\n    alpha"  # 4-space indent vs 2-space render
        assert reconstruct_theorem_source(code, ["alpha"], render_by_append) is None

    def test_returns_none_for_empty_tactics(self):
        assert reconstruct_theorem_source(SOURCE, [], render_by_append) is None


# ══════════════════════════════════════════════════════════════
# The wiring itself
# ══════════════════════════════════════════════════════════════


class TestWiring:
    def test_without_attributor_nothing_changes(self):
        """Opt-in: no attributor ⇒ the view is exactly the L0/L2 one, plus a reason."""
        strategy = MCTSStrategy(
            action_generator=chain_generator(["alpha", "beta"]),
            state_transition=transition_over(SOURCE, ChainCompiler()),
            max_iterations=10,
        )
        trajectory, diagnosis = strategy.run_diagnosed(SOURCE)
        assert diagnosis.attribution is None
        assert diagnosis.attribution_error == "no attributor injected"
        assert diagnosis.repair_targets == []
        assert diagnosis.targets_confirmed is False
        assert "L3 not attributed: no attributor injected" in diagnosis.summary()
        assert trajectory.steps  # the search result itself is untouched

    def test_confirmed_target_on_the_culprit(self):
        compiler = ChainCompiler(culprits=("alpha",))
        _, diagnosis = run(compiler, candidates=[0])
        att = diagnosis.attribution
        assert att is not None
        assert [t.index for t in att.targets] == [0]
        assert att.targets[0].tactic == "alpha"
        assert att.targets[0].is_fix is True
        assert att.smearing_detected is False
        assert att.confirmed is True
        assert diagnosis.targets_confirmed is True
        assert "erase/repair tactic #0" in diagnosis.repair_targets[0].action

    def test_l0_hot_zone_pointing_wrong_is_caught_by_the_reverse_assertion(self):
        """Hot zone says #2, the real culprit is #0 → no false target."""
        _, diagnosis = run(ChainCompiler(culprits=("alpha",)), candidates=[2])
        att = diagnosis.attribution
        assert att is not None
        assert att.targets == []
        assert att.smearing_detected is True
        assert att.confirmed is False
        assert diagnosis.repair_targets == []
        assert "串扰" in att.note

    def test_default_candidates_come_from_the_heat_buckets(self):
        """No explicit candidates → L0 heat decides; full coverage ⇒ not confirmed."""
        _, diagnosis = run(ChainCompiler(culprits=("alpha",)))
        att = diagnosis.attribution
        assert att is not None
        assert att.n_candidates == 3  # one heat bucket covers every tactic here
        assert [t.index for t in att.targets] == [0]
        assert att.confirmed is False  # no non-candidate left to falsify against

    def test_raising_compiler_never_loses_the_search(self):
        _, diagnosis = run(RaisingCompiler(), candidates=[0])
        assert diagnosis.attribution is None
        assert "attributor raised" in diagnosis.attribution_error
        assert diagnosis.explored_nodes > 1

    def test_exploding_attributor_is_contained(self):
        strategy = MCTSStrategy(
            action_generator=chain_generator(["alpha", "beta"]),
            state_transition=transition_over(SOURCE, ChainCompiler()),
            max_iterations=10,
        )
        _, diagnosis = strategy.run_diagnosed(
            SOURCE,            attributor=ExplodingAttributor(lambda _code: Result(True, []))
        )
        assert diagnosis.attribution is None
        assert "attributor raised: ValueError" in diagnosis.attribution_error

    def test_unreproducible_baseline_is_refused_not_attributed(self):
        """Indent mismatch ⇒ the baseline is not the diagnosed artefact ⇒ refuse."""
        _, diagnosis = run(ChainCompiler(), indent="    ", candidates=[0])
        assert diagnosis.attribution is None
        assert "cannot reconstruct" in diagnosis.attribution_error
        assert "L3 not attributed" in diagnosis.summary()

    def test_explicit_source_that_does_not_reproduce_the_code_is_refused(self):
        _, diagnosis = run(
            ChainCompiler(), candidates=[0], theorem_source="theorem t : P"
        )
        assert diagnosis.attribution is None
        assert "does not reproduce" in diagnosis.attribution_error

    def test_explicit_source_that_reproduces_is_accepted(self):
        _, diagnosis = run(
            ChainCompiler(culprits=("alpha",)),
            candidates=[0],
            theorem_source=SOURCE,
        )
        assert diagnosis.attribution is not None
        assert [t.index for t in diagnosis.repair_targets] == [0]

    def test_solved_run_reports_nothing_to_attribute(self):
        compiler = ChainCompiler(culprits=())  # every tactic compiles
        _, diagnosis = run(compiler, tactics=["alpha"], candidates=[0])
        att = diagnosis.attribution
        assert att is not None
        assert att.baseline_metric == 0.0
        assert att.targets == []
        assert "无失败可归因" in att.note

    def test_empty_search_reports_no_expanded_path(self):
        strategy = MCTSStrategy(
            action_generator=lambda _state: [],
            state_transition=transition_over(SOURCE, ChainCompiler()),
            max_iterations=5,
        )
        _, diagnosis = strategy.run_diagnosed(
            SOURCE, attributor=CounterfactualAttributor(ChainCompiler(), render=render_by_append)
        )
        assert diagnosis.attribution is None
        assert "no expanded path" in diagnosis.attribution_error

    def test_view_reports_l3_in_summary_once_attributed(self):
        _, diagnosis = run(ChainCompiler(culprits=("alpha",)), candidates=[0])
        assert "L3 targets=1" in diagnosis.summary()
        assert "confirmed=True" in diagnosis.summary()

    def test_diagnosis_view_is_constructible_without_attribution(self):
        """The new fields must default in a way that reads as 'not attributed'."""
        view = DiagnosisView(theorem="t")
        assert view.attribution is None
        assert view.attribution_error == ""
        assert view.repair_targets == []
        assert view.targets_confirmed is False
