"""Regression tests from the real-Lean end-to-end run (2026-09-10).

Each test here pins a defect that only a **real** Lean toolchain surfaced; the
injected-collaborator tests could not see any of them. See
``docs/experiments/mcts-lean-e2e-2026-09-10.md``.

1. ``unsolved goals`` must classify as UNSOLVED_GOAL, never NO_ERROR.
   ``NO_ERROR`` means "no compile errors" and carries proximity 1.0, so the old
   behaviour valued a dead end as a finished proof.
2. An entry Lean flagged as severity=error must never come back NO_ERROR, even
   if the message text matches no pattern.
3. ``--generator llm`` must reach ``omega.llm``'s real entry point
   (``resolve_generate_fn``) instead of probing non-existent flat names.
4. A JSON-mode reply (what ``resolve_generate_fn("deepseek/...")`` returns) must
   yield candidates, and a multi-line ``tactic`` payload must be split into one
   candidate per tactic - the search applies exactly one action per node.
5. ``append_tactic`` must indent *every* line of a multi-line block.
"""

from __future__ import annotations

import pytest

from omega.engine.lean_adapters import (
    ERROR_PROXIMITY,
    CompileGateTransition,
    LeanActionGenerator,
)
from omega.loop.errors import (
    CompileErrorClass,
    classify_compile_error,
    classify_diagnostic_entry,
    classify_diagnostics,
)


# ── 1 & 2: error classification ────────────────────────────────────────────────
def test_unsolved_goals_is_classified_not_swallowed_as_no_error():
    cls = classify_compile_error("unsolved goals")
    assert cls is CompileErrorClass.UNSOLVED_GOAL
    assert cls is not CompileErrorClass.NO_ERROR


def test_tactic_failed_variants_are_classified():
    for msg in (
        "Tactic `rfl` failed: The left-hand side",
        "tactic 'omega' failed",
        "Tactic `exact` failed",
    ):
        assert classify_compile_error(msg) is CompileErrorClass.TACTIC_FAILED, msg


def test_severity_error_never_maps_to_no_error():
    """Lean's severity field is ground truth; NO_ERROR would claim success."""
    entry = {"message": "some phrasing nobody has mapped yet", "severity": "error"}
    assert classify_diagnostic_entry(entry) is CompileErrorClass.OTHER


def test_info_context_lines_do_not_pollute_error_buckets():
    """A trailing goal line (``⊢ False``) is context, not an error."""
    assert classify_diagnostic_entry({"message": "⊢ False", "severity": "info"}) is (
        CompileErrorClass.NO_ERROR
    )
    assert classify_diagnostic_entry({"message": "", "severity": "info"}) is (
        CompileErrorClass.NO_ERROR
    )


def test_real_lean_false_theorem_buckets_are_not_no_error():
    """Exact parsed diagnostics for `theorem false_demo : 1 + 1 = 3 := by norm_num`.

    The ``unsolved goals`` entry is the real failure and must be named. The
    trailing ``⊢ False`` is a *context* line (severity=info) and is legitimately
    NO_ERROR - what matters is that no **error-severity** entry lands there.
    """
    diagnostics = [
        {"message": "unsolved goals", "severity": "error", "line": 3, "column": 34},
        {"message": "⊢ False", "severity": "info", "line": 1, "column": 1},
    ]
    counts = classify_diagnostics(diagnostics)
    assert counts.get(CompileErrorClass.UNSOLVED_GOAL) == 1

    error_severity_classes = [
        classify_diagnostic_entry(d) for d in diagnostics if d["severity"] == "error"
    ]
    assert error_severity_classes == [CompileErrorClass.UNSOLVED_GOAL]
    assert CompileErrorClass.NO_ERROR not in error_severity_classes


def test_proximity_keeps_dead_ends_below_a_finished_proof():
    """The whole point of the proximity table: NO_ERROR == 1.0 is a *success*."""
    assert ERROR_PROXIMITY[CompileErrorClass.NO_ERROR.value] == 1.0
    assert ERROR_PROXIMITY[CompileErrorClass.UNSOLVED_GOAL.value] < 1.0
    assert ERROR_PROXIMITY[CompileErrorClass.TACTIC_FAILED.value] < 1.0


# ── 3 & 4: LLM generator parsing ───────────────────────────────────────────────
def test_default_llm_call_uses_resolve_generate_fn(monkeypatch):
    """Must not raise 'omega.llm exposes no known completion entry point'."""
    called: dict[str, object] = {}

    def fake_resolve(model_id: str):
        called["model_id"] = model_id
        return lambda _prompt: "norm_num"

    import omega.engine.lean_adapters as la
    import omega.llm as llm_mod

    monkeypatch.setattr(llm_mod, "resolve_generate_fn", fake_resolve, raising=False)
    assert la._default_llm_call("prompt") == "norm_num"
    assert "model_id" in called


def test_default_llm_call_raises_when_backend_unavailable(monkeypatch):
    import omega.engine.lean_adapters as la
    import omega.llm as llm_mod

    monkeypatch.setattr(llm_mod, "resolve_generate_fn", lambda _model_id: None, raising=False)
    with pytest.raises(RuntimeError, match="no LLM backend available"):
        la._default_llm_call("prompt")


@pytest.mark.parametrize(
    "reply",
    [
        '{"tactic": "norm_num", "confidence": 0.9, "is_complete": true}',
        '```json\n{"tactic": "simp", "confidence": 0.8}\n```',
        "```\n{\"tactic\": \"omega\"}\n```",
        '[{"tactic": "rfl"}, {"tactic": "simp"}]',
    ],
)
def test_json_mode_replies_yield_candidates(reply):
    actions = LeanActionGenerator.parse(reply)
    assert actions, f"JSON reply parsed to zero candidates: {reply!r}"
    assert all(a.content for a in actions)


def test_multiline_tactic_payload_splits_into_one_candidate_per_tactic():
    """A whole proof in the `tactic` field must not be applied as a single action."""
    actions = LeanActionGenerator.parse('{"tactic": "norm_num\\nrfl\\ndecide"}')
    assert [a.content for a in actions] == ["norm_num", "rfl", "decide"]
    assert all(a.metadata.get("reply_format") == "json" for a in actions)


def test_plain_line_replies_still_parse():
    actions = LeanActionGenerator.parse("norm_num\nsimp\nomega")
    assert [a.content for a in actions] == ["norm_num", "simp", "omega"]


def test_empty_reply_yields_no_candidates():
    assert LeanActionGenerator.parse("") == []


# ── 5: code assembly ───────────────────────────────────────────────────────────
def test_append_tactic_indents_every_line_of_a_block():
    t = CompileGateTransition()
    out = t.append_tactic("theorem t : 1 + 1 = 2 := by", "norm_num\nrfl\ndecide")
    assert out == "theorem t : 1 + 1 = 2 := by\n  norm_num\n  rfl\n  decide"


def test_append_tactic_preserves_relative_indentation():
    t = CompileGateTransition()
    out = t.append_tactic("theorem t : True := by", "induction n with\n| zero => simp")
    assert out == "theorem t : True := by\n  induction n with\n  | zero => simp"


def test_append_tactic_single_line_unchanged():
    t = CompileGateTransition()
    assert t.append_tactic("theorem t : True := by", "trivial") == "theorem t : True := by\n  trivial"


def test_append_tactic_ignores_blank_payload():
    t = CompileGateTransition()
    code = "theorem t : True := by"
    assert t.append_tactic(code, "   ") == code
    assert t.append_tactic(code, "") == code
