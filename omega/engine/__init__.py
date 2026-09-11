"""Ω-Architect Engine Layer — Multi-Path Trajectory Exploration.

Core Framework
--------------
The engine layer frames theorem proving as a **search over proof trajectories**,
inspired by Tree-of-Thoughts (ToT), AlphaZero/MCTS, and beam search.

Difficulty estimation uses an **NLP algorithm spectrum** (TokenJaccard +
EditDistance + BM25Retrieval + EmbeddingSimilarity) trained on a labeled
corpus of known theorems — replaces hand-crafted keyword scoring.

Search Strategies
-----------------
| Strategy | Algorithm | Module | Description |
|----------|-----------|--------|-------------|
| DFS      | Dialogue  | omega/loop/inner_loop | Single deep trajectory, backtrack on error |
| Beam     | Sampling  | omega/prover/go_prover | Top-K parallel candidates |
| Hybrid   | Multi-Path| omega/engine/hybrid | DFS first, beam on stuck |

Mode Router
-----------
The router automatically selects the optimal strategy based on
theorem difficulty + resource availability + execution history.
Difficulty is estimated via the NLP spectrum.

    from omega.engine import ModeRouter, RoutingContext, ResourceProfile

    router = ModeRouter()
    decision = router.route(RoutingContext(theorem_header="...", resource_profile=...))
    trajectory = decision.strategy.run(theorem_header)

Usage
-----
    from omega.engine import get_strategy, list_strategies
    from omega.engine.trajectory import ProofState, ProofAction, Trajectory
    from omega.engine.difficulty_spectrum import DifficultySpectrum

    spectrum = DifficultySpectrum()
    est = spectrum.estimate("theorem t : 1+1=2 := by")
    # est.level == "EASY", est.confidence

    strategy = get_strategy("hybrid")
    result = strategy.run(theorem)
    # result.success, result.proof, result.steps
"""

from __future__ import annotations

from omega.engine.counterfactual import (
    CounterfactualAttributor,
    CounterfactualReport,
    RepairTarget,
    ReverseCheck,
    reconstruct_theorem_source,
    render_by_append,
    render_by_block,
)
from omega.engine.difficulty_spectrum import (
    DifficultyEstimate,
    DifficultySpectrum,
)
from omega.engine.hybrid import HybridV2Config, HybridV2Result, run_hybrid_v2
from omega.engine.mcts_diagnosis import (
    BlindSpot,
    DiagnosisCollector,
    DiagnosisView,
    StuckNode,
    candidates_from_heat,
    visit_entropy,
)
from omega.engine.orchestrator import (
    PRIMITIVES,
    Orchestrator,
    OrchestratorConfig,
    OrchestratorResult,
    OrchestratorState,
    apply_lemma,
    calc_chain,
    case_split,
    extract_proof,
    fallback_decompose,
    induction,
    rewrite_goal,
    search_lemma,
)
from omega.engine.router import (
    DifficultyLevel,
    ModeRouter,
    ResourceProfile,
    RouterConfig,
    RoutingContext,
    RoutingDecision,
)
from omega.engine.strategy_mcts import MCTSStrategy, ucb1_score
from omega.engine.trajectory import (
    BeamStrategy,
    DFSStrategy,
    HybridStrategy,
    ProofAction,
    ProofState,
    SearchStrategy,
    Trajectory,
    TrajectoryStep,
    get_strategy,
    list_strategies,
)

__all__ = [
    # Multi-Path Strategy API
    "get_strategy",
    "list_strategies",
    "SearchStrategy",
    "DFSStrategy",
    "MCTSStrategy",
    "ucb1_score",
    "DiagnosisView",
    "DiagnosisCollector",
    "visit_entropy",
    # L3 counterfactual attribution (repair targets)
    "CounterfactualAttributor",
    "CounterfactualReport",
    "RepairTarget",
    "ReverseCheck",
    "candidates_from_heat",
    "render_by_append",
    "render_by_block",
    "reconstruct_theorem_source",
    "BeamStrategy",
    "HybridStrategy",
    # Core abstractions
    "ProofState",
    "ProofAction",
    "Trajectory",
    "TrajectoryStep",
    # Mode Router
    "ModeRouter",
    "RoutingContext",
    "RoutingDecision",
    "ResourceProfile",
    "DifficultyLevel",
    "RouterConfig",
    # Difficulty Spectrum
    "DifficultySpectrum",
    "DifficultyEstimate",
    # Hybrid v2
    "run_hybrid_v2",
    "HybridV2Config",
    "HybridV2Result",
]
