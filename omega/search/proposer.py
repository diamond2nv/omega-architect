"""Proposer interface — generates next tactics for proof search.

The proposer is the core LLM interaction point. It takes a goal state
and generates candidate tactics or proof completions.

Three proposer modes:
1. ``goedel`` — Parallel sampling: generate N independent tactic attempts
2. ``rethlas`` — Blueprint-based: decompose goal into subgoals
3. ``archon`` — Draft-first: generate full proof, then refine

In Omega, the proposer is pluggable:
- Default: Hermes Agent (current model) via ``delegate_task``
- Fallback: basic tactic templates (no LLM needed)
- External: any callable that takes a goal and returns tactics
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from omega.search.tree import GoalState, SearchNode


@dataclass
class TacticSuggestion:
    """A single tactic or proof attempt suggested by the proposer.

    Attributes
    ----------
    tactic : str
        The suggested tactic (Lean syntax).
    confidence : float
        Confidence score 0.0-1.0.
    description : str
        Human-readable explanation.
    is_complete : bool
        Whether this is a complete proof (no subgoals).
    strategy : str
        Which strategic mode produced this (``"goedel"``, ``"rethlas"``, ``"archon"``).
    lean_code : str or None
        Full Lean code if this is a complete proof.
    """

    tactic: str
    confidence: float = 0.5
    description: str = ""
    is_complete: bool = False
    strategy: str = "goedel"
    lean_code: str | None = None


TacticGenerator = Callable[
    [GoalState, list[SearchNode], dict[str, Any]],
    list[TacticSuggestion],
]
"""Type alias for a tactic generator function.

Signature: ``fn(goal, context, config) -> list[TacticSuggestion]``

