"""MCTSStrategy - UCB1 proof search wired onto the engine's strategy ABC.

This is the *wiring* half of the MCTS plan: the engine already ships a search
tree with UCB1 selection (``omega/search/tree.py``) but no strategy calls it.
Here we add a strategy that

* runs UCB1 selection over a **pure-Python search core** (so it is testable
  without Lean, an LLM, or a GPU), and
* emits a :class:`~omega.engine.mcts_diagnosis.DiagnosisView` describing *where*
  the search stalled, *which* branches were never tried, and how failures are
  distributed by depth.

Non-invasive *towards the other strategies*: DFS/Beam/Hybrid are untouched.

Failure containment (added after an independent review, 2026-09-10): the three
injected collaborators are user-supplied code - an LLM call, a Lean compile, a
value model - and any of them can raise or hang on a bad day. Each call site is
wrapped, degrades to a diagnosable state, and never propagates out of
``run_diagnosed``. Step 2's acceptance criterion "a raising compiler must not
crash the search" is therefore met *here*, not in the adapters.

Budget honesty: a node whose actions are all tried (or that has none) is marked
``exhausted``; the generator is invoked **once per node** (it may be a paid LLM
call); the loop stops early when everything is exhausted; and
``DiagnosisView.iterations`` counts productive iterations, so the diagnosis never
claims work that did not happen.

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
#: When this many expansions in a row fail, the tree is exhausted in practice.
STALL_FACTOR = 2
STALL_FLOOR = 3


def ucb1_score(
    value: float, visits: int, parent_visits: int, c: float = 1.4
) -> float:
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
    """Lightweight search node (duck-compatible with the diagnosis collector).

    ``generated`` distinguishes "the action generator has not been consulted
    yet" from "it returned nothing" - without it, a generator that legitimately
    returns no actions would be re-invoked on every visit (a paid LLM call in
    Step 2) and would be misreported as a coverage gap.
    """

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
    generated: bool = False
    exhausted: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def value(self) -> float:
        """Mean value (0.0 when never evaluated)."""
        return (self.value_sum / self.visits) if self.visits else 0.0

    @property
    def is_solved(self) -> bool:
        return bool(self.state.is_terminal)

    @property
    def error_class(self) -> str:
        """Normalised error class of this node's state (for diagnosis)."""
        return str(getattr(self.state, "error_class", "") or "")


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

    Success is decided by the *environment* (``ProofState.is_terminal``). An
    evaluator-driven ``solve_threshold`` is available but **off by default**:
    letting a value model declare a proof "done" produced unverified code as
    ``Trajectory.proof`` in an earlier revision. When enabled, the run is
    recorded as ``solved_by="evaluator"`` so the audit trail stays honest.
    """

    def __init__(
        self,
        action_generator: ActionGenerator | None = None,
        evaluator: Evaluator | None = None,
        *,
        state_transition: StateTransition | None = None,
        max_iterations: int = 50,
        c: float = 1.4,
        solve_threshold: float | None = None,
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

    # -- SearchStrategy ABC -------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return (
            "Monte-Carlo tree search with standard UCB1 selection over proof "
            "states. Explores a non-symmetric tree (budget concentrated on "
            "promising branches), exports a diagnosis view of stalled nodes, "
            "untried branches and error heat, and is anytime: when the budget "
            "runs out it returns the best path found so far, marked unsuccessful."
        )

    def run(self, theorem: str) -> Trajectory:
        """Run the search and return the trajectory (ABC contract)."""
        trajectory, _ = self.run_diagnosed(theorem)
        return trajectory

    # -- diagnosis-aware entry point ---------------------------------------
    def run_diagnosed(self, theorem: str) -> tuple[Trajectory, DiagnosisView]:
        """Run the search; return both the trajectory and the diagnosis view."""
        start = time.monotonic()
        self._counter = 0
        root = self._make_node(_root_state(theorem), None, None, depth=0)
        root.visits = 1
        solved: _Node | None = None
        solved_by = ""
        productive = 0
        failures = 0

        for _ in range(self.max_iterations):
            node = self._select(root)
            if node.is_solved:
                solved, solved_by = node, "terminal"
                break
            child = self._expand(node)
            if child is None:
                # nothing left to try here: de-prioritise and stop early once
                # the whole tree is dry, instead of burning the budget silently
                node.visits += 1
                failures += 1
                if self._stalled(root, failures):
                    break
                continue
            failures = 0
            productive += 1
            value = self._safe_evaluate(child.state)
            self._backprop(child, value)
            if child.is_solved:
                solved, solved_by = child, "terminal"
                break
            if self.solve_threshold is not None and value >= self.solve_threshold:
                solved, solved_by = child, "evaluator"
                break

        # anytime: if unsolved, still report the best path found
        best = solved if solved is not None else self._best_so_far(root)
        steps = self._steps_for(best)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        trajectory = Trajectory(
            steps=steps,
            theorem=theorem,
            success=solved is not None,
            elapsed_ms=elapsed_ms,
            strategy=self.name if solved is not None else f"{self.name} (unsolved)",
        )
        diagnosis = DiagnosisCollector.collect(
            root=root,
            theorem=theorem,
            errors_by_node=self._errors_by_node(root),
            iterations=productive,
        )
        diagnosis.solved_by = solved_by
        return trajectory, diagnosis

    # -- internals ---------------------------------------------------------
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
        """Descend by UCB1 until reaching a node that can still be expanded.

        Exhausted nodes are still selectable (so their visit counts keep the
        UCB1 terms honest) but :meth:`_expand` refuses to act on them.
        """
        node = root
        guard = 0
        while guard < MAX_DIAGNOSIS_CHAIN:
            guard += 1
            if node.is_solved or node.exhausted:
                return node
            if not node.generated:
                # the action generator has not run yet: this node is expandable
                # (available_actions is 0 only because nothing has been asked for)
                return node
            if node.expanded_actions < node.available_actions:
                return node
            if not node.children:
                node.exhausted = True
                return node
            node = max(
                node.children,
                key=lambda ch: ucb1_score(ch.value, ch.visits, node.visits, self.c),
            )
        return node

    def _expand(self, node: _Node) -> _Node | None:
        """Expand one untried action of ``node`` (returns the new child or None).

        The action generator is consulted **at most once per node** - it can be
        a paid LLM call - and candidate actions are cached on the node, never on
        its ``state`` (the state object flows into the transition and must stay
        free of search bookkeeping).
        """
        if node.exhausted:
            return None
        if not node.generated:
            node.generated = True
            try:
                actions = list(self._action_generator(node.state) or [])
            except Exception as exc:  # noqa: BLE001 - never break the search
                actions = []
                node.errors.append(f"generator_error: {type(exc).__name__}: {exc}")
            node.pending_actions = actions
            node.available_actions = len(actions)
        if node.expanded_actions >= len(node.pending_actions):
            node.exhausted = True
            return None
        action = node.pending_actions[node.expanded_actions]
        node.expanded_actions += 1
        try:
            child_state = self._transition(node.state, action)
        except Exception as exc:  # noqa: BLE001 - degrade to a diagnosable state
            child_state = replace(
                node.state,
                depth=node.state.depth + 1,
                errors=[f"transition_error: {type(exc).__name__}: {exc}"],
                error_class="other",
                is_terminal=False,
            )
        if not isinstance(child_state, ProofState):
            child_state = replace(node.state, depth=node.state.depth + 1)
        child = self._make_node(child_state, node, action, depth=node.depth + 1)
        node.children.append(child)
        return child

    def _safe_evaluate(self, state: ProofState) -> float:
        """Evaluate a state, degrading to 0.0 if the value model misbehaves."""
        try:
            return float(self._evaluator(state))
        except Exception:  # noqa: BLE001 - a broken value model is not fatal
            return 0.0

    def _backprop(self, node: _Node, value: float) -> None:
        cur: _Node | None = node
        guard = 0
        while cur is not None and guard < MAX_DIAGNOSIS_CHAIN:
            cur.visits += 1
            cur.value_sum += value
            cur = cur.parent
            guard += 1

    def _stalled(self, root: _Node, consecutive_failures: int) -> bool:
        """True once this many expansions in a row produced nothing new.

        Scaling the threshold with the tree size means a small tree stops after
        a handful of dry expansions while a large one is still allowed to run -
        bounded by ``max_iterations`` either way.
        """
        limit = STALL_FACTOR * self._count_nodes(root) + STALL_FLOOR
        return consecutive_failures >= limit

    @staticmethod
    def _count_nodes(root: _Node) -> int:
        count = 0
        stack = [root]
        while stack:
            node = stack.pop()
            count += 1
            stack.extend(node.children)
        return count

    def _best_so_far(self, root: _Node) -> _Node | None:
        """Deepest, highest-value non-terminal node (the anytime answer)."""
        best: _Node | None = None
        best_key: tuple[int, float] = (-1, float("-inf"))
        stack = [root]
        while stack:
            node = stack.pop()
            if not node.is_solved:
                key = (node.depth, node.value)
                if key > best_key:
                    best, best_key = node, key
            stack.extend(node.children)
        return best

    def _steps_for(self, node: _Node | None) -> list[TrajectoryStep]:
        """Path root->node as trajectory steps (empty when nothing was explored)."""
        if node is None or node.parent is None:
            return []
        steps: list[TrajectoryStep] = []
        for item in self._path_to_root(node):
            if item.parent is None:
                continue
            steps.append(
                TrajectoryStep(
                    state_before=item.parent.state,
                    action=ProofAction(
                        type="tactic",
                        content=str(item.tactic_applied or ""),
                        description=f"expand {item.id}",
                    ),
                    state_after=item.state,
                )
            )
        return steps

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


# -- default (empty) callables ------------------------------------------------
def _root_state(theorem: str) -> ProofState:
    return ProofState(theorem=theorem, code="", is_terminal=False)


def _no_actions(_state: ProofState) -> list[ProofAction]:
    return []


def _zero_value(_state: ProofState) -> float:
    return 0.0
