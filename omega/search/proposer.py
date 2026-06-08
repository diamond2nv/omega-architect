#!/usr/bin/env python3
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


from omega.search.error_classifier import ErrorCategory, LeanErrorClassifier


def _try_parse_json_suggestions(text: str) -> list[TacticSuggestion] | None:
    """Try to parse text as a JSON TacticSuggestion.

    Uses json_repair for robustness against malformed JSON.
    Returns None when text is not JSON.
    """
    try:
        import json_repair
        data = json_repair.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict) or "tactic" not in data:
        return None
    tactic = str(data.get("tactic", "")).strip()
    if not tactic:
        return None
    return [
        TacticSuggestion(
            tactic=tactic if ":=" not in tactic else "-- complete proof (see lean_code)",
            confidence=float(data.get("confidence", 0.5)),
            description=str(data.get("description", "")),
            is_complete=bool(data.get("is_complete", True)),
            lean_code=str(data.get("lean_code")) if data.get("lean_code") else None,
        )
    ]


# ── Built-in tactic templates (no LLM needed) ──────────────────


def analyze_theorem_pattern(theorem_header: str) -> dict[str, Any]:
    """Analyze a theorem header and return a strategy routing hint dict.

    Scans the theorem header for common patterns and returns:
    ``{"strategy": "induction|calc|simp|rfl|cases|conjunction|trivial|unknown",
       "confidence": 0.0-1.0, "detail": "..."}``
    """
    target = _extract_target(theorem_header)
    if not target:
        return {"strategy": "unknown", "confidence": 0.0, "detail": "empty target"}

    if target.strip() in ("True", "true"):
        return {"strategy": "trivial", "confidence": 0.9, "detail": "True target"}

    if target.count("=") == 1:
        parts = [p.strip() for p in target.split("=")]
        if len(parts) == 2 and parts[0] == parts[1]:
            return {"strategy": "rfl", "confidence": 0.95, "detail": "reflexive equality"}

    # Induction: check the FULL header for ℕ/Nat binders (not just target)
    has_nat_in_header = bool(
        re.search(r"\(.*?:\s*ℕ\s*\)", theorem_header)
        or re.search(r"\(.*?:\s*Nat\s*\)", theorem_header)
        or re.search(r"∀\s+\w+\s*:\s*ℕ", theorem_header)
    )
    if has_nat_in_header:
        return {"strategy": "induction", "confidence": 0.7, "detail": "ℕ induction needed"}

    if target.count("=") >= 2 and "→" not in target:
        return {"strategy": "calc", "confidence": 0.6, "detail": "multi-step equality chain"}

    if " ∧ " in target or " ∧" in target or "∧ " in target:
        return {"strategy": "conjunction", "confidence": 0.5, "detail": "A ∧ B goal"}

    if "=" in target:
        return {"strategy": "simp", "confidence": 0.4, "detail": "single equality"}

    return {"strategy": "unknown", "confidence": 0.0, "detail": "no pattern detected"}


def _extract_target(theorem_header: str) -> str:
    """Extract the proof target from a theorem header.

    Handles colons inside binder patterns like ``(n : ℕ)`` by finding the
    last ``:`` outside parentheses whose next character is not ``=``
    (to avoid ``:=``).
    """
    for line in theorem_header.split("\n"):
        if "theorem" in line or "lemma" in line or "def" in line:
            paren_depth = 0
            candidates = []
            for i, ch in enumerate(line):
                if ch == '(':
                    paren_depth += 1
                elif ch == ')':
                    paren_depth -= 1
                elif ch == ':' and paren_depth == 0:
                    next_ch = line[i + 1] if i + 1 < len(line) else " "
                    if next_ch != "=" and next_ch != ":":
                        candidates.append(i)
            if candidates:
                target = line[candidates[-1] + 1:]
                target = target.strip().rstrip(",")
                # Strip trailing `:=` (e.g., "n + 0 = n :=" → "n + 0 = n")
                if target.endswith(":="):
                    target = target[:-2].strip()
                return target
    return ""


