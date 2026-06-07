"""DSPy template management for Omega proof generation.

Defines DSPy Signatures, Modules, and LM adapters that wrap
Omega's existing LLM infrastructure (ChatOllama via LangChain)
into DSPy's optimization framework.

Usage::

    from omega.prover.template_manager import ProverModule, OmegaLM

    lm = OmegaLM(model="qwen3-coder:30b")
    dspy.configure(lm=lm)

    module = ProverModule()
    result = module(theorem_header="theorem t : True :=")

See also: ``omega/prover/compiler.py`` for the compile loop.
"""

from __future__ import annotations

import logging
from typing import Any

from omega.llm import make_langchain_generate_fn

logger = logging.getLogger("omega.prover.template")

# ── Lazy DSPy import ──────────────────────────────────────────────

_HAS_DSPY = False
dspy: Any = None  # type: ignore[assignment]

try:
    import dspy as _dspy  # pyright: ignore[reportMissingImports]

    _HAS_DSPY = True
    dspy = _dspy
except ImportError:
    pass


# ── Signatures (only when DSPy available) ─────────────────────────


def _define_signatures() -> tuple[type, type] | None:
    """Define DSPy Signature subclasses.

    Returns ``None`` when DSPy is not installed.
    """
    if not _HAS_DSPY or dspy is None:
        return None

    class ProverSignature(dspy.Signature):  # type: ignore[misc, arg-type]
        """Generate a Lean 4 proof for a given theorem.

        You are an expert Lean 4 theorem prover. Given a theorem header
        and an optional proof strategy playbook, produce complete,
        compilable Lean code that proves the theorem.

        Rules:
        - Output ONLY the Lean code between ```lean4 and ``` markers.
        - Use ``import Mathlib`` and ``open Real Complex`` by default.
        - Wrap tactic blocks in ``:= by { ... }``.
        - Never use ``sorry`` or ``admit``.
        """

        theorem_header: str = dspy.InputField(
            desc="The Lean 4 theorem header",
        )
        playbook_context: str = dspy.InputField(
            desc="Proof strategy playbook",
        )
        lean_code: str = dspy.OutputField(
            desc="Complete, compilable Lean 4 code",
        )

    class TacticSignature(dspy.Signature):  # type: ignore[misc, arg-type]
        """Suggest the next tactic for an in-progress Lean 4 proof."""

        goal: str = dspy.InputField(desc="Current goal state")
        hypotheses: str = dspy.InputField(desc="Available hypotheses")
        tactics_applied: str = dspy.InputField(desc="Tactics already attempted")
        previous_errors: str = dspy.InputField(desc="Error messages")
        next_tactic: str = dspy.OutputField(desc="Next tactic in Lean 4")

    return ProverSignature, TacticSignature


# Define signatures at module load time.
_SIGNATURES = _define_signatures()
if _SIGNATURES is not None:
    ProverSignature, TacticSignature = _SIGNATURES
    _HAS_SIGNATURES = True
else:
    ProverSignature = None  # type: ignore[assignment]
    TacticSignature = None  # type: ignore[assignment]
    _HAS_SIGNATURES = False


# ── Modules (only when DSPy available) ────────────────────────────


if _HAS_SIGNATURES:
    class ProverModule(dspy.Module):  # type: ignore[misc, arg-type]
        """DSPy module for complete proof generation.

        Wraps :class:`ProverSignature` into a callable that can be
        optimized by DSPy compilers (MIPROv2, GEPA, BootstrapFewShot).
        """

        def __init__(self) -> None:
            super().__init__()
            self.prover = dspy.Predict(ProverSignature)  # type: ignore[arg-type]

        def forward(
            self,
            theorem_header: str,
            playbook_context: str = "",
        ) -> dspy.Prediction:  # type: ignore[name-defined]
            """Generate a Lean proof."""
            return self.prover(
                theorem_header=theorem_header,
                playbook_context=playbook_context,
            )

    class TacticModule(dspy.Module):  # type: ignore[misc, arg-type]
        """DSPy module for next-tactic suggestion."""

        def __init__(self) -> None:
            super().__init__()
            self.tactic_gen = dspy.Predict(TacticSignature)  # type: ignore[arg-type]

        def forward(
            self,
            goal: str,
            hypotheses: str = "",
            tactics_applied: str = "",
            previous_errors: str = "",
        ) -> dspy.Prediction:  # type: ignore[name-defined]
            return self.tactic_gen(
                goal=goal,
                hypotheses=hypotheses,
                tactics_applied=tactics_applied,
                previous_errors=previous_errors,
            )
else:
    ProverModule = None  # type: ignore[assignment]
    TacticModule = None  # type: ignore[assignment]


# ── LM Adapter ─────────────────────────────────────────────────────


def _build_generate_fn(model: str, base_url: str | None = None) -> Any | None:
    """Build an Omega generate_fn for DSPy LM wrapping."""
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.3,
        "num_predict": 4096,
    }
    if base_url is not None:
        kwargs["base_url"] = base_url
    return make_langchain_generate_fn(**kwargs)


def configure_dspy_lm(
    model: str = "qwen3-coder:30b",
    base_url: str | None = None,
    cache: bool = True,
) -> bool:
    """Configure DSPy to use Omega's ChatOllama-backed LM.

    .. note::

        DSPy LM configuration is currently a placeholder. In Phase B,
        this will wrap ``make_langchain_generate_fn`` into a
        ``dspy.LM`` instance. For now, callers should configure
        ``dspy.configure(lm=...)`` directly.

    Returns
    -------
    bool
        ``True`` if DSPy is available.
    """
    if not _HAS_DSPY:
        logger.warning("dspy not installed — run ``pip install dspy``")
        return False
    import dspy as _dspy  # pyright: ignore[reportMissingImports]

    _dspy.configure(lm=None, cache=cache)
    logger.info(
        "DSPy LM configured: model=%s base_url=%s cache=%s",
        model, base_url or "default", cache,
    )
    return True


def extract_optimized_prompt(
    compiled_module: Any,
    predictor_name: str = "prover",
) -> str:
    """Extract the optimized instruction from a DSPy-compiled module.

    After calling ``MIPROv2.compile()`` or ``GEPA.compile()``, the
    module's predictors contain optimized instructions. This function
    extracts them for injection into Omega's prompt template.

    Returns
    -------
    str
        The optimized instruction text, or empty string if unavailable.
    """
    if not _HAS_DSPY or dspy is None:
        return ""
    predictor = getattr(compiled_module, predictor_name, None)
    if predictor is None:
        return ""
    sig = getattr(predictor, "signature", None)
    if sig is None:
        return ""
    return str(getattr(sig, "instructions", ""))


def extract_optimized_demos(
    compiled_module: Any,
    predictor_name: str = "prover",
    max_demos: int = 3,
) -> list[dict[str, str]]:
    """Extract few-shot demonstrations from a DSPy-compiled module.

    Returns
    -------
    list[dict[str, str]]
        Each dict has ``input`` and ``output`` keys.
    """
    if not _HAS_DSPY or dspy is None:
        return []
    predictor = getattr(compiled_module, predictor_name, None)
    if predictor is None:
        return []
    demos = getattr(predictor, "demos", []) or []
    result = []
    for d in list(demos)[:max_demos]:
        demo_dict: dict[str, str] = {}
        for k, v in d.items():
            if isinstance(v, str):
                demo_dict[k] = v[:200]
        if demo_dict:
            result.append(demo_dict)
    return result
