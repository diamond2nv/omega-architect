"""Single theorem runner for Inner Loop.

Provides:
- run_theorem(): run a single theorem through the Inner Loop
- LoopExperiment: manage a batch of theorems (lightweight, no parallelism yet)
- JSONL logging for result analysis
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omega.loop.inner import inner_loop, InnerLoopConfig, InnerLoopResult

logger = logging.getLogger("omega.loop.runner")


@dataclass
class TheoremProblem:
    """A single theorem to prove.
    
    Attributes
    ----------
    name : str
        Human-readable name (e.g. "mathd_numbertheory_3").
    header : str
        Lean 4 theorem header.
    difficulty : str
        "easy", "medium", or "hard".
    category : str
        "algebra", "number_theory", "induction", etc.
    """
    name: str
    header: str
    difficulty: str = "medium"
    category: str = "unknown"


@dataclass
class LoopExperiment:
    """Batch of theorems to run through the Inner Loop.
    
    Attributes
    ----------
    problems : list[TheoremProblem]
        Theorems to prove.
    config : InnerLoopConfig
        Shared configuration.
    results : list[InnerLoopResult]
        Results after run().
    log_dir : str
        Directory for JSONL logs.
    """
    problems: list[TheoremProblem]
    config: InnerLoopConfig = field(default_factory=InnerLoopConfig)
    results: list[InnerLoopResult] = field(default_factory=list)
    log_dir: str = "logs/loop/"
    
    def run(self, parallel: bool = False) -> list[InnerLoopResult]:
        """Run all theorems sequentially (or parallel in Phase 1+).
        
        Parameters
        ----------
        parallel : bool
            Not yet implemented — reserved for Phase 1.
        """
        self.results.clear()
        
        log_path = Path(self.log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        jsonl_path = log_path / f"loop_exp_{timestamp}.jsonl"
        
        total = len(self.problems)
        passed = 0
        
        logger.info(f"=== LoopExperiment: {total} theorems ===")
        print(f"\n{'='*60}")
        print(f"  LoopExperiment: {total} theorems")
        print(f"  Config: max_rounds={self.config.max_rounds}, model={self.config.model}")
        print(f"  Log: {jsonl_path}")
        print(f"{'='*60}\n")
        
        from omega.loop.compile_gate import CompileGate
        from omega.loop.deepseek_client import DeepSeekClient
        
        ds = DeepSeekClient(model=self.config.model)
        gate = CompileGate(timeout=self.config.compile_timeout)
        
        for i, problem in enumerate(self.problems):
            t0 = time.perf_counter()
            
            logger.info(f"[{i+1}/{total}] {problem.name} ({problem.difficulty})")
            print(f"  [{i+1}/{total}] {problem.name:50s} ", end="", flush=True)
            
            result = inner_loop(
                theorem_header=problem.header,
                config=self.config,
                client=ds,
                gate=gate,
            )
            
            elapsed = int((time.perf_counter() - t0) * 1000)
            self.results.append(result)
            
            if result.success:
                passed += 1
                print(f"✅ {result.rounds}r/{elapsed}ms/{result.budget_used_cost:.4f}$")
            elif result.dead_loop:
                print(f"❌ DEAD {result.error_class.value if result.error_class else '?'} ({result.rounds}r)")
            else:
                print(f"❌ {result.error[:50] if result.error else 'fail'} ({result.rounds}r)")
            
            # Log to JSONL
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "problem": problem.name,
                "difficulty": problem.difficulty,
                "success": result.success,
                "rounds": result.rounds,
                "cost_usd": result.budget_used_cost,
                "total_tokens": result.budget_used_tokens,
                "elapsed_ms": elapsed,
                "compile_ms": result.compile_elapsed_ms,
                "api_ms": result.api_elapsed_ms,
                "dead_loop": result.dead_loop,
                "error": result.error,
                "error_class": result.error_class.value if result.error_class else None,
            }
            with open(jsonl_path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        
        # Summary
        print(f"\n{'='*60}")
        print(f"  Result: {passed}/{total} passed ({passed/total*100:.1f}%)")
        print(f"{'='*60}")
        
        return self.results
    
    def summary(self) -> dict:
        """Summarize experiment results."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.success)
        dead = sum(1 for r in self.results if r.dead_loop)
        total_cost = sum(r.budget_used_cost for r in self.results)
        avg_rounds = sum(r.rounds for r in self.results) / total if total else 0
        
        return {
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total * 100, 1) if total else 0,
            "dead_loops": dead,
            "total_cost_usd": round(total_cost, 4),
            "avg_rounds": round(avg_rounds, 1),
            "per_difficulty": {
                diff: {
                    "total": sum(1 for r, p in zip(self.results, self.problems) if p.difficulty == diff),
                    "passed": sum(1 for r, p in zip(self.results, self.problems) if p.difficulty == diff and r.success),
                }
                for diff in ["easy", "medium", "hard"]
            },
        }


