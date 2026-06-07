#!/usr/bin/env python3
"""OmegaRunner — multi-theorem autonomous proof search with checkpoint/resume.

Designed for long-running (2/4/8/12h) unsupervised proof search sessions.
Takes a research goal, decomposes into theorem sequences, runs each
with KnowledgeProver, checkpoints progress, and produces a research report.

Example:
    >>> from omega.runner import OmegaRunner
    >>> runner = OmegaRunner(time_budget_s=14400)  # 4 hours
    >>> report = runner.run("Formalize NV center spin Hamiltonian")
    >>> print(report.summary())
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omega.llm import make_langchain_generate_fn, resolve_ollama_model
from omega.resource import BudgetTracker, ModelAllocator
from omega.verify.t2_real import make_real_compile_callback

logger = logging.getLogger("omega.runner")

# -- Compile callback auto-detection ------------------------------

_COMPILE_CALLBACK_CACHE = None


def _detect_compile_callback():
    """Auto-detect Lean project and return a real compile callback.

    Looks for ``~/lean-paper-plane`` (the standard Mathlab-cached project).
    If found, returns a ``compile_fn`` for T2 verification.
    If not found, returns ``None`` and logs a loud warning.
    """
    global _COMPILE_CALLBACK_CACHE
    if _COMPILE_CALLBACK_CACHE is not None:
        return _COMPILE_CALLBACK_CACHE

    from pathlib import Path

    project = Path.home() / "lean-paper-plane"
    lake = Path.home() / ".elan" / "toolchains" / "4.30.0" / "bin" / "lake"
    lean = Path.home() / ".elan" / "toolchains" / "4.30.0" / "bin" / "lean"

    if not project.exists():
        logger.warning(
            "T2 VERIFICATION DISABLED: Lean project not found at %s. "
            "Every proof attempt will be UNVERIFIED — the system can generate "
            "proof candidates but cannot confirm their correctness. "
            "Clone a Mathlab-cached project (e.g. lean-paper-plane) to enable "
            "real verification.",
            project,
        )
        _COMPILE_CALLBACK_CACHE = None
        return None

    if not lake.exists() or not lean.exists():
        logger.warning(
            "T2 VERIFICATION DISABLED: Lean toolchain not found "
            "(lake=%s, lean=%s). Install elan + Lean 4.30.0.",
            lake,
            lean,
        )
        _COMPILE_CALLBACK_CACHE = None
        return None

    try:
        cb = make_real_compile_callback(project_dir=project, timeout=60)
        logger.info("T2 verification ENABLED: using %s with Mathlab cache", project)
        _COMPILE_CALLBACK_CACHE = cb
        return cb
    except Exception as exc:
        logger.warning("T2 compile callback creation failed: %s", exc)
        _COMPILE_CALLBACK_CACHE = None
        return None


def _make_generate_fn(model_id: str = "local/default") -> Callable[[str], str] | None:
    """Create a ``generate_fn`` for GoedelProver's Proposer using ChatOllama.

    Uses :func:`omega.llm.make_langchain_generate_fn` under the hood —
    replaces the previous ``curl`` + ``subprocess`` approach with
    LangChain's ``ChatOllama`` for:
    - ``httpx`` connection pooling (~20ms saved per call)
    - Proper HTTP error handling
    - Optional Langfuse tracing (when env vars configured)
    - Token usage metadata

    Falls back to ``None`` (template-only) when unreachable.
    """
    ollama_model = resolve_ollama_model(model_id)
    return make_langchain_generate_fn(
        model=ollama_model,
        temperature=0.3,
        num_predict=4096,
        enable_tracing=True,
    )


# -- Progress callback type -------------------------------------

ProgressCallback = Callable[["ProgressEvent"], None]


@dataclass
class ProgressEvent:
    """Event emitted during runner progress."""

    theorem_header: str
    status: str  # pending / running / succeeded / failed
    elapsed_s: float
    n_attempts: int
    n_total_succeeded: int
    n_total_failed: int
    budget_remaining_pct: float
    message: str = ""


# -- TheoremSpec ------------------------------------------------


@dataclass
class TheoremSpec:
    """A theorem to prove as part of a research goal."""

    header: str
    """Lean4 theorem header (e.g. ``theorem add_comm (a b : Nat) : a + b = b + a :=""``)."""

    description: str = ""
    """Natural-language description of what this theorem states."""

    priority: int = 1
    """Priority (1=highest). Higher-priority theorems run first."""

    depends_on: list[str] = field(default_factory=list)
    """Headers of theorems that must be proved first."""

    domain: str = "general"
    """Domain tag (algebra, physics, quantum, optics, etc.)."""

    expected_time_s: float = 60.0
    """Expected proving time in seconds (for scheduling)."""

    status: str = "pending"
    """Current status: pending / running / succeeded / failed."""

    proof_code: str = ""
    """The Lean4 proof code, if succeeded."""

    n_attempts: int = 0
    """Number of attempts made."""

    elapsed_s: float = 0.0
    """Actual wall-clock time spent."""

    errors: list[str] = field(default_factory=list)
    """Error messages from failed attempts."""


# -- Checkpoint -------------------------------------------------


@dataclass
class Checkpoint:
    """Serialisable snapshot of runner state for pause/resume."""

    research_goal: str
    started_at: str
    elapsed_s: float
    theorems: list[dict[str, Any]]
    discoveries: list[str]
    budget: dict[str, Any]

    def save(self, path: str) -> None:
        """Write checkpoint to JSON file."""
        Path(path).write_text(
            json.dumps(
                {
                    "research_goal": self.research_goal,
                    "started_at": self.started_at,
                    "elapsed_s": self.elapsed_s,
                    "theorems": self.theorems,
                    "discoveries": self.discoveries,
                    "budget": self.budget,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str) -> Checkpoint:
        """Load checkpoint from JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            research_goal=data["research_goal"],
            started_at=data["started_at"],
            elapsed_s=data["elapsed_s"],
            theorems=data["theorems"],
            discoveries=data.get("discoveries", []),
            budget=data.get("budget", {}),
        )


