"""DSPy compilation orchestration for Omega proof generation.

Wraps DSPy optimizers (MIPROv2, GEPA, BootstrapFewShot) into an
Omega-native interface. Converts theorem/playbook data into DSPy
``Example`` objects, runs compilation, and persists results.

Usage::

    from omega.prover.compiler import ProofCompiler
    from omega.prover.template_manager import ProverModule

    compiler = ProofCompiler(
        module=ProverModule(),
        optimizer="MIPROv2",
        trainset=[...],
        valset=[...],
    )
    compiled = compiler.compile()
    print(compiler.get_optimized_prompt())
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from omega.prover.go_prover import GoedelResult

logger = logging.getLogger("omega.prover.compiler")

# ── Lazy DSPy import ──────────────────────────────────────────────

_HAS_DSPY = False
dspy: Any = None  # type: ignore[assignment]

try:
    import dspy as _dspy  # pyright: ignore[reportMissingImports]

    _HAS_DSPY = True
    dspy = _dspy
except ImportError:
    pass

# ── Constants ─────────────────────────────────────────────────────

DEFAULT_COMPILE_DIR = Path.home() / ".omega" / "compiled"
OPTIMIZER_REGISTRY: dict[str, str] = {
    "MIPROv2": "dspy.MIPROv2",
    "GEPA": "dspy.GEPA",
    "BootstrapFewShot": "dspy.BootstrapFewShot",
}


# ── Data preparation ──────────────────────────────────────────────


def theorem_to_example(
    theorem_header: str,
    lean_code: str = "",
    playbook_context: str = "",
) -> Any | None:
    """Convert a theorem + proof pair to a DSPy ``Example``.

    Parameters
    ----------
    theorem_header:
        Lean theorem header, e.g. ``\"theorem t (n : ℕ) : n + 0 = n :=\"``.
    lean_code:
        The successful Lean proof code (used as ``gold`` for training).
    playbook_context:
        ACE-style playbook context to include in the example.

    Returns
    -------
    dspy.Example or None
        ``None`` when DSPy is not available.
    """
    if not _HAS_DSPY or dspy is None:
        return None

    example = dspy.Example(
        theorem_header=theorem_header,
        playbook_context=playbook_context,
        lean_code=lean_code,
    ).with_inputs("theorem_header", "playbook_context")
    return example


def results_to_trainset(
    results: list[tuple[str, GoedelResult]],
    max_examples: int = 50,
) -> list[Any]:
    """Convert (theorem_header, GoedelResult) pairs to DSPy training set.

    Only includes successful proofs (``result.succeeded == True``) to
    provide positive training examples.

    Parameters
    ----------
    results:
        List of ``(theorem_header, GoedelResult)`` pairs from prior runs.
    max_examples:
        Maximum number of examples to include.

    Returns
    -------
    list[dspy.Example]
        Training examples, or empty list if DSPy unavailable / no successes.
    """
    if not _HAS_DSPY or dspy is None:
        return []

    examples: list[Any] = []
    for header, result in results:
        if not result.succeeded or not result.proof:
            continue
        ex = theorem_to_example(
            theorem_header=header,
            lean_code=result.proof,
        )
        if ex is not None:
            examples.append(ex)
        if len(examples) >= max_examples:
            break

    logger.info(
        "Prepared %d training examples from %d results",
        len(examples), len(results),
    )
    return examples


def headers_to_trainset(
    theorem_headers: list[str],
    max_examples: int = 50,
) -> list[Any]:
    """Convert bare theorem headers to DSPy examples (no gold code).

    Use this for zero-shot / few-shot optimization where only the
    theorem statement is known (no prior successful proofs).

    Parameters
    ----------
    theorem_headers:
        List of theorem headers to use as training inputs.
    max_examples:
        Maximum examples.

    Returns
    -------
    list[dspy.Example]
        Examples with empty ``lean_code`` field.
    """
    if not _HAS_DSPY or dspy is None:
        return []

    examples: list[Any] = []
    for header in theorem_headers[:max_examples]:
        ex = theorem_to_example(
            theorem_header=header,
            lean_code="",
        )
        if ex is not None:
            examples.append(ex)
    return examples


# ── Metric ─────────────────────────────────────────────────────────


def _compile_metric(
    gold: Any,
    prediction: Any,
    trace: Any = None,  # noqa: ARG001 — DSPy metric signature compatibility
) -> bool | float:
    """Metric function for DSPy optimization.

    Compares the generated ``lean_code`` against the gold standard.
    When gold has a known proof, checks structural match.
    When no gold proof is available, checks that the output is
    structurally valid Lean code (basic syntax checks).

    This is a **mock metric** for development use. In production,
    replace with a real T2 compilation call.
    """
    predicted = getattr(prediction, "lean_code", "") or ""
    gold_code = getattr(gold, "lean_code", "") or ""

    # If no gold code, check basic structural validity.
    if not gold_code:
        has_theorem = "theorem" in predicted or "lemma" in predicted
        has_colon_eq = ":=" in predicted
        return bool(has_theorem and has_colon_eq)

    # If gold exists, check structural similarity.
    if not predicted:
        return False
    # Simple heuristic: both must have same theorem structure.
    return bool(":=" in predicted)


# ── Main Compiler ─────────────────────────────────────────────────


class ProofCompilerError(Exception):
    """Raised when DSPy compilation fails."""


class ProofCompiler:
    """Orchestrates DSPy-based prompt optimization for Omega.

    Parameters
    ----------
    module:
        DSPy module to optimize (typically ``ProverModule``).
    optimizer:
        Name of the optimizer: ``\"MIPROv2\"``, ``\"GEPA\"``,
        ``\"BootstrapFewShot\"``.
    trainset:
        List of ``dspy.Example`` objects for training.
    valset:
        List of ``dspy.Example`` objects for validation.
    metric:
        Callable ``(gold, prediction, trace) → bool | float``.
        Default: :func:`_compile_metric`.
    compile_dir:
        Directory for persisting compiled programs.
    """

    def __init__(
        self,
        module: Any = None,
        optimizer: str = "MIPROv2",
        trainset: list[Any] | None = None,
        valset: list[Any] | None = None,
        metric: Any = None,
        compile_dir: str | Path | None = None,
    ) -> None:
        self.module = module
        self.optimizer_name = optimizer
        self.trainset = trainset or []
        self.valset = valset or []
        self.metric = metric or _compile_metric
        self.compile_dir = Path(compile_dir or DEFAULT_COMPILE_DIR)

        self._compiled_program: Any = None
        self._optimizer_instance: Any = None
        self._compile_stats: dict[str, Any] = {}

    # ── Compilation ──────────────────────────────────────────────

    def compile(
        self,
        max_evals: int = 8,
        num_threads: int = 1,
    ) -> Any:
        """Run DSPy optimization on the training set.

        Parameters
        ----------
        max_evals:
            Maximum optimizer evaluations (candidates to try).
            For ``auto=\"light\"`` mode, this is the number of
            instruction candidates generated.
        num_threads:
            Number of parallel threads for evaluation.

        Returns
        -------
        Any
            The compiled DSPy module.

        Raises
        ------
        ProofCompilerError
            If DSPy is unavailable or configuration is invalid.
        """
        if not _HAS_DSPY or dspy is None:
            raise ProofCompilerError(
                "DSPy is not installed. Run ``pip install dspy``."
            )
        if self.module is None:
            raise ProofCompilerError("No DSPy module provided for compilation.")
        if not self.trainset:
            raise ProofCompilerError(
                "Training set is empty. Run results_to_trainset() first."
            )

        t_start = time.time()
        logger.info(
            "Starting %s compilation: %d train, %d val, %d max_evals",
            self.optimizer_name,
            len(self.trainset),
            len(self.valset),
            max_evals,
        )

        # Build optimizer.
        optimizer_factory = self._resolve_optimizer()
        if self.optimizer_name.lower() == "bootstrapfewshot":
            self._optimizer_instance = optimizer_factory()
        else:
            self._optimizer_instance = optimizer_factory(num_threads=num_threads)

        # Run compilation.
        compile_kwargs: dict[str, Any] = {"trainset": self.trainset}
        if self.valset and self.optimizer_name.lower() != "bootstrapfewshot":
            compile_kwargs["valset"] = self.valset

        try:
            self._compiled_program = self._optimizer_instance.compile(
                self.module,
                **compile_kwargs,
            )
        except Exception as exc:
            raise ProofCompilerError(
                f"DSPy {self.optimizer_name} compilation failed: {exc}"
            ) from exc

        elapsed = time.time() - t_start
        self._compile_stats = {
            "optimizer": self.optimizer_name,
            "train_size": len(self.trainset),
            "val_size": len(self.valset),
            "max_evals": max_evals,
            "elapsed_s": round(elapsed, 2),
            "success": True,
        }

        optimized_prompt = self.get_optimized_prompt()
        logger.info(
            "Compilation complete in %.2fs. Optimized prompt snippet: %s …",
            elapsed,
            optimized_prompt[:100] if optimized_prompt else "(empty)",
        )

        return self._compiled_program

    def _resolve_optimizer(self) -> Any:
        """Resolve optimizer name to DSPy class."""
        if not _HAS_DSPY or dspy is None:
            raise ProofCompilerError("DSPy not available")

        name = self.optimizer_name.lower()
        if name == "miprov2":
            return lambda **kw: dspy.MIPROv2(metric=self.metric, **kw)
        elif name == "gepa":
            return lambda **kw: dspy.GEPA(metric=self.metric, **kw)
        elif name == "bootstrapfewshot":
            return lambda **kw: dspy.BootstrapFewShot(metric=self.metric, **kw)
        else:
            raise ProofCompilerError(
                f"Unknown optimizer: {self.optimizer_name}. "
                f"Choose from: {', '.join(OPTIMIZER_REGISTRY)}"
            )

    # ── Evaluation ───────────────────────────────────────────────

    def evaluate(self, valset: list[Any] | None = None) -> dict[str, float]:
        """Evaluate the compiled program on a validation set.

        Parameters
        ----------
        valset:
            Override validation set. Defaults to ``self.valset``.

        Returns
        -------
        dict
            ``{\"accuracy\": float, \"num_correct\": int, \"num_total\": int}``.
        """
        if self._compiled_program is None:
            return {"accuracy": 0.0, "num_correct": 0, "num_total": 0}

        valset = valset or self.valset
        if not valset:
            return {"accuracy": 0.0, "num_correct": 0, "num_total": 0}

        correct = 0
        for example in valset:
            try:
                prediction = self._compiled_program(
                    theorem_header=example.theorem_header,
                    playbook_context=example.playbook_context or "",
                )
                score = self.metric(example, prediction)
                if score:
                    correct += 1
            except Exception:
                pass

        total = len(valset)
        accuracy = correct / max(total, 1)
        logger.info("Evaluation: %d/%d correct (%.1f%%)", correct, total, accuracy * 100)
        return {"accuracy": accuracy, "num_correct": correct, "num_total": total}

    # ── Results extraction ───────────────────────────────────────

    def get_optimized_prompt(self) -> str:
        """Extract the optimized instruction from compiled program."""
        from omega.prover.template_manager import extract_optimized_prompt

        if self._compiled_program is None:
            return ""
        return extract_optimized_prompt(self._compiled_program)

    def get_optimized_demos(self, max_demos: int = 3) -> list[dict[str, str]]:
        """Extract optimized few-shot demonstrations."""
        from omega.prover.template_manager import extract_optimized_demos

        if self._compiled_program is None:
            return []
        return extract_optimized_demos(self._compiled_program, max_demos=max_demos)

    def get_stats(self) -> dict[str, Any]:
        """Return compilation statistics."""
        return dict(self._compile_stats)

    # ── Persistence ──────────────────────────────────────────────

    def save(self, name: str = "default") -> Path:
        """Save compiled program and optimized prompt to disk.

        Parameters
        ----------
        name:
            Identifier for this compiled program.

        Returns
        -------
        Path
            Path to the saved directory.
        """
        save_dir = self.compile_dir / name
        save_dir.mkdir(parents=True, exist_ok=True)

        # Save stats.
        stats_path = save_dir / "compile_stats.json"
        stats_path.write_text(json.dumps(self._compile_stats, indent=2))

        # Save optimized prompt.
        prompt = self.get_optimized_prompt()
        prompt_path = save_dir / "optimized_prompt.txt"
        prompt_path.write_text(prompt)

        # Save demos.
        demos = self.get_optimized_demos()
        demos_path = save_dir / "optimized_demos.json"
        demos_path.write_text(json.dumps(demos, indent=2))

        logger.info("Compiled program saved to %s", save_dir)
        return save_dir

    def load(self, name: str = "default") -> str:
        """Load the optimized prompt from a previous compilation.

        Parameters
        ----------
        name:
            Identifier for the compiled program.

        Returns
        -------
        str
            The optimized prompt text.
        """
        prompt_path = self.compile_dir / name / "optimized_prompt.txt"
        if not prompt_path.exists():
            logger.warning("No saved prompt found at %s", prompt_path)
            return ""
        return prompt_path.read_text()
