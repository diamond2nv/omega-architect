"""Orchestrator: state machine driving the Ω-Architect pipeline.

Follows AGENTS.md for state transitions. Uses delegate_task-style
semantics but orchestrated programmatically from Python for testability.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from omega.agent.message_log import MessageLog
from omega.skills import Primitive, SkillSelection


class StateName(Enum):
    """All states in the Ω-Architect state machine."""
    ANALYZE_QUERY = "analyze_query"
    SELECT_SKILL = "select_skill"
    DECOMPOSE_TASK = "decompose_task"
    SKILL_EXEC = "skill_exec"
    T1_VERIFY = "t1_verify"
    T2_VERIFY = "t2_verify"
    SYNTHESIZE_RESULT = "synthesize_result"
    ERROR = "error"


@dataclass
class StateContext:
    """Mutable context carried through the state machine."""
    query: str
    formal_target: str = ""
    selected_skill: SkillSelection | None = None
    sub_goals: list[dict] = field(default_factory=list)
    proof_attempt: str = ""
    t1_result: dict | None = None
    t1_retries: int = 0
    t2_result: dict | None = None
    messages: MessageLog = field(default_factory=lambda: MessageLog(max_size=200))
    iteration: int = 0
    error: str = ""


StateHandler = Callable[[StateContext], StateName]


class Orchestrator:
    """Deterministic state machine orchestrator.

    The state graph is defined in AGENTS.md. Each state is implemented
    as a method that takes StateContext and returns the next state name.

    Usage:
        ctx = StateContext(query="prove: n + 0 = n")
        orch = Orchestrator()
        result = orch.run(ctx)
    """

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations
        self._handlers: dict[StateName, StateHandler] = {
            StateName.ANALYZE_QUERY: self._analyze_query,
            StateName.SELECT_SKILL: self._select_skill,
            StateName.DECOMPOSE_TASK: self._decompose_task,
            StateName.SKILL_EXEC: self._skill_exec,
            StateName.T1_VERIFY: self._t1_verify,
            StateName.T2_VERIFY: self._t2_verify,
            StateName.SYNTHESIZE_RESULT: self._synthesize_result,
            StateName.ERROR: self._error,
        }

    def run(self, ctx: StateContext) -> dict:
        """Run the state machine to completion."""
        state = StateName.ANALYZE_QUERY
        while state != StateName.ERROR:
            ctx.messages.add("system", f"Entering state: {state.value}")
            handler = self._handlers.get(state)
            if handler is None:
                ctx.error = f"No handler for state {state}"
                break
            next_state = handler(ctx)
            if next_state == StateName.ERROR:
                break
            state = next_state
            if state in (StateName.SYNTHESIZE_RESULT, StateName.ERROR):
                break
            ctx.iteration += 1
            if ctx.iteration >= self.max_iterations:
                ctx.error = f"Max iterations ({self.max_iterations}) exceeded"
                state = StateName.ERROR
                break
        return {
            "success": state == StateName.SYNTHESIZE_RESULT,
            "theorem": ctx.formal_target,
            "proof": ctx.proof_attempt,
            "verified_by": "t2" if ctx.t2_result and ctx.t2_result.get("verified") else "none",
            "iterations": ctx.iteration,
            "error": ctx.error,
            "message_count": len(ctx.messages),
        }

    # ── state handlers ────────────────────────────────────────

    def _analyze_query(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", f"Analyzing query: {ctx.query}")
        # Stub: in production, calls LLM or pattern-matches the query
        ctx.formal_target = ctx.query  # simplified
        return StateName.SELECT_SKILL

    def _select_skill(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", "Selecting skill primitive")
        # Stub: simple heuristic
        ctx.selected_skill = SkillSelection(
            primitive=Primitive.INDUCTION,
            confidence=0.7,
            reason="Default selection for proof tasks",
        )
        return StateName.DECOMPOSE_TASK

    def _decompose_task(self, ctx: StateContext) -> StateName:
        skill_name = ctx.selected_skill.primitive.value if ctx.selected_skill else "fallback_decompose"
        ctx.messages.add("assistant", f"Decomposing using {skill_name}")
        ctx.sub_goals = [{"goal": ctx.formal_target, "expected_type": "Prop"}]
        return StateName.SKILL_EXEC

    def _skill_exec(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", "Executing skill")
        ctx.proof_attempt = "theorem sample : True := by trivial"
        ctx.messages.add_cache_boundary()
        return StateName.T1_VERIFY

    def _t1_verify(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", "Running T1 fast verify")
        try:
            from omega.verify.t1_llm import verify as t1_verify
            result = t1_verify(ctx.proof_attempt)
            ctx.t1_result = {
                "verified": result.verified,
                "issues": result.issues,
                "warnings": result.warnings,
                "confidence": result.confidence,
            }
        except Exception as e:
            ctx.t1_result = {
                "verified": False,
                "issues": [f"T1 internal error: {e}"],
                "warnings": [],
                "confidence": 0.0,
            }
        ctx.messages.add("assistant",
            f"T1 {'PASS' if ctx.t1_result['verified'] else 'FAIL'}: "
            f"{len(ctx.t1_result['issues'])} issues, confidence {ctx.t1_result['confidence']:.2f}"
        )
        if not ctx.t1_result["verified"] and ctx.t1_retries < 2:
            ctx.t1_retries += 1
            ctx.messages.add("assistant", f"Issues: {ctx.t1_result['issues']}")
            return StateName.T1_VERIFY  # retry (max 2)
        return StateName.T2_VERIFY

    def _t2_verify(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", "Running T2 (Lean) compiler verify")
        try:
            from omega.verify.t2_lean import format_code
            from omega.verify.t2_lean import verify as t2_verify
            formatted = format_code(ctx.proof_attempt)
            ctx.messages.add("assistant", f"Compiling formatted code ({len(formatted)} chars)")

            # Build a compile_callback that MCP users provide at runtime.
            # In agent mode: delegate to a subagent with MCP tools.
            # In test/offline mode: returns descriptive error.
            result = t2_verify(ctx.proof_attempt, compile_fn=None)
            if not result.verified and "No compile_fn" in result.errors[0]:
                # Offline mode — mark as unverified but note it's a tool gap
                ctx.t2_result = {
                    "verified": False,
                    "errors": [f"T2 requires a compile_fn (MCP lean_run_code) — "
                               f"proof attempt saved for later compilation: "
                               f"{formatted[:100]}..."],
                    "elapsed_ms": 0,
                }
            else:
                ctx.t2_result = {
                    "verified": result.verified,
                    "errors": result.errors,
                    "warnings": result.warnings,
                    "elapsed_ms": result.elapsed_ms,
                }
        except Exception as e:
            ctx.t2_result = {
                "verified": False,
                "errors": [f"T2 internal error: {e}"],
                "elapsed_ms": 0,
            }
        ctx.messages.add("assistant",
            f"T2 {ctx.t2_result['summary'] if 'summary' in ctx.t2_result else ('PASS' if ctx.t2_result['verified'] else 'FAIL')}: "
            f"{len(ctx.t2_result['errors'])} errors in {ctx.t2_result.get('elapsed_ms', 0)}ms"
        )
        return StateName.SYNTHESIZE_RESULT

    def _synthesize_result(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", "Synthesizing final result")
        return StateName.SYNTHESIZE_RESULT  # terminal

    def _error(self, ctx: StateContext) -> StateName:
        ctx.messages.add("assistant", f"Error: {ctx.error}")
        return StateName.ERROR  # terminal
