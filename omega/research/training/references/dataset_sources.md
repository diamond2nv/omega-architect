# ModelRouter Training — Dataset Sources

## Primary Sources (Goedel-LM, Apache-2.0)

### Goedel-LM/Lean-workbook-proofs
- **URL**: https://huggingface.co/datasets/Goedel-LM/Lean-workbook-proofs
- **Size**: ~100K theorem-proof pairs
- **License**: Apache-2.0
- **Content**: Lean 4 workbook problems with complete proofs, imports, and
  difficulty estimates.  Best single source for Tier-2/3 training.
- **Use**: Theorem headers → feature extraction. Built-in difficulty → proxy labels.
- **Citation**: Goedel-Prover arXiv paper

### Goedel-LM/Goedel-Prover-SFT
- **URL**: https://huggingface.co/datasets/Goedel-LM/Goedel-Prover-SFT
- **Size**: ~200K instruction-following examples
- **License**: Apache-2.0
- **Content**: instruction + formal_statement + output triples. Good diversity
  across math domains (algebra, number theory, combinatorics).
- **Use**: Formal statements → feature extraction (sample 50K).
- **Citation**: Goedel-Prover arXiv paper

### Goedel-LM/RL_dataset_V2
- **URL**: https://huggingface.co/datasets/Goedel-LM/RL_dataset_V2
- **Size**: ~50K RL training examples
- **License**: Apache-2.0
- **Content**: Lean 4 code with RL difficulty metrics.  Contains per-example
  ``difficulty`` scores from the Goedel-Prover RL pipeline — best proxy label
  source.
- **Use**: Theorem headers + built-in difficulty → best label-quality source.
- **Citation**: Goedel-Prover arXiv paper

## Secondary Sources (FrenzyMath, Apache-2.0)

### FrenzyMath/Herald_statements
- **URL**: https://huggingface.co/datasets/FrenzyMath/Herald_statements
- **Size**: 580K NL↔FL pairs
- **License**: Apache-2.0
- **Content**: Natural language math problems + Lean 4 formal statements.
  Good for NL→theorem-header domain adaptation.
- **Use**: Lean formal statements → feature extraction (sample 20K).
- **Citation**: Herald (FrenzyMath AI4Math) paper

## Local Sources (Omega-Architect, Apache-2.0)

### Omega Benchmark Suite (Tiers 1-3 + MiniF2F subset)
- **Path**: `omega/benchmark/suite.py`
- **Size**: 25 (T1-T3) + 5 (T4 MiniF2F) = 30 theorems
- **License**: Apache-2.0
- **Content**: Hand-picked theorems with known correct tiers. Gold standard
  for validation.
- **Use**: Testing and calibration only (not training — too few examples).

### Omega T2 Compile Cache
- **Path**: `~/.omega/benchmark_interim.json`
- **Size**: N per run (typically 50-500 records)
- **Content**: Per-theorem T2 compile metrics: time, attempts, success.
  Can be used as a secondary difficulty proxy.
- **Use**: Cross-validation of heuristic/ML labels against real compile difficulty.

## Summary

| Source | Examples | Labels | License | Priority |
|--------|----------|--------|---------|----------|
| RL_dataset_V2 | 50K | ✅ built-in difficulty | Apache-2.0 | ⭐ highest |
| Lean-workbook-proofs | 100K | ✅ difficulty est. | Apache-2.0 | ⭐ high |
| Goedel-Prover-SFT | 50K | ❌ heuristic | Apache-2.0 | medium |
| Herald_statements | 20K | ❌ heuristic | Apache-2.0 | low |
| Omega Benchmarks | 30 | ✅ gold standard | Apache-2.0 | validation only |
