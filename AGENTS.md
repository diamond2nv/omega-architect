# Ω-Architect AGENTS.md — Deterministic State Machine

## Goedel-Prover-V2-8B 本地推理

- **Repo**: https://github.com/Goedel-LM/Goedel-Prover-V2
- **Cache**: `~/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B/snapshots/dfd02e6271a58375dfbf3ece0175277cf6b6a89a/`
- **WSL vLLM**: miniconda3 (Python 3.13) + vLLM 0.22.1
- **Params**: `gpu_memory_utilization=0.85, max_model_len=4096, dtype=bfloat16, enforce_eager=True`
  - 官方 `MAX_MODEL_LEN=40960` (40K)，WSL 24GB GPU 只能到 4096
  - `max_tokens=2048` (API server mode) / `4096` (inline mode)
- **Official prompt** (src/utils.py, DeepSeekCoTHandler): 见 scripts/goedel_local_prover.py
- **当前状态**: vLLM server 常驻 :8001，goedel_prover 通过 GPU Layer 自动路由

## GPU Layer (`omega/gpu_layer/`)

```
omega/gpu_layer/
├── __init__.py       # 统一导出
├── detector.py       # 硬件检测（GPU/VRAM/CUDA/Ollama/vLLM/Transformers）
├── backends.py       # 三后端统一接口 (VLLMBackend / OllamaBackend / TransformersBackend)
└── scheduler.py      # 全局调度器 GPUScheduler（单例 gpu_scheduler）
scripts/gpu-cli       # CLI: gpu-cli status|start|stop|restart|generate
```

### 检测能力
- GPU: nvidia-smi → PyTorch fallback (WSL 无 nvidia-smi 也能检测 RTX 4500 Ada 24GB)
- vLLM: health check :8001
- Ollama: HTTP API localhost:11434 (WSL2 自动端口转发到 Windows)，不依赖 /mnt/
- Transformers: torch.cuda.is_available()

### 自动路由优先级
1. vLLM server 已运行 → 直接使用
2. GPU 可用 → 自动启动 vLLM server
3. Ollama 运行中 → 通过 HTTP API
4. Transformers fallback

### 使用方式
```python
from omega.gpu_layer import gpu_scheduler
texts = gpu_scheduler.generate(messages=[...], model="goedel", n=4)
```
  
