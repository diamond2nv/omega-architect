"""机理白箱 —— 从 Lean 编译器**免费**信号里解析目标态 (goal state)。

审计结论
--------
wiki `concepts/zero-token-diagnosis-layer-audit-2026` §5.3：

    「缺 L2 机理白箱**不是成本问题，是漏读**」

Lean 在 `unsolved goals` 之后直接给出 ``⊢`` 目标态 + 局部假设上下文，
**全是编译器免费给的机理信息**；而 omega 此前只读 `error_class` **字符串**，
不读目标态（当时 ``grep proof_state|tactic_state|goal_state`` = 0 命中）。
该页把这条列为**本审计最重要的可执行结论**。

本模块补这一读：把 Lean 的 `unsolved goals` 诊断块解析成结构化目标态，
填入 ``ProofState.goals`` / ``ProofState.goal_state``，供诊断与 L3 反事实验证使用。

**0-token**：纯正则，不调 LLM。

Lean 输出形状（本节即模块的输入契约）
------------------------------------
::

    error: unsolved goals
    case succ
    n : Nat
    ih : n + 0 = n
    ⊢ n.succ + 0 = n.succ

或多目标::

    unsolved goals
    h : P
    ⊢ Q
    ⊢ R
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

#: 目标符号：Lean 用 ``⊢``（U+22A2）；ASCII 回退 ``|-``。
_TURNSTILE = re.compile(r"^\s*(?:⊢|\|-)\s?(.*)$")
_CASE = re.compile(r"^\s*case\s+(\S+)\s*$")
#: 假设行形如 ``n : Nat`` / ``h₁ h₂ : P``。要求冒号两侧有内容。
_HYP = re.compile(r"^\s*(\S[^:]*?)\s*:\s*(\S.*)$")

#: 纯噪声行（前缀）——不当作假设。
_NOISE_PREFIXES = ("error:", "warning:", "info:", "unsolved goals")


@dataclass(frozen=True)
class ParsedGoal:
    """一个解析出的目标态（机理层信号）。"""

    target: str
    hypotheses: list[str] = field(default_factory=list)
    case: str = ""

    @property
    def is_valid(self) -> bool:
        """有目标表达式才算有效（空 target 是解析残留）。"""
        return bool(self.target.strip())

    def rendering(self) -> str:
        """还原成 Lean 风格的多行文本（便于回注提示 / 日志）。"""
        lines = [f"case {self.case}"] if self.case else []
        lines.extend(self.hypotheses)
        lines.append(f"⊢ {self.target}")
        return "\n".join(lines)


def parse_goals(message: str) -> list[ParsedGoal]:
    """把一段 Lean 诊断消息解析成目标态列表。

    ``⊢`` 行**结束**一个目标：在它之前收集的假设属于它。因此多目标消息
    （``⊢ Q`` 紧跟 ``⊢ R``）会得到两个目标，第一个的假设非空、第二个为空 ——
    这与 Lean 的实际打印一致（后续目标只重复打印自身上下文）。
    """
    goals: list[ParsedGoal] = []
    hyps: list[str] = []
    case = ""

    for raw in message.splitlines():
        line = raw.rstrip()

        m_turn = _TURNSTILE.match(line)
        if m_turn:
            goals.append(ParsedGoal(
                target=m_turn.group(1).strip(),
                hypotheses=list(hyps),
                case=case,
            ))
            hyps, case = [], ""
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith(_NOISE_PREFIXES):
            continue

        m_case = _CASE.match(line)
        if m_case:
            case = m_case.group(1)
            continue

        if _HYP.match(line):
            hyps.append(stripped)

    return [g for g in goals if g.is_valid]


def parse_goal_state(message: str) -> ParsedGoal | None:
    """只取**第一个**目标态；没有则 ``None``。"""
    goals = parse_goals(message)
    return goals[0] if goals else None


def extract_goal_texts(messages: Iterable[str]) -> list[str]:
    """从多条诊断消息里汇总目标表达式（去重、保序）。

    直接可赋给 ``ProofState.goals``。
    """
    out: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        for g in parse_goals(str(msg)):
            if g.target not in seen:
                seen.add(g.target)
                out.append(g.target)
    return out


def format_goal_context(messages: Sequence[str], limit: int = 3) -> str:
    """把目标态渲染成可直接塞进提示的机理上下文（空则返回空串）。"""
    parts: list[str] = []
    for msg in messages:
        for g in parse_goals(str(msg)):
            parts.append(g.rendering())
            if len(parts) >= limit:
                return "\n\n".join(parts)
    return "\n\n".join(parts)
