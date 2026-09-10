"""MCTSStrategy — UCB1 proof search wired onto the engine's strategy ABC.

This is the *wiring* half of the MCTS plan: the engine already ships a search
tree with UCB1 selection (``omega/search/tree.py``) but no strategy calls it.
Here we add a strategy that

* runs UCB1 selection over a **pure-Python search core** (so it is testable
  without Lean, an LLM, or a GPU), and
* emits a :class:`~omega.engine.mcts_diagnosis.DiagnosisView` describing *where*
  the search stalled, *which* branches were never tried, and how failures are
  distributed by depth.

Non-invasive by design: DFS/Beam/Hybrid are untouched. Production wiring to the
Lean prover is Step 2 — inject an ``action_generator`` / ``state_transition`` /
``evaluator`` trio backed by the LLM and ``CompileGate``.

Note on selection: this module implements **standard UCB1**
(``Q + c·sqrt(ln N_parent / n)``). The existing ``ProofTree._select_ucb1`` uses a
non-standard variant (``c·sqrt(N_root)/(1+n)`` restricted to unexplored nodes);
:func:`ucb1_score` documents the difference and a test asserts the divergence, so
the discrepancy is explicit rather than assumed away.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from omega.engine.mcts_diagnosis import DiagnosisCollector, DiagnosisView
from omega.engine.trajectory import (
    ProofAction,
    ProofState,
    SearchStrategy,
    Trajectory,
    TrajectoryStep,
)

ActionGenerator = Callable[[ProofState], list[ProofAction]]
StateTransition = Callable[[ProofState, ProofAction], ProofState]
Evaluator = Callable[[ProofState], float]

MAX_DIAGNOSIS_CHAIN = 10_000


def ucb1_score(value: float, visits: int, parent_visits: int, c: float = 1.4) -> float:
    """Standard UCB1 score for a node.

    ``Q + c * sqrt(ln(N_parent) / n)`` with ``Q`` the mean value. Unvisited
    nodes score ``inf`` so they are expanded first.

    Differs from ``omega.search.tree.ProofTree._select_ucb1`` which uses
    ``c * sqrt(N_root) / (1 + n)`` and only considers nodes whose status is
    ``UNEXPLORED``. Both are deliberate; see the module docstring.
    """
    if visits <= 0:
        return float("inf")
    return value + c * math.sqrt(math.log(max(parent_visits, 2)) / visits)


@dataclass
class _Node:
    """Lightweight search node (duck-compatible with the diagnosis collector)."""

    id: str
    state: ProofState
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)
    depth: int = 0
    visits: int = 0
    value_sum: float = 0.0
    tactic_applied: str | None = None
    available_actions: int = 0
    expanded_actions: int = 0
    pending_actions: list[ProofAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def value(self) -> float:
        """Mean value (0.0 when never evaluated)."""
        return (self.value_sum / self.visits) if self.visits else 0.0

    @property
    def is_solved(self) -> bool:
        return bool(self.state.is_terminal)


def _default_transition(state: ProofState, action: ProofAction) -> ProofState:
    """Default state transition.

    Prefers an explicitly attached successor (``action.metadata['next_state']``,
    used by tests and by injectable generators); otherwise appends the action to
    the proof code and marks the resulting state as fresh.
    """
    nxt = action.metadata.get("next_state")
    if isinstance(nxt, ProofState):
        return nxt
    return replace(
        state,
        code=(state.code + "\n" + action.content).strip(),
        depth=state.depth + 1,
        metadata={**state.metadata, "last_action": action.content},
    )


class MCTSStrategy(SearchStrategy):
    """UCB1 search over proof states with an exported diagnosis view.

    Usage (production, Step 2)::

        strategy = MCTSStrategy(
            action_generator=llm_candidates,
            state_transition=compile_and_apply,
            evaluator=compile_gate_value,
            max_iterations=64,
        )
        trajectory, diagnosis = strategy.run_diagnosed(theorem)

    Usage (tests / offline)::

        strategy = MCTSStrategy(action_generator=toy_actions, evaluator=toy_value)
    """

    def __init__(
        self,
        action_generator: ActionGenerator | None = None,
        evaluator: Evaluator | None = None,
        *,
        state_transition: StateTransition | None = None,
        max_iterations: int = 50,
        c: float = 1.4,
        solve_threshold: float = 0.999,
        name: str = "MCTS (UCB1)",
    ) -> None:
        self._action_generator = action_generator or _no_actions
        self._evaluator = evaluator or _zero_value
        self._transition = state_transition or _default_transition
        self.max_iterations = max_iterations
        self.c = c
        self.solve_threshold = solve_threshold
        self._name = name
        self._counter = 0

    # ── SearchStrategy ABC ───────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return (
            "Monte-Carlo tree search with standard UCB1 selection over proof "
            "states. Explores a non-symmetric tree (budget concentrated on "
            "promising branches) and exports a diagnosis view of stalled nodes, "
            "untried branches and error heat. Anytime: returns the best node "
            "found when the iteration budget runs out."
        )

    def run(self, theorem: str) -> Trajectory:
        """Run the search and return the trajectory (ABC contract)."""
        trajectory, _ = self.run_diagnosed(theorem)
        return trajectory

    # ── diagnosis-aware entry point ──────────────────────────────────────
    def run_diagnosed(self, theorem: str) -> tuple[Trajectory, DiagnosisView]:
        """Run the search; return both the trajectory and the diagnosis view."""
        start = time.monotonic()
        self._counter = 0
        root = self._make_node(_root_state(theorem), None, None, depth=0)
        root.visits = 1
        solved: _Node | None = None
        iterations = 0

        for _ in range(self.max_iterations):
            iterations += 1
            node = self._select(root)
            if node.is_solved:
                solved = node
                break
            child = self._expand(node)
            if child is None:
                continue
            value = float(self._evaluator(child.state))
            self._backprop(child, value)
            # A state the environment marks terminal is a solution regardless of
            # the evaluator's scale; the threshold only catches evaluator-declared
            # solutions for environments that never set is_terminal.
            if child.is_solved or value >= self.solve_threshold:
                solved = child
                break

        elapsed_ms = int((time.monotonic() - start) * 1000)
        steps: list[TrajectoryStep] = []
        if solved is not None:
            for n in self._path_to_root(solved):
                steps.append(
                    TrajectoryStep(
                        state_before=n.parent.state if n.parent else n.state,
                        action=ProofAction(
                            type="tactic",
                            content=str(n.tactic_applied or ""),
                            description=f"expand {n.id}",
                        ),
                        state_after=n.state,
                    )
                )
        trajectory = Trajectory(
            steps=steps,
            theorem=theorem,
            success=solved is not None,
            elapsed_ms=elapsed_ms,
            strategy=self.name,
        )
        diagnosis = DiagnosisCollector.collect(
            root=root,
            theorem=theorem,
            errors_by_node=self._errors_by_node(root),
            iterations=iterations,
        )
        return trajectory, diagnosis

    # ── internals ────────────────────────────────────────────────────────
    def _make_node(
        self,
        state: ProofState,
        parent: _Node | None,
        action: ProofAction | None,
        *,
        depth: int,
    ) -> _Node:
        self._counter += 1
        return _Node(
            id=f"n{self._counter}",
            state=state,
            parent=parent,
            depth=depth,
            tactic_applied=(action.content if action else None),
            errors=list(state.errors),
        )

    def _select(self, root: _Node) -> _Node:
        """Descend by UCB1 until reaching a node that can still be expanded."""
        node = root
        while True:
            expandable = node.expanded_actions < node.available_actions
            if expandable or not node.children:
                return node
            node = max(
                node.children,
                key=lambda ch: ucb1_score(ch.value, ch.visits, node.visits, self.c),
            )

    def _expand(self, node: _Node) -> _Node | None:
        """Expand one untried action of ``node`` (returns the new child).

        Candidate actions are cached on the *node*, never on its ``state`` — the
        state object is what flows into the transition/transition-mock and must
        stay free of search bookkeeping.
        """
        if node.available_actions == 0 and not node.pending_actions:
            node.pending_actions = list(self._action_generator(node.state) or [])
            node.available_actions = len(node.pending_actions)
        actions = node.pending_actions
        if node.expanded_actions >= len(actions):
            return None
        action = actions[node.expanded_actions]
        node.expanded_actions += 1
        child_state = self._transition(node.state, action)
        child = self._make_node(child_state, node, action, depth=node.depth + 1)
        node.children.append(child)
        return child

    def _backprop(self, node: _Node, value: float) -> None:
        cur: _Node | None = node
        while cur is not None:
            cur.visits += 1
            cur.value_sum += value
            cur = cur.parent

    @staticmethod
    def _path_to_root(node: _Node) -> list[_Node]:
        chain: list[_Node] = []
        cur: _Node | None = node
        guard = 0
        while cur is not None and guard < MAX_DIAGNOSIS_CHAIN:
            chain.append(cur)
            cur = cur.parent
            guard += 1
        chain.reverse()
        return chain

    @staticmethod
    def _errors_by_node(root: _Node) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        stack = [root]
        while stack:
            n = stack.pop()
            if n.errors:
                out[n.id] = list(n.errors)
            stack.extend(n.children)
        return out


# ── default (empty) callables ────────────────────────────────────────────
def _root_state(theorem: str) -> ProofState:
    return ProofState(theorem=theorem, code="", is_terminal=False)


def _no_actions(_state: ProofState) -> list[ProofAction]:
    return []


def _zero_value(_state: ProofState) -> float:
    return 0.0
