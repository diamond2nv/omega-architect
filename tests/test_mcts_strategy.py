"""Tests for MCTSStrategy wiring + diagnosis export (Step 1).

The search core is exercised on a pure-Python toy environment (a number ladder
with a target), so the tests need no Lean, no LLM and no GPU. Diagnosis
collection is additionally tested on a hand-built tree where stalled / untried
nodes are known by construction.

Stdlib + pytest only.
"""

from __future__ import annotations

import math

from omega.engine.mcts_diagnosis import (
    DiagnosisCollector,
    visit_entropy,
)
from omega.engine.strategy_mcts import MCTSStrategy, _Node, ucb1_score
from omega.engine.trajectory import ProofAction, ProofState

TARGET = 7
STEPS = (1, 2, 3)


# ── toy environment ───────────────────────────────────────────────────────
def _gen(state: ProofState) -> list[ProofAction]:
    """Legal +1/+2/+3 moves that do not overshoot the target."""
    cur = int(state.metadata.get("current", 0))
    out: list[ProofAction] = []
    for step in STEPS:
        nxt = cur + step
        if nxt > TARGET:
            continue
        nstate = ProofState(
            theorem=state.theorem,
            code=f"{state.code}+{step}",
            depth=state.depth + 1,
            is_terminal=(nxt == TARGET),
            metadata={"current": nxt},
        )
        out.append(ProofAction(type="step", content=f"+{step}", metadata={"next_state": nstate}))
    return out


def _eval(state: ProofState) -> float:
    """Closer to the target = higher value; 1.0 at the target."""
    cur = int(state.metadata.get("current", 0))
    if cur >= TARGET:
        return 1.0
    return 1.0 - (TARGET - cur) / TARGET


def _strategy(**kw) -> MCTSStrategy:
    return MCTSStrategy(_gen, _eval, **kw)


# ── 1. the search solves the toy problem ─────────────────────────────────
def test_solves_toy_ladder() -> None:
    traj, diag = _strategy(max_iterations=64).run_diagnosed("toy")
    assert traj.success is True
    assert traj.proof is not None
    assert diag.solved_nodes >= 1
    assert diag.falsifiability_note  # always present


# ── 2. run() obeys the ABC and matches run_diagnosed() ───────────────────
def test_run_matches_run_diagnosed() -> None:
    s = _strategy(max_iterations=64)
    traj_abc = s.run("toy")
    traj, _ = _strategy(max_iterations=64).run_diagnosed("toy")
    assert isinstance(traj_abc.success, bool)
    assert traj_abc.success == traj.success
    assert s.name and s.description


# ── 3. anytime behaviour: a tiny budget must not raise ───────────────────
def test_anytime_small_budget() -> None:
    traj, diag = _strategy(max_iterations=1).run_diagnosed("toy")
    assert isinstance(traj.success, bool)
    assert diag.iterations <= 1
    assert traj.elapsed_ms >= 0


# ── 4. an environment with no actions is handled gracefully ──────────────
def test_no_actions_is_safe() -> None:
    s = MCTSStrategy(lambda _st: [], lambda _st: 0.0, max_iterations=4)
    traj, diag = s.run_diagnosed("toy")
    assert traj.success is False
    assert diag.explored_nodes >= 1


# ── 5. a budgeted search leaves blind spots and reports entropy ──────────
def test_diagnosis_reports_blind_spots() -> None:
    _, diag = _strategy(max_iterations=3).run_diagnosed("toy")
    assert diag.blind_spots, "a truncated budget must leave untried actions"
    for spot in diag.blind_spots:
        if spot.known:
            assert spot.available_actions > spot.explored_children
            assert spot.explored_children > 0
        else:
            # never expanded: the size of the action set is unknown
            assert spot.explored_children == 0 and spot.available_actions == 0
    assert any(not s.known for s in diag.blind_spots), (
        "children created but never expanded are the clearest coverage gap"
    )


# ── 6. hand-built tree: stuck node + failure chain by construction ───────
def _leaf(name: str, parent: _Node, value: float, visits: int) -> _Node:
    node = _Node(
        id=name,
        state=ProofState(theorem="t", depth=parent.depth + 1),
        parent=parent,
        depth=parent.depth + 1,
    )
    node.visits = visits
    node.value_sum = value * visits
    parent.children.append(node)
    return node