# -- Research Report --------------------------------------------


@dataclass
class ResearchReport:
    """Final report from an OmegaRunner session."""

    research_goal: str
    started_at: str
    total_elapsed_s: float
    theorems: list[TheoremSpec]
    discoveries: list[str]
    n_succeeded: int
    n_failed: int
    n_pending: int
    budget_summary: str

    def summary(self) -> str:
        """Human-readable summary of the research session."""
        lines = [
            "=" * 60,
            "Omega Research Report",
            "=" * 60,
            f"Research Goal: {self.research_goal}",
            f"Started:       {self.started_at}",
            f"Elapsed:       {self.total_elapsed_s:.1f}s ({self.total_elapsed_s / 3600:.1f}h)",
            f"Results:       ✅ {self.n_succeeded} succeeded, "
            f"❌ {self.n_failed} failed, ⏳ {self.n_pending} pending",
            "",
            "Theorems:",
        ]
        for t in self.theorems:
            icon = "✅" if t.status == "succeeded" else "❌" if t.status == "failed" else "⏳"
            lines.append(f"  {icon} {t.header[:80]}")
            if t.description:
                lines.append(f"       {t.description}")
            lines.append(f"       attempts={t.n_attempts}, elapsed={t.elapsed_s:.1f}s")

        if self.discoveries:
            lines.append("")
            lines.append("Discoveries:")
            for d in self.discoveries:
                lines.append(f"  💡 {d}")

        lines.append("")
        lines.append(f"Budget: {self.budget_summary}")
        return "\n".join(lines)


# -- OmegaRunner ------------------------------------------------