## States


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
[ENTRY] → analyze_query
               │
               ▼
     [NEW] generate_blueprint  ──── 输出全局 DAG（不是单定理子目标）
               │
               ▼
         prove_lemmas  ──────── 并行证明 DAG 中所有未解决引理
          (via OmegaPassKManager)
               │
          ┌────┴────┐
          ▼         ▼
     all_proved  has_failures
          │         │
          ▼         ▼
  [NEW] refine_blueprint ←── 全局调整蓝图（拆/修/重连）
          │          │          成功引理保留不动
          │          ▼
          │    ← 回到 prove_lemmas（最多 N 次精炼迭代）
          │
          ▼
       [EXIT] → synthesize_result
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Parse user query into formal target + expected type signature
- **output**: `{"formal_target": "theorem statement in Lean", "target_type": "signature", "difficulty": "easy|medium|hard", "domain": "physics|combinatorics|..."


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: leaf
- **toolsets**: ["terminal", "file", "web"]
- **goal**: Choose from 8 primitives (see below). If none match, fallback to decompose.
- **output**: `{"primitive": "name", "reason": "..."}`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: orchestrator
- **toolsets**: ["terminal", "file"]
- **goal**: Split into sub-goals. Each sub-goal = a single `delegate_task` call.
- **constraints**: max_iterations=5, each sub-goal ≤50 lines Lean
- **output**: `[{"goal": "...", "expected_type": "..."}, ...]`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Execute the selected primitive. Returns proof attempt or failure state.
- **output**: `{"proof_attempt": "...", "success": true|false, "error": "..."}`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Fast LLM-based verification of the proof attempt. Check: type consistency, variable usage, missing imports, structural completeness.
- **constraints**: target ~5s per check
- **output**: `{"verified": true|false, "issues": ["..."], "confidence": 0.0-1.0}`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: leaf
- **toolsets**: ["terminal"]
- **goal**: Full Lean compiler verification via `lake build` or `lean` CLI.
- **constraints**: target ~30s per check, timeout 60s
- **output**: `{"verified": true|false, "errors": ["..."], "elapsed_ms": 123}`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Combine all sub-goal results into final theorem statement + proof.
- **output**: `{"theorem": "...", "proof": "...", "verified_by": "t2", "elapsed_ms": 123}`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
|---|-----------|---------|-------------|
| 1 | `apply_lemma` | `apply` keywords | Apply existing lemma from Mathlib |
| 2 | `rewrite_goal` | `rw` / `simp` | Rewrite target using known identities |
| 3 | `induction` | `∀ n:ℕ` or recursive structure | Structural induction |
| 4 | `case_split` | `h : A ∨ B` or `if ...` | Case analysis |
| 5 | `calc_chain` | equality chain | `calc a = b := ...` |
| 6 | `search_lemma` | unknown identity | leansearch / loogle query |
| 7 | `extract_proof` | previous similar problem | Adapt known proof structure |
| 8 | `fallback_decompose` | complex goal | Delegate to decompose_task |


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- T2 failure (Lean compile error): log error, retry once with error-aware rewrite
- Total max_iterations: 5
- After 5 failures: return best attempt + error diagnosis


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
**only one code path** makes the routing decision, eliminating path uncertainty:


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
omega.toml [lean]  ←── generated by ``omega init``, the single source of truth
       │
       ▼
Auto-discovery     ←── filesystem scan if [lean] section is missing or stale
       │
       ▼
Hardcoded fallback  ←── ~/lean-paper-plane + ~/.elan/toolchains/<version>
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
omega init --force
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- Lean version from ``lean-paper-plane/lean-toolchain``
- Binary paths from ``~/.elan/toolchains/<version>/bin/lean`` (``lake``)
- Mathlib cache health (olean count, size in GB)
- Test which compile channel works (``lake_env`` / ``bare_lean``)


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
omega config show            # shows budget + lean toolchain
omega config show --lean     # lean toolchain only (via load_lean_config())
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
from omega.resource.lean_config import load_lean_config
cfg = load_lean_config()
# cfg.project_path, cfg.lean_bin, cfg.lake_bin, cfg.olean_count, ...
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
# 1. Update project
echo "leanprover/lean4:v4.NEW" > lean-paper-plane/lean-toolchain


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
omega init --force


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
omega config show           # "Lean 4.NEW | ✅ binaries | ✅ mathlib"
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- **路径发现链**：见上方 Lean/Mathlib Path Discovery 章节
- **调用**：`lake env lean --stdin`（使用 lean-paper-plane 项目，含 Mathlib ~6.7GB / ~8109 oleans 缓存）
- **集成**：`from omega.verify.t2_real import make_real_compile_callback`
- **返回**：MCP 兼容格式 `{"diagnostics": [...], "exit_code": N}`
- **耗时**：~2.5s/定理（含 Mathlib）
- **测试**：17 tests，含真实编译测试（需 Mathlib 项目存在）
- **已知问题**：纯 Lean 定理 Init 预声明显冲突，建议始终加 `import Mathlib`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
EnsembleProver.__init__(generate_fn=generate_fn)
  ├── GoedelProver(generate_fn=...)    ← 以前未传递，已修复
  ├── RethlasProver(generate_fn=...)   ← 以前未传递，已修复
  └── ArchonProver(generate_fn=...)    ← 以前未传递，已修复
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
|----------|---------|--------|------|
| `(n : ℕ)` binder | `induction` | 0.70 | `add_zero`, `mul_comm` |
| Multi-step equality | `calc` | 0.60 | `a = b = c` |
| `True` target | `trivial` | 0.90 | — |
| Reflexive equality | `rfl` | 0.95 | `a = a` |
| `A ∧ B` target | `conjunction` | 0.50 | — |
| Single equality | `simp` | 0.40 | `x^2 = y^2` |


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
2. **`_decompose_cases`** — 析取/if-then-else → cases skeleton
3. **`_decompose_conjunction`** — ∧ target → 两个子目标
4. **`_decompose_direct`** — 直接证明（单一 tactic）
5. **`_decompose_calc`** — 等式链 → calc skeleton（2026-07 新增）


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
|:-----|:-----|:-----|:-----|
| ① `generate_fn` 通路断路 | `EnsembleProver` 透传到三个 prover | `ensemble.py:219-244` | P0 |
| ② RE/AR 缺 LLM | `RethlasProver`/`ArchonProver` 新增参数 | `re_prover.py:393`, `ar_prover.py:496` | P1 |
| ③ induction `ih` 占位 | 从 `"h_ih : goal for n"` 改为实际 target | `re_prover.py:239` | P1 |
| ④ 无 calc 蓝图 | 新增 `_decompose_calc` 并注册 | `re_prover.py:297-347` | P2 |
| ⑤ 定理模式分析器 | 自动检测 ℕ/等式/∧ 并路由策略提示 | `proposer.py:70-106` | P2 |
| ⑥ `_extract_target` 括号冒号 | 改用 paren-depth + next-char 检测 | `proposer.py:107-132` | P2 |


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
T1 pass: 241/244 (98.8%)
T2 pass: 0/244 (0.0%) — 预期，MiniF2F 为 statement-only
含 Mathlib 编译时间: ~2.5s/定理 × 244 = ~10min
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
omega/search/
├── __init__.py        # 统一导出
├── tree.py            # ProofTree, SearchNode, GoalState, NodeStatus
└── proposer.py        # Proposer, TacticSuggestion, LLM/template 生成
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
|------|------|---------|------|
| `go_prover.py` | 并行抽样 + 自修正 (2 rounds) | Goedel-Prover-V2 | 435 |
| `re_prover.py` | 蓝图分解 + 递归子目标 + LemmaCache | Rethlas | 647 |
| `ar_prover.py` | 多策略集成 + ProgressCritic (CONVERGING/CHURNING/STUCK) | Archon | 680 |
| `ensemble.py` | 联合运行三个 → 对比选举最优 | 自研 | 312 |


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
|------|------|
| `config.py` | `DEFAULT_BUDGET` — token(1M)/cost($0.50)/time(300s)/attempt(50) 四维度 + 模型定价 |
| `budget.py` | `BudgetTracker` — check/consume/remaining/summary/reset |
| `tracker.py` | `ConvergenceTracker` — epoch 记录、收敛速率、Stuck 检测、best_epoch |


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- 每次 attempt 前: `check_attempts()` + `check_time()`
- 每次 attempt 后: `consume(tokens, cost, time)`
- 每轮后: `record_epoch(n_errors, proof_length, errors)`
- 结果含 `convergence_summary`、`convergence_rate`、`stuck`、`budget_summary`


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
bt = BudgetTracker()
ct = ConvergenceTracker(window=3)
gp = GoedelProver(compile_fn=compile_fn, budget_tracker=bt, convergence_tracker=ct)
result = gp.run(theorem)
print(result.budget_summary)
print(result.convergence_summary)
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- hfpclawer: 一次 LLM 调用/篇论文，用 `check_budget()` 决定走 DeepSeek 还是 Ollama fallback
- Omega: 多次尝试/条定理，每次尝试按估计消耗扣减，epoch 跟踪类似 loss curve


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
from omega.prover import EnsembleProver


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
compile_fn = make_real_compile_callback()
prover = EnsembleProver(compile_fn=compile_fn)
result = prover.run(theorem_header)
print(result.summary())
print(result.comparison_table)
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
tests/test_prover.py: 29 tests (含 2 个真实编译集成测试)
pytest 150/150 passed
```


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- Goedel-Prover-V2 (Apache 2.0) — 并行抽样算法
- Rethlas (Apache 2.0) — 蓝图分解模式
- Archon (Apache 2.0) — 进度评判机制
- Mathlib (Apache 2.0) — T2 编译环境
- aesop (MIT) — 依赖项


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- Proof file: max 200 lines per sub-goal
- T2 timeout: 60s (hard fail)


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
|:-|:-----|:--------|:-------------|:--------|
| 1 | **PutnamBench 672 benchmark run** | ★★★ | P0-P5 done, data at `benchmarks/putnambench/` (674 Lean files) | Full MiniF2F (244) + PutnamBench (672) + MathOlympiadBench |
| 2 | **5-channel full ensemble** | ★★★ | P5 done (channels.py) | Run ensemble with all 5 channels vs Goedel-Architect baseline |
| 3 | **Git push to NAS** | ★★ | Commits ready (2 pending) | `git push local main` via SSH port 222 |
| 4 | **Goedel-Architect comparison table** | ★★ | Post-benchmark | Produce arXiv-ready table: pass@1, pass@k, cost, time vs GA |
| 5 | **MathOlympiadBench download** | ★★ | HF token gated access | Dataset `Goedel-LM/MathOlympiadBench` requires HF Pro/gated access |
| 6 | **MiniF2F full results analysis** | ★ | PID 749950 running | ~44/244 complete as of last check; waiting for completion |


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
- MathOlympiadBench not downloadable via hf-mirror.com — try direct huggingface.co with VPN or HF Pro token
- gitclone.com mirror has intermittent 502 errors


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
  ```bash
  /home/shenli/miniconda3/bin/python -c "import vllm; print(vllm.__version__)"
  ```
  The Hermes venv (`/home/shenli/.hermes/hermes-agent/venv/`, Python 3.11) does NOT have vLLM.
  Omega runs from the Hermes venv → `_vllm_strategy` import fails there.
  **Fix**: either install vLLM in omega's venv, or use `/home/shenli/miniconda3/bin/python` for hybrid strategies.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
  `/home/shenli/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B/snapshots/dfd02e6271a58375dfbf3ece0175277cf6b6a89a/`
  (Qwen3-based, 8B params, 4096 hidden, 36 layers)


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
  `--gpu-memory-utilization 0.40 --enforce-eager --trust-remote-code --dtype bfloat16 --max-model-len 4096`
  UVA patch needed in `vllm/platforms/interface.py` → `is_pin_memory_available()` returns True.


Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.
  Previous estimate of ~7% was due to the T1 filter discarding 87.5% of candidates.

