"""Compile gate — local Lean + Mathlib compilation wrapper.

Wraps existing t2_real.py infrastructure with:
1. Error classification (13 classes)
2. Compilation result caching (SHA256 hash)
3. Dead loop detection

NOT a tool_call — compilation is a local gate between model responses.

Usage:
    from omega.loop.compile_gate import CompileGate

    gate = CompileGate()
    result = gate.compile(lean_code)
    if result.success:
        print("Proof verified!")
    else:
        print(f"Error: {result.error_class} at line {result.line}")
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from omega.loop.errors import (
    CompileErrorClass,
    classify_diagnostics,
)


@dataclass
class CompileResult:
    """Result from the compile gate.

    Attributes
    ----------
    success : bool
        Whether compilation passed (exit code 0).
    errors : list[str]
        Error messages (empty if success).
    error_class : CompileErrorClass or None
        Most severe error class (None if success).
    line : int
        First error line number (0 if success).
    diagnostics : list[dict]
        Full diagnostic output.
    elapsed_ms : int
        Compilation wall time.
    cached : bool
        Whether result was served from cache.
    """
    success: bool
    errors: list[str] = field(default_factory=list)
    error_class: CompileErrorClass | None = None
    line: int = 0
    diagnostics: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    cached: bool = False


class CompileGate:
    """Local Lean compilation gate with caching + error classification.

    Wraps make_real_compile_callback() from the existing t2_real.py.
    Optionally uses PersistentCompileShell for batch compilation.
    """

    def __init__(self, timeout: int = 60, cache_dir: str = "~/.cache/omega/compile/",
                 persistent: bool = False):
        self.timeout = timeout
        self.persistent = persistent
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = self.cache_dir / "cache.json"
        self._cache: dict[str, dict] = self._load_cache()
        self._compile_fn = None  # lazy init — single-shot fallback
        self._persistent_shell = None  # lazy init — persistent mode

    def _load_cache(self) -> dict[str, dict]:
        if self._cache_path.exists():
            try:
                with open(self._cache_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_cache(self):
        # Keep cache lean: max 500 entries, oldest evicted
        cache = dict(list(self._cache.items())[-500:])
        with open(self._cache_path, "w") as f:
            json.dump(cache, f)

    def _code_hash(self, code: str) -> str:
        """SHA256 hash of normalized Lean code."""
        lines2 = code.split("\n")
        normalized = "\n".join(
            line.rstrip() for line in lines2
            if line.strip() and not line.strip().startswith("--")
        )
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def compile(self, code: str) -> CompileResult:
        """Compile Lean code against Mathlib cache.

        Returns cached result if same code was compiled before.
        """
        t0 = time.perf_counter()

        # Check cache
        code_h = self._code_hash(code)
        cached = self._cache.get(code_h)
        if cached is not None:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return CompileResult(
                success=cached["success"],
                errors=cached.get("errors", []),
                error_class=CompileErrorClass(cached["error_class"]) if cached.get("error_class") else None,
                line=cached.get("line", 0),
                diagnostics=cached.get("diagnostics", []),
                elapsed_ms=cached.get("elapsed_ms", 0),
                cached=True,
            )

        # Lazy init — persistent shell or single-shot callback
        if self.persistent:
            if self._persistent_shell is None:
                from omega.loop.persistent_shell import PersistentCompileShell
                self._persistent_shell = PersistentCompileShell(timeout=self.timeout)
                self._persistent_shell.start()
            compiled = self._persistent_shell.compile(code)
            # Add elapsed_ms if not present (persistent shell already includes it)
            if "elapsed_ms" not in compiled:
                compiled["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        else:
            if self._compile_fn is None:
                from omega.verify.t2_real import make_real_compile_callback
                self._compile_fn = make_real_compile_callback(timeout=self.timeout)
            try:
                compiled_raw = self._compile_fn(code)
            except Exception as e:
                elapsed = int((time.perf_counter() - t0) * 1000)
                result = CompileResult(
                    success=False,
                    errors=[f"Compile exception: {e}"],
                    error_class=CompileErrorClass.OTHER,
                    elapsed_ms=elapsed,
                )
                self._cache[code_h] = {
                    "success": False,
                    "errors": result.errors,
                    "error_class": result.error_class.value if result.error_class else None,
                    "line": 0,
                    "elapsed_ms": elapsed,
                }
                self._save_cache()
                return result
            compiled = compiled_raw

        elapsed = compiled.get("elapsed_ms", int((time.perf_counter() - t0) * 1000))
        diagnostics = compiled.get("diagnostics", [])
        exit_code = compiled.get("exit_code", -1)

        # Extract errors
        errors = [d["message"] for d in diagnostics if d.get("severity") == "error"]

        # Classify — pick the most severe error class (prefer over NO_ERROR)
        cls_counts = classify_diagnostics(diagnostics)
        dominant_cls = None
        if not cls_counts and exit_code != 0:
            # Failed compile with no diagnostics at all: the failure is the fact.
            dominant_cls = CompileErrorClass.OTHER
        elif cls_counts:
            # Prefer error classes over NO_ERROR
            error_classes = {k: v for k, v in cls_counts.items()
                           if k not in (CompileErrorClass.NO_ERROR, CompileErrorClass.OTHER)}
            if error_classes:
                dominant_cls = max(error_classes, key=lambda k: error_classes[k])
            else:
                # Fallback must never land on NO_ERROR for a *failed* compile: Lean
                # already flagged an error, the message text simply matched no named
                # class - which is exactly what OTHER means. Emptying the named pool
                # then taking max(cls_counts) used to pick NO_ERROR whenever the
                # info-level context lines outnumbered the single error line (e.g.
                # `decide` on a false proposition: 1 error + 2 info lines), and
                # ERROR_PROXIMITY[NO_ERROR] = 1.0 valued that dead end like a
                # finished proof. Regression: tests/test_compile_gate_error_class.py
                pool = cls_counts
                if exit_code != 0:
                    # A failed compile can never be NO_ERROR. If every diagnostic
                    # is info-level (nothing Lean flagged as an error), the empty
                    # pool must become OTHER explicitly - falling back to
                    # ``or cls_counts`` would silently resurrect NO_ERROR, which
                    # is how the residual miss (3/257 compiles, real corpus)
                    # survived the first fix. Regression:
                    # tests/test_compile_gate_error_class.py::test_*_is_other
                    pool = {k: v for k, v in cls_counts.items()
                            if k is not CompileErrorClass.NO_ERROR}
                    if not pool:
                        pool = {CompileErrorClass.OTHER: 1}
                dominant_cls = max(pool, key=lambda k: pool[k])

        # Find first error line
        first_line = 0
        for d in diagnostics:
            if d.get("severity") == "error":
                first_line = d.get("line", 0)
                break

        result = CompileResult(
            success=(exit_code == 0),
            errors=errors,
            error_class=dominant_cls,
            line=first_line,
            diagnostics=diagnostics,
            elapsed_ms=elapsed,
        )

        # Cache
        self._cache[code_h] = {
            "success": result.success,
            "errors": result.errors,
            "error_class": result.error_class.value if result.error_class else None,
            "line": result.line,
            "diagnostics": result.diagnostics,
            "elapsed_ms": result.elapsed_ms,
        }
        self._save_cache()

        return result

    def close(self) -> None:
        """关闭持久 shell（如果有）。"""
        if self._persistent_shell is not None:
            self._persistent_shell.close()
            self._persistent_shell = None
