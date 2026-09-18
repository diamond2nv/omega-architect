---
name: exo-suite-linkage
description: >
  Link the AI-for-Science toolchain — hfpclawer (literature + formula registry),
  expflow (experiments + HPO), omega-architect (Lean 4 proofs) — through file and CLI
  contracts, with per-layer cost tiers and a degradation ladder.
category: research
tags: [ai-for-science, formal-verification, literature, hpo, workflow]
author: Li Shen
version: 1.0.1
permissions: [shell, file_read, file_write, network]
metadata:
  hermes:
    homepage: https://github.com/diamond2nv/omega-architect
    tags: [ai-for-science, integration, workflow, papers, hpo, formal-verification]
    related_skills: [omega-architect-formal-proof]
---

# Exo Suite Linkage — literature → experiments → machine-checked proof

Three independent tools, one chain. Each keeps its own license and release cycle; they meet
through **files and CLI calls**, never through imports.

| Layer | Tool | Job | Cost tier |
|:--|:--|:--|:--|
| Knowledge | `hfpclawer` (PyPI `hfpclawer`) | discover/dedup papers, extract formulas into a registry, verify citations | **low** |
| Experiment | `expflow-pde` | run trials, HPO, record metrics for reproducibility | **medium** |
| Proof | `omega-architect` (`omega`) | turn a statement into a compiler-checked Lean 4 proof | **high** |

Tiers are design targets, not guarantees: even the low tier spends bandwidth, disk and CPU, and
rate-limited sources can throttle. Describe the chain as **low → medium → high** with the LLM
steps named — never as "zero-token".

## The contracts (files are the interface)

| Producer | Artifact | Consumer |
|:--|:--|:--|
| `hfpclawer` | formula registry (JSONL: `fid`, `latex`, source id) + paper store | expflow, omega, humans |
| `hfpclawer` | citation audit report (per-reference status) | humans, reviewers |
| `expflow-pde` | trial records (JSONL: trial id, params, metric, seed) + best-parameter export | omega, humans |
| `omega-architect` | Lean project (statement + proof) + compile result + run metadata | humans, reports |

Design rules: ① every artifact is self-describing (identity + provenance), ② no cross-tool
imports, ③ every hand-off is a command someone can paste, ④ the proof layer only ever receives
statements that survived the earlier gates.

## Minimal end-to-end run

```bash
# 1) Knowledge — collect the material and pin the statement
hfpclawer search --max-pages 3 && hfpclawer download && hfpclawer convert --to-wiki
#    → formula-registry entry: fid + latex + source id

# 2) Experiment — produce a number worth making a claim about
expflow pipeline submit --trials 20 --parallel 4
#    → trial records: params + metric + seed

# 3) Proof — formalize only the statement that carries the claim
omega prove 'theorem my_bound (x : ℝ) (h : 0 ≤ x) : 0 ≤ x^2 := by' --samples 4 --rounds 2
#    → compiler-accepted proof, or an honest "unverified"
```

## Degradation ladder (run what you have installed)

| Installed | What still works |
|:--|:--|
| hfpclawer only | literature review, formula registry, citation verification |
| + expflow | experiments with recorded provenance; the formalization step stays an explicit TODO |
| + omega | machine-checked proofs for the statements that deserve the cost |

Missing layers must degrade **loudly**: mark the unverified statement as *unverified* in the
hand-off artifact instead of describing it in prose.

## Cost discipline

- Formalize **few** statements — the expensive layer scales with the number of theorems, not pages.
- Raise `--samples` / `--rounds` only after the plumbing works at 1 / 1.
- **Move the high tier from API tokens toward local compute** where a GPU is available (local
  Ollama / vLLM backend in `omega-architect`). How large that saving is depends on model strength
  and workload — measure it (same theorem set, both backends) before quoting any figure.
- Record model and budget next to each proof artifact so a reader can see what the claim cost.
- Keep the low tier genuinely low: mechanical extraction and verification make no LLM call, but
  bandwidth, disk and upstream rate limits still apply.

## Pitfalls

1. **Claiming a proof layer ran when it did not** — unverified statements must be labelled as such.
2. **Letting the expensive layer absorb noise** — filter first (dedup, citation status, metrics),
   then `prove`.
3. **Implicit coupling** — importing one tool from another breaks version independence; use files.
4. **Cost anecdotes** — quote recorded run metadata (model, samples, rounds), not an estimate.
5. **Version drift** — pin each tool (tag or released version) in the artifact provenance.

## Verification checklist

- [ ] Each artifact carries identity + provenance (tool version, model, seeds)
- [ ] Unverified steps are labelled unverified in the final artifact
- [ ] Proof claims are backed by the compiler, not by an LLM's word
- [ ] Cost stated as a tier (low / medium / high) with LLM steps named
