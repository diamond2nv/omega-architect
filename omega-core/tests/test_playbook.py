"""Tests for ACE-inspired Proof Playbook system."""

from omega.prover.go_prover import GoedelProver
from omega.prover.playbook import (
    ErrorAnalyzer,
    Playbook,
    PlaybookBullet,
    PlaybookManager,
    compile_with_dspy,
)

# ── Constants ──────────────────────────────────────────────────────

THEOREM_TRIVIAL = "theorem t (n : ℕ) : n + 0 = n :="

# ── Helpers ────────────────────────────────────────────────────────


def _make_mock_diagnostics(messages: list[str]) -> list[dict]:
    """Convert plain error messages to mock T2 diagnostics."""
    return [{"message": msg, "severity": "error", "line": 1, "column": 1} for msg in messages]


def _mock_pass(_code: str) -> dict:
    """Mock compiler that always passes."""
    return {"diagnostics": [], "exit_code": 0}


# ── Playbook Data Structure ────────────────────────────────────────


class TestPlaybook:
    def test_empty_playbook(self):
        """Empty playbook renders as placeholder."""
        pb = Playbook()
        assert "(no proof strategies" in pb.render()
        assert len(pb) == 0

    def test_add_bullet(self):
        """Adding a bullet increments count and assigns ID."""
        pb = Playbook()
        b = pb.add_bullet("strategies", "Use nlinarith for algebra")
        assert b.bullet_id == "str-00001"
        assert b.section == "strategies"
        assert b.content == "Use nlinarith for algebra"
        assert b.helpful == 0
        assert b.harmful == 0
        assert len(pb) == 1

    def test_add_multiple_bullets_increments_ids(self):
        """Bullet IDs are sequential per section."""
        pb = Playbook()
        b1 = pb.add_bullet("strategies", "S1")
        b2 = pb.add_bullet("mistakes", "M1")
        b3 = pb.add_bullet("strategies", "S2")
        assert b1.bullet_id == "str-00001"
        assert b2.bullet_id == "mis-00002"
        assert b3.bullet_id == "str-00003"
        assert len(pb) == 3

    def test_render_includes_sections(self):
        """Rendered playbook shows section headers and bullet content."""
        pb = Playbook()
        pb.add_bullet("strategies", "Use nlinarith")
        pb.add_bullet("mistakes", "Don't forget imports")
        rendered = pb.render()
        assert "STRATEGIES" in rendered
        assert "MISTAKES" in rendered
        assert "Use nlinarith" in rendered
        assert "Don't forget imports" in rendered

    def test_find_by_content_exact_match(self):
        """Exact content match returns the bullet."""
        pb = Playbook()
        pb.add_bullet("strategies", "Use nlinarith for algebra")
        found = pb.find_by_content("Use nlinarith for algebra")
        assert found is not None
        assert found.section == "strategies"

    def test_find_by_content_no_match(self):
        """No match returns None."""
        pb = Playbook()
        found = pb.find_by_content("Use ring for expansion")
        assert found is None

    def test_find_by_content_whitespace_normalized(self):
        """Whitespace differences are tolerated."""
        pb = Playbook()
        pb.add_bullet("strategies", "Use  nlinarith  for  algebra")
        found = pb.find_by_content("Use nlinarith for algebra")
        assert found is not None

    def test_record_success_increments_helpful(self):
        """Bullets referenced in a success get helpful++."""
        pb = Playbook()
        b = pb.add_bullet("strategies", "S1")
        pb.record_success([b.bullet_id])
        assert b.helpful == 1
        assert b.harmful == 0

    def test_record_failure_increments_harmful(self):
        """Bullets referenced in a failure get harmful++."""
        pb = Playbook()
        b = pb.add_bullet("strategies", "S1")
        pb.record_failure([b.bullet_id])
        assert b.harmful == 1
        assert b.helpful == 0

    def test_is_harmful_threshold(self):
        """A bullet is harmful when harmful > helpful * 2."""
        b = PlaybookBullet(
            bullet_id="test-00001",
            section="strategies",
            content="bad advice",
            helpful=1,
            harmful=3,
        )
        assert b.is_harmful is True

    def test_is_not_harmful_below_threshold(self):
        """Bullet not harmful when helpful outweighs harmful."""
        b = PlaybookBullet(
            bullet_id="test-00001",
            section="strategies",
            content="good advice",
            helpful=5,
            harmful=1,
        )
        assert b.is_harmful is False

    def test_prune_removes_harmful_bullets(self):
        """prune() removes bullets where harmful >> helpful."""
        pb = Playbook()
        pb.add_bullet("strategies", "good")
        # Manually set counters on the first bullet
        pb.bullets[0].helpful = 1
        pb.bullets[0].harmful = 0
        # Add a bad one
        _bad = pb.add_bullet("mistakes", "bad")
        pb.bullets[1].helpful = 0
        pb.bullets[1].harmful = 5
        assert len(pb) == 2
        pruned = pb.prune()
        assert pruned == 1
        assert len(pb) == 1
        assert pb.bullets[0].content == "good"

    def test_deduplicate_merges_similar(self):
        """Deduplicate merges bullets with near-identical content."""
        pb = Playbook()
        pb.add_bullet("strategies", "Use nlinarith for algebra")
        pb.add_bullet("strategies", "Use nlinarith for algebra")
        pb.add_bullet("strategies", "completely different")
        assert len(pb) == 3
        removed = pb.deduplicate(threshold=0.9)
        assert removed == 1
        assert len(pb) == 2

    def test_enforce_budget_keeps_best_ones(self):
        """When budget exceeded, lowest-scoring bullets removed; best kept."""
        pb = Playbook(max_tokens=30)
        b1 = pb.add_bullet("strategies", "A" * 60)  # 60 chars, score 0 — worst
        b2 = pb.add_bullet("strategies", "B" * 60)  # 60 chars, score 0 — same
        b3 = pb.add_bullet("strategies", "C" * 10)  # 10 chars, fits
        b1.helpful, b1.harmful = 0, 0
        b2.helpful, b2.harmful = 1, 0  # score 1 — best
        b3.helpful, b3.harmful = 0, 0  # score 0
        assert len(pb) == 3
        removed = pb.enforce_budget()
        assert removed == 2  # b1 and b3 removed
        assert len(pb) == 1
        assert "B" * 60 in pb.bullets[0].content

    def test_score_net_helpfulness(self):
        """score = helpful - harmful."""
        b = PlaybookBullet("t-1", "strategies", "test", helpful=5, harmful=2)
        assert b.score == 3.0