def run_theorem(
    theorem_header: str,
    max_rounds: int = 20,
    model: str = "deepseek-v4-pro",
) -> InnerLoopResult:
    """Run Inner Loop on a single theorem (convenience function).
    
    Parameters
    ----------
    theorem_header : str
        Lean 4 theorem header.
    max_rounds : int
        Max API rounds.
    model : str
        DeepSeek model name.
    
    Returns
    -------
    InnerLoopResult
    """
    config = InnerLoopConfig(max_rounds=max_rounds, model=model)
    return inner_loop(theorem_header, config=config)


def load_10_problems() -> list[TheoremProblem]:
    """Load the 10-problem development set from MiniF2F."""
    import json as _json
    from pathlib import Path as _Path
    
    candidates = [
        _Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
        / "goedel-prover-v2" / "dataset" / "minif2f.jsonl",
    ]
    
    data_path = None
    for p in candidates:
        if p.exists():
            data_path = p
            break
    
    if data_path is None:
        raise FileNotFoundError("MiniF2F dataset not found. Clone goedel-prover-v2 first.")
    
    problems = []
    with open(data_path) as f:
        for line in f:
            if line.strip():
                d = _json.loads(line)
                problems.append(d)
    
    by_name = {p["name"]: p for p in problems}
    
    # The 10-problem development set
    targets = [
        ("mathd_numbertheory_3", "easy", "number_theory"),
        ("induction_12dvd4expnp1p20", "easy", "induction"),
        ("mathd_algebra_33", "easy", "algebra"),
        ("amc12a_2020_p10", "medium", "algebra"),
        ("algebra_sqineq_unitcircatbpabsamblt1", "medium", "inequality"),
        ("amc12_2001_p5", "medium", "algebra"),
        ("algebra_amgm_sum1toneqn_prod1tonleq1", "medium", "inequality"),
        ("imo_1959_p1", "hard", "number_theory"),
        ("aime_1983_p1", "hard", "number_theory"),
        ("imo_1992_p1", "hard", "number_theory"),
    ]
    
    result = []
    for name, difficulty, category in targets:
        p = by_name.get(name)
        if p is None:
            logger.warning(f"Problem not found: {name}")
            continue
        formal = p.get("formal_statement", p.get("lean4_code", ""))
        if not formal:
            logger.warning(f"Empty formal statement for: {name}")
            continue
        
        # Auto-fix syntax for Lean 4.30+ compatibility
        # ∑ x in Finset → ∑ x ∈ Finset  (v4.30 dropped 'in' binder)
        formal = formal.replace("∑ x in Finset.", "∑ x ∈ Finset.")
        formal = formal.replace("∑ x in Finset", "∑ x ∈ Finset")
        formal = formal.replace("∏ x in Finset.", "∏ x ∈ Finset.")
        formal = formal.replace("∏ x in Finset", "∏ x ∈ Finset")
        # Also handle variable binder names
        import re
        formal = re.sub(r'∑\s+(\w+)\s+in\s+Finset', r'∑ \1 ∈ Finset', formal)
        formal = re.sub(r'∏\s+(\w+)\s+in\s+Finset', r'∏ \1 ∈ Finset', formal)
        
        result.append(TheoremProblem(
            name=name,
            header=formal,
            difficulty=difficulty,
            category=category,
        ))
    
    return result
