#!/usr/bin/env python3
"""Inner Loop — integrated optimization pass (search fix + MCP feedback + sub-lemma growth + adaptive strategy)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from omega.loop.compile_gate import CompileGate, CompileResult
from omega.loop.compress import compress_messages, estimate_tokens
from omega.loop.memory_processor import extract_attempts, summarize_lessons
from omega.loop.deepseek_client import (
    _DEFAULT_TOOLS,
    _NON_SEARCH_TOOL_NAMES,
    DeepSeekClient,
)
from omega.loop.dialogue_cache import DialogueCache
from omega.loop.errors import (
    CompileErrorClass,
    classify_diagnostics,
)
from omega.loop.mcp_sync import McpToolResult, PersistentMcpClient
from omega.loop.verifier import VerifierAgent, Verdict
from omega.loop.file_pipeline import FilePipeline
from omega.loop.error_memory import ProofErrorMemory
from omega.classifier import ThreeLayerClassifier, ErrorContext as ClassifierContext
from omega.plan.execution import ExecutionPlan
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
   - `Nat.dvd_add_right h` / `Nat.dvd_add_left h` when d∣a+b and you know d∣b
     (e.g., induction on 12 | 4^(n+1)+20: apply (Nat.dvd_add_right h60).mp)
4. Output code in ```lean4 ... ``` blocks.
5. If compilation fails, read the error carefully and fix ONLY the specific issue.
6. If you have multiple possible tactics to try, use lean_multi_attempt to test them all at once (faster).
7. Use lean_run_code to quickly test proof snippets before finalizing.

# Available tools (use sparingly, max 1 search before writing code):
- lean_loogle: search Mathlib by type signature
- lean_leansearch: search Mathlib by natural language
- lean_multi_attempt: try multiple proof tactics at once (fast)
- lean_run_code: compile a Lean snippet quickly"""

_MULTI_CANDIDATE_HINT = """
# Beam Search mode
When the problem is difficult, output MULTIPLE distinct proof approaches in a single response.
Number them and the system will try all of them in parallel:

Approach 1 (strategy_name):
```lean4
...
```

Approach 2 (strategy_name):
```lean4
...
```

The approaches should use DIFFERENT tactics/strategies so if one
fails, another might succeed. The system automatically compiles
and checks all approaches."""


_SKETCH_SYSTEM_PROMPT = """You are a Lean 4 theorem proving agent.

# Phase 1: PROOF SKETCH (Round 1 only)
DO NOT write any Lean code yet. Instead, output a natural language proof sketch.

Your sketch should include:
1. Key observations about the theorem structure
2. The main proof strategy (induction? case analysis? algebraic rewriting?)
3. Key lemmas you expect to need
4. Step-by-step logical flow (bullet points)

Format:
```sketch
## Strategy
[overall approach]

## Steps
1. [step 1 description]
2. [step 2 description]
...

## Key lemmas needed
- [lemma 1]: why needed
- [lemma 2]: why needed
```

After you provide the sketch, you will proceed to Phase 2 where you formalize
each step into Lean code. For now, plan carefully."""


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
    max_rounds: int = 512  # 10题开发模式=512; N>10时建议 5120/N, clamp [64,512]
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
    persistent_compile: bool = False
    goal_extraction: bool = True  # extract goal states at sorry locations via MCP
    memory_processor: bool = True  # LLM-based history compression
    run_verifier: bool = True  # independent VerifierAgent after compile success (Ax-Prover style)
    proof_sketch: bool = True  # Phase 1: natural language sketch before Lean code
    file_pipeline: bool = False  # use hash-anchored FilePipeline (Step 5, experimental)
    parallel_candidates: int = 1  # Beam Search: N candidates per API call, parallel compile (1=disabled)
    error_memory: bool = True  # proof error signature → fix template memory
    dead_loop_detection: bool = True  # False = 禁用收敛检测，让预算做唯一停止条件
    candidate_fusion: bool = False  # True = beam search 失败后融合多候选

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
    extra_metrics: dict = field(default_factory=dict)  # goal_extraction, memory_processor, etc.


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


