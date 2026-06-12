"""Trajectory — Multi-Path Trajectory Exploration framework for theorem proving.

Formalizes the Ω-Architect engine layer as a **search over proof trajectories**,
inspired by Tree-of-Thoughts (ToT), AlphaZero/MCTS, and beam search.

Core Concepts
-------------
State
    A snapshot in the proof search: theorem + partial code + errors + goals.

Action
    A transformation on a state: tactic application, rewrite, search, decomposition.

Trajectory
    A sequence of (State, Action, Outcome) triples — one path through the tree.

Search Strategy
    The algorithm that decides which trajectories to explore:
    - DFS (Dialogue): single deep path, backtrack on error
    - Beam (Sampling): keep top-K candidates at each level
    - Hybrid (Multi-Path): DFS first, beam on stuck, re-explore with context

┌─────────────────────────────────────────────────────────────────┐
│  SearchStrategy (abstract)                                       │
│  ├── DFSStrategy     (omega/loop/inner_loop → Dialogue)          │
│  ├── BeamStrategy    (omega/prover/go_prover → Sampling)         │
│  └── HybridStrategy  (omega/engine/hybrid → Multi-Path)         │
└─────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger("omega.engine.trajectory")


# ═══════════════════════════════════════════════════════════════════
# State & Action
# ═══════════════════════════════════════════════════════════════════


@dataclass
class ProofState:
    """A snapshot of the theorem-proving process.

    Represents a node in the search tree.

    Parameters
    ----------
    theorem : str
        The original theorem header (immutable across the search tree).
    code : str
        The current Lean 4 code (partial or complete proof).
    errors : list[str]
        Compile errors from the last attempt (empty if code is valid).
    error_class : str or None
        Dominant error category.
    goals : list[str]
        Remaining proof goals (extracted via MCP or LLM).
    depth : int
        Depth in the search tree (number of actions applied so far).
    value : float
        Estimated utility of this state (0.0 = failure, 1.0 = proven).
    is_terminal : bool
        True if the proof is complete (compiles with no errors).
    confidence : float
        LLM/classifier confidence in this state (0.0-1.0).
    metadata : dict
        Additional context (LLM reasoning, budget usage, timestamp).
    """

    theorem: str
    code: str = ""
    errors: list[str] = field(default_factory=list)
    error_class: str = ""
    goals: list[str] = field(default_factory=list)
    depth: int = 0
    value: float = 0.0
    is_terminal: bool = False
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProofAction:
    """An action that transforms a ProofState.

    Parameters
    ----------
    type : str
        Action category: "tactic", "rewrite", "search", "decompose", "complete".
    content : str
        The Lean code or tactic applied.
    confidence : float
        LLM's confidence this action will improve the state (0.0-1.0).
    description : str
        Human-readable description of the action.
    metadata : dict
        Additional info (LLM reasoning, tool call results).
    """

    type: str = "tactic"
    content: str = ""
    confidence: float = 0.0
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# Trajectory
# ═══════════════════════════════════════════════════════════════════


@dataclass
class TrajectoryStep:
    """A single step in a proof trajectory.

    Parameters
    ----------
    state_before : ProofState
        State before the action.
    action : ProofAction
        The action taken.
    state_after : ProofState
        State after the action is evaluated.
    elapsed_ms : int
        Time taken for this step (LLM call + compile + analysis).
    """

    state_before: ProofState
    action: ProofAction
    state_after: ProofState
    elapsed_ms: int = 0


@dataclass
class Trajectory:
    """A complete trajectory through the proof search tree.

    Parameters
    ----------
    steps : list[TrajectoryStep]
        Ordered sequence of steps.
    theorem : str
        The theorem being proved.
    success : bool
        Whether the trajectory reached a terminal proof state.
    elapsed_ms : int
        Total wall-clock time for the trajectory.
    n_budget_used_tokens : int
        Total tokens consumed.
    n_budget_used_cost : float
        Total USD cost.
    """

    steps: list[TrajectoryStep] = field(default_factory=list)
    theorem: str = ""
    success: bool = False
    elapsed_ms: int = 0
    n_budget_used_tokens: int = 0
    n_budget_used_cost: float = 0.0

    @property
    def depth(self) -> int:
        return len(self.steps)

    @property
    def final_state(self) -> ProofState | None:
        return self.steps[-1].state_after if self.steps else None

    @property
    def proof(self) -> str | None:
        if self.success and self.final_state:
            return self.final_state.code
        return None


# ═══════════════════════════════════════════════════════════════════
# Search Strategies
# ═══════════════════════════════════════════════════════════════════


class SearchStrategy(ABC):
    """Abstract base for all proof search strategies.

    A strategy defines: how to generate actions, how to evaluate states,
    how to select the next state to explore, and when to stop.
    """

    @abstractmethod
    def run(self, theorem: str) -> Trajectory:
        """Execute the search strategy on a theorem.

        Parameters
        ----------
        theorem : str
            The Lean 4 theorem header to prove.

        Returns
        -------
        Trajectory
            The search result, including success status and all explored states.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this strategy."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of how this strategy works."""
        ...


# ═══════════════════════════════════════════════════════════════════
# Concrete Strategies (descriptors — implementations reference)
# ═══════════════════════════════════════════════════════════════════

# These classes are conceptual wrappers. The actual execution is
# delegated to the existing modules.

# ── Strategy A: DFS (Depth-First Search) ─────────────────────────


