"""Tests for MessageLog: append-only slice cache."""
import pytest

from omega.agent.message_log import Message, MessageLog


class TestMessageLog:
    """Core MessageLog functionality."""

    def test_empty_log(self):
        """A freshly created log has length 0."""
        log = MessageLog()
        assert len(log) == 0

    def test_append_and_len(self):
        """Adding messages increases length."""
        log = MessageLog()
        log.add("user", "hello")
        log.add("assistant", "world")
        assert len(log) == 2

    def test_iteration(self):
        """Iterating yields all messages in order."""
        log = MessageLog()
        log.add("user", "a")
        log.add("assistant", "b")
        msgs = list(log)
        assert len(msgs) == 2
        assert msgs[0].content == "a"
        assert msgs[1].content == "b"

    def test_slicing(self):
        """Slicing returns a new log with the same underlying data."""
        log = MessageLog()
        log.add("user", "a")
        log.add("user", "b")
        log.add("user", "c")
        sliced = log[1:3]
        assert isinstance(sliced, MessageLog)
        assert len(sliced) == 2
        msgs = list(sliced)
        assert msgs[0].content == "b"
        assert msgs[1].content == "c"

    def test_slicing_does_not_share_mutations(self):
        """Modifying original after slice does not affect slice view (via trim)."""
        log = MessageLog(max_size=10)
        log.add("user", "a")
        sliced_msglog = log[0:1]
        # Fill past max_size to trigger trim
        for i in range(10):
            log.add("user", str(i))
        assert len(sliced_msglog) == 1  # slice is independent
        assert list(sliced_msglog)[0].content == "a"

    def test_cache_boundary(self):
        """add_cache_boundary marks the latest message."""
        log = MessageLog()
        log.add("user", "prefix")
        log.add_cache_boundary()
        assert log._messages[-1].is_cache_boundary

    def test_cache_boundary_on_empty_raises(self):
        """Cannot set cache boundary on empty log."""
        log = MessageLog()
        with pytest.raises(IndexError):
            log.add_cache_boundary()

    def test_cache_prefix(self):
        """cache_prefix returns messages up to and including the newest boundary."""
        log = MessageLog()
        log.add("user", "a")
        log.add("user", "b")
        log.add_cache_boundary()
        log.add("user", "c")
        log.add("user", "d")
        prefix = log.cache_prefix()
        assert len(prefix) == 2
        msgs = list(prefix)
        assert msgs[0].content == "a"
        assert msgs[1].content == "b"
        assert msgs[1].is_cache_boundary is True

    def test_cache_prefix_no_boundary(self):
        """cache_prefix returns empty log when no boundary is set."""
        log = MessageLog()
        log.add("user", "a")
        prefix = log.cache_prefix()
        assert len(prefix) == 0

    def test_to_openai_format(self):
        """Conversion to OpenAI message format."""
        log = MessageLog()
        log.add("user", "Hello")
        log.add("assistant", "Hi")
        fmt = log.to_openai_format()
        assert fmt == [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]

    def test_last_n(self):
        """last(n) returns the most recent n messages."""
        log = MessageLog()
        for i in range(5):
            log.add("user", str(i))
        last = log.last(2)
        assert len(last) == 2
        assert last[0].content == "3"
        assert last[1].content == "4"

    def test_message_is_frozen(self):
        """Message is frozen — fields cannot be modified after creation."""
        m = Message(role="user", content="test")
        with pytest.raises((AttributeError, TypeError)):
            m.content = "modified"  # type: ignore[attr-defined]

    def test_role_type_constraint(self):
        """Invalid role should be caught by type checker, not at runtime."""
        m = Message(role="user", content="ok")  # type: ignore
        assert m.role == "user"

    def test_max_size_no_trim_if_under(self):
        """Log under max_size is not trimmed."""
        log = MessageLog(max_size=5)
        for i in range(3):
            log.add("user", str(i))
        assert len(log) == 3

    def test_metadata_storage(self):
        """Custom metadata is stored on the message."""
        log = MessageLog()
        m = log.add("user", "hello", custom_key="value", score=0.95)
        assert m.metadata["custom_key"] == "value"
        assert m.metadata["score"] == 0.95


class TestOrchestrator:
    """Basic state machine tests."""

    def test_full_pipeline_success(self):
        """A simple query runs through the full state machine."""
        from omega.agent.orchestrator import Orchestrator, StateContext
        orch = Orchestrator(max_iterations=6)
        ctx = StateContext(query="prove True")
        result = orch.run(ctx)
        assert result["success"] is True
        assert result["proof"] != ""
        assert result["iterations"] == 5
        assert result["message_count"] > 0

    def test_max_iterations(self):
        """The state machine respects max_iterations (max N state transitions)."""
        from omega.agent.orchestrator import Orchestrator, StateContext
        orch = Orchestrator(max_iterations=2)
        ctx = StateContext(query="test")
        result = orch.run(ctx)
        assert result["error"] == "Max iterations (2) exceeded"
        assert result["iterations"] == 2
        assert result["success"] is False