# ── Error Analyzer ─────────────────────────────────────────────────


class TestErrorAnalyzer:
    def test_extract_unsolved_goals(self):
        """ "unsolved goals" maps to strategies section."""
        diag = _make_mock_diagnostics(["unsolved goals remaining"])
        bullets = ErrorAnalyzer.extract("code", diag)
        assert len(bullets) >= 1
        assert all(b.section == "strategies" for b in bullets)

    def test_extract_syntax_error(self):
        """ "expected '{' maps to mistakes section."""
        diag = _make_mock_diagnostics(["expected '{' at line 3"])
        bullets = ErrorAnalyzer.extract("code", diag)
        assert len(bullets) >= 1
        assert all(b.section == "mistakes" for b in bullets)

    def test_extract_type_mismatch(self):
        """ "type mismatch" maps to strategies."""
        diag = _make_mock_diagnostics(["type mismatch at argument"])
        bullets = ErrorAnalyzer.extract("code", diag)
        assert len(bullets) >= 1

    def test_extract_no_errors(self):
        """Empty diagnostics produce no bullets."""
        bullets = ErrorAnalyzer.extract("code", [], epoch=0)
        assert len(bullets) == 0

    def test_extract_multiple_errors(self):
        """Multiple distinct errors produce multiple bullets."""
        diag = _make_mock_diagnostics(
            [
                "unsolved goals remaining",
                "expected '{' at line 5",
                "type mismatch",
            ]
        )
        bullets = ErrorAnalyzer.extract("code", diag)
        # At most one per diagnostic, some may map to same lesson
        assert 1 <= len(bullets) <= 3

    def test_extract_deduplicates_same_pattern(self):
        """Same error pattern does not produce duplicate bullets."""
        diag = _make_mock_diagnostics(
            [
                "unsolved goals: goal 1",
                "unsolved goals: goal 2",
            ]
        )
        bullets = ErrorAnalyzer.extract("code", diag)
        assert len(bullets) == 1  # both map to same lesson


# ── Playbook Manager ────────────────────────────────────────────────


