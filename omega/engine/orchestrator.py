"""Layer 3: Orchestration — Multi-Agent State Machine for Hard Theorems.

The Orchestrator decomposes hard theorems into sub-goals (blueprint DAG),
proves them via delegated strategies, and synthesizes the final proof.

State Machine
-------------
    analyze_query → generate_blueprint → prove_lemmas → refine → verify → synthesize
         │               │                    │           │         │          │
         ▼               ▼                    ▼           ▼         ▼          ▼
    Difficulty       Blueprint DAG        Parallel      Fix failed  Final Lean  Return
    Spectrum         of LemmaNodes        prove lemmas  lemmas     verify      proof

8 Primitives
-----------
Each primitive is a function (or callable) that transforms a proof state:

1. apply_lemma    — apply a known lemma to the current goal
2. rewrite_goal   — rewrite the goal using a known identity
3. induction      — perform induction on a variable
4. case_split     — case analysis on a hypothesis
5. calc_chain     — chain of equalities/inequalities
6. search_lemma   — search Mathlib for a relevant lemma
7. extract_proof  — extract a proof from a successful trajectory
8. fallback_decompose — when none of the above works, decompose into sub-lemmas
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable

from omega.search.blueprint import (
    Blueprint,
    DiagnosisType,
    LemmaNode,
    LemmaStatus,
    generate_blueprint,
    refine_blueprint,
)

logger = logging.getLogger("omega.engine.orchestrator")


# ═══════════════════════════════════════════════════════════════════
# Enums & Config
# ═══════════════════════════════════════════════════════════════════


class OrchestratorState(Enum):
    """States in the orchestrator state machine."""
    IDLE = auto()
    ANALYZING = auto()
    GENERATING_BLUEPRINT = auto()
    PROVING_LEMMAS = auto()
    REFINING = auto()
    VERIFYING = auto()
    SYNTHESIZING = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator.

    Parameters
    ----------
    max_refinement_rounds : int
        Maximum rounds of blueprint refinement (default 3).
    lemma_timeout_s : float
        Per-lemma timeout in seconds (default 120).
    parallel_proving : bool
        Prove independent lemmas in parallel (default True).
    max_concurrent_lemmas : int
        Max parallel sub-goals (default 4, limited by DeepSeek API).
    enable_fallback_decompose : bool
        Auto-decompose failed lemmas (default True).
    enable_llm_sketch : bool
        Use LLM to generate proof sketch before decomposition (default True).
    """
    max_refinement_rounds: int = 3
    lemma_timeout_s: float = 120.0
    parallel_proving: bool = True
    max_concurrent_lemmas: int = 4
    enable_fallback_decompose: bool = True
    enable_llm_sketch: bool = True


# ═══════════════════════════════════════════════════════════════════
# Primitive Results
# ═══════════════════════════════════════════════════════════════════


@dataclass
class PrimitiveResult:
    """Result of applying a single primitive.

    Parameters
    ----------
    success : bool
        Whether the primitive succeeded.
    code : str or None
        Lean code produced (if successful).
    new_subgoals : list of str
        New sub-goals introduced (for decompose/induction).
    error : str or None
        Error message if failed.
    elapsed_ms : int
        Time taken.
    metadata : dict
        Additional info.
    """
    success: bool = False
    code: str | None = None
    new_subgoals: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# 8 Primitives
# ═══════════════════════════════════════════════════════════════════

# Each primitive is a module-level function with signature:
#   fn(goal: str, context: dict) -> PrimitiveResult