class OmegaRunner:
    """Autonomous theorem-proving runner with checkpoint/resume.

    Parameters
    ----------
    time_budget_s : float
        Maximum wall-clock seconds for the entire session.
        Default: 14400 (4 hours).
    model_id : str
        Model to use for proof generation (default: ``"local/default"``).
    budget_tracker : BudgetTracker or None
        Budget tracker. If None, created from ``load_config()``.
    progress_callback : ProgressCallback or None
        Called after each theorem attempt with a ProgressEvent.
    checkpoint_dir : str
        Directory for checkpoint files (default: ``~/.omega/checkpoints/``).
    """

    def __init__(
        self,
        time_budget_s: float = 14400.0,
        model_id: str = "local/default",
        budget_tracker: BudgetTracker | None = None,
        progress_callback: ProgressCallback | None = None,
        checkpoint_dir: str = "",
        allocator: ModelAllocator | None = None,
    ) -> None:
        self._time_budget = time_budget_s
        self._model_id = model_id
        self._bt = budget_tracker or BudgetTracker()
        self._on_progress = progress_callback
        self._checkpoint_dir = Path(checkpoint_dir or (Path.home() / ".omega" / "checkpoints"))
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._allocator = allocator or ModelAllocator()

        # Auto-detect real T2 compile callback
        self._compile_fn = _detect_compile_callback()

        self._theorems: list[TheoremSpec] = []
        self._discoveries: list[str] = []
        self._started_at = ""
        self._start_time = 0.0

    # -- Public API ---------------------------------------------

    def run(
        self,
        research_goal: str,
        theorems: list[TheoremSpec] | None = None,
        resume_checkpoint: str = "",
    ) -> ResearchReport:
        """Run the autonomous proof search.

        Args:
            research_goal: Natural-language research goal description.
            theorems: List of theorems to prove. If None, uses defaults.
            resume_checkpoint: Path to a checkpoint file to resume from.

        Returns:
            ``ResearchReport`` with complete session summary.
        """
        self._started_at = datetime.now(UTC).isoformat()
        self._start_time = time.perf_counter()

        # Resume from checkpoint
        if resume_checkpoint:
            cp = Checkpoint.load(resume_checkpoint)
            self._restore_from_checkpoint(cp)
        else:
            self._theorems = list(theorems or [])
            self._discoveries = []

        # Sort theorems by dependency count then priority (highest first)
        self._theorems.sort(key=lambda t: (len(t.depends_on), -t.priority))

        # Main loop
        while self._time_remaining() > 0:
            theorem = self._next_theorem()
            if theorem is None:
                logger.info("All theorems attempted. Stopping.")
                break

            self._prove_theorem(theorem)
            self._checkpoint()

            # Emit progress
            if self._on_progress:
                pct = (1.0 - self._time_remaining() / self._time_budget) * 100
                self._on_progress(
                    ProgressEvent(
                        theorem_header=theorem.header,
                        status=theorem.status,
                        elapsed_s=theorem.elapsed_s,
                        n_attempts=theorem.n_attempts,
                        n_total_succeeded=sum(1 for t in self._theorems if t.status == "succeeded"),
                        n_total_failed=sum(1 for t in self._theorems if t.status == "failed"),
                        budget_remaining_pct=pct,
                        message=f"{'Success' if theorem.status == 'succeeded' else 'Failed'}: "
                        f"{theorem.n_attempts} attempts",
                    )
                )

            # Check budget
            if not self._bt.check_attempts(1, self._model_id):
                logger.info("Attempt budget exhausted. Stopping.")
                break

        # Build report
        return self._build_report(research_goal)

    # -- Theorem selection -------------------------------------

    def _next_theorem(self) -> TheoremSpec | None:
        """Pick the next pending theorem with all deps satisfied (highest priority first)."""
        proven_headers = {t.header for t in self._theorems if t.status == "succeeded"}

        # Sort candidates: fewest deps first, then highest priority
        candidates = [
            t
            for t in self._theorems
            if t.status == "pending" and all(dep in proven_headers for dep in t.depends_on)
        ]
        candidates.sort(key=lambda t: (len(t.depends_on), -t.priority))
        return candidates[0] if candidates else None

    def _prove_theorem(self, theorem: TheoremSpec) -> TheoremSpec:
        """Attempt to prove a single theorem using allocator-routed model."""
        from omega.prover.go_prover import GoedelProver
        from omega.research.prover import KnowledgeProver

        theorem.status = "running"
        t0 = time.perf_counter()

        # Use allocator to select model
        allocation = self._allocator.select_model(theorem.header, theorem.domain)
        model_id = allocation.model_id
        blueprint_mode = allocation.blueprint_mode

        logger.info(
            "Proving %s with %s (complexity=%s, blueprint=%s, append=%s, reason=%s)",
            theorem.header[:40],
            model_id,
            allocation.complexity.value,
            blueprint_mode,
            allocation.append_only,
            allocation.reason,
        )

        # Create prover with budget for the selected model
        bt_for_theorem = BudgetTracker(cfg=self._bt._cfg)
        num_samples = 2 if blueprint_mode else (8 if allocation.append_only else 4)
        max_rounds = 1 if blueprint_mode else (3 if allocation.append_only else 2)
        gp = GoedelProver(
            compile_fn=self._compile_fn,
            generate_fn=_make_generate_fn(model_id),
            budget_tracker=bt_for_theorem,
            num_samples=num_samples,
            max_correction_rounds=max_rounds,
        )
        kp = KnowledgeProver(
            base_prover=gp,
            use_cache=True,
        )

        try:
            # In append_only mode, pass existing proof code as context
            # with a sliding window to prevent unbounded context growth.
            append_context = ""
            if allocation.append_only and theorem.proof_code:
                proof_code = theorem.proof_code
                # Truncate to last 8000 chars (≈ 2000-3000 tokens) to stay
                # well within DeepSeek's 1M token window while preserving
                # the most recent (and most relevant) proof steps.
                max_append_chars = 8000
                if len(proof_code) > max_append_chars:
                    proof_code = "..." + proof_code[-max_append_chars:]
                append_context = (
                    f"(continuing from existing partial proof; "
                    f"last {len(proof_code)} chars shown)\n"
                    f"```lean4\n{proof_code}\n```\n"
                )
            prompt = theorem.header
            if append_context:
                prompt = f"{append_context}\nContinue the proof for:\n\n{prompt}"

            result = kp.run(prompt)
            theorem.elapsed_s = time.perf_counter() - t0
            theorem.n_attempts = getattr(result, "n_attempts", 0) or getattr(
                result, "research", {}
            ).get("knowledge", {}).get("search_stats", {}).get("n_attempts", 0)
            theorem.proof_code = result.proof or ""

            succeeded = result.succeeded if hasattr(result, "succeeded") else False

            # Record provenance
            self._allocator.record_outcome(
                theorem_header=theorem.header,
                model_id=model_id,
                complexity=allocation.complexity,
                succeeded=succeeded,
                elapsed_s=theorem.elapsed_s,
                n_attempts=theorem.n_attempts,
                tokens_consumed=int(getattr(bt_for_theorem, "_total_tokens", 0)),
                cost_usd=getattr(bt_for_theorem, "_total_cost", 0.0),
                blueprint_used=blueprint_mode,
                append_only=allocation.append_only,
            )

            if succeeded:
                theorem.status = "succeeded"
                mode_tag = (
                    " [append]"
                    if allocation.append_only
                    else " [blueprint]"
                    if blueprint_mode
                    else ""
                )
                self._discoveries.append(
                    f"Proved {theorem.header.split(' ')[1] if ' ' in theorem.header else theorem.header[:40]} "
                    f"in {theorem.elapsed_s:.0f}s ({theorem.n_attempts} attempts, {model_id}){mode_tag}"
                )
            else:
                theorem.status = "failed"
                theorem.errors.append(f"No proof found with {model_id} ({allocation.reason})")
                # In append_only mode, keep existing proof_code for next continuation
                if allocation.append_only and theorem.proof_code:
                    theorem.errors[-1] += " (append attempt, proof preserved for retry)"

        except Exception as exc:
            theorem.elapsed_s = time.perf_counter() - t0
            theorem.status = "failed"
            error_msg = str(exc)
            theorem.errors.append(error_msg)
            self._allocator.record_outcome(
                theorem_header=theorem.header,
                model_id=model_id,
                complexity=allocation.complexity,
                succeeded=False,
                elapsed_s=theorem.elapsed_s,
                n_attempts=0,
                error=error_msg,
                append_only=allocation.append_only,
            )

        return theorem

    # -- Checkpoint --------------------------------------------

    def _checkpoint_path(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(self._checkpoint_dir / f"checkpoint_{ts}.json")

    def _checkpoint(self) -> str:
        """Save a checkpoint and return its path."""
        path = self._checkpoint_path()
        cp = Checkpoint(
            research_goal="",
            started_at=self._started_at,
            elapsed_s=time.perf_counter() - self._start_time,
            theorems=[
                {
                    "header": t.header,
                    "description": t.description,
                    "priority": t.priority,
                    "depends_on": t.depends_on,
                    "domain": t.domain,
                    "expected_time_s": t.expected_time_s,
                    "status": t.status,
                    "proof_code": t.proof_code,
                    "n_attempts": t.n_attempts,
                    "elapsed_s": t.elapsed_s,
                    "errors": t.errors,
                }
                for t in self._theorems
            ],
            discoveries=self._discoveries,
            budget=self._bt.remaining(self._model_id),
        )
        cp.save(path)
        logger.info("Checkpoint saved: %s", path)
        return path

    def _restore_from_checkpoint(self, cp: Checkpoint) -> None:
        """Restore runner state from checkpoint."""
        self._started_at = cp.started_at
        self._start_time = time.perf_counter() - cp.elapsed_s
        self._discoveries = cp.discoveries
        self._theorems = []
        for td in cp.theorems:
            self._theorems.append(
                TheoremSpec(
                    header=td["header"],
                    description=td.get("description", ""),
                    priority=td.get("priority", 1),
                    depends_on=td.get("depends_on", []),
                    domain=td.get("domain", "general"),
                    expected_time_s=td.get("expected_time_s", 60.0),
                    status=td.get("status", "pending"),
                    proof_code=td.get("proof_code", ""),
                    n_attempts=td.get("n_attempts", 0),
                    elapsed_s=td.get("elapsed_s", 0.0),
                    errors=td.get("errors", []),
                )
            )
        logger.info(
            "Resumed from checkpoint: %d theorems (%d succeeded, %d failed, %d pending)",
            len(self._theorems),
            sum(1 for t in self._theorems if t.status == "succeeded"),
            sum(1 for t in self._theorems if t.status == "failed"),
            sum(1 for t in self._theorems if t.status == "pending"),
        )

    # -- Time management -------------------------------------

    def _time_remaining(self) -> float:
        return max(self._time_budget - (time.perf_counter() - self._start_time), 0.0)

    # -- Report -----------------------------------------------

    def _build_report(self, research_goal: str) -> ResearchReport:
        elapsed = time.perf_counter() - self._start_time
        return ResearchReport(
            research_goal=research_goal,
            started_at=self._started_at,
            total_elapsed_s=elapsed,
            theorems=self._theorems,
            discoveries=self._discoveries,
            n_succeeded=sum(1 for t in self._theorems if t.status == "succeeded"),
            n_failed=sum(1 for t in self._theorems if t.status == "failed"),
            n_pending=sum(1 for t in self._theorems if t.status == "pending"),
            budget_summary=self._bt.summary(self._model_id),
        )
