#!/usr/bin/env python3
"""Inner Loop — integrated optimization pass (search fix + MCP feedback + sub-lemma growth + adaptive strategy)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omega.loop.compile_gate import CompileGate, CompileResult
from omega.loop.compress import compress_messages
from omega.loop.dialogue_cache import DialogueCache
from omega.loop.deepseek_client import (
    DeepSeekClient,
    DeepSeekResponse,
    _DEFAULT_TOOLS,
    _SEARCH_TOOL_NAMES,
    _NON_SEARCH_TOOL_NAMES,
)
from omega.loop.errors import (
    CompileErrorClass,
    classify_diagnostics,
    is_dead_loop,
)
from omega.loop.mcp_sync import PersistentMcpClient, McpToolResult
from omega.resource.budget import BudgetTracker
from omega.resource.tracker import ConvergenceTracker

logger = logging.getLogger("omega.loop.inner")


# ═══════════════════════════════════════════════════════════════
# Prompt & Strategy templates
# ═══════════════════════════════════════════════════════════════

_INNER_SYSTEM_PROMPT = """You are a Lean 4 theorem proving agent.

# Core rules
1. WRITE CODE FIRST. Do NOT search unless you're stuck on a specific lemma name.
2. Each response must contain a COMPLETE Lean proof, not a search query.
3. Use these tactics for common patterns:
   - `native_decide` for Nat/Int decidable calculations
   - `nlinarith` for polynomial inequalities over ℝ/ℚ
   - `field_simp` for ℝ denominators, then `nlinarith`/`ring`
   - `ring` for ring algebra
   - `induction n` then `simp` for ℕ induction
   - `calc` for chains of equalities/inequalities
   - `omega` for linear arithmetic over ℕ
4. Output code in ```lean4 ... ``` blocks.
5. If compilation fails, read the error carefully and fix ONLY the specific issue.
6. If you have multiple possible tactics to try, use lean_multi_attempt to test them all at once (faster).
7. Use lean_run_code to quickly test proof snippets before finalizing.

