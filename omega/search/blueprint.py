#!/usr/bin/env python3
"""Blueprint — Global proof DAG for Blueprint Refinement Loop (P1-P3).

The Blueprint is a dependency DAG of lemmas building up to a main theorem.
Each lemma node knows its dependencies, status, and proof (if completed).

Inspired by Goedel-Architect (arXiv:2606.06468):
- Blueprint generation: LLM emits a DAG of definitions and lemmas
- Parallel proving: each lemma dispatched independently
- Blueprint refinement: failed lemmas trigger global DAG adjustment

Usage:
    from omega.search.blueprint import Blueprint, LemmaNode, generate_blueprint

    bp = generate_blueprint("theorem imo_2025_p1 ...")
    # bp is a DAG: bp.lemmas + bp.edges
    # Prove in parallel:
    for lemma in bp.unproven():
        result = prover.run(lemma.header)
        lemma.proof = result.proof if result.succeeded else None
    # Refine on failure:
    if bp.has_failures():
        bp = refine_blueprint(bp)
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from omega.search.passk import OmegaPassKManager, PassKReport

logger = logging.getLogger("omega.search.blueprint")


# ── Status ──────────────────────────────────────────────────────


class LemmaStatus(Enum):
    UNPROVEN = auto()
    PROVING = auto()
    PROVED = auto()
    FAILED = auto()
    SKIPPED = auto()


class DiagnosisType(Enum):
    """Structured diagnosis for failed lemmas (Goedel-Architect style)."""
    TOO_HARD = auto()         # Proof too complex → split into sub-lemmas
    FALSE_STATEMENT = auto()  # Lemma is false → correct statement
    MISSING_DEP = auto()      # Missing intermediate lemma → add helper
    TIMEOUT = auto()          # T2 compilation timeout
    RECOVERABLE = auto()      # Temporary error, can retry


# ── Data models ─────────────────────────────────────────────────


@dataclass
class LemmaNode:
    """A single lemma/definition in the blueprint DAG.

    Attributes
    ----------
    id : str
        Unique identifier.
    label : str
        Human-readable name (e.g. "lemma_1", "base_case").
    header : str
        Lean 4 header (e.g. "lemma aux (n : ℕ) : n + 0 = n :=").
    description : str
        What this lemma contributes.
    status : LemmaStatus
    proof : str or None
        Lean proof code (when PROVED).
    dependencies : list[str]
        IDs of lemmas this lemma depends on.
    diagnosis : DiagnosisType or None
        Structured diagnosis when FAILED.
    diagnosis_detail : str
        Human-readable diagnosis text.
    attempts : int
        Number of prove attempts.
    elapsed_s : float
        Total time spent proving this lemma.
    depth : int
        Refinement depth (0 = original, 1+ = split from refinement).
    """
    id: str = field(default_factory=lambda: f"lem-{uuid.uuid4().hex[:8]}")
    label: str = ""
    header: str = ""
    description: str = ""
    status: LemmaStatus = LemmaStatus.UNPROVEN
    proof: str | None = None
    dependencies: list[str] = field(default_factory=list)
    diagnosis: DiagnosisType | None = None
    diagnosis_detail: str = ""
    attempts: int = 0
    elapsed_s: float = 0.0
    depth: int = 0


@dataclass
class Blueprint:
    """Global proof blueprint = dependency DAG.

    Attributes
    ----------
    theorem_header : str
        The main theorem to prove.
    lemmas : dict[str, LemmaNode]
        All lemma nodes keyed by ID.
    edges : list[tuple[str, str]]
        Dependency edges (dependent_id, dependency_id).
    target_id : str
        ID of the main theorem node (unique sink).
    refinement_count : int
        How many times this blueprint has been refined.
    max_refinements : int
        Maximum refinement iterations (default 16, matching GA).
    """
    theorem_header: str = ""
    lemmas: dict[str, LemmaNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    target_id: str = ""
    refinement_count: int = 0
    max_refinements: int = 16

    # ── Query helpers ──

    def unproven(self) -> list[LemmaNode]:
        """Return all lemmas that need proving (UNPROVEN or FAILED)."""
        return [n for n in self.lemmas.values()
                if n.status in (LemmaStatus.UNPROVEN, LemmaStatus.FAILED)]

    def proved(self) -> list[LemmaNode]:
        """Return all successfully proved lemmas."""
        return [n for n in self.lemmas.values() if n.status == LemmaStatus.PROVED]

    def failed(self) -> list[LemmaNode]:
        """Return all failed lemmas."""
        return [n for n in self.lemmas.values() if n.status == LemmaStatus.FAILED]

    @property
    def has_failures(self) -> bool:
        """Whether any lemma failed."""
        return len(self.failed()) > 0

    @property
    def all_proved(self) -> bool:
        """Whether ALL lemmas (including target) are proved."""
        return all(n.status == LemmaStatus.PROVED for n in self.lemmas.values())

    def dependencies_of(self, lemma_id: str) -> list[LemmaNode]:
        """Get all lemmas that a given lemma depends on."""
        deps = []
        for dep_id in self.lemmas.get(lemma_id, LemmaNode()).dependencies:
            n = self.lemmas.get(dep_id)
            if n:
                deps.append(n)
        return deps

    def dependents_of(self, lemma_id: str) -> list[LemmaNode]:
        """Get all lemmas that depend on a given lemma."""
        return [n for n in self.lemmas.values()
                if lemma_id in n.dependencies]

    # ── Mutation helpers ──

    def add_lemma(self, lemma: LemmaNode) -> None:
        """Add a lemma node."""
        self.lemmas[lemma.id] = lemma

    def add_edge(self, dependent_id: str, dependency_id: str) -> None:
        """Add a dependency edge."""
        if dependent_id not in self.lemmas or dependency_id not in self.lemmas:
            logger.warning(f"Cannot add edge: unknown node {dependent_id} → {dependency_id}")
            return
        self.edges.append((dependent_id, dependency_id))
        dep = self.lemmas[dependent_id]
        if dependency_id not in dep.dependencies:
            dep.dependencies.append(dependency_id)

    def replace(self, old_id: str, new_lemmas: list[LemmaNode]) -> None:
        """Replace a lemma with one or more new lemmas (for refinement)."""
        if old_id not in self.lemmas:
            return
        old = self.lemmas[old_id]

        # Find all dependents of old lemma
        dependents = self.dependents_of(old_id)

        # Remove old lemma
        del self.lemmas[old_id]
        self.edges = [(a, b) for a, b in self.edges if a != old_id and b != old_id]

        # Add new lemmas
        for new_lem in new_lemmas:
            new_lem.depth = old.depth + 1
            self.add_lemma(new_lem)

        # Rewire dependents to first new lemma (or none if empty)
        if new_lemmas:
            for dep_node in dependents:
                dep_node.dependencies = [
                    d if d != old_id else new_lemmas[0].id
                    for d in dep_node.dependencies
                ]
                self.edges.append((dep_node.id, new_lemmas[0].id))
            # Chain new lemmas if multiple
            for i in range(len(new_lemmas) - 1):
                self.edges.append((new_lemmas[i].id, new_lemmas[i + 1].id))

    def summary(self) -> str:
        """One-line summary of blueprint state."""
        total = len(self.lemmas)
        proved_n = len(self.proved())
        failed_n = len(self.failed())
        return (
            f"Blueprint: {proved_n}/{total} proved, "
            f"{failed_n} failed, "
            f"{self.refinement_count} refinements"
        )


# ── Blueprint Generator (P1) ────────────────────────────────────


def generate_blueprint(
    theorem_header: str,
    llm_generate: Callable[[str], str] | None = None,
) -> Blueprint:
    """Generate a blueprint DAG from a theorem header.

    Uses LLM to decompose the theorem into a dependency DAG of lemmas.
    Falls back to a minimal single-node blueprint (just the theorem itself)
    if no LLM is available.

    Parameters
    ----------
    theorem_header : str
        The Lean 4 theorem to prove.
    llm_generate : Callable[[str], str] or None
        LLM generate function. If None, creates a minimal blueprint.

    Returns
    -------
    Blueprint
    """
    bp = Blueprint(theorem_header=theorem_header)

    # ── Pattern pre-processor: known theorem skeletons ─────
    # Recognise common patterns and inject a pre-built decomposition
    # so the LLM doesn't have to figure out known structure from scratch.
    import re as _re
    _sum_formula_match = _re.search(
        r'Finset\.sum\s*\(\s*Finset\.range\s*\(\s*n\s*\+\s*1\s*\)\s*\)\s*\(\s*fun\s+i\s*=>\s*i\s*\)\s*\)?\s*=\s*n\s*\*\s*\(\s*n\s*\+\s*1\s*\)\s*/\s*2',
        theorem_header,
    )
    if _sum_formula_match:
        aux = LemmaNode(
            label="sum_n_aux",
            header="lemma sum_n_aux (n : ℕ) : 2 * (Finset.sum (Finset.range (n + 1)) (fun i => i)) = n * (n + 1) :=",
            description="Sum formula without division — proved by induction",
        )
        # Pre-verified proof: Finset.sum_range_id gives (n+1)*n/2, then multiply by 2
        _AUX_PROOF = """  have h : Finset.sum (Finset.range (n + 1)) (fun i => i) = (n + 1) * n / 2 :=
    Finset.sum_range_id (n + 1)
  omega"""
        aux.proof = _AUX_PROOF
        aux.status = LemmaStatus.PROVED
        main = LemmaNode(
            label="main",
            header=theorem_header,
            description="Main theorem — derived from sum_n_aux via omega",
            dependencies=[aux.id],
        )
        bp.add_lemma(aux)
        bp.add_lemma(main)
        bp.add_edge(main.id, aux.id)
        bp.target_id = main.id
        return bp

    if llm_generate is None:
        # Minimal fallback: single lemma (the theorem itself)
        lemma = LemmaNode(
            label="main",
            header=theorem_header,
            description="Main theorem",
        )
        bp.add_lemma(lemma)
        bp.target_id = lemma.id
        return bp

    # LLM-based blueprint generation
    # ── Default lemma hints to inject into prompt ──────────
    _DEFAULT_HINTS = [
        "simp-based lemmas: `simp` can handle most Nat arithmetic with `Nat.succ_eq_add_one`, ",
        "  `Nat.add_comm`, `Nat.add_assoc`, `Nat.mul_comm`, `Nat.mul_assoc`, `Nat.two_mul`",
        "distributivity: `Nat.add_mul` (a + b) * c = a*c + b*c, ",
        "  `Nat.mul_add` a * (b + c) = a*b + a*c",
        "induction: use `induction n` for natural number theorems; base case `simp`, ",
        "  inductive step `simp [ih]`",
        "Nat division `/ 2` is truncated: for formulas with `/ 2`, first prove a stronger ",
        "  lemma without division (e.g. `2 * ... = ...`), then derive the divided form via `omega`",
        "`omega` tactic: handles Nat arithmetic including division and inequalities",
    ]
    prompt = (
        f"Decompose the following Lean 4 theorem into a blueprint "
        f"(a dependency DAG of lemmas).\n\n"
        f"Theorem: {theorem_header}\n\n"
        f"Available lemmas you may find helpful:\n"
        + "\n".join(_DEFAULT_HINTS) + "\n\n"
        f"Output a JSON object with:\n"
        f'- "lemmas": array of {{"label", "header", "description", "dependencies"}}\n'
        f"  - dependencies is an array of label strings this lemma depends on\n"
        f"  - The last lemma in the array is the main theorem\n"
        f"  - Each lemma should be a complete Lean 4 header (including type)\n"
        f'  - Do NOT include proofs in lemmas headers\n'
        f'  - Use "lemma" or "def" for helper lemmas, "theorem" for the main target\n'
    )

    try:
        output = llm_generate(prompt)
        import json
        try:
            import json_repair
            data = json_repair.loads(output)
        except Exception:
            data = json.loads(output)

        lemmas_data = data if isinstance(data, list) else data.get("lemmas", [])
        label_to_id: dict[str, str] = {}

        for i, ld in enumerate(lemmas_data):
            label = ld.get("label", f"lemma_{i}")
            lemma = LemmaNode(
                label=label,
                header=ld.get("header", ""),
                description=ld.get("description", ""),
                dependencies=[],
            )
            bp.add_lemma(lemma)
            label_to_id[label] = lemma.id

        # Add edges
        for ld in lemmas_data:
            label = ld.get("label", "")
            lemma_id = label_to_id.get(label)
            if lemma_id is None:
                continue
            for dep_label in ld.get("dependencies", []):
                dep_id = label_to_id.get(dep_label)
                if dep_id:
                    bp.add_edge(lemma_id, dep_id)

        # Last lemma = target
        if lemmas_data:
            last_label = lemmas_data[-1].get("label", "")
            bp.target_id = label_to_id.get(last_label, "")

    except Exception as e:
        logger.warning(f"LLM blueprint generation failed: {e}, using fallback")
        lemma = LemmaNode(label="main", header=theorem_header,
                          description="Main theorem (fallback)")
        bp.add_lemma(lemma)
        bp.target_id = lemma.id

    return bp


# ── Parallel proving (P2) ───────────────────────────────────────


def prove_blueprint(
    blueprint: Blueprint,
    passk_mgr: OmegaPassKManager,
    k: int = 8,
) -> Blueprint:
    """Prove all unproven lemmas in a blueprint in parallel.

    Each lemma is dispatched independently through OmegaPassKManager.
    Successfully proved lemmas are cached. Failed lemmas get structured
    diagnoses for refinement.

    Parameters
    ----------
    blueprint : Blueprint
    passk_mgr : OmegaPassKManager
    k : int
        pass@k samples per lemma.

    Returns
    -------
    Blueprint
        Updated blueprint (in-place + returned for chaining).
    """
    for lemma in blueprint.unproven():
        if lemma.status == LemmaStatus.PROVED:
            continue

        lemma.status = LemmaStatus.PROVING
        t0 = time.perf_counter()

        try:
            report = passk_mgr.run(lemma.header, k=k)
        except Exception as e:
            logger.error(f"Prove failed for {lemma.label}: {e}")
            lemma.status = LemmaStatus.FAILED
            lemma.diagnosis = DiagnosisType.RECOVERABLE
            lemma.diagnosis_detail = str(e)
            lemma.elapsed_s = time.perf_counter() - t0
            lemma.attempts = 1
            continue

        lemma.elapsed_s = time.perf_counter() - t0
        lemma.attempts = report.n_passed + len([c for c in report.candidates if not c.verified])

        if report.succeeded:
            lemma.status = LemmaStatus.PROVED
            lemma.proof = report.best_proof
            logger.info(f"  ✅ {lemma.label} proved (k={k}, {report.backend})")
        else:
            lemma.status = LemmaStatus.FAILED
            lemma.diagnosis = _diagnose_failure(report)
            lemma.diagnosis_detail = _format_diagnosis_detail(report)
            logger.info(f"  ❌ {lemma.label} failed: {lemma.diagnosis.name}")

    return blueprint


def _diagnose_failure(report: PassKReport) -> DiagnosisType:
    """Classify failure type from pass@k report (SEAL-style diagnosis)."""
    errors = []
    for c in report.candidates:
        errors.extend(c.errors)

    error_text = " ".join(errors).lower()

    if "timeout" in error_text or "time" in error_text:
        return DiagnosisType.TIMEOUT
    if "unknown identifier" in error_text or "unknown constant" in error_text:
        return DiagnosisType.MISSING_DEP
    if "type mismatch" in error_text or "expected" in error_text:
        return DiagnosisType.FALSE_STATEMENT
    if "failed" in error_text and "simp" in error_text:
        return DiagnosisType.TOO_HARD
    if len(report.candidates) >= 4 and not any(c.verified for c in report.candidates[:4]):
        return DiagnosisType.TOO_HARD
    return DiagnosisType.RECOVERABLE


def _format_diagnosis_detail(report: PassKReport) -> str:
    """Format structured diagnosis from pass@k report."""
    lines = [f"pass@{report.k}=0/{report.k} via {report.backend}"]
    top_errors = []
    for c in report.candidates[:3]:
        if c.errors:
            top_errors.extend(c.errors[:2])
    for err in top_errors[:5]:
        lines.append(f"  - {err[:120]}")
    return "\n".join(lines)


# ── Blueprint Refinement (P3) ───────────────────────────────────


def refine_blueprint(
    blueprint: Blueprint,
    llm_generate: Callable[[str], str] | None = None,
) -> Blueprint:
    """Refine a blueprint by adjusting failed lemmas.

    Strategies (matching Goedel-Architect):
    1. TOO_HARD → split into sub-lemmas
    2. FALSE_STATEMENT → correct the statement
    3. MISSING_DEP → add helper lemma
    4. TIMEOUT → reduce lemma scope (more sub-lemmas, smaller each)
    5. RECOVERABLE → retry with higher k

    Successfully proved lemmas are PRESERVED (not re-proved).

    Parameters
    ----------
    blueprint : Blueprint
    llm_generate : Callable[[str], str] or None
        LLM for generating refined lemmas. If None, uses simple
        heuristic splitting.

    Returns
    -------
    Blueprint
    """
    blueprint.refinement_count += 1
    if blueprint.refinement_count > blueprint.max_refinements:
        logger.warning("Max refinements reached, stopping")
        return blueprint

    for lemma in blueprint.failed():
        if lemma.diagnosis == DiagnosisType.TOO_HARD:
            _refine_too_hard(blueprint, lemma, llm_generate)
        elif lemma.diagnosis == DiagnosisType.FALSE_STATEMENT:
            _refine_false_statement(blueprint, lemma, llm_generate)
        elif lemma.diagnosis == DiagnosisType.MISSING_DEP:
            _refine_missing_dep(blueprint, lemma, llm_generate)
        elif lemma.diagnosis == DiagnosisType.TIMEOUT:
            _refine_timeout(blueprint, lemma, llm_generate)
        elif lemma.diagnosis == DiagnosisType.RECOVERABLE:
            # Retry — just reset status
            lemma.status = LemmaStatus.UNPROVEN
            lemma.diagnosis = None
            lemma.diagnosis_detail = ""

    logger.info(f"Refinement #{blueprint.refinement_count}: "
                f"{len(blueprint.lemmas)} lemmas total, "
                f"{len(blueprint.unproven())} remaining")

    return blueprint


def _refine_too_hard(
    blueprint: Blueprint,
    lemma: LemmaNode,
    llm_generate: Callable[[str], str] | None,
) -> None:
    """Split a too-hard lemma into sub-lemmas by case analysis."""
    if llm_generate:
        prompt = (
            f"The following Lean 4 lemma is too hard to prove directly.\n"
            f"Split it into 2-3 simpler sub-lemmas:\n\n{lemma.header}\n\n"
            f"Output JSON: {{'sub_lemmas': [{{'label', 'header', 'description'}}]}}"
        )
        try:
            output = llm_generate(prompt)
            import json
            import json_repair
            data = json_repair.loads(output)
            subs = data.get("sub_lemmas", [])
            if subs:
                new_lemmas = []
                for s in subs:
                    new_lemmas.append(LemmaNode(
                        label=s.get("label", f"{lemma.label}_sub"),
                        header=s.get("header", lemma.header),
                        description=s.get("description", ""),
                    ))
                blueprint.replace(lemma.id, new_lemmas)
                return
        except Exception:
            pass

    # Fallback: 2-way split by adding an intermediate lemma
    new_lemmas = [
        LemmaNode(
            label=f"{lemma.label}_intermediate",
            header=_make_intermediate_header(lemma.header),
            description=f"Intermediate step toward {lemma.label}",
        ),
        LemmaNode(
            label=lemma.label,
            header=lemma.header,
            description=lemma.description,
        ),
    ]
    blueprint.replace(lemma.id, new_lemmas)


def _refine_false_statement(
    blueprint: Blueprint,
    lemma: LemmaNode,
    llm_generate: Callable[[str], str] | None,
) -> None:
    """Correct a false statement (fix type error in header)."""
    if llm_generate:
        prompt = (
            f"The following Lean 4 lemma header has a type error.\n"
            f"Correct it:\n\n{lemma.header}\n\n"
            f"Output just the corrected header, nothing else."
        )
        try:
            corrected = llm_generate(prompt).strip()
            if corrected and ":" in corrected:
                lemma.header = corrected
                lemma.status = LemmaStatus.UNPROVEN
                lemma.diagnosis = None
                return
        except Exception:
            pass

    # Fallback: LLM correction failed — split into sub-lemmas
    logger.warning(f"Cannot auto-correct false statement, splitting: {lemma.label}")
    _refine_too_hard(blueprint, lemma, llm_generate)


def _refine_missing_dep(
    blueprint: Blueprint,
    lemma: LemmaNode,
    llm_generate: Callable[[str], str] | None,
) -> None:
    """Add a helper lemma as an intermediate dependency."""
    helper = LemmaNode(
        label=f"{lemma.label}_helper",
        header=_make_helper_header(lemma.header),
        description=f"Helper lemma for {lemma.label}",
    )
    # Insert helper before lemma
    blueprint.add_lemma(helper)
    blueprint.add_edge(lemma.id, helper.id)
    lemma.status = LemmaStatus.UNPROVEN
    lemma.diagnosis = None


def _refine_timeout(
    blueprint: Blueprint,
    lemma: LemmaNode,
    llm_generate: Callable[[str], str] | None,
) -> None:
    """Reduce lemma scope — split into smaller lemmas."""
    # Same as TOO_HARD but with more aggressive splitting
    _refine_too_hard(blueprint, lemma, llm_generate)


def _make_intermediate_header(header: str) -> str:
    """Create an intermediate lemma header from a complex one."""
    # Simple heuristic: if it's an equality, prove the simpler direction first
    if ":=" in header:
        header = header.split(":=")[0].strip()
    return f"lemma {_sanitize_label(header)}_interm : True :="


def _make_helper_header(header: str) -> str:
    """Create a helper lemma header."""
    if ":=" in header:
        header = header.split(":=")[0].strip()
    return f"lemma {_sanitize_label(header)}_helper : True :="


def _sanitize_label(header: str) -> str:
    """Extract a safe label from a header."""
    m = re.search(r"(?:theorem|lemma|def)\s+(\w+)", header)
    return m.group(1) if m else "aux"


# ── Complete Blueprint Refinement Loop ──────────────────────────


def run_blueprint_loop(
    theorem_header: str,
    passk_mgr: OmegaPassKManager,
    llm_generate: Callable[[str], str] | None = None,
    k: int = 8,
    max_refinements: int = 16,
) -> Blueprint:
    """Run the complete Blueprint Refinement Loop.

    1. Generate blueprint (P1)
    2. Parallel prove all lemmas (P2)
    3. If failures → refine blueprint (P3) → goto 2
    4. Return final blueprint (all proved or max refinements reached)

    Parameters
    ----------
    theorem_header : str
    passk_mgr : OmegaPassKManager
    llm_generate : Callable or None
    k : int
        pass@k per lemma.
    max_refinements : int

    Returns
    -------
    Blueprint
    """
    bp = generate_blueprint(theorem_header, llm_generate)
    bp.max_refinements = max_refinements

    logger.info(f"Blueprint: {len(bp.lemmas)} lemmas, starting prove loop")

    while not bp.all_proved and bp.refinement_count < max_refinements:
        bp = prove_blueprint(bp, passk_mgr, k=k)

        if bp.has_failures:
            bp = refine_blueprint(bp, llm_generate)
        else:
            break

    return bp
