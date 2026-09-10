"""Diagnosis view for MCTS-style proof search.

Search trees are diagnosis structures: they record *where* exploration got stuck,
*which* branches were never tried, and *how* failures are distributed by depth.
This module turns a search trace into that view.

Design notes
------------
* The UCB1 score itself lives in ``omega.engine.strategy_mcts.ucb1_score``
  - this module is about *reading* a search tree, not selecting within it.
* Zero third-party dependencies (stdlib only).
* Non-invasive: nothing here imports or mutates the existing engine modules at
  import time, so DFS/Beam/Hybrid strategies are unaffected.
* Thresholds are module-level constants and intentionally marked as *to be
  calibrated* — Step 2 calibrates them on real proof trajectories.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── thresholds (to be calibrated on real trajectories in Step 2) ──────────
STUCK_MIN_VISITS = 2
STUCK_MAX_VALUE = 0.34
HEAT_DEPTH_BUCKET = 3  # errors grouped per 3 depth levels

FALSIFIABILITY_NOTE = (
    "This diagnosis describes a *search strategy* failure (where exploration "
    "stalled or never reached), not a proof that the problem is unprovable."
)


@runtime_checkable
class DiagnosisNode(Protocol):
    """The duck-typed node contract :class:`DiagnosisCollector` relies on.

    Both the MCTS core's node and (with adapters) ``omega.search.tree`` nodes
    can satisfy this, but note two semantic traps:

    * ``value`` - for the MCTS core it is a **mean** (``value_sum / visits``);
      ``omega.search.tree.SearchNode.value`` is a raw estimate. The thresholds
      below are calibrated for mean-valued nodes.
    * ``is_solved`` - the MCTS core means ``state.is_terminal``; the search tree
      means ``status == VERIFIED``.

    ``generated`` distinguishes "the action generator was never consulted" from
    "it was consulted and returned nothing" - only the former is a coverage gap.
    """

    id: str
    depth: int
    visits: int
    value: float
    children: list[Any]
    available_actions: int
    generated: bool
    is_solved: bool
    error_class: str


@dataclass
class StuckNode:
    """A node the search revisited while its value stayed low.

    Empirically the analogue of a saddle point: the trajectory is weakly
    attracted to it, cannot leave it, and values it low.
    """

    node_id: str
    visits: int
    value: float
    depth: int
    tactic: str | None = None

    def line(self) -> str:
        tac = f" via {self.tactic}" if self.tactic else ""
        return (
            f"stuck {self.node_id} (visits={self.visits}, "
            f"value={self.value:.3f}, depth={self.depth}){tac}"
        )


@dataclass
class BlindSpot:
    """A coverage gap: actions left untried.

    Two shapes, both meaning "we do not know what is in there":

    * ``known=True``  — the action set is known but only partly expanded
      (``explored_children < available_actions``);
    * ``known=False`` — the node was never expanded at all, so even the size of
      its action set is unknown (``available_actions`` is reported as 0).
    """

    parent_id: str
    depth: int
    explored_children: int
    available_actions: int
    known: bool = True

    def line(self) -> str:
        if not self.known:
            return f"blind {self.parent_id} (depth={self.depth}, never expanded)"
        return (
            f"blind {self.parent_id} (depth={self.depth}, "
            f"{self.explored_children}/{self.available_actions} actions tried)"
        )


@dataclass
class DiagnosisView:
    """Structured diagnosis of one search run (see module docstring)."""

    theorem: str = ""
    explored_nodes: int = 0
    max_depth: int = 0
    solved_nodes: int = 0
    iterations: int = 0
    stuck_nodes: list[StuckNode] = field(default_factory=list)
    blind_spots: list[BlindSpot] = field(default_factory=list)
    error_heat: dict[str, int] = field(default_factory=dict)
    failure_chain: list[str] = field(default_factory=list)
    visit_entropy: float = 0.0
    visit_entropy_norm: float = 0.0
    solved_by: str = ""
    falsifiability_note: str = FALSIFIABILITY_NOTE

    # ── derived ─────────────────────────────────────────────────────────
    def top_error_buckets(self, k: int = 3) -> list[tuple[str, int]]:
        """Most frequent ``error_class@depth`` buckets."""
        return sorted(self.error_heat.items(), key=lambda kv: -kv[1])[:k]

    def summary(self) -> str:
        """One-paragraph human-readable diagnosis."""
        parts = [
            f"explored={self.explored_nodes} max_depth={self.max_depth} "
            f"solved={self.solved_nodes} iterations={self.iterations}",
            f"stuck={len(self.stuck_nodes)} blind={len(self.blind_spots)} "
            f"visit_entropy={self.visit_entropy:.3f}(norm {self.visit_entropy_norm:.3f})",
        ]
        if self.top_error_buckets():
            parts.append("hot: " + ", ".join(f"{k}×{v}" for k, v in self.top_error_buckets()))
        return " | ".join(parts)


def visit_entropy(visits: list[int], *, normalise: bool = False) -> float:
    """Shannon entropy of the visit distribution.

    A cheap proxy for basin entropy: a search that pours all visits into one
    path has entropy ~0 (deterministic, "not luck-dependent"), while a search
    spread thin across many equally-visited nodes has high entropy.

    Raw entropy grows with the node count (``ln n``), so it is only comparable
    between runs of similar size; pass ``normalise=True`` for the 0..1 variant
    used in :attr:`DiagnosisView.visit_entropy_norm`.

    Returns 0.0 for an empty or single-node distribution.
    """
    total = sum(visits)
    if total <= 0 or len(visits) < 2:
        return 0.0
    ent = 0.0
    for v in visits:
        if v > 0:
            p = v / total
            ent -= p * math.log(p)
    if normalise:
        return ent / math.log(len(visits))
    return ent


class DiagnosisCollector:
    """Builds a :class:`DiagnosisView` from a search trace.

    Nodes must satisfy :class:`DiagnosisNode` (see its docstring for the two
    semantic traps around ``value`` and ``is_solved``). The contract is duck-typed
    rather than enforced, so the same collector can read the pure-Python MCTS
    core and - with an adapter for the differing field names - the existing
    ``omega.search.tree`` nodes.
    """

    @staticmethod
    def collect(
        *,
        root,
        theorem: str = "",
        errors_by_node: dict[str, list[str]] | None = None,
        iterations: int = 0,
        target: str | None = None,
    ) -> DiagnosisView:
        """Walk the tree once and produce the diagnosis view."""
        view = DiagnosisView(theorem=theorem, iterations=iterations)
        errors_by_node = errors_by_node or {}
        visits: list[int] = []

        stack = [root]
        best_failure: object = None
        # tie-break by lowest value so the reported chain does not depend on
        # traversal order when several nodes share the deepest non-solved depth
        best_failure_key: tuple[int, float] = (-1, float("inf"))
        while stack:
            node = stack.pop()
            view.explored_nodes += 1
            view.max_depth = max(view.max_depth, getattr(node, "depth", 0))
            if getattr(node, "is_solved", False):
                view.solved_nodes += 1

            n_visits = int(getattr(node, "visits", 0))
            value = float(getattr(node, "value", 0.0))
            visits.append(n_visits)

            # ① stuck nodes: revisited but low value (never the root - the
            #    root's mean is the whole tree's mean and would always qualify)
            node_depth = int(getattr(node, "depth", 0))
            if node_depth > 0 and n_visits >= STUCK_MIN_VISITS and value <= STUCK_MAX_VALUE:
                view.stuck_nodes.append(
                    StuckNode(
                        node_id=str(getattr(node, "id", "?")),
                        visits=n_visits,
                        value=value,
                        depth=int(getattr(node, "depth", 0)),
                        tactic=getattr(node, "tactic_applied", None),
                    )
                )

            # ③ blind spots: (a) known-but-untried actions, (b) never-expanded nodes
            available = int(getattr(node, "available_actions", 0))
            explored = len(getattr(node, "children", []) or [])
            node_depth = int(getattr(node, "depth", 0))
            solved = bool(getattr(node, "is_solved", False))
            if available > explored:
                view.blind_spots.append(
                    BlindSpot(
                        parent_id=str(getattr(node, "id", "?")),
                        depth=node_depth,
                        explored_children=explored,
                        available_actions=available,
                    )
                )
            elif (
                available == 0
                and explored == 0
                and node_depth > 0
                and not solved
                and not bool(getattr(node, "generated", False))
            ):
                # never consulted the generator -> size of the action set unknown.
                # (A node whose generator ran and returned nothing is *exhausted*,
                # not a coverage gap.)
                view.blind_spots.append(
                    BlindSpot(
                        parent_id=str(getattr(node, "id", "?")),
                        depth=node_depth,
                        explored_children=0,
                        available_actions=0,
                        known=False,
                    )
                )

            # ① error heat: aggregated by *normalised* error class, not raw text
            #    (raw messages are unique-ish and would never bucket together)
            errs = errors_by_node.get(str(getattr(node, "id", "")), [])
            node_class = str(getattr(node, "error_class", "") or "")
            if node_class or errs:
                key = node_class or errs[0][:60]
                bucket = f"{key}@{int(getattr(node, 'depth', 0)) // HEAT_DEPTH_BUCKET}"
                view.error_heat[bucket] = view.error_heat.get(bucket, 0) + 1

            # ② failure chain: deepest non-solved node, lowest value on ties
            depth = int(getattr(node, "depth", 0))
            if not getattr(node, "is_solved", False):
                key = (depth, -float(getattr(node, "value", 0.0)))
                if key > best_failure_key:
                    best_failure, best_failure_key = node, key

            stack.extend(list(getattr(node, "children", []) or []))

        view.visit_entropy = visit_entropy(visits)
        view.visit_entropy_norm = visit_entropy(visits, normalise=True)
        view.failure_chain = DiagnosisCollector._ancestors(best_failure, target)
        view.stuck_nodes.sort(key=lambda s: -s.visits)
        view.blind_spots.sort(key=lambda b: -b.depth)
        return view

    @staticmethod
    def _ancestors(node, target: str | None) -> list[str]:
        """Root→node id chain (the failure propagation path)."""
        chain: list[str] = []
        cur = node
        seen = 0
        while cur is not None and seen < 10_000:
            node_id = str(getattr(cur, "id", ""))
            if target and node_id == target:
                break
            chain.append(node_id)
            cur = getattr(cur, "parent", None)
            seen += 1
        chain.reverse()
        return chain