def _extract_multiple_candidates(content: str) -> list[str]:
    """Extract ALL ```lean4 code blocks from a single response.

    Used for Beam Search: the LLM outputs N distinct proof candidates,
    and the system compiles them in parallel.

    Returns a list of unique Lean code snippets (deduplicated by hash).
    """
    if not content:
        return []
    blocks: list[str] = []
    seen_hashes: set[str] = set()
    for pat in [r'```lean4?\s*\n(.*?)```', r'```\s*\n(.*?)```']:
        for m in re.finditer(pat, content, re.DOTALL):
            code = m.group(1).strip()
            if not code:
                continue
            # Deduplicate: skip if same content already seen
            import hashlib
            h = hashlib.sha256(code.encode()).hexdigest()[:16]
            if h not in seen_hashes:
                seen_hashes.add(h)
                blocks.append(code)
    return blocks


def _parallel_compile(candidates: list[str], gate: CompileGate,
                      max_workers: int | None = None) -> tuple[int, CompileResult]:
    """Compile N candidates in parallel, return the first successful one.

    Args:
        candidates: List of Lean code strings to compile.
        gate: CompileGate instance (thread-safe, uses subprocess).
        max_workers: Max parallel compilations (default: len(candidates)).

    Returns:
        Tuple of (index_of_first_success, CompileResult) or (-1, last_result) if all fail.
    """
    if not candidates:
        return -1, CompileResult(success=False, errors=["No candidates to compile"])
    if len(candidates) == 1:
        return (0, gate.compile(candidates[0])) if gate.compile(candidates[0]).success else (-1, gate.compile(candidates[0]))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    success_lock = threading.Lock()
    first_success: list[tuple[int, CompileResult]] = []

    n_workers = max_workers or min(len(candidates), 10)  # cap at 10 parallel
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(gate.compile, code): i for i, code in enumerate(candidates)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                result = future.result(timeout=120)
            except Exception as e:
                result = CompileResult(success=False, errors=[str(e)])
            with success_lock:
                if result.success and not first_success:
                    first_success.append((i, result))
                    # Cancel remaining futures (best-effort)
                    for f in futures:
                        f.cancel()
                    return i, result

    # All failed — return -1 and the last error
    return -1, result


