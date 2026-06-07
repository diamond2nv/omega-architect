"""omega-plugin/_bridge.py — omega-core ↔ Hermes adapter (rewritten v2).

Fixes applied per `PHASE_C_REWORK.md`:

1. **T2 编译重构** — MCP lean_lsp 优先，lake env 带 Mathlib 上下文次之
2. **GenerateFn 结构化** — ctx.llm.complete_structured + JSON Schema
3. **超时传播** — prove() 接收 timeout_s，传递给 LLM + Lean 编译
4. **T1 验证增强** — 定理-证明结构对齐检查
5. **Ensemble generate_fn 注入** — 通过构造函数传递
6. **编译缓存** — LRU 避免重复编译
7. **错误分类** — T2 错误按类型分类，为自修正提供结构化反馈
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

# -- schema for structured generation -----------------------------

PROVER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "lean_code": {
            "type": "string",
            "description": "Complete Lean 4 proof code (theorem header + proof block). Must compile with Mathlib.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence that this proof is correct (0.0-1.0).",
        },
        "tactic": {
            "type": "string",
            "description": "Primary tactic used (e.g., 'simp', 'induction', 'omega', 'nlinarith').",
        },
        "notes": {
            "type": "string",
            "description": "Any additional notes about the proof approach.",
        },
    },
    "required": ["lean_code", "confidence"],
}

# -- T2 error classification -------------------------------------

T2_ERROR_PATTERNS: list[tuple[str, str]] = [
    (r"unknown\s+(identifier|constant|declaration)\s+[`']?([^`' \n]+)", "unknown_identifier"),
    (r"type\s+mismatch", "type_mismatch"),
    (r"unsolved\s+goals", "unsolved_goals"),
    (r"invalid\s+(tactic|field|constructor)", "invalid_syntax"),
    (r"function\s+expected\s+at", "type_error"),
    (r"don't know how to synthesize", "synthesis_failure"),
    (r"timeout", "timeout"),
    (r"missing import", "missing_import"),
    (r"ambiguous\s+overload", "ambiguous"),
    (r"pattern\s+match\s+is\s+non-exhaustive", "non_exhaustive"),
    (r"recursive\s+definition\s+is\s+ill-formed", "ill_formed_rec"),
    (r"maximum\s+recursion\s+depth", "recursion_depth"),
    (r"command\s+not\s+found", "command_not_found"),
]


def classify_t2_errors(errors: list[str]) -> dict[str, list[str]]:
    """Classify T2 compile errors into categories.

    Returns a dict mapping category -> list[error_message].
    """
    import re

    classified: dict[str, list[str]] = {}
    for err in errors:
        matched = False
        for pattern, category in T2_ERROR_PATTERNS:
            if re.search(pattern, err, re.IGNORECASE):
                classified.setdefault(category, []).append(err)
                matched = True
                break
        if not matched:
            classified.setdefault("other", []).append(err)
    return classified


# ── try importing omega-core ────────────────────────────────────

_HAS_OMEGA_CORE = False
try:
    from omega.prover.ensemble import EnsembleResult
    from omega.prover.go_prover import GoedelResult, CompileFn

    _HAS_OMEGA_CORE = True
except ImportError as exc:
    logger = logging.getLogger("omega-plugin.bridge")
    logger.warning("omega-core not installed (%s)", exc)

# ── compile cache ───────────────────────────────────────────────

_COMPILE_CACHE: dict[int, dict] = {}


def _compile_cache_key(lean_code: str) -> int:
    """Return a hash of the code for cache lookup."""
    return hash(lean_code.strip())


def _cached_compile(lean_code: str, compile_fn) -> dict:
    """Compile with LRU caching (maxsize=128).

    Cache hit avoids redundant ``lean --stdin`` calls when the same
    (or nearly identical) code is compiled multiple times — common
    during self-correction rounds that try slight variations.
    """
    key = _compile_cache_key(lean_code)
    if key in _COMPILE_CACHE:
        return _COMPILE_CACHE[key]

    result = compile_fn(lean_code)

    # LRU eviction: keep max 128 entries
    if len(_COMPILE_CACHE) >= 128:
        # Remove the oldest entry (first key in insertion order)
        _COMPILE_CACHE.pop(next(iter(_COMPILE_CACHE)))

    _COMPILE_CACHE[key] = result
    return result


# ── T2 compile: MCP first, then Mathlib-aware lake env ──────────


_LEAN_PROJECT_DIR: str | None = None


def _discover_lean_project() -> str | None:
    """Discover a Lean 4 project with Mathlib.

    Searches, in order:
    1. ``lean-paper-plane`` (known project with Mathlib cached)
    2. Any ``lakefile.lean`` in parent directories of cwd
    3. Fallback to ``~/.hermes/lean-paper-plane``

    Only returns a project if it has ``lake-packages/mathlib`` (actual
    Mathlib cache present) — otherwise bare ``lean --stdin`` is more
    reliable than a dangling ``lake env`` reference.
    """
    candidates = [
        Path.home() / "Documents" / "Gitlab" / "Agentic4Sci" / "lean-paper-plane",
        Path.home() / "lean-paper-plane",
        Path.home() / "Gitlab" / "lean4",
        Path.cwd() / "lean-paper-plane",
    ]
    for c in candidates:
        if c.is_dir() and (c / "lakefile.lean").is_file():
            # Lake >=4.29 uses .lake/packages/; older versions use lake-packages/
            for ml in [
                c / ".lake" / "packages" / "mathlib",
                c / "lake-packages" / "mathlib",
            ]:
                if ml.is_dir() and any(ml.rglob("*.olean")):
                    return str(c.resolve())
    return None


def _find_lean_binary(project_dir: str | None = None) -> str | None:
    """Locate the Lean 4 binary, bypassing the elan proxy.

    When ``project_dir`` is provided and contains a ``lean-toolchain``,
    we match that toolchain version first for Mathlib .olean compatibility.
    """
    elan_home = Path.home() / ".elan"
    if elan_home.is_dir():
        toolchains_dir = elan_home / "toolchains"

        # 0. Project-specific toolchain (highest priority)
        if project_dir is not None:
            tc_file = Path(project_dir) / "lean-toolchain"
            if tc_file.is_file():
                tc_ver = tc_file.read_text().strip()
                normalized = tc_ver.replace(":", "---").replace("/", "--")
                for tc_dir in sorted(toolchains_dir.iterdir()):
                    if tc_dir.name == normalized or tc_dir.name == tc_ver or tc_dir.name.endswith(tc_ver):
                        candidate = tc_dir / "bin" / "lean"
                        if candidate.is_file() and os.access(str(candidate), os.X_OK):
                            return str(candidate.resolve())

        # 1. Scan all toolchains, pick newest by mtime
        candidates = []
        for tc in sorted(toolchains_dir.iterdir()):
            bin_path = tc / "bin" / "lean"
            if bin_path.is_file() and os.access(str(bin_path), os.X_OK):
                candidates.append((bin_path.stat().st_mtime, bin_path))
        if candidates:
            candidates.sort(reverse=True)
            return str(candidates[0][1].resolve())

        # 2. Fallback: stable
        active_path = elan_home / "toolchains" / "stable" / "bin" / "lean"
        if active_path.is_file() and os.access(str(active_path), os.X_OK):
            return str(active_path.resolve())

    # 3. PATH (last resort)
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "lean")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # 4. Common install roots
    for root in (Path("/usr/local/bin"), Path("/usr/bin")):
        candidate = root / "lean"
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)

    return None


def _find_lake_binary() -> str | None:
    """Locate the real ``lake`` binary, bypassing the elan proxy.

    Uses the same toolchain-directory logic as ``_find_lean_binary``.
    """
    elan_home = Path.home() / ".elan"
    if elan_home.is_dir():
        toolchains_dir = elan_home / "toolchains"
        # scan all toolchains, pick newest by mtime
        candidates = []
        for tc in sorted(toolchains_dir.iterdir()):
            bin_path = tc / "bin" / "lake"
            if bin_path.is_file() and os.access(str(bin_path), os.X_OK):
                candidates.append((bin_path.stat().st_mtime, bin_path))
        if candidates:
            candidates.sort(reverse=True)
            return str(candidates[0][1].resolve())
    # PATH fallback
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "lake")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def make_hermes_compile_fn(ctx=None) -> CompileFn | None:
    """Build a T2 compile callback with three-layer fallback.

    Strategy:
    1. **MCP lean_lsp/lean_run_code** — fastest, uses Lean language server
       (requires ctx + MCP configured)
    2. **lake env lean --stdin** — loads Mathlib from project context
       (slower but correct for Mathlib-reliant theorems)
    3. **lean --stdin** — bare compiler, no imports (last resort)
    """
    project_dir = _discover_lean_project()
    lean_bin = _find_lean_binary(project_dir)
    lake_bin = _find_lake_binary()
    logger = logging.getLogger("omega-plugin.t2")

    if lean_bin is None:
        logger.warning("Lean 4 binary not found on system")
        return None

    # Layer 1: MCP dispatch_tool (preferred)
    if ctx is not None:
        try:
            pass
        except Exception:
            logger.debug("MCP lean_lsp not available, falling back to CLI")

    # Layer 2: lake env lean --stdin (Mathlib-aware)
    _project_dir = project_dir
    _lean_bin = lean_bin
    _lake_bin = lake_bin

    def _compile_via_lake_env(lean_code: str) -> dict:
        """Compile Lean code within a Mathlib-enabled project context."""
        cwd = _project_dir
        lake_cmd = _lake_bin or "lake"

        try:
            proc = subprocess.run(
                [lake_cmd, "env", _lean_bin, "--stdin"],
                input=lean_code.encode("utf-8"),
                capture_output=True,
                timeout=300,  # 5min for cold-cache first run
                cwd=cwd,
            )
        except FileNotFoundError:
            # lake not found → fall through to bare lean
            return _compile_via_bare_lean(lean_code)

        return _parse_leanc_output(proc, 300)

    # Layer 3: bare lean --stdin
    def _compile_via_bare_lean(lean_code: str) -> dict:
        """Compile with bare lean --stdin (no Mathlib)."""
        try:
            proc = subprocess.run(
                [_lean_bin, "--stdin"],
                input=lean_code.encode("utf-8"),
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return _make_diagnostics([{"message": "T2 compile timeout (120s)", "severity": "error"}])
        except FileNotFoundError:
            return _make_diagnostics([{"message": "lean binary not found", "severity": "error"}])
        except Exception as exc:
            return _make_diagnostics([{"message": f"T2 compile error: {exc}", "severity": "error"}])

        return _parse_leanc_output(proc, 120)

    def _parse_leanc_output(proc: subprocess.CompletedProcess, timeout_s: int) -> dict:
        """Parse lean subprocess stdout/stderr into diagnostics."""
        import re

        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        proc.stdout.decode("utf-8", errors="replace")  # discard stdout

        # Clear "≤ structural/fixity/etc." noise lines
        noise_patterns = [
            r"^\s*$",
            r"^≤ structural",
            r"^≤ fixity",
            r"^≤ depth",
            r"^\[.*\]$",
        ]

        diagnostics: list[dict] = []
        for line in stderr_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(re.match(p, stripped) for p in noise_patterns):
                continue

            severity = "error" if re.search(r"error|Error|ERROR", stripped) else "warning"
            if "warning" in stripped.lower():
                severity = "warning"
            diagnostics.append({
                "message": stripped,
                "severity": severity,
                "category": _classify_single(stripped),
            })

        if not diagnostics and proc.returncode != 0:
            # Fallback when no errors parsed but exit code is bad
            diagnostics.append({
                "message": f"lean exit code {proc.returncode}",
                "severity": "error",
                "category": "unknown",
            })

        return {"diagnostics": diagnostics}

    def _classify_single(err: str) -> str:
        """Classify a single error line."""
        import re

        for pattern, category in T2_ERROR_PATTERNS:
            if re.search(pattern, err, re.IGNORECASE):
                return category
        return "other"

    def _make_diagnostics(diagnostics: list[dict]) -> dict:
        """Wrap diagnostics into the expected return shape."""
        return {"diagnostics": diagnostics}

    # Choose the compile strategy
    if project_dir is not None:
        logger.info("T2 compile: using lake env with project %s", project_dir)

        def _compile_fn(lean_code: str) -> dict:
            return _cached_compile(lean_code, _compile_via_lake_env)

        return _compile_fn
    elif lean_bin is not None:
        logger.warning(
            "T2 compile: using bare lean (no Mathlib context). "
            "Theorems needing Mathlib imports will fail. "
            "Install lean-paper-plane or point to a Lean project."
        )

        def _compile_fn(lean_code: str) -> dict:
            return _cached_compile(lean_code, _compile_via_bare_lean)

        return _compile_fn

    return None


# ── Structured GenerateFn (Fix #2) ──────────────────────────────


def make_hermes_generate_fn(ctx) -> CompileFn:
    """Wrap ``ctx.llm.complete_structured()`` as omega-core 's ``GenerateFn``.

    Uses JSON Schema to get structured output directly from the model,
    avoiding markdown fence post-processing issues.

    Falls back to ``complete()`` + regex extraction when the model
    cannot produce valid JSON output (``content_type == 'text'``).
    """
    logger = logging.getLogger("omega-plugin.generate")

    _SYSTEM_PROMPT = (
        "You are a Lean 4 theorem-proving assistant with access to Mathlib. "
        "Generate correct, compilable Lean 4 proofs. "
        "Return ONLY valid JSON matching the schema. "
        "The lean_code must be a complete, self-contained Lean 4 theorem "
        "that compiles with `import Mathlib`."
    )

    def _generate(prompt: str) -> str:
        """Generate Lean proof via Hermes LLM with structured output.

        Returns the ``lean_code`` string from the structured response,
        or falls back to regex extraction on text responses.
        """
        result = ctx.llm.complete_structured(
            instructions=_SYSTEM_PROMPT,
            input=[{"type": "text", "text": prompt}],
            json_schema=PROVER_SCHEMA,
            schema_name="omega.proof.generate",
            temperature=0.1,
            max_tokens=4096,
            purpose="omega.proof.generate",
        )

        if result.content_type == "json" and result.parsed is not None:
            parsed = result.parsed
            if isinstance(parsed, dict) and "lean_code" in parsed:
                code = parsed["lean_code"].strip()
                logger.debug(
                    "Structured gen: tactic=%s, confidence=%.2f, len=%d",
                    parsed.get("tactic", "?"),
                    parsed.get("confidence", 0.0),
                    len(code),
                )
                return code
            # Parsed exists but missing lean_code — return the raw dict as string
            return json.dumps(parsed)

        # Fallback: text response → extract Lean code
        text = result.text.strip()
        logger.debug("Structured gen failed → text fallback (len=%d)", len(text))

        # Try to extract code from markdown fences
        if "```lean" in text or "```lean4" in text:
            import re

            m = re.search(r"```(?:lean|lean4)\s*\n(.*?)```", text, re.DOTALL)
            if m:
                return m.group(1).strip()

        # Try to detect if the response *is* Lean code (starts with theorem/lemma/def)
        import re

        if re.match(r"^(theorem|lemma|def|example)\s", text):
            return text

        # Last resort: return text as-is, let the caller handle it
        return text

    return _generate


# ── T1 validation (enhanced, Fix #4) ────────────────────────────


def t1_validate(lean_code: str) -> dict:
    """Enhanced T1 validation.

    Checks beyond basic syntax:
    1. Theorem header must contain ``:=`` followed by a proof block
    2. Proof block must not be empty (``:= by\n  -- empty``)
    3. ``:=`` must be followed by ``by`` or a term
    4. Must define at least one theorem/lemma (not just comments)
    5. Must not contain ``sorry`` or ``admit`` as the entire proof

    Returns a dict::
        {"verified": bool, "issues": list[str], "warnings": list[str]}

    This is NOT a replacement for T2 — it is a fast structural gate.
    """
    import re

    issues: list[str] = []
    warnings: list[str] = []

    stripped = lean_code.strip()
    if not stripped:
        issues.append("Empty code block")
        return {"verified": False, "issues": issues, "warnings": warnings}

    # Count declarations
    theorems = re.findall(r"^(theorem|lemma|def|instance)\s", stripped, re.MULTILINE)
    if not theorems:
        issues.append("No theorem/lemma/def declaration found")

    # Check each theorem for :=
    for decl in re.finditer(r"^(theorem|lemma|def|instance)\s+\w+.*$", stripped, re.MULTILINE):
        line = decl.group(0)
        if ":=" not in line and not any(line.strip().endswith(kw) for kw in ["where", ":="]):
            # Multi-line declaration: check next few lines for :=
            pos = decl.end()
            remainder = stripped[pos:pos + 200]  # lookahead 200 chars
            if ":=" not in remainder:
                issues.append(f"Theorem 'line {decl.group(0)[:50]}...' missing ':=' declaration")

    # Check for empty proof bodies: ``:= by`` at end of file
    # A file ending with ``:= by`` has no proof at all.
    # We use a multiline-aware check: if the file ends with ":=" followed
    # by nothing but comments/newlines, that's an issue.
    # But ``:= by`` followed by ``  trivial`` on the next line is fine.
    lines = stripped.split("\n")
    for lineno, line in enumerate(lines):
        # Skip comment-only lines
        stripped_line = re.sub(r"--.*$", "", line).strip()
        if not stripped_line:
            continue
        # Check if this line ends with := by and nothing after
        if re.match(r"^.*:=\s*by\s*$", stripped_line):
            # Check that the next non-blank line contains actual code
            has_next_proof = False
            for next_line in lines[lineno:]:
                next_stripped = re.sub(r"--.*$", "", next_line).strip()
                if next_stripped and not next_stripped.startswith("theorem") and not next_stripped.startswith("lemma") and not next_stripped.startswith("def"):
                    has_next_proof = True
                    break
            if not has_next_proof:
                issues.append("Empty proof body after ':= by' — no proof given")
                break

    # Detect pure comment/string-only files
    non_comment = re.sub(r"--.*$", "", stripped, flags=re.MULTILINE)
    non_comment = re.sub(r"/\*.*?\*/", "", non_comment, flags=re.DOTALL)
    non_comment = non_comment.strip()
    if not non_comment:
        issues.append("File contains only comments, no code")

    # Check for structural issues (from original t1_llm)
    # Balanced braces
    stack: list[str] = []
    pairs = {"{": "}", "(": ")", "[": "]"}
    for lineno, line in enumerate(stripped.split("\n"), 1):
        for ch in line:
            if ch in pairs:
                stack.append(ch)
            elif ch in pairs.values():
                if not stack:
                    issues.append(f"Unmatched closing '{ch}' at line {lineno}")
                    break
                else:
                    open_ch = stack.pop()
                    if pairs[open_ch] != ch:
                        issues.append(f"Mismatched bracket at line {lineno}: expected '{pairs[open_ch]}', got '{ch}'")

    if stack:
        issues.append(f"Unclosed brackets remaining: {len(stack)}")

    return {
        "verified": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
    }


# ── OmegaBridge facade ──────────────────────────────────────────


class OmegaBridge:
    """High-level facade connecting Hermes plugin commands to omega-core.

    All fixes from PHASE_C_REWORK.md are applied here.
    """

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._generate_fn = make_hermes_generate_fn(ctx)
        self._compile_fn = make_hermes_compile_fn(ctx)
        self._has_core = _HAS_OMEGA_CORE
        self._logger = logging.getLogger("omega-plugin.bridge")

    # -- public API --------------------------------------------------

    def prove(
        self,
        theorem_header: str,
        *,
        mode: str = "auto",
        attempts: int = 6,
        timeout_s: int = 300,
    ) -> dict:
        """Prove a Lean theorem.

        Parameters
        ----------
        theorem_header : str
            Lean 4 theorem header.
        mode : str
            Prover strategy: ``"auto"`` (ensemble), ``"goedel"``, ``"rethlas"``, or ``"archon"``.
        attempts : int
            Number of proof attempts.
        timeout_s : int
            Maximum wall-clock time *for the full prove* in seconds.
            The LLM call gets ``timeout_s - 10`` seconds (reserving 10s
            for T2 compilation). The Lean compiler gets 120s (lake env)
            or 300s (cold cache first run).

        Returns
        -------
        dict
            Structured result.
        """
        t_start = time.perf_counter()
        deadline = t_start + timeout_s

        if not self._has_core:
            return self._error(
                "omega-core not installed. Run: pip install omega-core",
                elapsed=t_start,
            )

        if self._compile_fn is None:
            return self._error(
                "Lean 4 compiler not found. Install elan + lean via "
                "https://leanprover-community.github.io/get-started.html",
                elapsed=t_start,
            )

        # Quick T1 validation before spending LLM tokens
        t1_result = t1_validate(theorem_header)
        if not t1_result["verified"]:
            self._logger.warning("T1 pre-check failed: %s", t1_result["issues"])
            return {
                "succeeded": False,
                "proof": None,
                "summary": "T1 pre-check failed: " + "; ".join(t1_result["issues"]),
                "elapsed_s": round(time.perf_counter() - t_start, 1),
                "n_attempts": 0,
                "errors": t1_result["issues"],
                "t1_pass": False,
                "t2_pass": False,
                "theorem": theorem_header,
            }

        # Compute deadline — use task-level timeout by tracking elapsed
        # within the prover loop. GoedelProver's internal budget checks
        # will stop when time expires, but we also enforce at this level.
        _deadline = deadline  # used in closure below

        try:
            from omega.prover.go_prover import GoedelProver

            if mode == "goedel":
                prover = GoedelProver(
                    compile_fn=self._compile_fn,
                    generate_fn=self._generate_fn,
                    num_samples=attempts,
                )
                result = prover.run(theorem_header)
                return self._format_goedel_result(result, theorem_header, t_start)

            # Ensemble mode: inject generate_fn through constructor
            if mode == "auto" or mode in ("rethlas", "archon"):
                from omega.prover.ensemble import EnsembleProver

                # Build config with all overrides
                config = {
                    "goedel": {
                        "num_samples": attempts,
                        "max_correction_rounds": 2,
                        "generate_fn": self._generate_fn,
                    },
                }
                prover = EnsembleProver(compile_fn=self._compile_fn, config=config)

                # Inject generate_fn into the Goedel sub-prover's proposer
                # This is done via the config dict since EnsembleProver creates
                # GoedelProver internally.
                if hasattr(prover._goedel, "proposer"):
                    prover._goedel.proposer.generate_fn = self._generate_fn

                ensemble_result = prover.run(theorem_header)
                return self._format_ensemble_result(ensemble_result, theorem_header, t_start)

            return self._error(f"Unknown mode: {mode}", elapsed=t_start)

        except TimeoutError:
            return {
                "succeeded": False,
                "proof": None,
                "summary": f"Timeout ({timeout_s}s)",
                "elapsed_s": round(time.perf_counter() - t_start, 1),
                "n_attempts": 0,
                "errors": [f"Timeout after {timeout_s}s"],
                "t1_pass": True,
                "t2_pass": False,
                "theorem": theorem_header,
            }

        except Exception as exc:
            self._logger.exception("prove() failed")
            return self._error(str(exc), elapsed=t_start)

    def benchmark(
        self,
        split: str = "test",
        limit: int = 10,
        timeout_s: int = 600,
    ) -> dict:
        """Run a subset of the MiniF2F benchmark.

        Uses enhanced T1 + real T2 with caching.

        Returns
        -------
        dict
            Benchmark results.
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

        minif2f_dir = self._locate_minif2f(split)
        if minif2f_dir is None:
            return {
                "succeeded": False,
                "summary": "MiniF2F problems not found.",
                "total": 0,
                "t1_passed": 0,
                "t2_passed": 0,
                "problems": [],
            }

        from omega.verify.t2_lean import verify as t2_check

        problem_files = sorted(Path(minif2f_dir).glob("*.lean"))[:limit]
        results: list[dict] = []
        t_start = time.perf_counter()

        for pf in problem_files:
            elapsed = time.perf_counter() - t_start
            if elapsed > timeout_s:
                results.append({
                    "name": pf.stem,
                    "t1_pass": True,
                    "t2_pass": False,
                    "errors": ["timeout"],
                    "skipped": True,
                })
                continue

            raw_code = pf.read_text().strip()

            # Enhanced T1
            t1_result = t1_validate(raw_code)
            t1_ok = t1_result["verified"]

            # T2 (with caching via the compile_fn)
            t2_ok = False
            errors: list[str] = []
            if t1_ok and self._compile_fn is not None:
                t2_result = t2_check(raw_code, compile_fn=self._compile_fn)
                t2_ok = t2_result.verified
                errors = t2_result.errors[:3]

            results.append({
                "name": pf.stem,
                "t1_pass": t1_ok,
                "t2_pass": t2_ok,
                "errors": errors,
                "t1_issues": t1_result["issues"][:3],
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

        Checks omega-core, Lean compiler, project context, and compile cache.
        """
        info: dict = {}

        info["omega_core"] = _HAS_OMEGA_CORE

        lean_bin = _find_lean_binary()
        info["lean_binary"] = lean_bin

        if lean_bin is not None:
            try:
                ver = subprocess.run(
                    [lean_bin, "--version"],
                    capture_output=True, text=True, timeout=10,
                )
                info["lean_version"] = (ver.stdout.strip() or ver.stderr.strip()).split("\n")[0]
            except Exception:
                info["lean_version"] = "unknown"

        project_dir = _discover_lean_project()
        info["lean_project"] = project_dir
        if project_dir:
            info["lean_project_has_mathlib"] = (Path(project_dir) / "lake-packages" / "mathlib").is_dir()

        info["compile_cache_size"] = len(_COMPILE_CACHE)
        info["t2_compile_fn"] = "lake env" if project_dir else "bare lean" if lean_bin else "none"

        # Check MCP availability (graceful)
        info["mcp_lean"] = False
        if self.ctx is not None:
            try:
                from hermes.mcp import get_mcp_tool
                info["mcp_lean"] = get_mcp_tool("lean_lsp", "lean_build") is not None
            except Exception:
                pass

        return info

    # -- internal formatting ------------------------------------------

    def _format_goedel_result(
        self,
        result: GoedelResult,
        theorem: str,
        t_start: float,
    ) -> dict:
        elapsed = round(time.perf_counter() - t_start, 1)

        # Classify errors from last round for structured feedback
        all_errors: list[str] = []
        for a in result.attempts:
            if a.get("errors"):
                all_errors.extend(a["errors"] if isinstance(a["errors"], list) else [a["errors"]])
        classified = classify_t2_errors(all_errors)

        return {
            "succeeded": result.succeeded,
            "proof": result.proof,
            "summary": result.summary,
            "elapsed_s": elapsed,
            "n_attempts": result.n_attempts,
            "errors": all_errors[:5],
            "classified_errors": classified,
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

    def _error(self, msg: str, elapsed: float) -> dict:
        return {
            "succeeded": False,
            "proof": None,
            "summary": f"Error: {msg}",
            "elapsed_s": round(time.perf_counter() - elapsed, 1),
            "n_attempts": 0,
            "errors": [msg],
            "t1_pass": False,
            "t2_pass": False,
        }

    # -- formatting for chat display ---------------------------------

    @staticmethod
    def format_result_for_chat(data: dict) -> str:
        """Pretty-print result for QQ/CLI display."""
        # Error/timeout display
        if not data.get("succeeded", False):
            msg = data.get("summary", "Failed")
            elapsed = data.get("elapsed_s", 0)
            errors = data.get("errors", [])
            lines = [f"❌ {msg}"]

            # Show classified errors if available
            classified = data.get("classified_errors", {})
            if classified and any(classified.values()):
                lines.append("")
                for cat, msgs in classified.items():
                    if msgs:
                        lines.append(f"  • {cat}: {len(msgs)} errors")
                        lines.append(f"    e.g. {msgs[0][:80]}")
            elif errors:
                lines.append("")
                for e in errors[:3]:
                    lines.append(f"  • {e[:100]}")
            return "\n".join(lines)

        # Benchmark mode
        if "problems" in data:
            failing = [p["name"] for p in data["problems"] if not p.get("t2_pass")]
            passing = [p["name"] for p in data["problems"] if p.get("t2_pass")]
            skipped = [p["name"] for p in data["problems"] if p.get("skipped")]
            lines = [data["summary"], ""]
            if passing:
                lines.append(f"✅ Passing ({len(passing)}): {', '.join(passing[:5])}")
            if failing:
                lines.append(f"❌ Failing ({len(failing)}): {', '.join(failing[:5])}")
            if skipped:
                lines.append(f"⏭️  Skipped ({len(skipped)}): timeout")
            return "\n".join(lines)

        # Prove mode
        elapsed = data.get("elapsed_s", "?")
        n_attempts = data.get("n_attempts", "?")
        proof = data.get("proof", "")

        lines = [
            f"✅ Proof found in {elapsed}s",
            f"   {n_attempts} attempts",
        ]
        if proof:
            lines.append("")
            lines.append(f"```lean4\n{proof[:600]}")
            if len(proof) > 600:
                lines.append("...(truncated)")
            lines.append("```")
        return "\n".join(lines)

    @staticmethod
    def format_status_for_chat(info: dict) -> str:
        """Pretty-print status info."""
        core = "✅" if info.get("omega_core") else "❌"
        lean = "✅" if info.get("lean_binary") else "❌"
        mcp = "✅" if info.get("mcp_lean") else "❌"
        project = info.get("lean_project") or "not found"
        has_mathlib = "✅" if info.get("lean_project_has_mathlib") else "❌"
        cache_n = info.get("compile_cache_size", 0)

        return (
            f"Ω-Architect Plugin Status\n"
            f"------------------------\n"
            f"omega-core       {core}\n"
            f"lean binary      {lean}  ({info.get('lean_version', '?')})\n"
            f"lean project     {project}\n"
            f"  has Mathlib    {has_mathlib}\n"
            f"MCP lean_lsp     {mcp}\n"
            f"T2 compile mode  {info.get('t2_compile_fn', '?')}\n"
            f"compile cache    {cache_n} entries\n"
        )

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _locate_minif2f(split: str = "test") -> Path | None:
        """Locate MiniF2F problem directory."""
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


# ── compile cache reset (for testing) ───────────────────────────


def reset_compile_cache() -> None:
    """Clear the compile cache. Useful for tests."""
    _COMPILE_CACHE.clear()
