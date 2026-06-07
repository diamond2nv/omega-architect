#!/usr/bin/env python3
"""Goedel-style prover — parallel sampling with T2 self-correction.

The Goedel prover is the primary proof generation engine in Ω-Architect.
It works by:

1. **Parallel sampling**: Generate N independent proof attempts via the
   :class:`~omega.search.proposer.Proposer` in a single round.
2. **T2 compilation**: Compile each attempt through the Lean compiler
   using the provided ``compile_fn`` callback.
3. **Error collection**: Gather T2 error messages from failed attempts.
4. **Self-correction**: Feed errors back to the Proposer and generate
   refined attempts (up to ``max_correction_rounds``).
5. **Return**: The first proof that passes T2, or ``None`` if all fail.

Usage::

    from omega.prover import GoedelProver
    from omega.verify.t2_real import make_real_compile_callback

    compile_fn = make_real_compile_callback()
    prover = GoedelProver(compile_fn=compile_fn, num_samples=8)
    result = prover.run(theorem_header="theorem add_zero (n : ℕ) : n + 0 = n :=")

    if result.proof:
        print(f"Found proof in {result.timings['total_s']:.2f}s")
    else:
        print(f"No proof found after {result.n_attempts} attempts")
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omega.search.proposer import Proposer, TacticSuggestion
from omega.search.tree import GoalState
from omega.verify.t2_lean import T2Result, parse_diagnostics

# -- optional playbook support ---------------------------------

try:
    from omega.prover.playbook import PlaybookManager

    _HAS_PLAYBOOK = True
except ImportError:
    PlaybookManager = None  # type: ignore[assignment]
    _HAS_PLAYBOOK = False

# -- optional resource tracking ---------------------------------

try:
    from omega.resource import BudgetTracker, ConvergenceTracker

    _HAS_RESOURCE = True
except ImportError:
    BudgetTracker = None  # type: ignore
    ConvergenceTracker = None  # type: ignore
    _HAS_RESOURCE = False

# -- type aliases ------------------------------------------------

logger = logging.getLogger("go_prover")

CompileFn = Callable[[str], dict | list | str | None]
"""Signature of a T2 compile callback.

Accepts a self-contained Lean 4 code string and returns a result
compatible with :func:`omega.verify.t2_lean.parse_diagnostics`.
"""


# -- result dataclass --------------------------------------------


@dataclass
class GoedelResult:
    """Result of running the Goedel prover.

    Attributes
    ----------
    proof : str or None
        The first proof that passed T2 compilation, or ``None`` if no
        proof was found.
    attempts : list[dict]
        Record of every proof attempt, in order. Each entry has keys:
        ``round``, ``tactic``, ``lean_code``, ``verified``, ``errors``,
        ``elapsed_s``.
    timings : dict[str, float]
        Timing breakdown in seconds. Keys include ``total_s`` and
        ``round_0_s``, ``round_1_s``, etc.
    n_attempts : int
        Total number of proof attempts across all correction rounds.
    n_passed : int
        Number of attempts that passed T2 compilation (0 or 1).
    corrections_used : int
        Number of self-correction rounds actually used (0 if first
        round succeeded).
    contains_sorry : bool
        Whether the ''proof'' (if any) contains ``sorry`` or ``admit``.
        A proof that passes T2 via ``sorry`` does NOT count as a real
        proof — it is structurally valid but logically incomplete.
    """

    proof: str | None = None
    attempts: list[dict] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    n_attempts: int = 0
    n_passed: int = 0
    corrections_used: int = 0
    contains_sorry: bool = False
    convergence_summary: str = ""
    convergence_rate: float = 0.0
    stuck: bool = False
    budget_summary: str = ""

    @property
    def succeeded(self) -> bool:
        """Whether a passing proof was found.

        Returns ``True`` only when a T2-verified proof exists AND
        it does NOT contain ``sorry`` / ``admit`` (which would make
        the compilation pass but leave the theorem unproven).
        """
        if self.proof is None:
            return False
        return not self.contains_sorry

    @property
    def summary(self) -> str:
        """A one-line human-readable summary."""
        if self.succeeded:
            return (
                f"[OK] proof found in {self.timings.get('total_s', 0):.2f}s "
                f"({self.n_attempts} attempts, "
                f"{self.corrections_used} correction rounds)"
            )
        if self.contains_sorry:
            return (
                f"[WARN] T2 passed but proof uses `sorry`/`admit` "
                f"(logically incomplete) in {self.timings.get('total_s', 0):.2f}s"
            )
        return (
            f"[FAIL] no proof ({self.n_attempts} attempts, {self.timings.get('total_s', 0):.2f}s)"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (JSON-safe)."""
        return {
            "succeeded": self.succeeded,
            "proof": self.proof,
            "attempts": self.attempts,
            "timings": self.timings,
            "n_attempts": self.n_attempts,
            "n_passed": self.n_passed,
            "corrections_used": self.corrections_used,
            "convergence_summary": self.convergence_summary,
            "convergence_rate": self.convergence_rate,
            "stuck": self.stuck,
            "budget_summary": self.budget_summary,
        }