def _extract_sketch(content: str) -> str:
    """Extract proof sketch from ```sketch ... ``` blocks."""
    if not content:
        return ""
    # Match ```sketch\n...``` with flexible closing backtick count (1-6)
    m = re.search(r'```sketch\s*\n(.*?)`{1,6}', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: if no ```sketch block but content reads like a plan (no Lean keywords)
    if not _has_code_output(content):
        return content.strip()
    return ""


def _write_temp_lean(header: str, code: str, temp_dir: str, name: str = "proof") -> str:
    os.makedirs(temp_dir, exist_ok=True)
    fpath = os.path.join(temp_dir, f"{name}.lean")
    if "import " in code or "theorem " in code or "lemma " in code:
        content = code
    else:
        content = header + "\n" + code if header else code
    with open(fpath, "w") as f:
        f.write(content)
    return fpath


def _format_compile_errors(cr: CompileResult, extra_suggestions: str = "") -> str:
    errors = cr.errors[:5]
    lines = [f"Error {i + 1}: {err}" for i, err in enumerate(errors)]
    if cr.line:
        lines.insert(0, f"First error at line {cr.line}:")
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
            f"Missing identifier '{mid}'. Before the main theorem, prove this lemma:\n"
            f"```lean4\nlemma {mid} : ... :=\n  by\n    ...\n```"
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


def _strategy_hint_for_error(cr: CompileResult, theorem_header: str = "") -> str:
    """Get an adaptive strategy hint based on the dominant error class.

    Also checks the theorem header for domain-specific hints.
    """
    dominant = _get_dominant_error_class(cr)
    hint_parts = []
    if dominant and dominant in _ERROR_STRATEGY_HINTS:
        hint_parts.append("Adaptive strategy:\n" + _ERROR_STRATEGY_HINTS[dominant])

    # Domain-specific hints based on theorem content
    if '∣' in theorem_header or 'dvd' in theorem_header.lower():
        hint_parts.append(
            "Hint for divisibility (∣):\n"
            "For Nat divisibility proofs with induction, try:\n"
            "- `Nat.dvd_add_right h` / `Nat.dvd_add_left h` "
            "to relate d∣a+b and d∣a when you know d∣b\n"
            "- `rcases ih with ⟨k, h⟩` then expand the expression "
            "to show it equals 12*(4*k) etc.\n"
            "- `omega` for linear arithmetic, `ring` for algebra\n"
            "- Example: `apply (Nat.dvd_add_right (by norm_num : 12∣60)).mp`"
        )

    return "\n\n".join(hint_parts)


# ── Three-layer error classifier (P1) ────────────────────────────

_three_layer_clf: ThreeLayerClassifier | None = None


def _get_classifier() -> ThreeLayerClassifier:
    """Lazy-init singleton for the three-layer classifier."""
    global _three_layer_clf
    if _three_layer_clf is None:
        _three_layer_clf = ThreeLayerClassifier(
            enable_llm_judge=True,
            llm_model="deepseek-chat",
        )
    return _three_layer_clf


def _classify_compile_error(
    error_msg: str,
    theorem_header: str = "",
    diagnostics: list[dict] | None = None,
    memory_errors: list[str] | None = None,
) -> tuple[str, float, list[str], str]:
    """Classify a compile error using the three-layer pipeline.

    Returns (category, confidence, fix_strategies, source).
    Thread-safe (no mutable state from callee side).
    """
    clf = _get_classifier()
    ctx = ClassifierContext(
        error_msg=error_msg,
        theorem_header=theorem_header,
        diagnostics=diagnostics or [],
        memory_errors=memory_errors or [],
    )
    result = clf.classify(ctx)
    return result.category, result.confidence, result.fix_strategies, result.source


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


def _extract_goals_at_sorry(
    mcp: PersistentMcpClient,
    temp_path: str,
    code: str,
    max_goals: int = 3,
) -> str:
    """Extract goal states at ``sorry`` locations via MCP lean_goal.

    Scans the code for ``sorry``, finds their line numbers, and calls
    lean_goal on each to show the LLM what remains to be proved.

    Returns:
        Formatted goal block (empty string if no sorry or none available).
    """
    import re
    lines = code.split("\n")
    sorry_lines = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Find "sorry" at the start of a statement (not in a string/comment)
        if stripped == "sorry" or stripped.startswith("sorry ") or stripped.endswith(" sorry"):
            sorry_lines.append(i)

    if not sorry_lines:
        return ""

    goals = []
    for line_num in sorry_lines[:max_goals]:
        try:
            result = mcp.call_tool("lean_goal", {
                "file_path": temp_path,
                "line": line_num,
            })
            if result.success and result.content:
                goals.append(f"  [line {line_num}] {result.content[:300]}")
        except Exception:
            pass

    if not goals:
        return ""

    return "Remaining goals (at sorry locations):\n" + "\n".join(goals)


# ── edit_file handler ──────────────────────────────────────────


def _handle_edit_file(
    fp: FilePipeline | None,
    cg: CompileGate,
    bt: BudgetTracker,
    budget_model_id: str,
    args: dict,
) -> str:
    """Handle an ``edit_file`` tool call: apply edit → compile → return result.

    Returns a formatted string with compile outcome + updated hash index
    that the LLM can use to decide the next edit.
    Compile time is tracked against the time budget via ``bt.record_time()``.
    """
    if fp is None or fp.index is None or fp.file_path is None:
        return (
            "Error: FilePipeline not initialized. "
            "Use ```lean4 blocks instead of edit_file."
        )

    anchor = args.get("anchor", "")
    new_block = args.get("new_block", "")
    if not anchor or not new_block:
        return (
            "Error: edit_file requires 'anchor' (16-char hash) and 'new_block' (complete block text). "
            f"Available anchors: {fp.format_index_for_prompt()}"
        )

    # Resolve the anchor before applying
    block = fp.index.resolve(anchor)
    if block is None:
        return (
            f"Error: Hash anchor '{anchor}' not found. "
            f"Available anchors:\n{fp.format_index_for_prompt()}"
        )

    # Apply the edit
    edit_ok = fp.edit(anchor, new_block)
    if not edit_ok:
        return (
            f"Error: Failed to apply edit for anchor '{anchor}'. "
            "The file may have been modified externally. "
            f"Current index:\n{fp.format_index_for_prompt()}"
        )

    # Read updated file content
    t0 = time.perf_counter()
    updated_source = fp.get_source()

    # Compile the updated file via CompileGate (uses lean --stdin)
    compile_result = cg.compile(updated_source)
    compile_time = time.perf_counter() - t0

    # Track compile time against budget (no tokens/cost/attempts)
    bt.record_time(compile_time, budget_model_id)

    # Format result
    lines = [
        f"Edit applied to block [{anchor}] ({block.name}).",
        f"Compile: {'✅ SUCCESS' if compile_result.success else '❌ FAILED'}",
    ]

    if compile_result.success:
        lines.append("The proof compiled successfully.")
    else:
        if compile_result.errors:
            for err in compile_result.errors[:5]:
                lines.append(f"  {err}")
        if compile_result.diagnostics:
            for d in compile_result.diagnostics[:3]:
                msg = d.get("message", str(d))[:200]
                lines.append(f"  {msg}")

    # Include updated hash index so LLM knows what anchors are now available
    lines.append(f"\nUpdated hash index:\n{fp.format_index_for_prompt()}")

    return "\n".join(lines)


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
    plan: ExecutionPlan | None = None,
) -> InnerLoopResult:
    """Inner Loop — LLM tool_calls + compile gate + fix → repeat.

    Args:
        theorem_header: The Lean 4 theorem header (e.g. ``"theorem t : 1 + 1 = 2 :="``).
        theorem_name: Optional name for the theorem.
        config: Inner loop configuration.
        client: DeepSeek client (created automatically if not provided).
        gate: Compile gate (created automatically if not provided).
        budget: Budget tracker (created automatically if not provided).
        convergence: Convergence tracker (created automatically if not provided).
        mcp: MCP client (created automatically if not provided).
        plan: Optional ExecutionPlan for pre-flight validation and tracking.
              When provided, ``pre_flight()`` is called before the loop starts.
    """
    cfg = config or InnerLoopConfig()
    ds = client or DeepSeekClient(model=cfg.model)
    cg = gate or CompileGate(timeout=cfg.compile_timeout, persistent=cfg.persistent_compile)
    bt = budget or BudgetTracker()
    ct = convergence or ConvergenceTracker(
        window=cfg.convergence_window,
        convergence_threshold=cfg.convergence_threshold,
    )
    em = ProofErrorMemory() if cfg.error_memory else None
    last_error_msg: str = ""  # 最后一个编译错误（用于 record on success）
    last_error_class: str = ""

    # ── Pre-flight: ExecutionPlan 集成 ──
    if plan is not None:
        try:
            pre_flight_msg = plan.pre_flight(model_id=cfg.budget_model_id)
            logger.info("Plan pre-flight:\n%s", pre_flight_msg)
        except Exception as e:
            logger.warning("Plan pre-flight failed (continuing): %s", e)

    user_content = "Prove the following Lean 4 theorem:\n\n```lean4\n" + theorem_header + "\n```"
    if cfg.proof_sketch:
        messages = [
            {"role": "system", "content": _SKETCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        sketch_done = False
    else:
        messages = [
            {"role": "system", "content": _INNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        sketch_done = True  # no sketch phase needed

    result = InnerLoopResult(theorem_header=theorem_header, config=cfg.to_dict())
    t_start = time.perf_counter()
    search_rounds_used = 0
    _FULL_TOOL_LIST: list[dict] = _DEFAULT_TOOLS

    # ── Init FilePipeline ────────────────────────────────────
    fp: FilePipeline | None = None
    if cfg.file_pipeline:
        fp = FilePipeline(base_dir=cfg.temp_dir)
        fp.init_file(theorem_header, name=theorem_name or "proof")
        logger.info("FilePipeline: %s (%d blocks)", fp.file_path, len(fp.index.blocks) if fp.index else 0)
        # Inject hash index into system prompt with edit_file usage instructions
        idx_info = fp.format_index_for_prompt()
        if idx_info and idx_info != "(no index)":
            edit_instruction = (
                "\n\n── FilePipeline (hash-anchored editing) ──\n"
                "The proof is stored in a .lean file. Each block has a unique hash anchor.\n"
                "Available blocks:\n"
                + idx_info +
                "\n\n"
                "How to use:\n"
                "1. **edit_file**(anchor='...', new_block='...') — edit a block. "
                "The file is recompiled and results are returned.\n"
                "2. The compile result + updated hash anchors are shown in the tool response.\n"
                "3. Iterate: edit → compile → fix → edit → compile → done.\n"
                "4. When compilation succeeds AND verifier passes, output the complete\n"
                "   ```lean4 proof in a regular message (not edit_file).\n"
                "\n"
                "Python-style dict syntax for multi-line new_block: use \\n for newlines.\n"
                "Example: new_block='theorem t : 1+1=2 := by\\n  native_decide'\n"
                "── end FilePipeline ──\n"
            )
            messages[0]["content"] += edit_instruction

    for round_idx in range(cfg.max_rounds):
        # ── Convergence-driven early stop ────────────────────
        if cfg.dead_loop_detection:
            if ct.is_stuck():
                result.termination = "stuck"
                result.error = "Stuck: errors not decreasing (early stop)"
                result.rounds = round_idx
                _fill_result_metadata(result, bt, ct, t_start, model_id=cfg.budget_model_id)
                break
            if ct.is_diverging():
                result.termination = "diverging"
                result.error = "Diverging: errors increasing (early stop)"
                result.rounds = round_idx
                _fill_result_metadata(result, bt, ct, t_start, model_id=cfg.budget_model_id)
                break

        # Budget checks
        if not bt.check_attempts(1, cfg.budget_model_id):
            result.termination = "budget_exhausted"
            result.error = "Budget exhausted"
            break
        if not bt.check_time(1.0, cfg.budget_model_id):
            result.termination = "budget_exhausted"
            result.error = "Budget exhausted (time)"
            break

        # Mark round boundary for budget tracking
        bt.record_round()

        # Compress history periodically — use Memory Processor if enabled
        if round_idx > 0 and round_idx % cfg.compress_interval == 0:
            if cfg.memory_processor:
                # Try LLM-based compression: summarize attempts + trim old messages
                attempts = extract_attempts(messages)
                lesson = summarize_lessons(attempts, theorem_name)
                if lesson:
                    # Keep: system prompt + last 2 rounds + lesson block
                    keep = 0
                    system_msg = messages[0]
                    # Find last 2 assistant messages + their tool results
                    recent: list[dict] = [system_msg]
                    assistant_count = 0
                    for msg in reversed(messages[1:]):
                        if msg.get("role") == "assistant" and ("```lean4" in msg.get("content", "") or "```lean" in msg.get("content", "")):
                            assistant_count += 1
                        recent.insert(1, msg)
                        if assistant_count >= 3:
                            break
                    # Prepend lesson block
                    recent.insert(1, {"role": "user", "content": lesson})
                    messages = recent
                    result.extra_metrics["memory_processor"] = True
                    logger.info("Memory processor: compressed %d messages into lesson", len(messages))
                else:
                    messages = compress_messages(messages)
            else:
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
                            input_tokens=int(response.usage.get("prompt_tokens", 0) or 0),
                            output_tokens=int(response.usage.get("completion_tokens", 0) or 0),
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
                    input_tokens=int(response.usage.get("prompt_tokens", 0) or 0),
                    output_tokens=int(response.usage.get("completion_tokens", 0) or 0),
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

            # ── Process tool_calls ───────────────────────────────
            if response.tool_calls:
                search_rounds_used += 1
                for tc in response.tool_calls:
                    fn_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    # edit_file is handled locally (needs fp + cg, not MCP)
                    if fn_name == "edit_file":
                        result_str = _handle_edit_file(fp, cg, bt, cfg.budget_model_id, args)
                    elif mcp is not None:
                        t_tool = time.perf_counter()
                        mcp_result = _call_mcp_tool(mcp, fn_name, args,
                                                    theorem_header=user_content)
                        result_str = mcp_result.content or json.dumps({"note": "empty result"})
                        # Track MCP tool execution time against budget
                        bt.record_time(time.perf_counter() - t_tool, cfg.budget_model_id)
                    else:
                        result_str = _simulate_tool(fn_name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
                continue

        # ── Sketch Phase: extract sketch from first response ──
        if cfg.proof_sketch and not sketch_done:
            content = messages[-1].get("content", "") if not response.tool_calls else ""
            sketch = _extract_sketch(content) if not response.tool_calls else _extract_sketch(messages[-1].get("content", ""))
            if sketch:
                sketch_done = True
                # Replace system prompt with code prompt + sketch context
                messages[0] = {
                    "role": "system",
                    "content": _INNER_SYSTEM_PROMPT
                        + "\n\nThe following is your proof sketch that you previously developed. "
                        + "Use it as a guide to write the Lean proof:\n\n```sketch\n"
                        + sketch + "\n```\n"
                        + "\nNow proceed to Phase 2: write Lean code to implement this sketch."
                }
                # Add a user confirmation to transition to code phase
                messages.append({
                    "role": "user",
                    "content": "Good. Now proceed to Phase 2: implement your proof sketch as Lean code. "
                               "Output ```lean4 ... ``` with the complete proof.",
                })
                logger.info("Proof sketch captured (%d chars), entering Phase 2", len(sketch))
                continue
            else:
                # LLM didn't output a sketch — remind it
                messages.append({
                    "role": "user",
                    "content": "Please output a proof sketch first (```sketch ... ```) before writing any Lean code. "
                               "Describe your proof strategy and key lemmas needed.",
                })
                continue

        # ── Gate: extract code(s) from latest assistant msg ──
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

        # ── Beam Search: extract multiple candidates ──────────
        candidates = [code]
        if cfg.parallel_candidates > 1:
            all_candidates = _extract_multiple_candidates(content)
            if len(all_candidates) > 1:
                candidates = all_candidates
                logger.info("Beam Search: %d candidates from 1 API call", len(candidates))

        # ── Compile (single or parallel) ──────────────────────
        if len(candidates) == 1:
            t_c = time.perf_counter()
            compile_result = cg.compile(code)
            result.compile_elapsed_ms += compile_result.elapsed_ms
            compile_idx = 0
            bt.record_time(time.perf_counter() - t_c, cfg.budget_model_id)
        else:
            t_c = time.perf_counter()
            compile_idx, compile_result = _parallel_compile(candidates, cg)
            result.compile_elapsed_ms += compile_result.elapsed_ms * len(candidates)
            bt.record_time(time.perf_counter() - t_c, cfg.budget_model_id)

        if compile_result.success:
            # ── Write to FilePipeline if active ──────────────
            if fp is not None and fp.file_path is not None:
                try:
                    final_code = candidates[compile_idx] if len(candidates) > 1 else code
                    with open(fp.file_path, "w") as f:
                        f.write(theorem_header + "\n" + final_code)
                    fp.rebuild_index()
                except Exception as e:
                    logger.warning("FilePipeline write failed: %s", e)

            # ── Verifier: independent correctness check ────────
            if cfg.run_verifier:
                t_v = time.perf_counter()
                va = VerifierAgent(compile_timeout=cfg.compile_timeout)
                verdict = va.verify(code)
                bt.record_time(time.perf_counter() - t_v, cfg.budget_model_id)
                if not verdict.verified:
                    # Verifier rejected the proof — inject feedback, continue loop
                    logger.warning("Verifier rejected proof (sorry=%s, errors=%s). Continuing.",
                                   verdict.has_sorry, verdict.has_errors)
                    feedback = va.format_verdict(verdict)
                    messages.append({
                        "role": "user",
                        "content": f"⚠️ **Verifier feedback**:\n{feedback}\n\nFix the issues above and resubmit the complete proof.",
                    })
                    # Mark as not proved, continue loop
                    ct.record_epoch(n_errors=len(verdict.errors) if verdict.has_errors else 1,
                                    proof_length=len(code),
                                    elapsed_s=time.perf_counter() - t_start,
                                    errors=verdict.errors)
                    # Don't return — let the loop continue for fixes
                    round_idx += 1
                    if round_idx >= cfg.max_rounds:
                        result.termination = "verifier_failed"
                        break
                    continue

            result.success = True
            result.code = code
            result.rounds = round_idx + 1
            result.history = messages
            result.termination = "proved"
            ct.record_epoch(n_errors=0, proof_length=len(code),
                            elapsed_s=time.perf_counter() - t_start, errors=[])
            _fill_result_metadata(result, bt, ct, t_start, model_id=cfg.budget_model_id)
            logger.info("Proved in %d rounds (%.6f$)", round_idx + 1, result.budget_used_cost)

            # Save successful dialogue to cache
            if theorem_name:
                try:
                    DialogueCache().save(result, theorem_name)
                except Exception as e:
                    logger.warning("Failed to cache dialogue: %s", e)

            # Record error→fix in memory (if there was a prior error)
            if em is not None and last_error_msg and last_error_class:
                em.record(
                    last_error_msg,
                    last_error_class,
                    code[:200],  # the successful proof as the fix
                    theorem_name=theorem_name,
                )
                logger.info("ErrorMemory: recorded fix for %s (%s)",
                            theorem_name, last_error_class)

            return result

        # ══════════════════════════════════════════════════════
        # Enhanced feedback: adaptive strategy + aesop + sub-lemmas
        # ══════════════════════════════════════════════════════
        n_errors = len(compile_result.errors)
        extra_suggestions = ""
        temp_path = None

        if mcp is not None:
            # Use FilePipeline path if active, else temp file
            if fp is not None and fp.file_path is not None:
                temp_path = fp.file_path
            else:
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

            # 2. Three-layer error classification (P1)
            if cfg.adaptive_strategy and compile_result.errors:
                first_error = compile_result.errors[0]
                category, confidence, strategies, source = _classify_compile_error(
                    error_msg=first_error,
                    theorem_header=user_content,
                    diagnostics=compile_result.diagnostics,
                )
                if confidence >= 0.85 and strategies:
                    extra_suggestions += f"🎯 [{source}] {strategies[0]}\n"
                    result.adaptive_strategy_used = True
                elif confidence >= 0.6 and strategies:
                    extra_suggestions += f"💡 [{source}] {strategies[0]}\n"
                    result.adaptive_strategy_used = True
                else:
                    # Fall back to old strategy hint
                    hint = _strategy_hint_for_error(compile_result, user_content)
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

            # 5. Goal state extraction at sorry locations
            goal_hint = ""
            if cfg.goal_extraction and temp_path and code:
                try:
                    goal_hint = _extract_goals_at_sorry(mcp, temp_path, code)
                    if goal_hint:
                        extra_suggestions += goal_hint + "\n"
                        result.extra_metrics["goal_extraction"] = True
                except Exception as e:
                    logger.warning("Goal extraction failed: %s", e)

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
            _fill_result_metadata(result, bt, ct, t_start, model_id=cfg.budget_model_id)
            return result
        if ct.is_diverging():
            result.termination = "diverging"
            result.error = "Diverging: errors increasing"
            result.rounds = round_idx + 1
            result.dead_loop = True
            result.history = messages
            _fill_result_metadata(result, bt, ct, t_start, model_id=cfg.budget_model_id)
            return result

        # ── ErrorMemory: lookup known fix ────────────────
        if em is not None and compile_result.errors:
            first_err = compile_result.errors[0]
            last_error_msg = first_err
            error_cls = str(compile_result.error_class.value or "")
            last_error_class = error_cls
            known_fix = em.lookup(first_err, error_cls)
            if known_fix:
                hint = f"[Ω ErrorMemory] This error resembles a known pattern. Proven fix: {known_fix[:200]}"
                extra_suggestions = hint + "\n" + extra_suggestions
                result.extra_metrics["error_memory_hit"] = True
                logger.info("ErrorMemory HIT: known fix injected for %s", compile_result.error_class)
            else:
                # 首次出现的错误: 记录「错误 → 刚失败的尝试」为**失败样本**。
                # success=False ⇒ failure_count=1 ⇒ lookup() 不会把它当
                # "Proven fix" 回注 (见 error_memory.ProofErrorMemory.record)。
                em.record(first_err, error_cls, code[:200],
                          theorem_name=theorem_name, success=False)
                logger.debug("ErrorMemory record (first occurrence, negative sample): %s",
                             error_cls)

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
    result.error = f"Max rounds ({cfg.max_rounds}) exceeded"
    result.rounds = cfg.max_rounds
    result.history = messages
    _fill_result_metadata(result, bt, ct, t_start, model_id=cfg.budget_model_id)
    return result


# ═══════════════════════════════════════════════════════════════
# MCP tool dispatch
# ═══════════════════════════════════════════════════════════════


def _call_mcp_tool(mcp: PersistentMcpClient | None, fn_name: str, args: dict,
                   theorem_header: str = "") -> McpToolResult:
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
        if mcp is None:
            return McpToolResult(success=False, content="MCP not available", tool_name=fn_name, is_error=True)
        result = mcp.call_tool(mcp_name, args)

        # Inject domain-relevant lemma suggestions for divisibility theorems
        if theorem_header and ('∣' in theorem_header or 'dvd' in theorem_header.lower()):
            if fn_name in ("lean_loogle", "lean_leansearch", "lean_search", "lean_local_search"):
                div_hint = (
                    "\n\n[Ω hint] For dvd+induction: `Nat.dvd_add_right h` / `Nat.dvd_add_left h` "
                    "to relate d∣a+b and d∣a when you know d∣b. "
                    "Example: `apply (Nat.dvd_add_right (by norm_num : 12∣60)).mp` then rewrite."
                )
                if result.content:
                    result.content += div_hint

        return result
    return McpToolResult(success=False, content="Unknown tool: " + fn_name, tool_name=fn_name, is_error=True)


# ═══════════════════════════════════════════════════════════════
# Metadata
# ═══════════════════════════════════════════════════════════════


def _fill_result_metadata(result, bt, ct, t_start, model_id: str = "deepseek/deepseek-v4-pro"):
    result.total_elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    remaining = bt.remaining(model_id)
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
