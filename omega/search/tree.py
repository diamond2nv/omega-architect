#!/usr/bin/env python3
"""Proof tree data structure for proof search.

Represents the search state as a tree where:
- Root node = the original theorem
- Internal nodes = intermediate goal states
- Leaf nodes = completed proofs or dead ends

Each node tracks: goal state, parent, children, attempted tactics,
and the result of T2 compilation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class NodeStatus(Enum):
    """Status of a search node."""

    UNEXPLORED = auto()  # Created but not yet attempted
    IN_PROGRESS = auto()  # A tactic is being attempted
    VERIFIED = auto()  # Compiled successfully
    FAILED = auto()  # All tactics exhausted
    PRUNED = auto()  # Pruned by search strategy


@dataclass
class GoalState:
    """An intermediate proof goal state.

    Attributes
    ----------
    goal_text : str
        The Lean goal state text (what needs to be proved).
    hypotheses : list[str]
        Available hypotheses.
    target_type : str
        The type of the goal (e.g., ``ℕ``, ``ℝ``, ``True``, ``a = b``).
    depth : int
        Depth in the proof tree.
    tactic_history : list[str]
        Tactics applied so far to reach this state.
    """

    goal_text: str
    hypotheses: list[str] = field(default_factory=list)
    target_type: str = ""
    depth: int = 0
    tactic_history: list[str] = field(default_factory=list)

    @classmethod
    def from_lean_header(cls, header: str) -> GoalState:
        """Create a GoalState from a Lean theorem header.

        Extracts the target type (return type) from the theorem signature.
        Strips parenthesized type annotations to avoid splitting on ``:``
        inside binders like ``(n : ℕ)``.

        Examples::

            theorem t : True := ...
            theorem add_zero (n : ℕ) : n + 0 = n := ...
        """
        import re

        target = ""

        for line in header.split("\n"):
            if "theorem" in line or "lemma" in line or "def" in line:
                # Remove parenthesized groups (binder annotations)
                clean = re.sub(r"\([^)]*\)", "", line)
                # Split on `` : `` (colon with surrounding spaces)
                parts = clean.split(" : ")
                if len(parts) >= 2:
                    # The last part after the last ` : ` is the target type
                    raw_target = parts[-1].strip()
                    # Strip trailing ``:=`` and everything after it
                    target = re.sub(r"\s*:=.*$", "", raw_target).strip()
                    target = target.rstrip(",").removesuffix("where").strip()
                    break

        return cls(
            goal_text=header,
            target_type=target,
            depth=0,
        )

    def __repr__(self) -> str:
        hyps = len(self.hypotheses)
        return f"GoalState(depth={self.depth}, hyps={hyps}, target='{self.target_type[:40]}')"


@dataclass
class SearchNode:
    """A node in the proof search tree.

    Attributes
    ----------
    id : str
        Unique node identifier.
    goal : GoalState
        The goal state at this node.
    parent : SearchNode or None
        Parent node (``None`` for root).
    children : list[SearchNode]
        Child nodes (subgoals after applying a tactic).
    status : NodeStatus
        Current status.
    attempted_tactics : list[tuple[str, float]]
        Tactics attempted so far, with timestamps.
    tactic_applied : str or None
        The tactic that produced this node from its parent.
    lean_code : str or None
        The full Lean code for this node's proof attempt.
    verification_result : dict or None
        Result from T2 compilation.
    value : float
        Value estimate (for MCTS / evaluation).
    visits : int
        Visit count (for MCTS).
    expanded_by : str
        Which prover expanded this node (``"goedel"``, ``"rethlas"``, ``"archon"``).
    """

    id: str
    goal: GoalState
    parent: SearchNode | None = None
    children: list[SearchNode] = field(default_factory=list)
    status: NodeStatus = NodeStatus.UNEXPLORED
    attempted_tactics: list[tuple[str, float]] = field(default_factory=list)
    tactic_applied: str | None = None
    lean_code: str | None = None
    verification_result: dict | None = None
    value: float = 0.0
    visits: int = 0
    expanded_by: str = "unknown"
    created_at: float = 0.0

    def __post_init__(self) -> None:
        self.created_at = time.time()

    @property
    def is_leaf(self) -> bool:
        """A leaf has no children."""
        return len(self.children) == 0

    @property
    def is_solved(self) -> bool:
        """Solved = T2 verified."""
        return self.status == NodeStatus.VERIFIED

    @property
    def path_from_root(self) -> list[SearchNode]:
        """Get the path from root to this node."""
        path: list[SearchNode] = []
        node: SearchNode | None = self
        while node is not None:
            path.append(node)
            node = node.parent
        path.reverse()
        return path

    @property
    def depth(self) -> int:
        """Depth from root."""
        return len(self.path_from_root) - 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (for reporting/debugging)."""
        return {
            "id": self.id,
            "status": self.status.name,
            "depth": self.depth,
            "tactic": self.tactic_applied or "",
            "goal": self.goal.target_type[:60],
            "value": self.value,
            "visits": self.visits,
            "prover": self.expanded_by,
            "n_children": len(self.children),
            "verified": self.is_solved,
            "lean_code_len": len(self.lean_code or ""),
        }


