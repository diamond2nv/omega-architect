"""omega.learn — Policy learning layer.

Provides the Policy abstraction for trainable theorem-proving strategies.
"""
from omega.learn.policy.base import (
    Policy,
    PolicyContext,
    PolicyResult,
)
from omega.learn.policy.llm_policy import LLMPolicy
from omega.learn.policy.router_policy import RouterPolicy

__all__ = [
    "Policy",
    "PolicyContext",
    "PolicyResult",
    "LLMPolicy",
    "RouterPolicy",
]