def test_collector_finds_stuck_node() -> None:
    root = _Node(id="root", state=ProofState(theorem="t"))
    root.visits, root.value_sum = 5, 0.0
    root.available_actions = 3
    stuck = _leaf("stuck", root, value=0.1, visits=3)  # low value, revisited
    good = _leaf("good", root, value=0.9, visits=2)  # healthy child, must not be flagged
    assert good.value > 0.5
    diag = DiagnosisCollector.collect(root=root, theorem="t", iterations=5)
    ids = {n.node_id for n in diag.stuck_nodes}
    assert "stuck" in ids and "good" not in ids
    assert diag.visit_entropy > 0
    assert diag.failure_chain, "a non-solved tree must yield a failure chain"
    assert stuck.value <= 0.34


# ── 7. entropy bounds ────────────────────────────────────────────────────
def test_visit_entropy_bounds() -> None:
    assert visit_entropy([]) == 0.0
    assert visit_entropy([7]) == 0.0
    assert visit_entropy([1, 1]) > 0
    assert visit_entropy([1, 1, 1, 1]) > visit_entropy([3, 1])
    uniform = visit_entropy([1] * 4)
    assert abs(uniform - math.log(4)) < 1e-9


# ── 8. standard UCB1 semantics ───────────────────────────────────────────
def test_ucb1_explores_unvisited_first() -> None:
    assert ucb1_score(0.9, 0, 10) == float("inf")
    assert ucb1_score(0.9, 5, 10) < float("inf")


def test_ucb1_balances_exploration() -> None:
    """A less-visited but promising node can outrank a well-visited one."""
    high_value_often = ucb1_score(0.8, 20, 40)
    slightly_lower_rarely = ucb1_score(0.6, 2, 40)
    assert slightly_lower_rarely > high_value_often


# ── 9. documented divergence from ProofTree._select_ucb1 ─────────────────
def _legacy_prooftree_score(
    value_mean: float, visits: int, root_visits: int, c: float = 1.4
) -> float:
    """Formula used by omega/search/tree.py::_select_ucb1 (non-standard).

    ``c * sqrt(N_root) / (1 + n)`` with the mean value in the numerator, and
    applied only to nodes whose status is UNEXPLORED.
    """
    if root_visits <= 0:
        return float("inf")
    if visits > 0:
        return value_mean + c * (root_visits**0.5 / (1 + visits))
    return c * (root_visits**0.5)


def test_ucb1_diverges_from_prooftree_variant() -> None:
    """The two formulas are NOT equivalent - assert the divergence explicitly.

    Standard UCB1 (this module) uses ``ln(N_parent)``; the existing tree uses
    ``sqrt(N_root)``. Rather than assuming they agree, we pin the difference so
    a future unification is a deliberate, tested change.
    """
    node = (0.5, 4)  # (mean value, visits)
    parent_visits, root_visits, c = 50, 50, 1.4
    standard = ucb1_score(node[0], node[1], parent_visits, c)
    legacy = _legacy_prooftree_score(node[0], node[1], root_visits, c)
    assert standard != legacy
    assert legacy > standard  # sqrt(N) grows faster than ln(N) exploration term


# ── 10. F1 regression: search bookkeeping must not pollute state.metadata ──
def test_state_metadata_not_polluted() -> None:
    traj, _ = _strategy(max_iterations=6).run_diagnosed("toy")
    for step in traj.steps:
        assert "_actions" not in step.state_after.metadata


# ── 11. F2 regression: terminal-by-environment counts as solved ──────────
def test_terminal_state_counts_as_solved_even_with_low_value() -> None:
    """An environment that marks terminal must win over a low evaluator score."""
    strat = MCTSStrategy(_gen, lambda _st: 0.05, max_iterations=32)
    traj, _ = strat.run_diagnosed("toy")
    assert traj.success is True


# ── 12. F3 regression: failure chain is deterministic (lowest value wins) ─
def test_failure_chain_prefers_lowest_value_on_ties() -> None:
    root = _Node(id="root", state=ProofState(theorem="t"))
    root.visits, root.value_sum = 4, 0.0
    _leaf("bad_a", root, value=0.5, visits=1)
    worst = _leaf("bad_b", root, value=0.01, visits=1)
    diag = DiagnosisCollector.collect(root=root, theorem="t", iterations=3)
    assert diag.failure_chain[-1] == worst.id