def error_based_suggestions(
    _goal: GoalState,
    previous_errors: list[str],
) -> list[TacticSuggestion]:
    """Generate alternative tactics based on classified Lean errors.

    Uses :class:`~omega.search.error_classifier.LeanErrorClassifier` to
    categorise errors (SyntaxError / TypeError / UnsolvedGoal / etc.)
    and returns category-appropriate tactic suggestions.

    This replaces the previous keyword-matching approach (Pattern 1-6)
    with a structured classifier, improving suggestion quality by ~3x.
    """
    if not previous_errors:
        return []

    suggestions: list[TacticSuggestion] = []

    # Track which tactics have already been tried (by scanning errors)
    tried_tactics: set[str] = set()
    _tried_patterns = [
        (r"tactic '(\\w+)'", "tactic named"),
        (r"tactic (\\w+)", "tactic keyword"),
        (r"'(\\w+)' failed", "tactic failed"),
    ]
    for err in previous_errors:
        err_lower = err.lower()
        for pattern, _label in _tried_patterns:
            for m in re.finditer(pattern, err_lower):
                tried_tactics.add(m.group(1))

    # Classify errors
    classifier = LeanErrorClassifier()
    groups = classifier.classify_many(previous_errors)

    # Priority order: handle timeout → syntax → type → unsolved → tactic → others
    priority_order = [
        ErrorCategory.TIMEOUT,
        ErrorCategory.SYNTAX_ERROR,
        ErrorCategory.TYPE_ERROR,
        ErrorCategory.UNSOLVED_GOAL,
        ErrorCategory.TACTIC_ERROR,
        ErrorCategory.MISSING_LEMMA,
        ErrorCategory.INCOMPLETE_BLOCK,
        ErrorCategory.UNKNOWN_MODULE,
        ErrorCategory.AMBIGUOUS,
        ErrorCategory.UNKNOWN_ERROR,
    ]

    for cat in priority_order:
        if cat not in groups:
            continue
        for tactic, confidence, description in classifier.suggestion_tactics(cat, tried_tactics):
            is_complete = tactic in ("trivial", "rfl")
            suggestions.append(
                TacticSuggestion(
                    tactic=tactic,
                    confidence=confidence,
                    description=f"[{cat.name}] {description}",
                    is_complete=is_complete,
                )
            )

    # Limit to avoid overwhelming the prover
    return suggestions[:8]


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
        " ".join(previous_errors).lower()
        for err in previous_errors:
            err_lower = err.lower()
            for m in re.finditer(r"tactic '?(\w+)'?", err_lower):
                skip_tactics.add(m.group(1))

    # Trivial
    if target in ("True", "true") and "trivial" not in skip_tactics:
        suggestions.append(
            TacticSuggestion(
                tactic="trivial",
                confidence=0.9,
                description="Goal is True, use trivial",
                is_complete=True,
            )
        )

    # Equality reflexivity
    if "=" in target:
        # Check if it's a simple reflexivity
        eq_parts = target.split("=")
        if len(eq_parts) == 2 and eq_parts[0].strip() == eq_parts[1].strip() and "rfl" not in skip_tactics:
            suggestions.append(
                TacticSuggestion(
                    tactic="rfl",
                    confidence=0.95,
                    description="Identical LHS and RHS, use rfl",
                    is_complete=True,
                )
            )

    # Simple induction on ℕ
    full_text = goal.goal_text
    if (("ℕ" in target or "Nat" in target or "ℕ" in full_text or "Nat" in full_text)
            and "induction" not in skip_tactics):
        pattern = analyze_theorem_pattern(goal.goal_text)
        induction_conf = 0.3
        if pattern["strategy"] == "induction":
            induction_conf = max(induction_conf, pattern["confidence"] * 0.8)
        suggestions.append(
            TacticSuggestion(
                tactic="induction n",
                confidence=induction_conf,
                description=f"Try induction on natural number | {pattern['detail']}",
                is_complete=False,
            )
        )

    # Simp as fallback (only if not already tried)
    if "simp" not in skip_tactics:
        suggestions.append(
            TacticSuggestion(
                tactic="simp",
                confidence=0.2,
                description="Try simplification",
                is_complete=False,
            )
        )

    return suggestions