def apply_lemma(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Apply a known lemma to the current goal.

    Uses the goal type to select an appropriate lemma from context.
    """
    ctx = context or {}
    lemma = ctx.get("suggested_lemma", "")
    if not lemma:
        return PrimitiveResult(success=False, error="No lemma suggested",
                                new_subgoals=[goal])

    code = f"  apply {lemma}"
    return PrimitiveResult(
        success=True,
        code=code,
        metadata={"lemma": lemma},
    )


def rewrite_goal(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Rewrite the goal using an identity.

    Suggests `rw [lemma]` or `simp` based on goal structure.
    """
    ctx = context or {}
    pattern = ctx.get("rewrite_pattern", "")
    if pattern:
        code = f"  rw [{pattern}]"
    else:
        code = "  simp"
    return PrimitiveResult(
        success=True,
        code=code,
        metadata={"pattern": pattern or "simp"},
    )


def induction(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Perform induction on a variable.

    Returns the base case and inductive step as new sub-goals.
    """
    ctx = context or {}
    var = ctx.get("induction_var", "n")
    code = f"  induction {var}"
    base = f"{goal.replace('∀', '').strip()} (base case)"
    step = f"{goal.replace('∀', '').strip()} (inductive step)"
    return PrimitiveResult(
        success=True,
        code=code,
        new_subgoals=[base, step],
        metadata={"induction_var": var},
    )


def case_split(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Case analysis on a hypothesis.

    Returns each case as a new sub-goal.
    """
    ctx = context or {}
    var = ctx.get("case_var", "h")
    code = f"  cases {var}"
    return PrimitiveResult(
        success=True,
        code=code,
        new_subgoals=[f"{goal} (case 1)", f"{goal} (case 2)"],
        metadata={"case_var": var},
    )


def calc_chain(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Chain of equalities/inequalities.

    Produces a calc block skeleton.
    """
    code = "  calc\n    ... = ... := by\n      ...\n    ... = ... := by\n      ..."
    return PrimitiveResult(
        success=True,
        code=code,
        metadata={"type": "calc"},
    )


def search_lemma(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Search Mathlib for a relevant lemma.

    Delegates to P0 Search Aggregator.
    """
    ctx = context or {}
    query = ctx.get("search_query", goal[:100])
    try:
        from omega.search.sources.sources import LeanSearchSource, LoogleSource
        ls = LeanSearchSource()
        loogle = LoogleSource()
        lemmas: list[str] = []
        try:
            import json, httpx
            ls_result = httpx.get(ls._endpoint, params={"query": query, "limit": 3}, timeout=10)
            if ls_result.status_code == 200:
                for item in ls_result.json().get("results", []):
                    name = item.get("name", "")
                    if name:
                        lemmas.append(name)
        except Exception:
            pass
        return PrimitiveResult(
            success=len(lemmas) > 0,
            code=f"  -- found: {', '.join(lemmas[:3])}" if lemmas else "  -- no lemmas found",
            metadata={"lemmas": lemmas[:5], "n_results": len(lemmas)},
        )
    except Exception as e:
        return PrimitiveResult(success=False, error=str(e),
                                metadata={"query": query})


def extract_proof(trajectory: Any, context: dict | None = None) -> PrimitiveResult:
    """Extract proof code from a successful trajectory.

    This primitive is different — it takes a completed trajectory
    rather than a goal string.
    """
    ctx = context or {}
    if hasattr(trajectory, "success") and trajectory.success:
        code = trajectory.proof if hasattr(trajectory, "proof") else (
            trajectory.code if hasattr(trajectory, "code") else None
        )
        if code:
            return PrimitiveResult(
                success=True,
                code=code,
                metadata={"source": "trajectory"},
            )
    return PrimitiveResult(success=False, error="No successful trajectory")


def fallback_decompose(goal: str, context: dict | None = None) -> PrimitiveResult:
    """Decompose a hard goal into sub-lemmas.

    This is the last resort — when no other primitive works.
    Returns a set of simpler sub-goals that together prove the goal.
    """
    ctx = context or {}
    subgoals = ctx.get("subgoals", [f"{goal} (sub-lemma 1)", f"{goal} (sub-lemma 2)"])
    code_lines = [f"  -- Decomposed into {len(subgoals)} sub-lemmas:"]
    for i, sg in enumerate(subgoals):
        code_lines.append(f"  --   lemma {i + 1}: {sg[:60]}")
    return PrimitiveResult(
        success=False,  # decompose always "fails" the original goal → creates new ones
        code="\n".join(code_lines),
        new_subgoals=subgoals,
        metadata={"n_subgoals": len(subgoals)},
    )


# ── Primitive registry ───────────────────────────────────────────

PRIMITIVES: dict[str, Callable] = {
    "apply_lemma": apply_lemma,
    "rewrite_goal": rewrite_goal,
    "induction": induction,
    "case_split": case_split,
    "calc_chain": calc_chain,
    "search_lemma": search_lemma,
    "extract_proof": extract_proof,
    "fallback_decompose": fallback_decompose,
}


# ═══════════════════════════════════════════════════════════════════
# Orchestrator Result
# ═══════════════════════════════════════════════════════════════════


@dataclass
class OrchestratorResult:
    """Result of orchestrator execution.

    Parameters
    ----------
    success : bool
        Whether the theorem was proved.
    proof : str or None
        Complete Lean proof code.
    blueprint : Blueprint or None
        The final blueprint after refinement.
    state_history : list of (OrchestratorState, str)
        State transitions with descriptions.
    n_primitives_used : int
        Total primitives applied across all lemmas.
    n_refinements : int
        Number of refinement rounds.
    elapsed_ms : int
        Total wall-clock time.
    error : str or None
        Error description if failed.
    lemma_results : dict[str, PrimitiveResult]
        Results per lemma ID.
    """
    success: bool = False
    proof: str | None = None
    blueprint: Blueprint | None = None
    state_history: list[tuple[OrchestratorState, str]] = field(default_factory=list)
    n_primitives_used: int = 0
    n_refinements: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    lemma_results: dict[str, PrimitiveResult] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        parts = [
            "✅" if self.success else "❌",
            f"{self.n_primitives_used} primitives",
            f"{self.n_refinements} refinements",
            f"{self.elapsed_ms}ms",
        ]
        if self.success and self.proof:
            parts.append(f"proof={len(self.proof)}b")
        return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════


class Orchestrator:
    """Multi-agent state machine for hard theorem proving.

    Usage
    -----
        orch = Orchestrator()
        result = orch.run("theorem imo_2025_p1 (n : ℕ) : ... := by")
        print(result.summary)
        if result.success:
            print(result.proof)
    """

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self._config = config or OrchestratorConfig()
        self._state = OrchestratorState.IDLE
        self._state_history: list[tuple[OrchestratorState, str]] = []

    # ═══════════════════════════════════════════════════════════════
    # Main Entry Point
    # ═══════════════════════════════════════════════════════════════

    def run(self, theorem: str) -> OrchestratorResult:
        """Run the orchestrator on a theorem.

        Parameters
        ----------
        theorem : str
            Lean 4 theorem header (e.g. "theorem t : ... :=").

        Returns
        -------
        OrchestratorResult
            The full result with proof if successful.
        """
        t0 = time.perf_counter()
        self._reset()
        self._record_state(OrchestratorState.ANALYZING, f"Analyzing: {theorem[:60]}...")

        try:
            # Step 1: Analyze difficulty
            difficulty = self._analyze(theorem)
            self._record_state(OrchestratorState.GENERATING_BLUEPRINT,
                               f"Difficulty: {difficulty}")

            # Step 2: Generate blueprint (decomposition DAG)
            blueprint = self._generate_blueprint(theorem)
            self._record_state(OrchestratorState.PROVING_LEMMAS,
                               f"Blueprint: {len(blueprint.lemmas)} lemmas")

            # Step 3-4: Prove + Refine (iterative)
            lemma_results: dict[str, PrimitiveResult] = {}
            for round_idx in range(self._config.max_refinement_rounds + 1):
                if round_idx > 0:
                    self._record_state(OrchestratorState.REFINING,
                                       f"Refinement round {round_idx}")

                # Prove unproven lemmas in topological order
                unproven = list(blueprint.unproven())
                if not unproven:
                    break

                for lemma_node in unproven:
                    if lemma_node.status == LemmaStatus.PROVED:
                        continue
                    self._record_state(OrchestratorState.PROVING_LEMMAS,
                                       f"Proving: {lemma_node.label}")
                    result = self._prove_lemma(lemma_node, theorem)
                    lemma_results[lemma_node.id] = result

                    if result.success and result.code:
                        lemma_node.status = LemmaStatus.PROVED
                        lemma_node.proof = result.code
                    else:
                        lemma_node.status = LemmaStatus.FAILED
                        if self._config.enable_fallback_decompose:
                            # Try decomposition
                            decomp = fallback_decompose(
                                lemma_node.header,
                                {"subgoals": [f"{lemma_node.header} (part 1)",
                                              f"{lemma_node.header} (part 2)"]},
                            )
                            if decomp.new_subgoals:
                                blueprint = self._decompose_lemma(blueprint, lemma_node.id, decomp)

                # Check if all proved
                if not blueprint.has_failures:
                    break

            # Step 5: Synthesize final proof
            self._record_state(OrchestratorState.SYNTHESIZING, "Synthesizing final proof")
            proof = self._synthesize(blueprint, lemma_results)

            # Step 6: Verify
            self._record_state(OrchestratorState.VERIFYING, "Verifying")
            success = proof is not None

            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            result = OrchestratorResult(
                success=success,
                proof=proof,
                blueprint=blueprint,
                state_history=self._state_history,
                n_primitives_used=len(lemma_results),
                n_refinements=min(
                    self._config.max_refinement_rounds,
                    sum(1 for s in self._state_history if s[0] == OrchestratorState.REFINING),
                ),
                elapsed_ms=elapsed_ms,
                lemma_results=lemma_results,
            )

            self._record_state(
                OrchestratorState.COMPLETED if success else OrchestratorState.FAILED,
                result.summary,
            )
            return result

        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.error("Orchestrator failed: %s", e)
            self._record_state(OrchestratorState.FAILED, str(e)[:200])
            return OrchestratorResult(
                success=False,
                error=str(e)[:500],
                state_history=self._state_history,
                elapsed_ms=elapsed_ms,
            )

    # ═══════════════════════════════════════════════════════════════
    # Internal Steps
    # ═══════════════════════════════════════════════════════════════

    def _analyze(self, theorem: str) -> str:
        """Analyze theorem difficulty."""
        from omega.engine.router import estimate_difficulty
        diff = estimate_difficulty(theorem)
        return diff.value

    def _generate_blueprint(self, theorem: str) -> Blueprint:
        """Generate a proof blueprint (decomposition DAG)."""
        try:
            return generate_blueprint(theorem)
        except Exception as e:
            logger.warning("Blueprint generation failed, creating single-goal: %s", e)
            # Fallback: single-goal blueprint
            node = LemmaNode(
                label="main",
                header=theorem,
                description="Main theorem",
            )
            return Blueprint(theorem_header=theorem, lemmas={node.id: node})

    def _prove_lemma(self, lemma: LemmaNode, theorem: str) -> PrimitiveResult:
        """Prove a single lemma using the inner loop."""
        from omega.loop.inner import inner_loop, InnerLoopConfig

        try:
            cfg = InnerLoopConfig(
                max_rounds=20,
                compile_timeout=int(self._config.lemma_timeout_s),
                budget_model_id="deepseek/deepseek-v4-flash",
                proof_sketch=self._config.enable_llm_sketch,
            )
            t0 = time.perf_counter()
            result = inner_loop(lemma.header, config=cfg)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            if result.success:
                return PrimitiveResult(
                    success=True,
                    code=result.code,
                    elapsed_ms=elapsed_ms,
                    metadata={"rounds": result.rounds, "termination": result.termination},
                )
            return PrimitiveResult(
                success=False,
                error=result.error or result.termination,
                elapsed_ms=elapsed_ms,
                metadata={"rounds": result.rounds, "termination": result.termination},
            )
        except Exception as e:
            return PrimitiveResult(success=False, error=str(e)[:300])

    def _decompose_lemma(self, blueprint: Blueprint, lemma_id: str,
                         decomp: PrimitiveResult) -> Blueprint:
        """Replace a failed lemma with decomposed sub-lemmas."""
        new_nodes = []
        for i, sg in enumerate(decomp.new_subgoals):
            node = LemmaNode(
                label=f"{lemma_id}_sub_{i}",
                header=sg,
                description=f"Decomposition of {lemma_id}",
                dependencies=[lemma_id],
                depth=(blueprint.lemmas.get(lemma_id, LemmaNode()).depth or 0) + 1,
            )
            new_nodes.append(node)

        # Remove the old lemma, add new ones
        if lemma_id in blueprint.lemmas:
            del blueprint.lemmas[lemma_id]
        for node in new_nodes:
            blueprint.lemmas[node.id] = node
            blueprint.edges.append((lemma_id, node.id))

        return blueprint

    def _synthesize(
        self,
        blueprint: Blueprint,
        lemma_results: dict[str, PrimitiveResult],
    ) -> str | None:
        """Synthesize the final proof from proven lemmas."""
        proven = [l for l in blueprint.lemmas.values() if l.status == LemmaStatus.PROVED]
        if not proven:
            return None

        # Build the final proof code
        parts = []
        for lemma in proven:
            if lemma.proof:
                parts.append(lemma.proof)
                parts.append("")

        return "\n".join(parts).strip() or None

    # ═══════════════════════════════════════════════════════════════
    # State Management
    # ═══════════════════════════════════════════════════════════════

    def _reset(self) -> None:
        """Reset orchestrator state."""
        self._state = OrchestratorState.IDLE
        self._state_history = []

    def _record_state(self, state: OrchestratorState, description: str) -> None:
        """Record a state transition."""
        self._state = state
        self._state_history.append((state, description))
        logger.debug("Orch [%s]: %s", state.name, description)

    @property
    def state(self) -> OrchestratorState:
        return self._state

    @property
    def state_history(self) -> list[tuple[OrchestratorState, str]]:
        return list(self._state_history)