# Available tools (use sparingly, max 1 search before writing code):
- lean_loogle: search Mathlib by type signature
- lean_leansearch: search Mathlib by natural language
- lean_multi_attempt: try multiple proof tactics at once (fast)
- lean_run_code: compile a Lean snippet quickly"""


# ── Adaptive strategy hints by error class ────────────────────

_ERROR_STRATEGY_HINTS: dict[CompileErrorClass, str] = {
    CompileErrorClass.TYPE_MISMATCH: (
        "Type mismatch. Try:\n"
        "- `rw [lemma]` to rewrite the target into the expected type\n"
        "- `calc` block to show each transformation step\n"
        "- `apply lemma` if the target matches a known result\n"
        "- `simpa` if the goal follows from the hypotheses with simplification"
    ),
    CompileErrorClass.SYNTAX_ERROR: (
        "Syntax error. Check:\n"
        "- Missing parentheses or brackets\n"
        "- Incorrect `:=` vs `by` syntax\n"
        "- Imports: make sure all required modules are imported\n"
        "- Stray characters or unbalanced quotes"
    ),
    CompileErrorClass.UNKNOWN_IDENT: (
        "Unknown identifier. Either:\n"
        "- Add the correct `import` (e.g., `import Mathlib.Data.Nat.Basic`)\n"
        "- Use the module-qualified name (e.g., `Nat.add_comm`)\n"
        "- Define the missing lemma before the main theorem"
    ),
    CompileErrorClass.FAILED_SYNTHESIS: (
        "Typeclass synthesis failed. Try:\n"
        "- Add `instance` declarations if needed\n"
        "- Use `inferInstance` to check what's available\n"
        "- For algebra: `field_simp` then `ring`, or `nlinarith`"
    ),
    CompileErrorClass.UNIVERSE: (
        "Universe constraint error. Try:\n"
        "- Adding explicit universe parameters: `theorem foo {u : Level} ...`\n"
        "- Using `Type` instead of `Prop` if the goal is not propositional"
    ),
    CompileErrorClass.FUNCTION_EXPECTED: (
        "Function expected. Check:\n"
        "- Are you applying a lemma to the wrong number of arguments?\n"
        "- Did you miss a hypothesis that provides the function?"
    ),
    CompileErrorClass.TIMEOUT: (
        "Timeout. The proof strategy is too complex. Try:\n"
        "- Simpler approach: `native_decide` for decidable goals\n"
        "- Break into sub-lemmas with intermediate steps\n"
        "- Use `aesop` for automation"
    ),
    CompileErrorClass.UNUSED_VARIABLE: (
        "Unused variable. Use `rename_i` or `_` to silence, or restructure the proof."
    ),
}

_AESOP_FALLBACK_TACTICS = [
    "aesop",
    "simp",
    "omega",
    "nlinarith",
    "ring",
    "positivity",
    "simp [*]",
    "aesop?",
]


# ═══════════════════════════════════════════════════════════════
# Config & Result types
# ═══════════════════════════════════════════════════════════════


@dataclass
class InnerLoopConfig:
    max_rounds: int = 15
    dead_loop_threshold: int = 5
    compress_interval: int = 3
    compile_timeout: int = 60
    cadence_base_delay_s: float = 0.5
    cadence_max_delay_s: float = 8.0
    model: str = "deepseek-v4-pro"
    budget_model_id: str = "deepseek/deepseek-v4-pro"
    convergence_window: int = 3
    convergence_threshold: float = 0.1
    max_search_rounds: int = 2
    temp_dir: str = "/tmp/omega_inner"
    auto_aesop_fallback: bool = True
    adaptive_strategy: bool = False

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass
class InnerLoopResult:
    success: bool = False
    theorem_header: str = ""
    code: str | None = None
    error: str | None = None
    error_class: CompileErrorClass | None = None
    rounds: int = 0
    dead_loop: bool = False
    compile_elapsed_ms: int = 0
    api_elapsed_ms: int = 0
    total_elapsed_ms: int = 0
    budget_used_tokens: float = 0.0
    budget_used_cost: float = 0.0
    budget_used_time: float = 0.0
    budget_used_attempts: int = 0
    budget_remaining: dict | None = None
    convergence_rate: float = 0.0
    convergence_status: str = "unknown"
    n_epochs: int = 0
    termination: str = "unknown"
    sub_lemmas: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    aesop_fallback_used: bool = False
    adaptive_strategy_used: bool = False


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _extract_lean_code(content: str) -> str:
    if not content:
        return ""
    for pat in [r'```lean4?\s*\n(.*?)```', r'```\s*\n(.*?)```']:
        m = re.search(pat, content, re.DOTALL)
        if m:
            return m.group(1).strip()
    if any(kw in content for kw in ["theorem ", "lemma ", "def ", "import "]):
        return content.strip()
    return ""


def _has_code_output(content: str) -> bool:
    if not content:
        return False
    if "```" in content:
        return True
    for kw in ["theorem ", "lemma ", "def ", "import Mathlib", " by", ":="]:
        if kw in content:
            return True
    return False


def _write_temp_lean(header: str, code: str, temp_dir: str, name: str = "proof") -> str:
    os.makedirs(temp_dir, exist_ok=True)
    fpath = os.path.join(temp_dir, f"{name}.lean")
    if "import " in code:
        content = code
    elif "theorem " in code or "lemma " in code:
        content = code
    else:
        content = header + "\n" + code if header else code
    with open(fpath, "w") as f:
        f.write(content)
    return fpath


def _format_compile_errors(cr: CompileResult, extra_suggestions: str = "") -> str:
    errors = cr.errors[:5]
    lines = ["Error {}: {}".format(i + 1, err) for i, err in enumerate(errors)]
    if cr.line:
        lines.insert(0, "First error at line {}:".format(cr.line))
    if extra_suggestions:
        lines.append("")
        lines.append(extra_suggestions)
    return "\n".join(lines) if lines else "Unknown compilation error."


def _find_missing_identifiers(diagnostics: list[dict]) -> list[str]:
    missing = []
    for d in diagnostics:
        msg = d.get("message", "")
        m = re.search(
            r"unknown\s+(?:identifier|constant|declaration)\s+(?:`)?'?([a-zA-Z_]\w*)'?",
            msg,
        )
        if m:
            missing.append(m.group(1))
    return missing


def _inject_sub_lemma_prompt(header: str, missing_ids: list[str]) -> str:
    lines = []
    for mid in missing_ids[:2]:
        lines.append(
            "Missing identifier '{}'. Before the main theorem, prove this lemma:\n"
            "```lean4\nlemma {} : ... :=\n  by\n    ...\n```".format(mid, mid)
        )
    return "\n".join(lines) + "\n\nWrite the COMPLETE code (sub-lemma + main theorem) in one block."


def _get_dominant_error_class(cr: CompileResult) -> CompileErrorClass | None:
    """Get the most severe error class from a compile result."""
    if not cr.diagnostics:
        return cr.error_class
    cls_counts = classify_diagnostics(cr.diagnostics)
    if not cls_counts:
        return cr.error_class
    for priority_cls in [
        CompileErrorClass.SYNTAX_ERROR,
        CompileErrorClass.UNKNOWN_IDENT,
        CompileErrorClass.TYPE_MISMATCH,
        CompileErrorClass.FAILED_SYNTHESIS,
        CompileErrorClass.FUNCTION_EXPECTED,
        CompileErrorClass.TIMEOUT,
        CompileErrorClass.UNIVERSE,
        CompileErrorClass.UNUSED_VARIABLE,
    ]:
        if cls_counts.get(priority_cls, 0) > 0:
            return priority_cls
    if cr.error_class and cr.error_class != CompileErrorClass.NO_ERROR:
        return cr.error_class
    return None


def _strategy_hint_for_error(cr: CompileResult) -> str:
    """Get an adaptive strategy hint based on the dominant error class."""
    dominant = _get_dominant_error_class(cr)
    if dominant and dominant in _ERROR_STRATEGY_HINTS:
        return "Adaptive strategy:\n" + _ERROR_STRATEGY_HINTS[dominant]
    return ""


def _auto_aesop_fallback(
    mcp: PersistentMcpClient, temp_path: str, compile_result: CompileResult
) -> tuple[bool, str]:
    """Auto-try aesop/simp/etc on the first error goal via MCP multi_attempt.
    
    Returns (any_succeeded, result_text).
    """
    if compile_result.line <= 0:
        return False, ""

    # Build tactics list — start with strongest, fall back
    tactics = _AESOP_FALLBACK_TACTICS[:6]  # don't send too many

    result = mcp.call_tool("lean_multi_attempt", {
        "file_path": temp_path,
        "line": compile_result.line,
        "snippets": tactics,
    })

    if not result.success or not result.content:
        return False, ""

    content = result.content[:500]

    # Check if any tactic succeeded
    if "success" in content.lower() or "no goals" in content.lower():
        return True, content

    return False, content


# ═══════════════════════════════════════════════════════════════
# Main Loop
# ═══════════════════════════════════════════════════════════════


def inner_loop(
    theorem_header: str,
    theorem_name: str = "",
    config: InnerLoopConfig | None = None,
    client: DeepSeekClient | None = None,
    gate: CompileGate | None = None,
    budget: BudgetTracker | None = None,
    convergence: ConvergenceTracker | None = None,
    mcp: PersistentMcpClient | None = None,
) -> InnerLoopResult:
    cfg = config or InnerLoopConfig()
    ds = client or DeepSeekClient(model=cfg.model)
    cg = gate or CompileGate(timeout=cfg.compile_timeout)
    bt = budget or BudgetTracker()
    ct = convergence or ConvergenceTracker(
        window=cfg.convergence_window,
        convergence_threshold=cfg.convergence_threshold,
    )

    user_content = "Prove the following Lean 4 theorem:\n\n```lean4\n" + theorem_header + "\n```"
    messages = [
        {"role": "system", "content": _INNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    result = InnerLoopResult(theorem_header=theorem_header, config=cfg.to_dict())
    t_start = time.perf_counter()
    search_rounds_used = 0
    _FULL_TOOL_LIST: list[dict] = _DEFAULT_TOOLS

    for round_idx in range(cfg.max_rounds):
        # Budget checks
        if not bt.check_attempts(1, cfg.budget_model_id):
            result.termination = "budget_exhausted"
            result.error = "Budget exhausted"
            break
        if not bt.check_time(1.0, cfg.budget_model_id):
            result.termination = "budget_exhausted"
            result.error = "Budget exhausted (time)"
            break

        # Compress history periodically
        if round_idx > 0 and round_idx % cfg.compress_interval == 0:
            messages = compress_messages(messages)

        # ── Decide which tools to give the model ──────────────
        current_tools: list[dict] | None = None
        forced_code = False
        if search_rounds_used >= cfg.max_search_rounds:
            current_tools = [
                t for t in _FULL_TOOL_LIST
                if t.get("function", {}).get("name") in _NON_SEARCH_TOOL_NAMES
            ]
            if round_idx > 0:
                last_assistant = None
                for m in reversed(messages):
                    if m["role"] == "assistant":
                        last_assistant = m
                        break
                if last_assistant and not _has_code_output(last_assistant.get("content", "")):
                    messages.append({
                        "role": "user",
                        "content": (
                            "STOP SEARCHING. You have already used your search budget. "
                            "Write the complete Lean proof code now. "
                            "Output ```lean4 ... ``` with your full proof."
                        )
                    })
                    response = ds.send(messages, tools=[])
                    result.api_elapsed_ms += response.elapsed_ms
                    if response.usage:
                        bt.consume(
                            input_tokens=int(response.usage.get("prompt_tokens", 0)),
                            output_tokens=int(response.usage.get("completion_tokens", 0)),
                            model_id=cfg.budget_model_id,
                            elapsed_s=response.elapsed_ms / 1000.0,
                        )
                    msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": response.content,
                        "reasoning_content": response.reasoning_content,
                    }
                    messages.append(msg)
                    forced_code = True

        # ── API call ──────────────────────────────────────────
        if (not forced_code) or messages[-1]["role"] != "assistant":
            response = ds.send(messages, tools=current_tools)
            result.api_elapsed_ms += response.elapsed_ms

            if response.usage:
                bt.consume(
                    input_tokens=int(response.usage.get("prompt_tokens", 0)),
                    output_tokens=int(response.usage.get("completion_tokens", 0)),
                    model_id=cfg.budget_model_id,
                    elapsed_s=response.elapsed_ms / 1000.0,
                )

            if response.finish_reason == "error":
                logger.warning("Round %d: API error, retrying", round_idx)
                continue

            msg = {
                "role": "assistant",
                "content": response.content,
                "reasoning_content": response.reasoning_content,
            }
            if response.tool_calls:
                msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in response.tool_calls
                ]
            messages.append(msg)

            # ── Process tool_calls ────────────────────────────
            if response.tool_calls:
                search_rounds_used += 1
                for tc in response.tool_calls:
                    fn_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    if mcp is not None:
                        mcp_result = _call_mcp_tool(mcp, fn_name, args)
                        result_str = mcp_result.content or json.dumps({"note": "empty result"})
                    else:
                        result_str = _simulate_tool(fn_name, args)

                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
                continue

        # ── Gate: extract code from latest assistant msg ──────
        latest = None
        for m in reversed(messages):
            if m["role"] == "assistant":
                latest = m
                break
        if latest is None:
            continue
        content = latest.get("content", "")
        code = _extract_lean_code(content)
        if not code:
            messages.append({
                "role": "user",
                "content": "You must output Lean4 code. Write ```lean4 ... ``` with a complete proof.",
            })
            continue

        # ── Compile ──────────────────────────────────────────
        compile_result = cg.compile(code)
        result.compile_elapsed_ms += compile_result.elapsed_ms

        if compile_result.success:
            result.success = True
            result.code = code
            result.rounds = round_idx + 1
            result.history = messages
            result.termination = "proved"
            ct.record_epoch(n_errors=0, proof_length=len(code),
                            elapsed_s=time.perf_counter() - t_start, errors=[])
            _fill_result_metadata(result, bt, ct, t_start)
            logger.info("Proved in %d rounds (%.6f$)", round_idx + 1, result.budget_used_cost)

            # Save successful dialogue to cache
            if theorem_name:
                try:
                    DialogueCache().save(result, theorem_name)
                except Exception as e:
                    logger.warning("Failed to cache dialogue: %s", e)

            return result

        # ══════════════════════════════════════════════════════
        # Enhanced feedback: adaptive strategy + aesop + sub-lemmas
        # ══════════════════════════════════════════════════════
        n_errors = len(compile_result.errors)
        extra_suggestions = ""
        temp_path = None

        if mcp is not None:
            temp_path = _write_temp_lean(theorem_header, code, cfg.temp_dir,
                                          name=f"round_{round_idx}")

            # 1. LSP code actions (best-effort)
            if compile_result.line > 0:
                try:
                    actions = mcp.call_tool("lean_code_actions", {
                        "file_path": temp_path,
                        "line": compile_result.line,
                    })
                    if actions.success and actions.content:
                        extra_suggestions += "LSP: " + actions.content[:200] + "\n"
                except Exception as e:
                    logger.warning("MCP code_actions unavailable: %s", e)

            # 2. Adaptive strategy hint by error class
            if cfg.adaptive_strategy:
                hint = _strategy_hint_for_error(compile_result)
                if hint:
                    extra_suggestions += hint + "\n"
                    result.adaptive_strategy_used = True

            # 3. Auto aesop/simp fallback via multi_attempt
            aesop_ok = False
            aesop_text = ""
            if cfg.auto_aesop_fallback and compile_result.line > 0 and temp_path:
                try:
                    aesop_ok, aesop_text = _auto_aesop_fallback(mcp, temp_path, compile_result)
                    if aesop_ok:
                        extra_suggestions += (
                            "Lean auto-tactics found a working approach:\n"
                            + aesop_text[:300]
                            + "\n"
                        )
                        result.aesop_fallback_used = True
                    elif aesop_text:
                        # Even if no tactic fully succeeded, show what was attempted
                        extra_suggestions += (
                            "Lean auto-tactics attempted (none fully succeeded):\n"
                            + aesop_text[:200]
                            + "\n"
                        )
                except Exception as e:
                    logger.warning("MCP multi_attempt fallback error: %s", e)

            # 4. Sub-lemma growth for unknown identifiers
            missing = _find_missing_identifiers(compile_result.diagnostics)
            for mid in missing[:2]:
                result.sub_lemmas.append(mid)
                logger.info("Missing identifier detected: %s (growing sub-lemma)", mid)

            if missing:
                sub_prompt = _inject_sub_lemma_prompt(theorem_header, missing)
                messages.append({"role": "user", "content": sub_prompt})
                ct.record_epoch(n_errors=n_errors, proof_length=len(code),
                                elapsed_s=time.perf_counter() - t_start,
                                errors=compile_result.errors)
                continue

        # Record epoch
        ct.record_epoch(n_errors=n_errors, proof_length=len(code),
                        elapsed_s=time.perf_counter() - t_start,
                        errors=compile_result.errors)

        # Convergence check
        if ct.is_stuck():
            result.termination = "stuck"
            result.error = "Stuck: errors not decreasing"
            result.rounds = round_idx + 1
            result.dead_loop = True
            result.history = messages
            _fill_result_metadata(result, bt, ct, t_start)
            return result
        if ct.is_diverging():
            result.termination = "diverging"
            result.error = "Diverging: errors increasing"
            result.rounds = round_idx + 1
            result.dead_loop = True
            result.history = messages
            _fill_result_metadata(result, bt, ct, t_start)
            return result

        # Build final feedback message
        error_text = _format_compile_errors(compile_result, extra_suggestions)
        fix_prompt = (
            "Compilation failed:\n\n"
            + error_text
            + "\n\n"
            + "Read the adaptive strategy hint above carefully. Fix the specific issue "
            + "and retry with the COMPLETE corrected version in ```lean4 ... ```."
        )
        messages.append({"role": "user", "content": fix_prompt})

    # Max rounds
    result.termination = "max_rounds"
    result.error = "Max rounds ({}) exceeded".format(cfg.max_rounds)
    result.rounds = cfg.max_rounds
    result.history = messages
    _fill_result_metadata(result, bt, ct, t_start)
    return result


# ═══════════════════════════════════════════════════════════════
# MCP tool dispatch
# ═══════════════════════════════════════════════════════════════


def _call_mcp_tool(mcp: PersistentMcpClient, fn_name: str, args: dict) -> McpToolResult:
    name_map = {
        "lean_search": "lean_leansearch",
        "lean_leansearch": "lean_leansearch",
        "lean_loogle": "lean_loogle",
        "lean_multi_attempt": "lean_multi_attempt",
        "lean_run_code": "lean_run_code",
        "lean_code_actions": "lean_code_actions",
        "lean_goal": "lean_goal",
        "lean_local_search": "lean_local_search",
    }
    mcp_name = name_map.get(fn_name)
    if mcp_name:
        return mcp.call_tool(mcp_name, args)
    return McpToolResult(success=False, content="Unknown tool: " + fn_name, tool_name=fn_name, is_error=True)


# ═══════════════════════════════════════════════════════════════
# Metadata
# ═══════════════════════════════════════════════════════════════


def _fill_result_metadata(result, bt, ct, t_start):
    result.total_elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    remaining = bt.remaining("deepseek/deepseek-v4-pro")
    result.budget_used_tokens = getattr(bt, '_total_tokens', 0.0)
    result.budget_used_cost = getattr(bt, '_total_cost', 0.0)
    result.budget_used_time = getattr(bt, '_total_time', 0.0)
    result.budget_used_attempts = getattr(bt, '_total_attempts', 0)
    result.budget_remaining = {
        "tokens": remaining.get("tokens", 0),
        "cost": remaining.get("cost", 0),
        "time": remaining.get("time", 0),
        "attempts": remaining.get("attempts", 0),
    }
    result.convergence_rate = ct.convergence_rate()
    result.n_epochs = len(getattr(ct, '_epochs', []))
    if ct.is_converged():
        result.convergence_status = "converged"
    elif ct.is_stuck():
        result.convergence_status = "stuck"
    elif ct.is_diverging():
        result.convergence_status = "diverging"
    else:
        result.convergence_status = "running"


# ═══════════════════════════════════════════════════════════════
# Simulation fallback
# ═══════════════════════════════════════════════════════════════

_KNOWN_LEMMAS: dict[str, str] = {
    "div": "Nat.dvd_of_mod_eq_zero, Nat.mod_add_div, Nat.dvd_add, Nat.dvd_mul, Nat.gcd_dvd_left",
    "dvd": "Nat.dvd_of_mod_eq_zero, Nat.dvd_add, Nat.dvd_mul, Nat.dvd_trans",
    "gcd": "Nat.gcd_dvd_left, Nat.gcd_dvd_right, Nat.gcd_eq_left, Nat.gcd_eq_right, Nat.gcd_mul_left",
    "pow": "Nat.pow_succ, Nat.pow_zero, Nat.pow_mul, Nat.pow_add, Nat.pow_two",
    "succ": "Nat.succ_eq_add_one, Nat.succ_mul, Nat.succ_add",
    "add_comm": "Nat.add_comm, Nat.add_assoc, Nat.add_left_comm, Nat.add_zero, Nat.zero_add",
    "induction": "Nat.rec, Nat.recOn, Nat.strong_induction_on",
    "inequality": "Nat.lt_of_lt_of_le, Nat.lt_of_le_of_lt, Nat.lt_succ_self",
    "sum": "Finset.sum_range_succ, Finset.sum_range_zero, Finset.sum_add_distrib",
    "prod": "Finset.prod_range_succ, Finset.prod_range_zero, Finset.prod_mul_distrib",
    "factorial": "Nat.factorial, Nat.factorial_succ, Nat.factorial_mul_prod_range",
    "log": "Real.log, Real.logb, Real.log_mul, Real.log_div, Real.log_pow",
    "abs": "abs_mul_abs_self, abs_mul, abs_add, abs_sub, abs_of_nonneg, abs_neg, sq_abs",
    "field": "field_simp, ring, nlinarith, positivity, polyrith",
    "ring": "ring, ring_nf, simp, nlinarith, field_simp, linarith",
    "algebra": "field_simp, ring, nlinarith, linarith, positivity, calc",
    "am_gm": "geom_mean_le_arith_mean2, Real.geom_mean_le_arith_mean, two_mul, sq_nonneg, nlinarith",
    "nnreal": "NNReal, NNReal.geom_mean_le_arith_mean, NNReal.exists_sq_eq",
    "digit": "Nat.digits, List.sum, Nat.digits_of_lt, Nat.digits_of_lt_base",
    "logb": "Real.logb, Real.logb_mul, Real.logb_div, Real.logb_pow",
    "even": "Nat.even_add, Even.add, Int.even_add",
}


def _simulate_tool(fn_name: str, args: dict) -> str:
    query = args.get("query", "").lower()
    matched: list[str] = []
    for keyword, lemmas_str in _KNOWN_LEMMAS.items():
        if keyword in query:
            matched.extend(l.strip() for l in lemmas_str.split(","))
    if not matched:
        matched = ["Nat.add_comm", "Nat.succ_eq_add_one", "simp"]
    return json.dumps({
        "results": [{"name": name, "type": "Mathlib lemma"} for name in matched[:8]],
        "suggestion": "Try: `simp`, `induction`, `ring`, `native_decide`, `nlinarith`",
    })