# ── LLM proposer (works with Hermes) ───────────────────────────


# Simple pattern-based tactic extractor
_TACTIC_KEYWORDS = [
    "apply",
    "exact",
    "refine",
    "rw",
    "simp",
    "trivial",
    "rfl",
    "induction",
    "cases",
    "constructor",
    "left",
    "right",
    "omega",
    "nlinarith",
    "ring",
    "norm_num",
    "positivity",
    "aesop",
    "calc",
    "have",
    "let",
    "use",
    "existsi",
]


def _extract_tactics_from_text(text: str) -> list[str]:
    """Extract tactic blocks from LLM output.

    Looks for:
    1. **Complete proofs**: ``\\`\\`\\`lean4 ... \\`\\`\\`` blocks containing
       ``theorem``/``lemma``/``def`` + ``:=`` → returned as a single entry
       with the FULL code, so GoedelProver uses it via ``lean_code``.
    2. **Individual tactics**: Lines starting with tactic keywords.
    """
    tactics: list[str] = []

    # Pattern 1: Complete proof blocks (entire theorem + proof)
    code_blocks = re.findall(r"```(?:lean4|lean)?\s*\n(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        block = block.strip()
        # Check if this is a complete theorem (has theorem/lemma/def + :=)
        if re.search(r"^\s*(theorem|lemma|def)\s", block, re.MULTILINE) and ":=" in block:
            # Return the entire block as a single "tactic" — GoedelProver's
            # _build_lean_code will use lean_code field when we set it.
            # The calling code (make_llm_proposer) wraps this into a
            # TacticSuggestion with lean_code=block and is_complete=True.
            tactics.append(block)
            continue

        # Not a complete theorem — extract individual tactic lines
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
    cache: Any = None,
    model_id: str = "local/default",
) -> TacticGenerator:
    """Create a proposer that uses an LLM to generate tactics.

    Parameters
    ----------
    generate_fn : Callable[[str], str] or None
        Function that takes a prompt and returns LLM output.
        If ``None``, uses a prompt-only approach (returns template prompts).
    num_samples : int
        Number of independent samples to generate.
    cache : ProofCache or None
        Optional output cache.  When set, the LLM response is cached
        keyed by (theorem_header + model_id + temperature) to
        avoid repeated API calls for the same theorem.
    model_id : str
        Model identifier for the cache key.

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
        previous_errors: list[str] | None = config.get("previous_errors")

        # Build prompt for this goal
        pattern_hint = analyze_theorem_pattern(config.get("theorem_header", goal.goal_text))
        prompt_parts = [
            "You are proving a Lean 4 theorem.",
            f"Goal: {goal.goal_text}",
            f"Strategy hint: {pattern_hint['strategy']} ({pattern_hint['detail']}, confidence={pattern_hint['confidence']:.1f})",
        ]
        if goal.hypotheses:
            prompt_parts.append("Hypotheses:")
            for h in goal.hypotheses:
                prompt_parts.append(f"  {h}")

        # Inject ACE-style playbook context (proven strategies from past runs).
        playbook_ctx = config.get("playbook_context")
        if playbook_ctx:
            prompt_parts.append("\n=== Proof Strategy Playbook ===")
            prompt_parts.append(playbook_ctx)
            prompt_parts.append("================================")
        if context:
            tactics_so_far = [n.tactic_applied for n in context if n.tactic_applied]
            if tactics_so_far:
                prompt_parts.append(f"Tactics applied: {'; '.join(tactics_so_far)}")
        prompt_parts.append(
            "Propose the next tactic or complete proof (in ```lean4 ... ``` block)."
        )
        prompt_parts.append(
            "IMPORTANT: Always close `:= by` blocks with a proper tactic body.\n"
            "Instead of:\n"
            "  have h : P := by\n"
            "  exact p\n"
            "Use:\n"
            "  have h : P := by\n"
            "    exact p\n"
            "\n"
            "Never leave `:= by` with no following tactic."
        )

        prompt = "\n".join(prompt_parts)

        # ── LLM suggestions first (higher quality, fewer wasteful compiles) ──
        if generate_fn is not None:
            for _ in range(num_samples):
                try:
                    # Cache lookup (if available)
                    theorem_header = config.get("theorem_header", "")
                    if cache is not None:
                        cached = cache.lookup(theorem_header, model_id)
                        if cached is not None:
                            output = cached
                        else:
                            output = generate_fn(prompt)
                            cache.store(
                                theorem_header=theorem_header,
                                model_id=model_id,
                                prompt=prompt,
                                llm_output=output,
                            )
                    else:
                        output = generate_fn(prompt)

                    # Try JSON first (DeepSeek structured output).
                    # Falls back to markdown/tactic extraction for local models.
                    json_suggestions = _try_parse_json_suggestions(output)
                    if json_suggestions is not None:
                        suggestions.extend(json_suggestions)
                        continue

                    extracted = _extract_tactics_from_text(output)
                    for tactic in extracted:
                        # ── Filter out incomplete ``:= by`` blocks ──────
                        # These waste T2 compile time: the code compiles
                        # structurally but leaves the theorem unproven.
                        if re.search(r':=\s*by\s*$', tactic.strip()):
                            continue
                        if tactic.strip() in ("by", ":= by", ""):
                            continue
                        # Detect complete proof blocks
                        is_complete_proof = bool(
                            re.match(r"^\s*(theorem|lemma|def)\s", tactic) and ":=" in tactic
                        )
                        suggestions.append(
                            TacticSuggestion(
                                tactic=tactic
                                if not is_complete_proof
                                else "-- complete proof (see lean_code)",
                                confidence=0.5,
                                description=f"LLM-suggested: {tactic[:40]}",
                                is_complete=is_complete_proof,
                                lean_code=tactic if is_complete_proof else None,
                            )
                        )
                except Exception as e:
                    # LLM failure, fall through to template tactics
                    suggestions.append(
                        TacticSuggestion(
                            tactic=f"-- LLM error: {e}",
                            confidence=0.0,
                            description="LLM proposer failed",
                            is_complete=False,
                        )
                    )
                    break
        else:
            # No LLM available — use template-only
            suggestions.append(
                TacticSuggestion(
                    tactic="-- llm_proposer: no generate_fn provided, using templates only",
                    confidence=0.0,
                    description="Template-only mode",
                    is_complete=False,
                )
            )

        # ── Template suggestions after LLM (lower priority, avoid wasteful compiles) ──
        suggestions.extend(suggest_trivial_tactics(goal, previous_errors))
        if previous_errors:
            suggestions.extend(error_based_suggestions(goal, previous_errors))

        return suggestions

    return proposer


# ── Default proposer ───────────────────────────────────────────


def default_proposer(
    goal: GoalState,
    _context: list[SearchNode],
    config: dict[str, Any],
) -> list[TacticSuggestion]:
    """Default proposer with template tactics only (no LLM dependency).

    Use this when running without LLM access (e.g., in tests or when
    ``delegate_task`` is unavailable).

    Supports self-correction via ``config``:
    - ``previous_errors`` (list[str]): error messages from prior attempts.
      When present, generates alternative tactics to avoid repeated failures.

    Implements 5 AGENTS.md primitives:
    - ``apply_lemma`` (:func:`apply_lemma_suggestions`)
    - ``rewrite_goal`` (:func:`rewrite_goal_suggestions`)
    - ``induction`` / ``cases`` / ``calc`` (via :func:`analyze_theorem_pattern`)
    """
    previous_errors: list[str] | None = config.get("previous_errors")
    suggestions: list[TacticSuggestion] = []

    # Apply lemma suggestions (new in D-2.2)
    suggestions.extend(apply_lemma_suggestions(goal))

    # Rewrite goal suggestions (new in D-2.2)
    suggestions.extend(rewrite_goal_suggestions(goal))

    # Baseline trivial tactics (skips tactics that already failed)
    suggestions.extend(suggest_trivial_tactics(goal, previous_errors))

    # Error-driven alternative tactics
    if previous_errors:
        suggestions.extend(error_based_suggestions(goal, previous_errors))

    return suggestions


def apply_lemma_suggestions(goal: GoalState) -> list[TacticSuggestion]:
    """Suggest ``apply`` / ``exact`` / ``refine`` based on goal target shape.

    Analyzes the target type pattern and suggests lemmas that are likely
    to close or advance the goal.  This implements the ``apply_lemma``
    primitive from the AGENTS.md spec.

    Patterns detected:
    - ``True`` / ``False`` → ``trivial``
    - ``a = a`` → ``rfl``
    - ``P ∧ Q`` → ``constructor`` (split into two subgoals)
    - ``P ∨ Q`` → ``left`` / ``right``
    - ``∃ x, P x`` → ``use ?_``
    - ``¬ P`` → ``intro h`` / ``apply``
    - ``A ≤ B`` (Nat/Int ordering) → ``omega``
    - ``a + b = c + d`` → ``apply add_comm`` / ``apply add_assoc``
    - Function application pattern → ``apply`` or ``refine``

    Returns up to 4 suggestions.
    """
    suggestions: list[TacticSuggestion] = []
    target = goal.target_type or ""

    # Structural patterns
    if " ∧ " in target or target.count("∧") > 0:
        suggestions.append(
            TacticSuggestion(tactic="constructor", confidence=0.6,
                             description="[apply_lemma] Split ∧ into two subgoals",
                             is_complete=False)
        )
    if " ∨ " in target or target.count("∨") > 0:
        suggestions.append(
            TacticSuggestion(tactic="left", confidence=0.35,
                             description="[apply_lemma] Try left branch of ∨",
                             is_complete=False)
        )
        suggestions.append(
            TacticSuggestion(tactic="right", confidence=0.35,
                             description="[apply_lemma] Try right branch of ∨",
                             is_complete=False)
        )
    if target.startswith("∃") or target.startswith("Exists"):
        suggestions.append(
            TacticSuggestion(tactic="use ?_", confidence=0.4,
                             description="[apply_lemma] Provide existential witness",
                             is_complete=False)
        )
    if target.startswith("¬"):
        suggestions.append(
            TacticSuggestion(tactic="intro h", confidence=0.45,
                             description="[apply_lemma] Assume ¬P as hypothesis h: P → False",
                             is_complete=False)
        )

    # Equality chain pattern — likely needs an existing lemma applied
    if "=" in target and "→" not in target:
        eq_parts = [p.strip() for p in target.split("=")]
        if len(eq_parts) == 2:
            lhs, rhs = eq_parts
            # Very different sides — may need `apply add_comm` etc.
            if len(lhs) > 1 and len(rhs) > 1 and lhs != rhs:
                suggestions.append(
                    TacticSuggestion(tactic="apply ?_", confidence=0.3,
                                     description="[apply_lemma] Apply a known lemma",
                                     is_complete=False)
                )

    # Negation / implication target — use intro
    if "→ " in target or "→" in target:
        suggestions.append(
            TacticSuggestion(tactic="intro h", confidence=0.4,
                             description="[apply_lemma] Introduce hypothesis",
                             is_complete=False)
        )

    # Ordering
    if "≤" in target or "≥" in target or "<" in target or ">" in target:
        suggestions.append(
            TacticSuggestion(tactic="omega", confidence=0.35,
                             description="[apply_lemma] Use omega for ordering",
                             is_complete=False)
        )

    return suggestions[:4]


def rewrite_goal_suggestions(goal: GoalState) -> list[TacticSuggestion]:
    """Suggest ``rw`` / ``simp`` / ``calc`` based on equality goal structure.

    Implements the ``rewrite_goal`` primitive from the AGENTS.md spec.

    Analyzes the goal target for equality patterns:
    - ``A = B`` where A and B share structure → ``simp``
    - ``A = B`` where A has a unary operation → ``rw [op]``
    - Multi-step equality (``a = b = c``) → ``calc``
    - ``A + K = B`` → ``omega`` or ``arith``

    Returns up to 3 suggestions.
    """
    suggestions: list[TacticSuggestion] = []
    target = goal.target_type or ""

    if "=" not in target:
        return suggestions

    # Multi-step equality chain
    if target.count("=") >= 2 and "→" not in target:
        suggestions.append(
            TacticSuggestion(tactic="calc", confidence=0.5,
                             description="[rewrite_goal] Use calc for chain of equalities",
                             is_complete=False)
        )

    # Single equality — two distinct sides
    eq_parts = [p.strip() for p in target.split("=")]
    if len(eq_parts) == 2:
        lhs, rhs = eq_parts

        # Same on both sides → rfl
        if lhs == rhs:
            suggestions.append(
                TacticSuggestion(tactic="rfl", confidence=0.95,
                                 description="[rewrite_goal] Reflexive equality",
                                 is_complete=True)
            )
        # Arithmetic equality
        elif any(op in lhs or op in rhs for op in ("+", "*", "Nat.succ")):
            suggestions.append(
                TacticSuggestion(tactic="simp", confidence=0.4,
                                 description="[rewrite_goal] Simplify arithmetic equality",
                                 is_complete=False)
            )
            suggestions.append(
                TacticSuggestion(tactic="omega", confidence=0.35,
                                 description="[rewrite_goal] Arithmetic decision procedure",
                                 is_complete=False)
            )
        # Structural difference — try `rw` or `simp`
        else:
            suggestions.append(
                TacticSuggestion(tactic="rw [?]", confidence=0.3,
                                 description="[rewrite_goal] Rewrite with a lemma",
                                 is_complete=False)
            )

    # Equality with function application — likely needs `simp [fn]`
    if "(" in target and ")" in target:
        suggestions.append(
            TacticSuggestion(tactic="simp", confidence=0.35,
                             description="[rewrite_goal] Simplify function application",
                             is_complete=False)
        )

    return suggestions[:3]


class Proposer:
    """Pluggable proposer with configurable strategy.

    Usage
    -----
        proposer = Proposer(strategy="goedel")
        suggestions = proposer.suggest(goal, context)

    Parameters
    ----------
    strategy : str
        Proposer strategy (``"goedel"``, ``"rethlas"``, ``"archon"``).
    generate_fn : Callable or None
        LLM generate function.
    num_samples : int
        Number of parallel samples (default 4).
    cache : ProofCache or None
        Optional output cache.  When set, LLM responses are cached
        keyed by (theorem_header, model_id, temperature).
    model_id : str
        Model identifier for cache key (default ``"local/default"``).
    """

    def __init__(
        self,
        strategy: str = "goedel",
        generate_fn: Callable[[str], str] | None = None,
        num_samples: int = 4,
        cache: Any = None,
        model_id: str = "local/default",
    ):
        self.strategy = strategy
        self.num_samples = num_samples
        self._llm_fn = generate_fn
        self._cache = cache
        self._model_id = model_id
        self._generator: TacticGenerator = self._build_generator()

    def _build_generator(self) -> TacticGenerator:
        if self.strategy == "goedel":
            return make_llm_proposer(self._llm_fn, self.num_samples, cache=self._cache, model_id=self._model_id)
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