- **⚠ Cache path trap**: Goedel model weights are in `~/.cache/huggingface/hub/` (24GB, 4 safetensors).
  The `/mnt/d/home/.cache/hub/...` path has ONLY config/tokenizer (16MB), NOT the weights.
  `HF_HOME=/mnt/d/home/.cache/` is separate from HuggingFace default cache.
  Always use `~/.cache/huggingface/hub/...Goedel-Prover-V2-8B...` for vLLM.

---

## 開発進捗 (Development Progress)

**Current focus**: Inner Loop with DeepSeek API v4-pro (tool_calls + thinking mode)  
**Status**: Phase 2 — Adaptive Strategy & Caching  
**MiniF2F dev (10题)**: Easy 3/3 ✅, Medium 1/4 (25%), Hard 0/3 (0%)

See [`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md) for:
- Full architecture diagram (Inner Loop: Cadence → Gate → Feedback)
- Phase-by-phase roadmap with completion status
- Current benchmarks table (10 problems, per-problem tracking)
- Key design decisions (loop engineering > prompt engineering, MCP integration, compile as local gate, search limit)
- File map of `omega/loop/` modules
- Glossary of terms (Inner Loop, Gate, Cadence, MCP, Dialogue Cache, etc.)

### Quick start

```bash
# Run a single theorem
python3 -c "
import os, sys; sys.path.insert(0, '.')
os.environ['DEEPSEEK_API_KEY'] = 'sk-...'
from omega.loop.inner import inner_loop, InnerLoopConfig
from omega.loop.mcp_sync import PersistentMcpClient
from omega.resource.budget import BudgetTracker
mcp = PersistentMcpClient(); mcp.initialize()
r = inner_loop('theorem ex (n:ℕ) : n + 0 = n := by', theorem_name='ex',
               config=InnerLoopConfig(), budget=BudgetTracker(), mcp=mcp)
print('Proved!' if r.success else f'{r.termination}: {r.error}')
"

# View cached proofs
python3 -c "
from omega.loop.dialogue_cache import DialogueCache
for p in DialogueCache().list_proofs():
    print(f'{p[\"theorem_name\"]}: {p[\"rounds\"]}r \${p[\"cost_usd\"]:.4f}')
"
```

### Core modules

| Module | Path | Purpose |
|--------|------|---------|
| Inner Loop | `omega/loop/inner.py` | Main agent loop: search→code→compile→feedback→iterate |
| DeepSeek Client | `omega/loop/deepseek_client.py` | API wrapper with tool_calls + thinking mode |
| Compile Gate | `omega/loop/compile_gate.py` | Local `lean --stdin` compilation with SHA256 cache |
| Error Classifier | `omega/loop/errors.py` | 13 error classes, dead-loop detection |
| MCP Client | `omega/loop/mcp_client.py` | `lean-lsp-mcp` connection (loogle/leansearch/multi_attempt) |
| Dialogue Cache | `omega/loop/dialogue_cache.py` | JSONL cache of successful proof conversations |
| Budget Tracker | `omega/resource/budget.py` | $2/5M tokens/300s cap |
| Convergence Tracker | `omega/resource/tracker.py` | Epoch-level stuck/diverging detection |