class ProofTree:
    """The proof search tree.

    Manages node creation, traversal, and search statistics.
    """

    def __init__(self, theorem_header: str):
        self.root = SearchNode(
            id="root",
            goal=GoalState.from_lean_header(theorem_header),
        )
        self.root.expanded_by = "root"
        self._nodes: dict[str, SearchNode] = {"root": self.root}

    @property
    def n_nodes(self) -> int:
        return len(self._nodes)

    @property
    def n_solved(self) -> int:
        return sum(1 for n in self._nodes.values() if n.is_solved)

    @property
    def n_failed(self) -> int:
        return sum(1 for n in self._nodes.values() if n.status == NodeStatus.FAILED)

    def add_node(self, node: SearchNode) -> None:
        """Add a node to the tree."""
        self._nodes[node.id] = node

    def get_node(self, node_id: str) -> SearchNode | None:
        """Get a node by ID."""
        return self._nodes.get(node_id)

    def add_child(self, parent: SearchNode, child: SearchNode) -> None:
        """Add a child to a parent node."""
        parent.children.append(child)
        child.parent = parent
        self._nodes[child.id] = child

    def select_best(self, strategy: str = "ucb1") -> SearchNode:
        """Select the best node for expansion.

        Parameters
        ----------
        strategy : str
            ``"ucb1"`` (MCTS), ``"best_value"``, ``"most_visits"``, or ``"deepest_unexplored"``.
        """
        if strategy == "ucb1":
            return self._select_ucb1()
        elif strategy == "best_value":
            unexplored = [n for n in self._nodes.values() if n.status == NodeStatus.UNEXPLORED]
            if unexplored:
                return max(unexplored, key=lambda n: n.value)
            return self.root
        elif strategy == "most_visits":
            return max(self._nodes.values(), key=lambda n: n.visits)
        elif strategy == "deepest_unexplored":
            unexplored = [n for n in self._nodes.values() if n.status == NodeStatus.UNEXPLORED]
            if unexplored:
                return max(unexplored, key=lambda n: n.depth)
            return self.root
        return self.root

    def _select_ucb1(self, c: float = 1.4) -> SearchNode:
        """Select using UCB1 formula for MCTS."""
        best_node = self.root
        best_score = float("-inf")
        parent_visits = max(self.root.visits, 1)

        for node in self._nodes.values():
            if node.status != NodeStatus.UNEXPLORED:
                continue
            if parent_visits > 0 and node.visits > 0:
                score = node.value / node.visits + c * (parent_visits**0.5 / (1 + node.visits))
            elif parent_visits > 0:
                score = c * (parent_visits**0.5)
            else:
                score = float("inf")
            if score > best_score:
                best_score = score
                best_node = node

        return best_node if best_node.status == NodeStatus.UNEXPLORED else self.root

    def get_proof(self) -> str | None:
        """Extract the proof from a solved node.

        Returns the Lean code of the first solved leaf.
        """
        solved = [n for n in self._nodes.values() if n.is_solved]
        if not solved:
            return None
        # Prefer the shallowest solved node
        best = min(solved, key=lambda n: n.depth)
        return best.lean_code

    def get_tactic_sequence(self) -> list[str]:
        """Get the tactic sequence for the best solved path."""
        solved = [n for n in self._nodes.values() if n.is_solved]
        if not solved:
            return []
        best = min(solved, key=lambda n: n.depth)
        tactics = []
        for node in best.path_from_root:
            if node.tactic_applied:
                tactics.append(node.tactic_applied)
        return tactics

    def stats(self) -> dict[str, Any]:
        """Search statistics."""
        return {
            "n_nodes": self.n_nodes,
            "n_solved": self.n_solved,
            "n_failed": self.n_failed,
            "n_unexplored": sum(
                1 for n in self._nodes.values() if n.status == NodeStatus.UNEXPLORED
            ),
            "n_in_progress": sum(
                1 for n in self._nodes.values() if n.status == NodeStatus.IN_PROGRESS
            ),
            "best_tactic_sequence": self.get_tactic_sequence(),
            "has_proof": self.get_proof() is not None,
        }

    def summary(self) -> str:
        """Human-readable summary."""
        s = self.stats()
        lines = [
            "ProofTree Summary",
            f"  Nodes: {s['n_nodes']} total, {s['n_solved']} solved, "
            f"{s['n_failed']} failed, {s['n_unexplored']} unexplored",
        ]
        if s["has_proof"]:
            tactics = " → ".join(s["best_tactic_sequence"][:5])
            if len(s["best_tactic_sequence"]) > 5:
                tactics += " → ..."
            lines.append(f"  Tactic sequence: {tactics}")
        else:
            lines.append("  No proof found")
        return "\n".join(lines)