class DFSStrategy(SearchStrategy):
    """Depth-First Search over proof trajectory.

    Explores a single path at a time, going deeper on each step.
    On error (dead end), backtracks to the last viable state and
    tries a different action.

    Implementation: omega/loop/inner_loop (Dialogue Mode)

    Properties:
    - Linear token cost (one trajectory)
    - Good for easy/medium theorems (3-20 rounds)
    - Cheap backtracking via LLM re-prompting
    - Vulnerable to: local optima, repetitive errors
    """

    @property
    def name(self) -> str:
        return "DFS (Dialogue)"

    @property
    def description(self) -> str:
        return (
            "Single trajectory depth-first search. "
            "Generates one proof attempt per round, compiles it, "
            "and uses compile errors as feedback to refine. "
            "Suitable for theorems where a direct proof strategy exists."
        )

    def run(self, theorem: str) -> Trajectory:
        from omega.loop.inner import inner_loop, InnerLoopConfig
        result = inner_loop(theorem, config=InnerLoopConfig(max_rounds=512))
        return Trajectory(
            theorem=theorem,
            success=result.success,
            elapsed_ms=result.total_elapsed_ms,
        )


# ── Strategy B: Beam (Beam Search) ──────────────────────────────


class BeamStrategy(SearchStrategy):
    """Beam Search over proof trajectories.

    Generates K independent candidates at each level, scores them
    via compile verification, and keeps the top candidates for
    self-correction. Breadth-first: multiple trajectories explored
    in parallel.

    Implementation: omega/prover/go_prover (Sampling Mode)

    Properties:
    - Parallel trajectory exploration
    - Good for: problems with multiple valid approaches
    - Cost scales linearly with beam width K
    - Weakness: limited depth without correction rounds
    """

    @property
    def name(self) -> str:
        return "Beam (Sampling)"

    @property
    def description(self) -> str:
        return (
            "Parallel beam search over proof candidates. "
            "Generates K independent proof attempts simultaneously, "
            "compiles all, and applies self-correction to failed attempts. "
            "Returns the first successful proof."
        )

    def run(self, theorem: str) -> Trajectory:
        from omega.prover.go_prover import GoedelProver
        from omega.verify import t2_real
        prover = GoedelProver(
            compile_fn=t2_real.make_real_compile_callback(),
            num_samples=6,
            max_correction_rounds=2,
        )
        result = prover.run(theorem_header=theorem)
        elapsed = int(result.timings.get("total_s", 0) * 1000)
        return Trajectory(
            theorem=theorem,
            success=result.succeeded,
            elapsed_ms=elapsed,
        )


# ── Strategy C: Hybrid (Multi-Path Trajectory Exploration) ──────


class HybridStrategy(SearchStrategy):
    """Multi-Path Trajectory Exploration.

    Two-phase search:
    Phase 1: DFS trajectory (Dialogue) — fast path for easy theorems.
    Phase 2 (if stuck): Beam search (Sampling) — second opinion with
    K parallel candidates.
    Phase 3 (if beam fails): Re-explore with full context — second DFS
    trajectory seeded with beam's best candidate.

    Implementation: omega/engine/hybrid.run_hybrid_v2()

    Properties:
    - Zero overhead for easy theorems (Phase 1 only)
    - Full multi-path exploration for hard theorems
    - Stuck detection prevents wasted rounds
    - Phase 3 uses beam candidates as context for a fresh DFS
    """

    @property
    def name(self) -> str:
        return "Hybrid (Multi-Path)"

    @property
    def description(self) -> str:
        return (
            "Multi-path trajectory exploration. "
            "Starts with DFS (Dialogue) for fast proof discovery. "
            "If the solver gets stuck (repeated errors, dead loop, divergence), "
            "launches a beam search (Sampling) over parallel trajectories. "
            "If beam also fails, seeds a fresh DFS trajectory with beam's "
            "best candidate as context. "
            "Easy theorems resolve in Phase 1 with zero overhead; "
            "hard theorems benefit from multi-path diversity."
        )

    def run(self, theorem: str) -> Trajectory:
        from omega.engine.hybrid import run_hybrid_v2, HybridV2Config
        result = run_hybrid_v2(theorem, HybridV2Config())
        elapsed = int(result.elapsed_s * 1000)
        return Trajectory(
            theorem=theorem,
            success=result.success,
            elapsed_ms=elapsed,
        )


# ═══════════════════════════════════════════════════════════════════
# Strategy Registry
# ═══════════════════════════════════════════════════════════════════

_STRATEGIES: dict[str, type[SearchStrategy]] = {
    "dfs": DFSStrategy,
    "beam": BeamStrategy,
    "hybrid": HybridStrategy,
}


def get_strategy(name: str) -> SearchStrategy:
    """Get a search strategy by name.

    Parameters
    ----------
    name : str
        One of: "dfs", "beam", "hybrid"

    Returns
    -------
    SearchStrategy
        An instance of the requested strategy.
    """
    cls = _STRATEGIES.get(name.lower())
    if cls is None:
        available = ", ".join(_STRATEGIES)
        raise ValueError(f"Unknown strategy '{name}'. Available: {available}")
    return cls()


def list_strategies() -> list[dict[str, str]]:
    """List all available search strategies with descriptions."""
    return [
        {
            "name": name,
            "class": cls.__name__,
        }
        for name, cls in _STRATEGIES.items()
    ]