- goal: current goal state
- context: path from root to current node (search history)
- config: proposer configuration dict
"""


# ── Built-in tactic templates (no LLM needed) ──────────────────


def _extract_target(theorem_header: str) -> str:
    """Extract the proof target from a theorem header."""
    for line in theorem_header.split("\n"):
        if "theorem" in line or "lemma" in line or "def" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                return parts[-1].strip().rstrip(",")
    return ""


def error_based_suggestions(
    goal: GoalState,
    previous_errors: list[str],
) -> list[TacticSuggestion]:
    """Generate alternative tactics based on patterns in previous errors.

    Examines the error messages and suggests DIFFERENT tactics than the
    ones that already failed, avoiding repeated failures.

    Parameters
    ----------
    goal : GoalState
        Current goal state.
    previous_errors : list[str]
        Error messages from prior tactic attempts.

    Returns
    -------
    list[TacticSuggestion]
        Alternative tactic suggestions.
    """
    if not previous_errors:
        return []

    suggestions: list[TacticSuggestion] = []
    combined = " ".join(previous_errors).lower()

    # Track which tactics have already been tried (by scanning errors)
    tried_tactics: set[str] = set()
    _TRIED_PATTERNS = [
        (r"tactic '(\w+)'", "tactic named"),
        (r"tactic (\w+)", "tactic keyword"),
        (r"'(\w+)' failed", "tactic failed"),
    ]
    for err in previous_errors:
        err_lower = err.lower()
        for pattern, _label in _TRIED_PATTERNS:
            for m in re.finditer(pattern, err_lower):
                tried_tactics.add(m.group(1))

    # ── Pattern 1: "unsolved goals" + tried "simp" ──
    if "unsolved" in combined and "simp" in combined:
        for alt in ("omega", "arith", "nlinarith", "norm_num"):
            if alt not in tried_tactics:
                suggestions.append(TacticSuggestion(
                    tactic=alt,
                    confidence=0.4,
                    description=f"Alternative after simp failed: try {alt}",
                    is_complete=False,
                ))

    # ── Pattern 2: "unsolved goals" + tried "induction" ──
    if "unsolved" in combined and "induction" in combined:
        for alt in ("cases", "arith", "omega"):
            if alt not in tried_tactics:
                suggestions.append(TacticSuggestion(
                    tactic=alt,
                    confidence=0.35,
                    description=f"Alternative after induction failed: try {alt}",
                    is_complete=False,
                ))

    # ── Pattern 3: "unknown tactic" ── the tactic doesn't exist
    if "unknown tactic" in combined:
        for fallback in ("simp", "trivial"):
            if fallback not in tried_tactics:
                suggestions.append(TacticSuggestion(
                    tactic=fallback,
                    confidence=0.3,
                    description="Tried non-existent tactic, fall back to simp/trivial",
                    is_complete=fallback == "trivial",
                ))

    # ── Pattern 4: "unknown identifier" ── missing import or wrong name
    if "unknown identifier" in combined:
        if "apply?" not in tried_tactics:
            suggestions.append(TacticSuggestion(
                tactic="apply?",
                confidence=0.25,
                description="Unknown identifier — try apply? to search",
                is_complete=False,
            ))

    # ── Pattern 5: "unknown module" ── missing import
    if "unknown module" in combined:
        suggestions.append(TacticSuggestion(
            tactic="-- missing import: check open/import statements",
            confidence=0.1,
            description="Unknown module — likely a missing import",
            is_complete=False,
        ))

    # ── Pattern 6: No specific pattern — try all alternatives with low confidence
    if not suggestions:
        all_alts: list[str] = []
        for pool in (("omega", "arith", "nlinarith", "norm_num"),
                      ("cases", "constructor", "left", "right"),
                      ("simp", "trivial", "rfl", "apply?")):
            for alt in pool:
                if alt not in tried_tactics:
                    all_alts.append(alt)
        if not all_alts:
            all_alts = ["simp", "trivial", "omega", "cases"]
        for alt in all_alts[:4]:
            suggestions.append(TacticSuggestion(
                tactic=alt,
                confidence=0.15,
                description=f"Fallback alternative after previous errors: try {alt}",
                is_complete=alt == "trivial",
            ))

    return suggestions


def suggest_trivial_tactics(
    goal: GoalState,
    previous_errors: list[str] | None = None,
) -> list[TacticSuggestion]:
    """Suggest basic tactics for simple goals.

    Parameters
    ----------
    goal : GoalState
        Current goal state.
    previous_errors : list[str] or None
        Error messages from prior tactic attempts. When provided, the
        function will avoid re-suggesting tactics that already failed.
    """
    suggestions: list[TacticSuggestion] = []
    target = goal.target_type or _extract_target(goal.goal_text)

    # Determine which tactics to skip based on previous errors
    skip_tactics: set[str] = set()
    if previous_errors:
        combined = " ".join(previous_errors).lower()
        for err in previous_errors:
            err_lower = err.lower()
            for m in re.finditer(r"tactic '?(\w+)'?", err_lower):
                skip_tactics.add(m.group(1))

    # Trivial
    if target in ("True", "true") and "trivial" not in skip_tactics:
        suggestions.append(TacticSuggestion(
            tactic="trivial",
            confidence=0.9,
            description="Goal is True, use trivial",
            is_complete=True,
        ))

    # Equality reflexivity
    if "=" in target:
        # Check if it's a simple reflexivity
        eq_parts = target.split("=")
        if len(eq_parts) == 2 and eq_parts[0].strip() == eq_parts[1].strip():
            if "rfl" not in skip_tactics:
                suggestions.append(TacticSuggestion(
                    tactic="rfl",
                    confidence=0.95,
                    description="Identical LHS and RHS, use rfl",
                    is_complete=True,
                ))

    # Simple induction on ℕ
    if "ℕ" in target or "Nat" in target:
        if "induction" not in skip_tactics:
            suggestions.append(TacticSuggestion(
                tactic="induction n",
                confidence=0.3,
                description="Try induction on natural number",
                is_complete=False,
            ))

    # Simp as fallback (only if not already tried)
    if "simp" not in skip_tactics:
        suggestions.append(TacticSuggestion(
            tactic="simp",
            confidence=0.2,
            description="Try simplification",
            is_complete=False,
        ))

    return suggestions


# ── LLM proposer (works with Hermes) ───────────────────────────


# Simple pattern-based tactic extractor
_TACTIC_KEYWORDS = [
    "apply", "exact", "refine", "rw", "simp", "trivial", "rfl",
    "induction", "cases", "constructor", "left", "right", "omega",
    "nlinarith", "ring", "norm_num", "positivity", "aesop",
    "calc", "have", "let", "use", "existsi",
]


def _extract_tactics_from_text(text: str) -> list[str]:
    """Extract tactic blocks from LLM output.

    Looks for:
    1. Lean code blocks (````lean4 ... ````)
    2. Lines starting with tactic keywords
    """
    tactics: list[str] = []

    # Pattern 1: Lean code blocks
    code_blocks = re.findall(r"```(?:lean4|lean)?\s*\n(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        block = block.strip()
        for line in block.split("\n"):
            stripped = line.strip()
            for kw in _TACTIC_KEYWORDS:
                if stripped.startswith(kw) and " " in stripped:
                    tactics.append(stripped)
                    break

    # Pattern 2: Lines starting with tactics (outside code blocks)
    for line in text.split("\n"):
        stripped = line.strip()
        for kw in _TACTIC_KEYWORDS:
            if stripped.startswith(kw):
                # Skip if this was already in a code block
                suggestion = stripped
                if suggestion not in tactics:
                    tactics.append(suggestion)
                break

    return tactics


def make_llm_proposer(
    generate_fn: Callable[[str], str] | None = None,
    num_samples: int = 4,
) -> TacticGenerator:
    """Create a proposer that uses an LLM to generate tactics.

    Parameters
    ----------
    generate_fn : Callable[[str], str] or None
        Function that takes a prompt and returns LLM output.
        If ``None``, uses a prompt-only approach (returns template prompts).
    num_samples : int
        Number of independent samples to generate.

    Returns
    -------
    TacticGenerator
    """
    def proposer(
        goal: GoalState,
        context: list[SearchNode],
        config: dict[str, Any],
    ) -> list[TacticSuggestion]:
        suggestions: list[TacticSuggestion] = []

        # Extract previous_errors from config for self-correction
        previous_errors: list[str] | None = config.get("previous_errors", None)

        # Always include trivial tactics (skipping any that already failed)
        suggestions.extend(suggest_trivial_tactics(goal, previous_errors))

        # Error-driven alternatives
        if previous_errors:
            suggestions.extend(error_based_suggestions(goal, previous_errors))

        # Build prompt for this goal
        prompt_parts = [
            "You are proving a Lean 4 theorem.",
            f"Goal: {goal.goal_text}",
        ]
        if goal.hypotheses:
            prompt_parts.append("Hypotheses:")
            for h in goal.hypotheses:
                prompt_parts.append(f"  {h}")
        if context:
            tactics_so_far = [n.tactic_applied for n in context if n.tactic_applied]
            if tactics_so_far:
                prompt_parts.append(f"Tactics applied: {'; '.join(tactics_so_far)}")
        prompt_parts.append("Propose the next tactic or complete proof (in ```lean4 ... ``` block).")

        prompt = "\n".join(prompt_parts)

        if generate_fn is not None:
            # Use LLM to generate
            for _ in range(num_samples):
                try:
                    output = generate_fn(prompt)
                    extracted = _extract_tactics_from_text(output)
                    for tactic in extracted:
                        suggestions.append(TacticSuggestion(
                            tactic=tactic,
                            confidence=0.5,
                            description=f"LLM-suggested: {tactic[:40]}",
                            is_complete=False,
                        ))
                except Exception as e:
                    # LLM failure, fall through to template tactics
                    suggestions.append(TacticSuggestion(
                        tactic=f"-- LLM error: {e}",
                        confidence=0.0,
                        description="LLM proposer failed",
                        is_complete=False,
                    ))
                    break
        else:
            # No LLM available — use template-only
            suggestions.append(TacticSuggestion(
                tactic="-- llm_proposer: no generate_fn provided, using templates only",
                confidence=0.0,
                description="Template-only mode",
                is_complete=False,
            ))

        return suggestions

    return proposer


# ── Default proposer ───────────────────────────────────────────


def default_proposer(
    goal: GoalState,
    context: list[SearchNode],
    config: dict[str, Any],
) -> list[TacticSuggestion]:
    """Default proposer with template tactics only (no LLM dependency).

    Use this when running without LLM access (e.g., in tests or when
    ``delegate_task`` is unavailable).

    Supports self-correction via ``config``:
    - ``previous_errors`` (list[str]): error messages from prior attempts.
      When present, generates alternative tactics to avoid repeated failures.
    """
    previous_errors: list[str] | None = config.get("previous_errors", None)
    suggestions: list[TacticSuggestion] = []

    # Baseline trivial tactics (skips tactics that already failed)
    suggestions.extend(suggest_trivial_tactics(goal, previous_errors))

    # Error-driven alternative tactics
    if previous_errors:
        suggestions.extend(error_based_suggestions(goal, previous_errors))

    return suggestions


class Proposer:
    """Pluggable proposer with configurable strategy.

    Usage
    -----
        proposer = Proposer(strategy="goedel")
        suggestions = proposer.suggest(goal, context)
    """

    def __init__(
        self,
        strategy: str = "goedel",
        generate_fn: Callable[[str], str] | None = None,
        num_samples: int = 4,
    ):
        self.strategy = strategy
        self.num_samples = num_samples
        self._llm_fn = generate_fn
        self._generator: TacticGenerator = self._build_generator()

    def _build_generator(self) -> TacticGenerator:
        if self.strategy == "goedel":
            return make_llm_proposer(self._llm_fn, self.num_samples)
        elif self.strategy == "rethlas":
            # Rethlas-style: more structured, fewer but higher-quality suggestions
            return make_llm_proposer(self._llm_fn, max(1, self.num_samples // 2))
        elif self.strategy == "archon":
            # Archon-style: generate full proof drafts
            return make_llm_proposer(self._llm_fn, self.num_samples)
        return default_proposer

    def suggest(
        self,
        goal: GoalState,
        context: list[SearchNode] | None = None,
        **config: Any,
    ) -> list[TacticSuggestion]:
        """Generate tactic suggestions for the given goal."""
        if context is None:
            context = []
        return self._generator(goal, context, config)

    def __repr__(self) -> str:
        return f"Proposer(strategy='{self.strategy}', n_samples={self.num_samples})"