class TestPlaybookManager:
    def test_init_creates_empty_playbook(self):
        """Default PlaybookManager has empty playbook."""
        mgr = PlaybookManager()
        assert len(mgr.playbook) == 0
        assert "(no proof" in mgr.playbook.render()

    def test_update_from_result_failure_adds_bullets(self):
        """Failed compilation adds error-pattern bullets."""
        mgr = PlaybookManager()
        diag = _make_mock_diagnostics(["unsolved goals remaining"])
        result = mgr.update_from_result(
            theorem=THEOREM_TRIVIAL,
            attempt="theorem t : True := by sorry",
            diagnostics=diag,
            succeeded=False,
        )
        assert result["bullets_added"] >= 1
        assert len(mgr.playbook) >= 1

    def test_update_from_result_success_no_new_bullets(self):
        """Successful compilation adds no error bullets."""
        mgr = PlaybookManager()
        result = mgr.update_from_result(
            theorem=THEOREM_TRIVIAL,
            attempt="theorem t : True := trivial",
            diagnostics=[],
            succeeded=True,
        )
        assert result["bullets_added"] == 0

    def test_update_increments_helpful_on_success(self):
        """Success increments helpful on referenced bullets."""
        mgr = PlaybookManager()
        b = mgr.playbook.add_bullet("strategies", "test", epoch=1)
        mgr.update_from_result(
            theorem=THEOREM_TRIVIAL,
            attempt="code",
            diagnostics=[],
            succeeded=True,
            referenced_bullets=[b.bullet_id],
        )
        assert b.helpful == 1

    def test_update_increments_harmful_on_failure(self):
        """Failure increments harmful on referenced bullets."""
        mgr = PlaybookManager()
        b = mgr.playbook.add_bullet("strategies", "test", epoch=1)
        mgr.update_from_result(
            theorem=THEOREM_TRIVIAL,
            attempt="code",
            diagnostics=[{"message": "error", "severity": "error"}],
            succeeded=False,
            referenced_bullets=[b.bullet_id],
        )
        assert b.harmful == 1

    def test_inject_into_prompt_with_playbook(self):
        """{{PLAYBOOK}} replaced with rendered content when playbook non-empty."""
        mgr = PlaybookManager()
        mgr.playbook.add_bullet("strategies", "Use nlinarith")
        template = "System prompt {{PLAYBOOK}} end"
        result = mgr.inject_into_prompt(template)
        assert "{{PLAYBOOK}}" not in result
        assert "Use nlinarith" in result

    def test_inject_empty_playbook_shows_placeholder(self):
        """Empty playbook still renders placeholder text."""
        mgr = PlaybookManager()
        template = "System prompt {{PLAYBOOK}} end"
        result = mgr.inject_into_prompt(template)
        assert "no proof strategies collected" in result

    def test_get_stats(self):
        """Stats reflect current playbook state."""
        mgr = PlaybookManager()
        mgr.playbook.add_bullet("strategies", "S1")
        mgr.playbook.add_bullet("mistakes", "M1")
        stats = mgr.get_stats()
        assert stats["total_bullets"] == 2
        assert stats["sections"]["strategies"] == 1
        assert stats["sections"]["mistakes"] == 1
        assert stats["epoch"] == 0

    def test_epoch_increments_on_each_update(self):
        """Each update_from_result call increments epoch."""
        mgr = PlaybookManager()
        # Access _epoch to check internal state
        assert mgr._epoch == 0  # type: ignore[attr-defined]
        mgr.update_from_result("t", "c", [], True)
        assert mgr._epoch == 1  # type: ignore[attr-defined]
        mgr.update_from_result("t", "c", [], True)
        assert mgr._epoch == 2  # type: ignore[attr-defined]


# ── GoedelProver + PlaybookManager Integration ──────────────────────


class TestGoedelProverWithPlaybook:
    def test_init_with_playbook_manager(self):
        """GoedelProver accepts playbook_manager parameter."""
        mgr = PlaybookManager()
        gp = GoedelProver(compile_fn=_mock_pass, playbook_manager=mgr)
        assert gp.playbook_manager is mgr

    def test_run_with_playbook_passes_no_errors(self):
        """Running with playbook and passing compiler works normally."""
        mgr = PlaybookManager()
        gp = GoedelProver(
            compile_fn=_mock_pass,
            num_samples=2,
            playbook_manager=mgr,
        )
        result = gp.run(THEOREM_TRIVIAL)
        assert result.succeeded is True
        # Playbook should have at least one success record
        assert len(mgr.playbook) == 0  # no errors added

    def test_run_with_playbook_accumulates_errors(self):
        """Running with playbook and failing compiler accumulates lessons."""

        # Create a mock that produces recognizable error messages.
        def _mock_type_error(code: str) -> dict:
            return {
                "diagnostics": [
                    {"message": "unsolved goals: cannot close", "severity": "error"},
                ],
                "exit_code": 1,
            }

        mgr = PlaybookManager()
        gp = GoedelProver(
            compile_fn=_mock_type_error,
            num_samples=2,
            max_correction_rounds=1,
            playbook_manager=mgr,
        )
        result = gp.run(THEOREM_TRIVIAL)
        assert result.succeeded is False
        # Playbook should have error-pattern bullets from "unsolved goals"
        assert len(mgr.playbook) >= 1

    def test_run_with_playbook_injects_into_prompt(self):
        """The playbook context reaches the proposer config."""

        mgr = PlaybookManager()
        mgr.playbook.add_bullet("strategies", "Use nlinarith", epoch=1)

        # Capture the config that reaches the proposer by using a spy proposer
        captured_configs = []

        class SpyProposer:  # noqa: F841
            def suggest(self, goal, context, **config):
                captured_configs.append(dict(config))
                from omega.search.proposer import TacticSuggestion

                return [
                    TacticSuggestion(
                        tactic="trivial",
                        confidence=0.5,
                        description="spy",
                    )
                ]

        gp = GoedelProver(
            compile_fn=_mock_pass,
            num_samples=1,
            max_correction_rounds=0,
            playbook_manager=mgr,
        )
        # Replace proposer with spy
        gp.proposer = SpyProposer()  # type: ignore[assignment]
        gp.run(THEOREM_TRIVIAL)

        assert len(captured_configs) >= 1
        ctx = captured_configs[0].get("playbook_context", "")
        assert "Use nlinarith" in ctx
        assert "STRATEGIES" in ctx


# ── DSPy Stub ──────────────────────────────────────────────────────


class TestDspyStub:
    def test_dspy_available_in_playbook(self):
        """DSPy import is available from playbook module."""

        # When dspy IS installed, the stub returns 'stub' status.
        result = compile_with_dspy([], [])
        assert result["status"] == "stub"
