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


def suggest_trivial_tactics(goal: GoalState) -> list[TacticSuggestion]:
    """Suggest basic tactics for simple goals."""
    suggestions: list[TacticSuggestion] = []
    target = goal.target_type or _extract_target(goal.goal_text)

    # Trivial
    if target in ("True", "true"):
        suggestions.append(TacticSuggestion(
            tactic="trivial",
            confidence=0.9,
            description="Goal is True, use trivial",
            is_complete=True,
        ))

    # Equality reflexivity
    if "=" in target and "=" in target:
        # Check if it's a simple reflexivity
        eq_parts = target.split("=")
        if len(eq_parts) == 2 and eq_parts[0].strip() == eq_parts[1].strip():
            suggestions.append(TacticSuggestion(
                tactic="rfl",
                confidence=0.95,
                description="Identical LHS and RHS, use rfl",
                is_complete=True,
            ))

    # Simple induction on ℕ
    if "ℕ" in target or "Nat" in target:
        suggestions.append(TacticSuggestion(
            tactic="induction n",
            confidence=0.3,
            description="Try induction on natural number",
            is_complete=False,
        ))

    # Simp as fallback
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

        # Always include trivial tactics
        suggestions.extend(suggest_trivial_tactics(goal))

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
    """
    return suggest_trivial_tactics(goal)


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
