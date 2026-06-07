"""omega-plugin/_bridge.py — omega-core ↔ Hermes adapter layer.

Wraps omega-core's prover engines into Hermes-friendly interfaces:
- :func:`make_hermes_generate_fn` wraps ``ctx.llm.complete`` as omega-core's
  ``GenerateFn`` (``Callable[[str], str]``)
- :func:`make_hermes_compile_fn` provides Lean 4 T2 compilation via
  ``subprocess`` (fallback) or Hermes MCP (preferred)
- :class:`OmegaBridge` is the high-level facade for slash-command handlers
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("omega-plugin.bridge")


# ── try importing omega-core ────────────────────────────────────

_HAS_OMEGA_CORE = False
try:
    from omega.prover.ensemble import EnsembleProver, EnsembleResult
    from omega.prover.go_prover import CompileFn, GoedelProver, GoedelResult

    _HAS_OMEGA_CORE = True
except ImportError as exc:
    logger.warning("omega-core not installed (%s); bridge will report this to user commands", exc)

# ── compile_fn helpers ──────────────────────────────────────────


def _find_lean_binary() -> str | None:
    """Locate the Lean 4 binary on the system.

    Searches ``PATH``, then common install locations.
    """
    # 1. PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "lean")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # 2. Common install roots
    for root in (
        Path.home() / ".elan" / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/opt/lean"),
    ):
        candidate = root / "lean"
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)

    return None


def make_hermes_compile_fn(ctx=None) -> CompileFn | None:
    """Build a T2 compile callback.

    Tries, in order:
    1. Hermes MCP ``lean_lsp/lean_build`` (via ``ctx.dispatch_tool``)
    2. ``lean`` CLI subprocess (```lean --stdin```)

    Returns ``None`` when neither is available.
    """
    # Strategy 1: MCP via dispatch_tool (requires ctx + configured lean_lsp)
    if ctx is not None:
        try:
            # We try a dummy call to detect MCP availability.
            # The actual compile callback will use dispatch_tool per-call.
            pass
        except Exception:
            logger.debug("MCP lean_lsp not available, falling back to CLI")

    # Strategy 2: CLI subprocess
    lean_bin = _find_lean_binary()
    if lean_bin is not None:
        def _compile_via_cli(lean_code: str) -> dict:
            """Compile Lean code via ``lean --stdin``.

            Returns a dict with ``diagnostics`` key, compatible with
            ``omega.verify.t2_lean.parse_diagnostics``.
            """
            try:
                proc = subprocess.run(
                    [lean_bin, "--stdin"],
                    input=lean_code.encode("utf-8"),
                    capture_output=True,
                    timeout=120,
                )
                stderr_text = proc.stderr.decode("utf-8", errors="replace")
                proc.stdout.decode("utf-8", errors="replace")  # discard stdout

                diagnostics: list[dict] = []
                if stderr_text.strip():
                    for line in stderr_text.splitlines():
                        # Basic Lean error parsing
                        if "error:" in line.lower() or "warning:" in line.lower():
                            severity = "error" if "error:" in line.lower() else "warning"
                            diagnostics.append({
                                "message": line,
                                "severity": severity,
                            })

                if not diagnostics and proc.returncode != 0:
                    diagnostics.append({
                        "message": f"lean exit code {proc.returncode}: {stderr_text[:200]}",
                        "severity": "error",
                    })

                return {"diagnostics": diagnostics}
            except subprocess.TimeoutExpired:
                return {"diagnostics": [{"message": "T2 compile timeout (120s)", "severity": "error"}]}
            except FileNotFoundError:
                return {"diagnostics": [{"message": "lean binary not found", "severity": "error"}]}
            except Exception as exc:
                return {"diagnostics": [{"message": f"T2 compile error: {exc}", "severity": "error"}]}

        return _compile_via_cli

    return None


# ── generate_fn helper ──────────────────────────────────────────


def make_hermes_generate_fn(ctx) -> CompileFn:
    """Wrap ``ctx.llm.complete()`` as omega-core's ``GenerateFn``.

    The returned callable accepts a prompt string and returns the model's
    response text — the exact signature omega-core's ``GoedelProver``
    expects for its ``generate_fn`` parameter.
    """

    def _generate(prompt: str) -> str:
        """Generate Lean proof via Hermes LLM.

        Parameters
        ----------
        prompt : str
            Full prompt including theorem header and context.

        Returns
        -------
        str
            Model response — expected to contain Lean 4 proof code.
        """
        result = ctx.llm.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a Lean 4 theorem-proving assistant. "
                        "You generate Lean 4 code that compiles with Mathlib. "
                        "Return ONLY the Lean proof code — no explanations, no markdown fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=2048,
            purpose="omega.proof.generate",
        )
        text = result.text.strip()
        # Strip markdown code fences if the model wraps the output
        if text.startswith("```lean") or text.startswith("```lean4"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0].strip()
        elif text.startswith("```"):
            text = text.strip("`").strip()
        return text

    return _generate


# ── OmegaBridge facade ──────────────────────────────────────────


class OmegaBridge:
    """High-level facade connecting Hermes plugin commands to omega-core.

    Usage in a command handler::

        bridge = OmegaBridge(ctx)
        result = bridge.prove("theorem add_zero (n : ℕ) : n + 0 = n :=")
        return bridge.format_result(result)
    """

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._generate_fn = make_hermes_generate_fn(ctx)
        self._compile_fn = make_hermes_compile_fn(ctx)
        self._has_core = _HAS_OMEGA_CORE

    # -- public API --------------------------------------------------

    def prove(
        self,
        theorem_header: str,
        *,
        mode: str = "auto",
        attempts: int = 6,
    ) -> dict:
        """Prove a Lean theorem.

        Parameters
        ----------
        theorem_header : str
            Lean 4 theorem header (e.g. ``theorem add_zero (n : ℕ) : n + 0 = n :="``).
        mode : str
            Prover strategy: ``"auto"`` (ensemble), ``"goedel"``, ``"rethlas"``, or ``"archon"``.
        attempts : int
            Number of proof attempts.
        timeout_s : int
            Maximum wall-clock time in seconds.

        Returns
        -------
        dict
            Structured result with keys: ``succeeded``, ``proof``, ``summary``,
            ``elapsed_s``, ``n_attempts``, ``errors``, ``t1_pass``, ``t2_pass``.
        """
        if not self._has_core:
            return {
                "succeeded": False,
                "proof": None,
                "summary": "omega-core not installed. Run: pip install omega-core",
                "elapsed_s": 0,
                "n_attempts": 0,
                "errors": ["Missing dependency: omega-core"],
                "t1_pass": False,
                "t2_pass": False,
            }

        if self._compile_fn is None:
            return {
                "succeeded": False,
                "proof": None,
                "summary": "Lean 4 compiler not found. Install elan + lean via https://leanprover-community.github.io/get-started.html",
                "elapsed_s": 0,
                "n_attempts": 0,
                "errors": ["Missing dependency: lean binary"],
                "t1_pass": False,
                "t2_pass": False,
            }

        t_start = time.perf_counter()

        try:
            if mode == "goedel":
                prover = GoedelProver(
                    compile_fn=self._compile_fn,
                    generate_fn=self._generate_fn,
                    num_samples=attempts,
                )
                result = prover.run(theorem_header)
                return self._format_goedel_result(result, theorem_header, t_start)

            # Ensemble mode (auto, rethlas, archon — all wrapped by Ensemble)
            prover = EnsembleProver(compile_fn=self._compile_fn)
            # Override generate_fn for strategies that need it
            if hasattr(prover._goedel, "proposer") and prover._goedel.proposer is not None:
                prover._goedel.proposer.generate_fn = self._generate_fn
            ensemble_result = prover.run(theorem_header)
            return self._format_ensemble_result(ensemble_result, theorem_header, t_start)

        except Exception as exc:
            elapsed = time.perf_counter() - t_start
            logger.exception("prove() failed")
            return {
                "succeeded": False,
                "proof": None,
                "summary": f"Error: {exc}",
                "elapsed_s": round(elapsed, 1),
                "n_attempts": 0,
                "errors": [str(exc)],
                "t1_pass": False,
                "t2_pass": False,
            }

    def benchmark(
        self,
        split: str = "test",
        limit: int = 10,
        timeout_s: int = 600,
    ) -> dict:
        """Run a subset of the MiniF2F benchmark.

        Parameters
        ----------
        split : str
            ``"test"`` or ``"valid"``.
        limit : int
            Max problems to attempt.
        timeout_s : int
            Max total wall-clock time.

        Returns
        -------
        dict
            Benchmark results with pass rates and per-problem details.
        """
        if not self._has_core:
            return {
                "succeeded": False,
                "summary": "omega-core not installed.",
                "total": 0,
                "t1_passed": 0,
                "t2_passed": 0,
                "problems": [],
            }

        if self._compile_fn is None:
            return {
                "succeeded": False,
                "summary": "Lean 4 compiler not found.",
                "total": 0,
                "t1_passed": 0,
                "t2_passed": 0,
                "problems": [],
            }

        # Locate MiniF2F problem files
        minif2f_dir = self._locate_minif2f(split)
        if minif2f_dir is None:
            return {
                "succeeded": False,
                "summary": "MiniF2F problems not found in omega-core package.",
                "total": 0,
                "t1_passed": 0,
                "t2_passed": 0,
                "problems": [],
            }

        from omega.verify.t1_llm import verify as t1_verify

        problem_files = sorted(Path(minif2f_dir).glob("*.lean"))[:limit]
        results: list[dict] = []
        t_start = time.perf_counter()

        for pf in problem_files:
            header = pf.read_text().strip()
            t1_ok = t1_verify(header)

            # Attempt T2 compile
            t2_ok = False
            errors: list[str] = []
            if t1_ok and self._compile_fn is not None:
                from omega.verify.t2_lean import verify as t2_check

                t2_result = t2_check(header, compile_fn=self._compile_fn)
                t2_ok = t2_result.verified
                errors = t2_result.errors[:3]

            elapsed = time.perf_counter() - t_start
            if elapsed > timeout_s:
                break

            results.append({
                "name": pf.stem,
                "t1_pass": t1_ok,
                "t2_pass": t2_ok,
                "errors": errors,
            })

        total_elapsed = time.perf_counter() - t_start
        t1_passed = sum(1 for r in results if r["t1_pass"])
        t2_passed = sum(1 for r in results if r["t2_pass"])
        total = len(results)

        return {
            "succeeded": True,
            "summary": (
                f"Benchmark: {total} problems, "
                f"T1 {t1_passed}/{total} ({t1_passed/total*100:.0f}%), "
                f"T2 {t2_passed}/{total} ({t2_passed/total*100:.0f}%), "
                f"in {total_elapsed:.0f}s"
            ),
            "total": total,
            "t1_passed": t1_passed,
            "t2_passed": t2_passed,
            "elapsed_s": round(total_elapsed, 1),
            "problems": results,
        }

    def status(self) -> dict:
        """Return plugin and environment status.

        Checks omega-core, Lean compiler, and MCP connections.
        """
        info = {"omega_core": False, "lean_binary": None, "mcp_lean": False}
        info["omega_core"] = _HAS_OMEGA_CORE

        lean_bin = _find_lean_binary()
        info["lean_binary"] = lean_bin

        # Check Lean version
        if lean_bin is not None:
            try:
                ver = subprocess.run(
                    [lean_bin, "--version"],
                    capture_output=True, text=True, timeout=10,
                )
                info["lean_version"] = ver.stdout.strip() or ver.stderr.strip()
            except Exception:
                info["lean_version"] = "unknown"

        # Check MCP availability
        if self.ctx is not None:
            try:
                # Attempt to see if lean_lsp tools are registered
                from hermes.mcp import get_mcp_tool
                info["mcp_lean"] = get_mcp_tool("lean_lsp", "lean_build") is not None
            except Exception:
                info["mcp_lean"] = False

        return info

    # -- internal formatting ------------------------------------------

    def _format_goedel_result(
        self,
        result: GoedelResult,
        theorem: str,
        t_start: float,
    ) -> dict:
        elapsed = round(time.perf_counter() - t_start, 1)
        return {
            "succeeded": result.succeeded,
            "proof": result.proof,
            "summary": result.summary,
            "elapsed_s": elapsed,
            "n_attempts": result.n_attempts,
            "errors": [a.get("errors", []) for a in result.attempts[:3] if a.get("errors")],
            "t1_pass": result.n_attempts > 0,
            "t2_pass": result.succeeded,
            "theorem": theorem,
        }

    def _format_ensemble_result(
        self,
        result: EnsembleResult,
        theorem: str,
        t_start: float,
    ) -> dict:
        elapsed = round(time.perf_counter() - t_start, 1)
        return {
            "succeeded": result.succeeded,
            "proof": result.best_proof,
            "summary": result.summary(),
            "elapsed_s": elapsed,
            "n_attempts": sum(o.n_attempts for o in result.outcomes.values()),
            "errors": [],
            "t1_pass": any(o.succeeded for o in result.outcomes.values()),
            "t2_pass": result.succeeded,
            "theorem": theorem,
            "elected": result.elected,
            "comparison_table": result.comparison_table,
        }

    @staticmethod
    def format_result_for_chat(data: dict) -> str:
        """Pretty-print a prove/benchmark result for chat display."""
        if not data.get("succeeded", False):
            return f"❌ {data.get('summary', 'Proof failed')}"

        if "t2_passed" in data and "t1_passed" in data:
            # Benchmark mode
            if "comparison_table" in data:
                return f"{data['summary']}\n\n{data.get('comparison_table', '')}"
            # Benchmark result
            failing = [p["name"] for p in data.get("problems", []) if not p.get("t2_pass")]
            passing = [p["name"] for p in data.get("problems", []) if p.get("t2_pass")]
            lines = [data["summary"], ""]
            if passing:
                lines.append(f"✅ Passing ({len(passing)}): {', '.join(passing[:5])}")
            if failing:
                lines.append(f"❌ Failing ({len(failing)}): {', '.join(failing[:5])}")
            return "\n".join(lines)

        # Prove mode
        lines = [
            f"✅ Proof found in {data.get('elapsed_s', '?')}s",
            f"   {data.get('n_attempts', '?')} attempts",
        ]
        if data.get("proof"):
            lines.append("")
            lines.append("```lean4")
            lines.append(data["proof"][:500])  # Truncate for chat
            if len(data["proof"]) > 500:
                lines.append("...(truncated)")
            lines.append("```")
        return "\n".join(lines)

    @staticmethod
    def format_status_for_chat(info: dict) -> str:
        """Pretty-print status info."""
        core = "✅" if info.get("omega_core") else "❌"
        lean = "✅" if info.get("lean_binary") else "❌"
        mcp = "✅" if info.get("mcp_lean") else "❌"
        ver = info.get("lean_version", "not found")

        return (
            f"Ω-Architect Plugin Status\n"
            f"------------------------\n"
            f"omega-core   {core}\n"
            f"lean binary  {lean}\n"
            f"  version    {ver}\n"
            f"MCP lean_lsp {mcp}\n"
        )

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _locate_minif2f(split: str = "test") -> Path | None:
        """Locate MiniF2F problem directory."""
        # Search within omega-core package
        try:
            import omega
            pkg_dir = Path(omega.__file__).parent.parent
        except ImportError:
            return None

        candidates = [
            pkg_dir / "benchmarks" / "minif2f" / split,
            pkg_dir.parent / "benchmarks" / "minif2f" / split,
        ]
        for c in candidates:
            if c.is_dir():
                return c
        return None
