"""Ω-Architect Loop Engineering — from prompt to loop paradigm.

Inner Loop:   tool_calls + thinking → compile gate → fix → repeat
Middle Loop:  theorem → sub-lemma tree → parallel proof
Outer Loop:   curriculum scheduler → budget tracker → logging
"""
from omega.loop.inner import inner_loop, InnerLoopResult, InnerLoopConfig
from omega.loop.runner import run_theorem, LoopExperiment
from omega.loop.errors import classify_compile_error, CompileErrorClass

__all__ = [
    "inner_loop", "InnerLoopResult", "InnerLoopConfig",
    "run_theorem", "LoopExperiment",
    "classify_compile_error", "CompileErrorClass",
]