# -- `sorry` / `admit` detection ---------------------------------


def _contains_sorry(lean_code: str) -> bool:
    """Check if Lean code contains ``sorry`` or ``admit`` as a proof term.

    These are structurally valid Lean tokens that cause T2 compilation
    to pass but leave the theorem unproven.  Any proof containing them
    must be flagged in :attr:`GoedelResult.contains_sorry`.
    """
    import re

    # Match `sorry` or `admit` as a standalone token (not inside comments)
    # by removing line comments and block comments first.
    text = re.sub(r"--[^\n]*", "", lean_code)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return bool(re.search(r"\bsorry\b|\badmit\b", text))


# -- main prover class -------------------------------------------


class GoedelProver:
    """Goedel-style prover with parallel sampling and self-correction.

    Parameters
    ----------
    compile_fn : CompileFn or None
        Callable that takes Lean 4 code (str) and returns compiler
        output compatible with ``parse_diagnostics``.  If ``None``,
        the prover runs in dry-run mode (generates attempts but
        cannot verify them).  Typical value:
        ``make_real_compile_callback()`` from ``omega.verify.t2_real``.

    proposer : Proposer or None
        The tactic/ proof proposer.  If ``None``, a default
        ``Proposer(strategy='goedel', num_samples=num_samples)``
        is created.

    num_samples : int
        Number of independent proof attempts to generate per round
        (default 4).

    max_correction_rounds : int
        Maximum number of self-correction rounds after the initial
        sampling (default 2).  Total rounds = 1 initial +
        ``max_correction_rounds`` corrections.

    min_confidence : float
        Minimum confidence threshold for considering a suggestion
        (default 0.0 — accept all).

    max_total_attempts : int or None
        Hard cap on total attempts across all rounds.  If ``None``
        (default), no cap is enforced.
    """

    def __init__(
        self,
        compile_fn: CompileFn | None = None,
        proposer: Proposer | None = None,
        generate_fn: Callable[[str], str] | None = None,
        num_samples: int = 6,
        max_correction_rounds: int = 2,
        min_confidence: float = 0.0,
        max_total_attempts: int | None = None,
        budget_tracker: Any = None,
        convergence_tracker: Any = None,
        playbook_manager: Any = None,
    ) -> None:
        self.compile_fn = compile_fn
        self.proposer = proposer or Proposer(
            strategy="goedel",
            generate_fn=generate_fn,
            num_samples=num_samples,
        )
        self.num_samples = num_samples
        self.max_correction_rounds = max_correction_rounds
        self.min_confidence = min_confidence
        self.max_total_attempts = max_total_attempts
        self.budget_tracker = budget_tracker
        self.convergence_tracker = convergence_tracker
        self.playbook_manager = playbook_manager

    # -- public API ----------------------------------------------

    def run(
        self,
        theorem_header: str,
        *,
        compile_fn: CompileFn | None = None,
    ) -> GoedelResult:
        """Run the Goedel prover on a theorem.

        Parameters
        ----------
        theorem_header : str
            The Lean 4 theorem header, e.g.::

                theorem add_zero (n : ℕ) : n + 0 = n :=

            This is parsed into a :class:`~omega.search.tree.GoalState`
            and used as the target for proof generation.

        compile_fn : CompileFn or None
            Optional per-call override of the instance-level
            ``compile_fn``.

        Returns
        -------
        GoedelResult
            The result of the proof search.
        """
        t_start = time.perf_counter()

        goal = GoalState.from_lean_header(theorem_header)
        result = GoedelResult()

        # Accumulate errors from all failed rounds for feedback.
        all_errors: list[str] = []
        # Track distinct error hashes to avoid repetitive feedback.
        seen_errors: set[str] = set()

        total_rounds = self.max_correction_rounds + 1  # initial + corrections

        for round_idx in range(total_rounds):
            round_start = time.perf_counter()

            # -- build config ------------------------------------
            config: dict[str, Any] = {
                "round": round_idx,
                "num_samples": self.num_samples,
                "theorem_header": theorem_header,
            }

            # Inject playbook context for ACE-style strategy accumulation.
            if self.playbook_manager is not None:
                config["playbook_context"] = self.playbook_manager.playbook.render()

            if round_idx > 0 and all_errors:
                # Provide unique error messages for self-correction.
                config["previous_errors"] = list(all_errors)
                config["correction_round"] = round_idx

            # -- generate suggestions ----------------------------
            context: list = []  # no search tree context in Goedel mode
            suggestions = self.proposer.suggest(goal, context, **config)

            if not suggestions:
                # No suggestions from proposer — skip round.
                result.timings[f"round_{round_idx}_s"] = time.perf_counter() - round_start
                continue

            # -- try each suggestion -----------------------------
            for suggestion in suggestions:
                # Check hard cap.
                if (
                    self.max_total_attempts is not None
                    and result.n_attempts >= self.max_total_attempts
                ):
                    break

                # Budget check: attempts
                if self.budget_tracker is not None and not self.budget_tracker.check_attempts(1):
                    break  # No more attempts allowed

                # Budget check: time
                elapsed_so_far = time.perf_counter() - t_start
                if self.budget_tracker is not None and not self.budget_tracker.check_time(elapsed_so_far):
                    break  # Time budget exhausted

                # Filter by confidence.
                if suggestion.confidence < self.min_confidence:
                    continue

                attempt_start = time.perf_counter()

                # Build complete Lean code for this attempt.
                lean_code = self._build_lean_code(theorem_header, suggestion)
                if lean_code is None:
                    logger.warning("Skipping incomplete suggestion (ends with `:= by`)")
                    continue

                # Compile via T2.
                t2_result = self._compile(lean_code, compile_fn)

                elapsed = time.perf_counter() - attempt_start

                # Record attempt.
                attempt_record = self._make_attempt_record(
                    round_idx=round_idx,
                    suggestion=suggestion,
                    lean_code=lean_code,
                    t2_result=t2_result,
                    elapsed_s=elapsed,
                )
                result.attempts.append(attempt_record)
                result.n_attempts += 1

                # Update playbook with T2 result (ACE-style reflection).
                if self.playbook_manager is not None and t2_result is not None:
                    self.playbook_manager.update_from_result(
                        theorem=theorem_header,
                        attempt=lean_code,
                        diagnostics=[
                            {"message": e, "severity": "error"}
                            for e in t2_result.errors
                        ],
                        succeeded=bool(t2_result.verified),
                    )

                # Consume budget for this attempt.
                if self.budget_tracker is not None:
                    # Estimate token count from character count.
                    # Lean code contains Unicode math symbols (∀, ∃, ℝ, ℂ, ⨁, ⊗)
                    # and mixed Chinese/English text.  A conservative estimate:
                    #   English ASCII:   ~0.25 tokens/char
                    #   Unicode math:    ~0.5-1.0 tokens/char
                    #   Mixed (typical): ~0.35 tokens/char
                    # We use 0.4 tokens/char to be conservative (overestimate
                    # by ~15% is better than the 10x underestimation of len//4).
                    # Also account for ~500 tokens of system prompt + instruction
                    # overhead that is NOT included in lean_code length.
                    system_overhead_tokens = 500
                    chars_per_token = 2.5  # conservative: 0.4 tokens/char
                    input_toks = system_overhead_tokens + max(
                        100, int(len(lean_code) / chars_per_token)
                    )
                    output_toks = max(50, int(len(suggestion.tactic) / chars_per_token))
                    self.budget_tracker.consume(
                        input_tokens=input_toks,
                        output_tokens=output_toks,
                        model_id=self._model_id(),
                        elapsed_s=elapsed,
                    )

                # Early exit on first passing proof.
                if t2_result is not None and t2_result.verified:
                    result.proof = lean_code
                    # CRITICAL: Detect `sorry` / `admit` in the proof.
                    # A T2 pass with `sorry` is a false positive — the
                    # code compiles but the theorem is NOT proved.
                    # See docs/design/minif2f-gsnv-roadmap-v1.md Issue #2.
                    if _contains_sorry(lean_code):
                        result.contains_sorry = True
                        attempt_record["warning"] = (
                            "T2 passed but proof contains `sorry` — "
                            "this is a false positive (logically incomplete)"
                        )
                    result.n_passed += 1
                    result.corrections_used = round_idx
                    result.timings["total_s"] = time.perf_counter() - t_start
                    return result

                # Collect new errors for self-correction feedback.
                if t2_result is not None and t2_result.errors:
                    for err in t2_result.errors:
                        # Deduplicate by hash of first 120 chars.
                        err_key = err[:120]
                        if err_key not in seen_errors:
                            seen_errors.add(err_key)
                            all_errors.append(err)

            # Record round timing.
            result.timings[f"round_{round_idx}_s"] = time.perf_counter() - round_start

            # Record convergence epoch after each round.
            if self.convergence_tracker is not None:
                n_errs = len([a for a in result.attempts if not a["verified"]])
                proof_len = len(result.proof or "")
                round_errors = list(all_errors)
                self.convergence_tracker.record_epoch(
                    n_errors=n_errs,
                    proof_length=proof_len,
                    elapsed_s=time.perf_counter() - t_start,
                    errors=round_errors,
                )

        # After all rounds, check convergence.
        if self.convergence_tracker is not None:
            result.convergence_summary = self.convergence_tracker.summary()
            result.convergence_rate = self.convergence_tracker.convergence_rate()
            result.stuck = self.convergence_tracker.is_stuck()
        if self.budget_tracker is not None:
            result.budget_summary = self.budget_tracker.summary()

        result.timings["total_s"] = time.perf_counter() - t_start
        return result

    # -- internal helpers ----------------------------------------

    def _build_lean_code(
        self,
        theorem_header: str,
        suggestion: TacticSuggestion,
    ) -> str | None:
        """Construct a complete, compilable Lean proof from a suggestion.

        Precedence:
        1. ``suggestion.lean_code`` — if already a complete proof, use it.
        2. ``suggestion.is_complete`` — wrap with ``:= by`` block.
        3. Fallback — append the tactic as a ``by`` block.
        """
        if suggestion.lean_code:
            return suggestion.lean_code

        # Reject incomplete ``:= by`` blocks — avoid wasting T2 time.
        if re.search(r':=\s+by\s*$', suggestion.tactic):
            return None  # caller handles None by skipping

        header = theorem_header.rstrip().rstrip(":=").rstrip()

        if suggestion.is_complete:
            # Tactics like `trivial`, `rfl` must be in a `by` block.
            return f"{header} :=\n  by\n    {suggestion.tactic}"

        # Append a single tactic in a by-block.
        return f"{header} :=\n  by\n    {suggestion.tactic}"

    def _compile(
        self,
        code: str,
        compile_fn: CompileFn | None = None,
    ) -> T2Result | None:
        """Run T2 compilation on Lean code.

        Parameters
        ----------
        code : str
            Self-contained Lean 4 code.
        compile_fn : CompileFn or None
            Per-call override.  Falls back to instance-level
            ``self.compile_fn`` if ``None``.

        Returns
        -------
        T2Result or None
            ``None`` when no compiler is available (both the
            per-call and instance-level ``compile_fn`` are ``None``).
        """
        fn = compile_fn if compile_fn is not None else self.compile_fn
        if fn is None:
            return None

        try:
            raw = fn(code)
            return parse_diagnostics(raw)
        except Exception as exc:
            return T2Result(
                verified=False,
                errors=[f"T2 compilation raised exception: {exc}"],
            )

    @staticmethod
    def _make_attempt_record(
        round_idx: int,
        suggestion: TacticSuggestion,
        lean_code: str,
        t2_result: T2Result | None,
        elapsed_s: float,
    ) -> dict[str, Any]:
        """Build a structured attempt record for the result."""
        if t2_result is not None:
            verified = t2_result.verified
            errors = list(t2_result.errors)
            warnings = list(t2_result.warnings)
        else:
            verified = False
            errors = ["No T2 compiler available (compile_fn is None)"]
            warnings = []

        return {
            "round": round_idx,
            "tactic": suggestion.tactic,
            "confidence": suggestion.confidence,
            "description": suggestion.description,
            "strategy": suggestion.strategy,
            "lean_code": lean_code,
            "verified": verified,
            "errors": errors,
            "warnings": warnings,
            "elapsed_s": round(elapsed_s, 4),
        }

    def _model_id(self) -> str:
        """Return the model ID for budget tracking.

        Defaults to ``"local/default"`` (free) when no specific model
        is configured.  Override in subclasses or pass via config.
        """
        return "local/default"

    def __repr__(self) -> str:
        return (
            f"GoedelProver("
            f"samples={self.num_samples}, "
            f"corrections={self.max_correction_rounds}, "
            f"compile_fn={'set' if self.compile_fn else 'None'})"
        )


# -- convenience factory -----------------------------------------


def make_goedel_prover(
    compile_fn: CompileFn | None = None,
    num_samples: int = 6,
    max_correction_rounds: int = 2,
    **kwargs: Any,
) -> GoedelProver:
    """Quick factory for a GoedelProver with default configuration.

    Parameters
    ----------
    compile_fn : CompileFn or None
        Passed to :class:`GoedelProver`.
    num_samples : int
        Passed to :class:`GoedelProver`.
    max_correction_rounds : int
        Passed to :class:`GoedelProver`.
    **kwargs
        Additional keyword arguments forwarded to :class:`GoedelProver`.

    Returns
    -------
    GoedelProver
    """
    return GoedelProver(
        compile_fn=compile_fn,
        num_samples=num_samples,
        max_correction_rounds=max_correction_rounds,
        **kwargs,
    )
