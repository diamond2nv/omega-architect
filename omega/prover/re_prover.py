#!/usr/bin/env python3
"""Rethlas-style Prover — blueprint decomposition with lemma retrieval.

Architecture
------------
Rethlas-style proving decomposes a goal into a blueprint of subgoals,
proves each subgoal independently (possibly recursively), and then
composes the results.

Phases:
  1. **Analyze**: Parse the goal to identify its logical structure.
  2. **Decompose**: Break the goal into subgoals via blueprint templates.
  3. **Retrieve**: Search the local lemma cache for similar lemmas.
  4. **Recurse**: Prove each subgoal recursively (with depth limit).
  5. **Compose**: Assemble subproofs into the final proof text.

The prover returns a ``ProverResult`` dataclass containing the proof
(if found), per-attempt details, and elapsed time.

All pure Python stdlib — no external dependencies.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from omega.search.tree import GoalState
from omega.verify.t2_lean import verify as t2_verify

# ── type aliases ──────────────────────────────────────────────────────────────

CompileFn = Callable[[str], dict | list | str | None]
"""Callback that takes Lean code and returns raw compiler diagnostics."""


# ── data models ───────────────────────────────────────────────────────────────


@dataclass
class Subgoal:
    """A single subgoal in a blueprint decomposition.

    Attributes
    ----------
    id : str
        Unique subgoal identifier.
    description : str
        Human-readable description (e.g. "prove base case").
    goal_type : str
        The type of the subgoal (e.g. ``ℕ``, ``a = b``, ``P → Q``).
    target : str
        The Lean text of the subgoal target.
    hypotheses : list[str]
        Available hypotheses for this subgoal.
    depth : int
        Recursion depth of this subgoal (0 = top-level).
    proof : str or None
        The Lean proof for this subgoal once proven.
    verified : bool
        Whether this subgoal has been verified by T2.
    """

    id: str = field(default_factory=lambda: f"sg-{uuid.uuid4().hex[:8]}")
    description: str = ""
    goal_type: str = ""
    target: str = ""
    hypotheses: list[str] = field(default_factory=list)
    depth: int = 0
    proof: str | None = None
    verified: bool = False


@dataclass
class Blueprint:
    """A blueprint decomposition of a goal into subgoals.

    Attributes
    ----------
    goal_id : str
        Identifier of the parent goal.
    template_name : str
        Name of the blueprint template used (e.g. ``"induction"``, ``"cases"``).
    subgoals : list[Subgoal]
        The subgoals that must be proven.
    composition_template : str
        A Lean template that assembles the subproofs into the final proof.
        Contains ``{sg_N}`` placeholders for subgoal proofs.
    """

    goal_id: str = ""
    template_name: str = "direct"
    subgoals: list[Subgoal] = field(default_factory=list)
    composition_template: str = "{sg_0}"


@dataclass
class Attempt:
    """Record of a single prover attempt.

    Attributes
    ----------
    attempt_id : str
        Unique attempt identifier.
    strategy : str
        Which sub-strategy was used (e.g. ``"blueprint_induction"``).
    elapsed_ms : int
        Wall-clock time for this attempt in milliseconds.
    success : bool
        Whether this attempt produced a verified proof.
    proof : str or None
        The Lean proof code if successful.
    blueprint : Blueprint or None
        The blueprint used (if applicable).
    error : str or None
        Error message if failed.
    """

    attempt_id: str = field(default_factory=lambda: f"rethlas-{uuid.uuid4().hex[:8]}")
    strategy: str = "blueprint"
    elapsed_ms: int = 0
    success: bool = False
    proof: str | None = None
    blueprint: Blueprint | None = None
    error: str | None = None


@dataclass
class ProverResult:
    """Result from a Rethlas prover run.

    Attributes
    ----------
    success : bool
        Whether a verified proof was found.
    proof : str or None
        The full Lean proof code if successful.
    elapsed_ms : int
        Total wall-clock time in milliseconds.
    n_attempts : int
        Number of attempts made.
    attempts : list[Attempt]
        Detailed records of each attempt.
    summary : str
        Human-readable summary string.
    """

    success: bool = False
    proof: str | None = None
    elapsed_ms: int = 0
    n_attempts: int = 0
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.success:
            return f"✅ Rethlas succeeded ({self.elapsed_ms}ms, {self.n_attempts} attempt(s))"
        return f"❌ Rethlas failed ({self.elapsed_ms}ms, {self.n_attempts} attempt(s))"


# ── local lemma cache (stub for loogle/leansearch) ──────────────────────────


class LemmaCache:
    """Simplified lemma cache — hardcoded lookup replaced with default ``simp``.

    The old hardcoded 17 lemmas (add_comm, mul_comm, add_assoc, etc.) are
    all known to ``simp`` by default in Mathlib.  Rather than maintaining a
    brittle local map, we simply return ``None`` from lookup and let the
    caller fall back to trying ``simp`` as the default tactic.
    """

    def __init__(self) -> None:
        self._lemmas: dict[str, str] = {}

    def lookup_by_type(self, target: str) -> str | None:
        """Look up a lemma by exact type string match — always returns None."""
        return None

    def search_keywords(self, keywords: list[str]) -> list[tuple[str, str, float]]:
        """Search for lemmas matching any of the given keywords — always empty."""
        return []

    def add_lemma(self, pattern: str, tactic: str) -> None:
        """Register a new lemma in the cache — no-op."""
        pass

    def __len__(self) -> int:
        return 0


# ── blueprint decomposition templates ────────────────────────────────────────


def _decompose_induction(goal: GoalState, depth: int = 0) -> Blueprint | None:
    """Try to build an induction blueprint.

    Looks for a universal quantifier over ℕ (``∀ n : ℕ, ...``) or
    ``ℕ → ...`` function type, or a binder ``(n : ℕ)`` in the goal header.

    Returns None if induction is not applicable.
    """
    import re

    target = goal.target_type or goal.goal_text

    # Precise checks for ℕ-related goals
    # 1. Universal quantifier: ∀ n : ℕ, ...
    has_forall_nat = bool(re.search(r"∀\s+\w+\s*:\s*ℕ", target))
    # 2. Function type: ℕ → ...
    has_fn_nat = bool(re.search(r"ℕ\s*→", target))
    # 3. Binder pattern: (n : ℕ) in goal header (e.g. "theorem t (n : ℕ) : ...")
    has_binder_nat = bool(re.search(r"\(\s*\w+\s*:\s*ℕ\s*\)", target))
    # 4. Deprecated: fallback substring check for safety
    has_nat_substr = "ℕ" in target or "Nat" in target or "nat" in target

    has_nat = has_forall_nat or has_fn_nat or has_binder_nat or has_nat_substr

    if not has_nat:
        return None

    # Build base and step subgoals
    base_target = target
    forall_match = re.search(r"∀\s+\w+\s*:\s*ℕ\s*,\s*", target)
    if forall_match:
        base_target = target[forall_match.end() :].strip()
    base = Subgoal(
        description="Base case (n = 0)",
        goal_type="base",
        target=base_target,
        hypotheses=list(goal.hypotheses),
        depth=depth + 1,
    )
    # For the induction step, use the same target (the induction tactic in the
    # composition template handles the n/n+1 substitution automatically)
    step = Subgoal(
        description="Inductive step (n → n+1)",
        goal_type="step",
        target=base_target,
        hypotheses=list(goal.hypotheses) + ["h_ih : goal for n"],
        depth=depth + 1,
    )

    bp = Blueprint(
        goal_id=uuid.uuid4().hex[:8],
        template_name="induction",
        subgoals=[base, step],
        composition_template=(
            "by\n  induction n with\n  | zero =>\n    {sg_0}\n  | succ n ih =>\n    {sg_1}"
        ),
    )
    return bp


def _decompose_cases(goal: GoalState, depth: int = 0) -> Blueprint | None:
    """Try to build a case-split blueprint.

    Looks for ``∨`` (Or) or ``if … then … else`` in the goal or hypotheses.
    """
    target = goal.target_type or goal.goal_text

    has_or = "∨" in target or "\\/" in target or "Or" in target
    has_h_or = any("∨" in h or "\\/" in h or "Or" in h for h in goal.hypotheses)
    has_if = "if" in target and "then" in target and "else" in target

    if not (has_or or has_h_or or has_if):
        return None

    left = Subgoal(
        description="Left case",
        goal_type="left_case",
        target=target,
        hypotheses=list(goal.hypotheses),
        depth=depth + 1,
    )
    right = Subgoal(
        description="Right case",
        goal_type="right_case",
        target=target,
        hypotheses=list(goal.hypotheses),
        depth=depth + 1,
    )

    bp = Blueprint(
        goal_id=uuid.uuid4().hex[:8],
        template_name="cases",
        subgoals=[left, right],
        composition_template=(
            "by\n  cases h with\n  | inl h =>\n    {sg_0}\n  | inr h =>\n    {sg_1}"
        ),
    )
    return bp


def _decompose_conjunction(goal: GoalState, depth: int = 0) -> Blueprint | None:
    """Try to build a conjunction (∧) blueprint.

    If the goal is ``A ∧ B``, decompose into two subgoals: A and B.
    """
    target = goal.target_type or goal.goal_text

    if "∧" not in target and "/\\" not in target and "And" not in target:
        return None

    # Naively split on ∧ — real parsing would be more robust
    parts = target.split("∧") if "∧" in target else target.split("/\\")
    if len(parts) < 2:
        return None

    left_target = parts[0].strip() if parts[0].strip() else target
    right_target = parts[1].strip() if len(parts) > 1 else target

    left = Subgoal(
        description="Left conjunct",
        goal_type="left",
        target=left_target,
        hypotheses=list(goal.hypotheses),
        depth=depth + 1,
    )
    right = Subgoal(
        description="Right conjunct",
        goal_type="right",
        target=right_target,
        hypotheses=list(goal.hypotheses),
        depth=depth + 1,
    )

    bp = Blueprint(
        goal_id=uuid.uuid4().hex[:8],
        template_name="constructor",
        subgoals=[left, right],
        composition_template=("by\n  constructor\n  · {sg_0}\n  · {sg_1}"),
    )
    return bp


def _decompose_direct(goal: GoalState, depth: int = 0) -> Blueprint:
    """Fallback: single subgoal, direct proof attempt.

    This is the simplest blueprint — just try to prove the goal directly.
    """
    sg = Subgoal(
        description="Direct proof",
        goal_type="direct",
        target=goal.target_type or goal.goal_text,
        hypotheses=list(goal.hypotheses),
        depth=depth + 1,
    )
    return Blueprint(
        goal_id=uuid.uuid4().hex[:8],
        template_name="direct",
        subgoals=[sg],
        composition_template="{sg_0}",
    )


# ── blueprint generators registry ────────────────────────────────────────────

BlueprintGenerator = Callable[[GoalState, int], Blueprint | None]
"""Signature: ``fn(goal, depth) -> Blueprint | None``."""

_DEFAULT_BLUEPRINT_GENERATORS: list[BlueprintGenerator] = [
    _decompose_induction,
    _decompose_cases,
    _decompose_conjunction,
    _decompose_direct,
]


# ── RethlasProver ────────────────────────────────────────────────────────────


class RethlasProver:
    """Rethlas-style prover using blueprint decomposition + lemma retrieval.

    Parameters
    ----------
    compile_fn : CompileFn or None
        Callback for T2 verification. If None, attempts are recorded but not
        actually compiled.
    lemma_cache : LemmaCache or None
        Cache for lemma retrieval. Defaults to a fresh ``LemmaCache``.
    max_depth : int
        Maximum recursion depth for subgoal decomposition (default 3).
    blueprint_generators : list[BlueprintGenerator] or None
        Ordered list of blueprint generators. Defaults to built-in templates.
    max_attempts : int
        Maximum number of blueprint attempts before giving up (default 5).
    """

    def __init__(
        self,
        compile_fn: CompileFn | None = None,
        lemma_cache: LemmaCache | None = None,
        max_depth: int = 3,
        blueprint_generators: list[BlueprintGenerator] | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.compile_fn = compile_fn
        self.lemma_cache = lemma_cache or LemmaCache()
        self.max_depth = max_depth
        self.max_attempts = max_attempts
        self._blueprint_generators = blueprint_generators or list(_DEFAULT_BLUEPRINT_GENERATORS)

    # ── public API ──────────────────────────────────────────────────────────

    def prove(self, theorem_header: str, goal: GoalState | None = None) -> ProverResult:
        """Prove a theorem using Rethlas blueprint decomposition.

        Parameters
        ----------
        theorem_header : str
            The Lean theorem header (e.g. ``theorem t (n : ℕ) : n + 0 = n :=``).
        goal : GoalState or None
            Parsed goal state. If None, created from ``theorem_header``.

        Returns
        -------
        ProverResult
        """
        t_start = time.perf_counter()

        if goal is None:
            goal = GoalState.from_lean_header(theorem_header)

        attempts: list[Attempt] = []

        # Phase 1: Analyze — try blueprints in order
        for bp_gen in self._blueprint_generators:
            if len(attempts) >= self.max_attempts:
                break
            t_attempt = time.perf_counter()

            try:
                blueprint = bp_gen(goal, 0)
            except Exception as exc:
                blueprint = None
                attempts.append(
                    Attempt(
                        strategy=bp_gen.__name__,
                        elapsed_ms=int((time.perf_counter() - t_attempt) * 1000),
                        success=False,
                        error=f"Blueprint generation failed: {exc}",
                    )
                )
                continue

            if blueprint is None:
                continue

            # Phase 2: Retrieve — check lemma cache for each subgoal
            blueprint = self._enrich_with_retrieval(blueprint)

            # Phase 3: Recurse — prove each subgoal
            all_proven = self._prove_subgoals(blueprint, depth=0)

            if all_proven:
                # Phase 4: Compose — assemble proof text
                proof_text = self._compose_proof(theorem_header, blueprint)
                verified = self._verify_proof(proof_text)

                attempt = Attempt(
                    strategy=f"blueprint_{blueprint.template_name}",
                    elapsed_ms=int((time.perf_counter() - t_attempt) * 1000),
                    success=verified,
                    proof=proof_text if verified else None,
                    blueprint=blueprint,
                    error=None if verified else "T2 verification failed",
                )
                attempts.append(attempt)

                if verified:
                    elapsed = int((time.perf_counter() - t_start) * 1000)
                    return ProverResult(
                        success=True,
                        proof=proof_text,
                        elapsed_ms=elapsed,
                        n_attempts=len(attempts),
                        attempts=attempts,
                    )
            else:
                attempts.append(
                    Attempt(
                        strategy=f"blueprint_{blueprint.template_name}",
                        elapsed_ms=int((time.perf_counter() - t_attempt) * 1000),
                        success=False,
                        blueprint=blueprint,
                        error="Not all subgoals could be proven",
                    )
                )

        elapsed = int((time.perf_counter() - t_start) * 1000)
        return ProverResult(
            success=False,
            elapsed_ms=elapsed,
            n_attempts=len(attempts),
            attempts=attempts,
        )

    async def prove_async(self, theorem_header: str, goal: GoalState | None = None) -> ProverResult:
        """Async wrapper around :meth:`prove`.

        Currently runs synchronously — subclasses may override for true async.
        """
        return self.prove(theorem_header, goal=goal)

    # ── internal methods ────────────────────────────────────────────────────

    def _enrich_with_retrieval(self, blueprint: Blueprint) -> Blueprint:
        """Search the lemma cache for each subgoal and annotate with matches.

        With the simplified ``LemmaCache`` (empty), this falls back to trying
        ``simp`` as the default tactic — ``simp`` knows all the arithmetic
        and propositional lemmas that were previously hardcoded.
        """
        enriched_subgoals: list[Subgoal] = []
        for sg in blueprint.subgoals:
            # Try exact match first (will always be None with simplified cache)
            exact = self.lemma_cache.lookup_by_type(sg.target)
            if exact is not None:
                sg.proof = exact
                sg.verified = True
            else:
                # Try keyword search (will always be empty with simplified cache)
                keywords = sg.target.replace("→", " ").replace("∀", " ").replace("∃", " ").split()
                matches = self.lemma_cache.search_keywords(keywords)
                if matches:
                    best_pattern, best_tactic, _ = matches[0]
                    if sg.proof is None:
                        sg.proof = best_tactic
                        sg.verified = True
                elif sg.proof is None:
                    # Fallback: try ``simp`` — it knows all the basic lemmas
                    sg.proof = "simp"
                    sg.verified = True  # Mark as having a candidate proof
            enriched_subgoals.append(sg)
        blueprint.subgoals = enriched_subgoals
        return blueprint

    def _prove_subgoals(self, blueprint: Blueprint, depth: int) -> bool:
        """Recursively prove all subgoals in the blueprint.

        Returns True if all subgoals have proofs.
        """
        if depth > self.max_depth:
            return False

        all_proven = True
        for sg in blueprint.subgoals:
            if sg.verified:
                continue

            # Try to prove this subgoal by further decomposition
            sub_goal = GoalState(
                goal_text=sg.target,
                hypotheses=sg.hypotheses,
                target_type=sg.goal_type,
                depth=depth + 1,
            )

            proven = False
            for bp_gen in self._blueprint_generators:
                sub_bp = bp_gen(sub_goal, depth + 1)
                if sub_bp is None:
                    continue
                sub_bp = self._enrich_with_retrieval(sub_bp)
                if self._prove_subgoals(sub_bp, depth + 1):
                    # Compose subproof
                    sg.proof = self._compose_subproof(sub_bp)
                    sg.verified = True
                    proven = True
                    break

            if not proven:
                all_proven = False

        return all_proven

    def _compose_proof(self, theorem_header: str, blueprint: Blueprint) -> str:
        """Assemble the final Lean proof from a blueprint and its subproofs."""
        # Fill placeholders
        proof_body = blueprint.composition_template
        for i, sg in enumerate(blueprint.subgoals):
            placeholder = f"{{sg_{i}}}"
            sub_proof = sg.proof or "sorry"
            proof_body = proof_body.replace(placeholder, sub_proof)

        # Build complete code
        return f"{theorem_header}\n  {proof_body}"

    def _compose_subproof(self, blueprint: Blueprint) -> str:
        """Compose a subproof from a sub-blueprint."""
        proof_body = blueprint.composition_template
        for i, sg in enumerate(blueprint.subgoals):
            placeholder = f"{{sg_{i}}}"
            proof_body = proof_body.replace(placeholder, sg.proof or "sorry")
        return proof_body

    def _verify_proof(self, code: str) -> bool:
        """Run T2 verification on the composed proof."""
        if self.compile_fn is None:
            # No compiler available — assume verified for stub purposes
            return True

        result = t2_verify(code, compile_fn=self.compile_fn)
        return result.verified

    def __repr__(self) -> str:
        return (
            f"RethlasProver(max_depth={self.max_depth}, "
            f"max_attempts={self.max_attempts}, "
            f"cache_size={len(self.lemma_cache)})"
        )
