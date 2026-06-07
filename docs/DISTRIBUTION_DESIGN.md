# Ω-Architect 分发架构：omega-core pip package + Hermes Plugin

> Hermes Agent v0.14.0 (2026.5.16) | omega v0.1.0
> 设计目标：第三方用户 `pip install omega-core` → `cp omega-plugin ~/.hermes/plugins/` → QQ Bot 直接调用

---

## 1. 现状分析

### 1.1 当前 omega 依赖图（耦合）

```
omega.llm (langchain, Ollama, DeepSeek API key, .env 配置)
   │
   ├── omega.search.proposer          ← 直接 import omega.llm
   ├── omega.runner                   ← 直接 import omega.llm
   ├── omega.prover.template_manager  ← 直接 import omega.llm
   └── experiments/*                  ← 直接 import omega.llm
        ↑
omega.prover.*  ──▶  omega.verify.*      ✔ clean (compile_fn callback)
```

**问题**：第三方用户必须配置 Ollama/DeepSeek API key / .env，和使用 Hermes 时的 provider 配置重复。

### 1.2 解耦接口（已存在）

| 接口 | 定义位置 | 形式 | 用途 |
|------|---------|------|------|
| `GenerateFn` | `omega/llm.py` | `Callable[[str], str]` | LLM 生成 |
| `CompileFn` | `omega/prover/go_prover.py` | `Callable[[str], dict]` | T2 编译 |

### 1.3 不需要解决的遗留（仍留在 omega-core 外）

| 模块 | 归属 | 不拆分理由 |
|------|------|----------|
| `omega/runner.py` | omega-plugin | 含 LLM 调用 + 完整 pipeline，是 Hermes plugin surface |
| `omega/agent/` | omega-plugin | 编排逻辑，使用者不需要 |
| `omega/research/` | 独立 pip | 知识源搜索，与证明正交 |
| `omega/resource/` | omega-core (可选) | budget 约束，core 用户可能也要 |
| `omega/cli/` | omega-plugin | Typer CLI，在 plugin 内暴露为 command |
| `benchmarks/` | 独立 package | 基准测试，非 core |
| `experiments/` | 独立 repo | 实验性代码 |

---

## 2. 新架构

```
                         ┌──────────────────────────────┐
pip install omega-core   │                              │
  ▼                      │  omega-core (pure Python)     │
omega/                    │                              │
├── search/               │  ProofTree, GoalState        │
│   ├── tree.py           │  NodeStatus, SearchNode     │
│   └── proposer.py       │  Proposer (generate_fn inj) │
├── prover/               │  GoedelProver, Rethlas      │
│   ├── go_prover.py      │  Archon, Ensemble           │
│   ├── re_prover.py      │  (no LLM imports)           │
│   ├── ar_prover.py      │                              │
│   ├── ensemble.py       │                              │
│   └── playbook.py       │                              │
├── verify/               │  T1 regex check, T2 Lean    │
│   ├── t1_llm.py         │  t2_lean, t2_mcp            │
│   ├── t2_lean.py        │                              │
│   └── t2_mcp.py         │                              │
├── resource/             │  BudgetTracker, Allocator    │
│   ├── budget.py         │  (可选依赖)                   │
│   └── config.py         │                              │
└── __init__.py           │  from omega import *         │
                          └──────────────┬───────────────┘
                                         │ dependency injection
                                         ▼
                          ┌──────────────────────────────┐
cp ~/.hermes/plugins/     │                              │
  omega-plugin/            │  omega-plugin (Hermes surf.) │
  ▼                        │                              │
├── plugin.yaml            │  ctx.register_command()      │
├── __init__.py            │  ctx.llm.complete_structured │
│   ├── _bridge.py         │  → wraps omega-core Prover  │
│   ├── benchmark_cmd.py   │  → omega-benchmark command  │
│   ├── prove_cmd.py       │  → omega-prove command      │
│   └── learn_cmd.py       │  → omega-learn command      │
└── dashboard/             │  (可选 Dashboard 面板)       │
    └── manifest.json      │                              │
                          └──────────────────────────────┘
```

### 2.1 接口契约

**omega-core `generate_fn`（由 omega-plugin 注入）：**

```python
# omega-core 定义（已在 omega/llm.py 中）
GenerateFn = Callable[[str], str]
# 签名: (prompt: str) → response: str

# omega-plugin 包装 Hermes ctx.llm:
def make_hermes_generate_fn(ctx) -> GenerateFn:
    """适配 hermes ctx.llm 到 omega-core GenerateFn。"""
    def generate(prompt: str) -> str:
        result = ctx.llm.complete_structured(
            instructions="You are a Lean4 proof assistant.",
            input=[{"type": "text", "text": prompt}],
            json_schema=PROVER_SCHEMA,
            purpose="omega.proof.generate",
            temperature=0.0,
        )
        tex = result.parsed
        # fallback: 如果 JSON 解析失败，直接用原始文本
        return tex.get("proof_attempt", result.text) if tex else result.text
    return generate
```

**omega-core `compile_fn`（由 omega-plugin 通过 Hermes MCP 包装）：**

```python
# omega-core 定义
CompileFn = Callable[[str], dict | list | str | None]
# 签名: (lean_code: str) → T2 diagnostics

# omega-plugin 包装 Hermes MCP:
def make_hermes_compile_fn() -> CompileFn:
    """使用 Hermes 已有的 Lean MCP 连接，不重复管理。"""
    # 如果有 lean-lsp-mcp 配置，复用其 MCP 连接
    from hermes.mcp import get_mcp_tool
    compile_fn = get_mcp_tool("lean_lsp", "lean_build")
    # fallback: t2_lean 本地编译
    from omega.verify.t2_lean import verify
    return verify
```

### 2.2 核心：`_bridge.py`

```python
"""omega-plugin/_bridge.py — omega-core ↔ Hermes 适配层"""

from omega.prover import GoedelProver, EnsembleProver
from omega.search.proposer import Proposer
from omega.resource import BudgetTracker

class OmegaBridge:
    """Hermes Plugin ↔ omega-core 适配器。"""
    
    def __init__(self, ctx):
        self.ctx = ctx
        self._generate_fn = make_hermes_generate_fn(ctx)
        self._compile_fn = make_hermes_compile_fn(ctx)
        self._prover = EnsembleProver(compile_fn=self._compile_fn)
    
    def prove(self, theorem: str, mode: str = "auto", attempts: int = 6) -> dict:
        """证明定理，返回结构化结果给 QQ 显示。"""
        if mode == "goedel":
            prover = GoedelProver(
                compile_fn=self._compile_fn,
                generate_fn=self._generate_fn,
                num_samples=attempts,
            )
        else:
            prover = self._prover
        
        result = prover.run(theorem)
        return {
            "theorem": theorem,
            "success": result.proof is not None,
            "proof": result.proof,
            "attempts": result.n_attempts,
            "time_s": result.timings.get("total_s", 0),
            "t1_pass": result.t1_passed,
            "t2_pass": result.t2_passed,
            "errors": result.errors[:3],  # 最多返回 3 个
        }
    
    def benchmark(self, split: str = "test", limit: int = 10) -> dict:
        """运行 MiniF2F 基准。"""
        from benchmarks.minif2f.run_benchmark import run_benchmark
        return run_benchmark(
            compile_fn=self._compile_fn,
            generate_fn=self._generate_fn,
            max_problems=limit,
        )
```

---

## 3. plugin.yaml & 命令注册

```yaml
# ~/.hermes/plugins/omega/plugin.yaml
name: omega
version: 0.1.0
description: "Ω-Architect — OpenAI Lean4 theorem proving via QQ Bot"
author: NousResearch (community fork)
hooks: []
provides:
  commands:
    - omega-prove          # 证明单条定理
    - omega-benchmark      # 跑 MiniF2F 基准
    - omega-learn          # 从成功证明学习
    - omega-status         # 查看当前证明状态
```

```python
# ~/.hermes/plugins/omega/__init__.py
from omega_plugin._bridge import OmegaBridge

def register(ctx):
    bridge = OmegaBridge(ctx)
    
    ctx.register_command(
        name="omega-prove",
        handler=lambda args: bridge.prove(args),  # 简化示例
        description="Prove a Lean theorem using Omega",
        args_hint="theorem_header (or --file <path>)",
    )
    
    ctx.register_command(
        name="omega-benchmark",
        handler=lambda args: bridge.benchmark(**parse_args(args)),
        description="Run MiniF2F benchmark and return summary",
        args_hint="--mode quick|full --max N",
    )
```

**QQ Bot 效果：**
```
你: omega-prove "theorem add_zero (n : ℕ) : n + 0 = n :="
Ω: ⏳ 开始证明 add_zero (attempts=6)...
Ω: ✅ T1 pass (100%) | ✅ T2 pass | proof found in 12.3s
   proof: by induction n with ...
   
你: omega-benchmark --mode quick --max 10
Ω: ⏳ 运行 MiniF2F quick (10 theorems)...
Ω: 📊 Benchmark Complete (142.5s)
   T1: 10/10 (100.0%) | T2: 3/10 (30.0%)
   Passing: add_zero, mul_comm, add_assoc
   Failing: aime_1983_p1 (no proof found)
```

---

## 4. 发布与安装流程

### 4.1 omega-core 发布

```bash
cd omega-architect/omega-core

# 1. 分离后的 pyproject.toml
cat > pyproject.toml << 'EOF'
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "omega-core"
version = "0.1.0"
description = "Open Formal Theorem Proving for Physics — proof engines"
requires-python = ">=3.11"
dependencies = []  # 零外部依赖！pure Python

[project.optional-dependencies]
dev = ["pytest>=7.4", "ruff>=0.1", "pyright>=1.1"]
lean = ["lean-lsp-mcp"]  # T2 compile via MCP
EOF

# 2. 发布到 PyPI
pip install build twine
python -m build
twine upload dist/*

# 3. 第三方安装
pip install omega-core
```

### 4.2 omega-plugin 安装

```bash
# 第三方用户
pip install omega-core

# 克隆插件到 hermes 插件目录
git clone https://github.com/example/omega-plugin ~/.hermes/plugins/omega

# 重启 Hermes
# 在 QQ 中:
你: omega-prove "theorem hello : True := by"
Ω: ✅ True (0.2s)
```

配置文件（可选）：

```yaml
# ~/.hermes/config.yaml （用户已有的 Hermes 配置）
plugins:
  entries:
    omega:
      llm:
        allow_model_override: true
        allowed_models: ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]
```

---

## 5. 代码拆分清单

### 5.1 omega-core 保留

| 文件 | 行数 | 修改 | 理由 |
|------|------|------|------|
| `omega/search/tree.py` | 329 | 无 | 纯数据结构 |
| `omega/search/proposer.py` | 556 | **移除 `omega.llm` import** | 接受 `generate_fn: GenerateFn` |
| `omega/prover/go_prover.py` | 600 | 无 | 已用 `compile_fn` callback |
| `omega/prover/re_prover.py` | 647 | 无 | — |
| `omega/prover/ar_prover.py` | 680 | 无 | — |
| `omega/prover/ensemble.py` | 312 | 无 | — |
| `omega/prover/playbook.py` | ~200 | 无 | — |
| `omega/prover/template_manager.py` | 270 | **移除 `omega.llm` import** | 接受 `generate_fn` |
| `omega/prover/compiler.py` | 430 | **移除 `omega.llm` import** | 接受 `generate_fn` |
| `omega/verify/t1_llm.py` | 318 | 无 | 纯 regex，无 LLM |
| `omega/verify/t2_lean.py` | ~200 | 无 | pure subprocess |
| `omega/verify/t2_mcp.py` | ~200 | 无 | MCP client |
| `omega/resource/budget.py` | ~100 | 无 | pure dataclass |
| `omega/resource/config.py` | ~100 | 无 | pure dataclass |
| `omega/resource/tracker.py` | ~100 | 无 | pure dataclass |

**总共**: ~5,000 行 → 零外部依赖（可选 `lean-lsp-mcp` 用于 T2）

### 5.2 omega-plugin 新写

| 文件 | 预计行数 | 内容 |
|------|---------|------|
| `__init__.py` | 30 | `register(ctx)` |
| `_bridge.py` | 120 | `OmegaBridge` 适配器 |
| `commands/prove.py` | 80 | `omega-prove` 命令 |
| `commands/benchmark.py` | 100 | `omega-benchmark` 命令 |
| `commands/learn.py` | 60 | `omega-learn` 命令 |
| `commands/status.py` | 50 | `omega-status` 命令 |
| `plugin.yaml` | 15 | 清单 |
| `dashboard/manifest.json` | 20 | Dashboard 面板 |
| `dashboard/dist/index.js` | ~200 | 编译后 React bundle |
| **总共** | **~700** | |

### 5.3 留在原 repo 不动的

| 路径 | 处理方式 |
|------|---------|
| `omega/llm.py` | 归 omega-plugin，作为 `make_hermes_generate_fn()` 的前身参考 |
| `omega/runner.py` | 归 omega-plugin，重写为 Hermes Runner |
| `omega/agent/` | 归 omega-plugin，供 benchmark 用的编排 |
| `omega/research/` | 保持独立，可选 pip |
| `omega/cli/` | 废弃或保留为 standalone CLI |
| `benchmarks/` | 保持独立，非 pip |
| `tests/` | 保持；omega-core 的测试和 plugin 的测试分开 |

---

## 6. omega-core pyproject.toml（设计稿）

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "omega-core"
version = "0.1.0"
description = "Open Formal Theorem Proving for Physics"
readme = "README.md"
requires-python = ">=3.11"
license = {text = "Apache-2.0"}
authors = [
    {name = "Shen Li", email = "lishen@example.com"},
]

dependencies = []
# ↑ 零外部依赖！整个 core 由纯 Python 数据结构和 subprocess 组成
# 对 Lean 编译器有运行时依赖（系统安装），非 Python 依赖

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "ruff>=0.1",
    "pyright>=1.1",
    "sympy>=1.12",
    "matplotlib>=3.7",
    "jupyter>=1.0",
]
lean = ["lean-lsp-mcp>=0.1.0"]

[project.urls]
Homepage = "https://github.com/example/omega-core"
Documentation = "https://omega-core.readthedocs.io"

[project.scripts]
omega = "omega.cli:main"
# ↑ standalone CLI 仍是可选的；用户也可以只用 Hermes plugin

[tool.ruff]
target-version = "py311"
line-length = 100
```

---

## 7. 迁移步骤

### Phase 1 (这个 session) — 设计确认
- [x] 分析依赖图
- [x] 你确认架构方向

### Phase 2 — 拆分 omega-core
- [x] 从 omega-architect 复制文件到 `omega-core/`
- [x] 移除所有 `from omega.llm import ...`
- [x] 修改 `proposer.py`, `template_manager.py`, `compiler.py` 接受 `generate_fn` 参数
- [x] 新建 `omega-core/pyproject.toml`
- [x] pytest 通过, ruff/pyright 通过

### Phase 3 — 写 omega-plugin
- [x] 新建 `omega-plugin/` 目录
- [x] `_bridge.py` 适配器
- [x] 4 条命令: prove / benchmark / learn / status
- [ ] QQ Bot 端到端测试
- [x] Dashboard 面板（可选）

### Phase 4 — 发布
- [ ] omega-core → PyPI (test or real)
- [ ] omega-plugin → GitHub（给用户 clone）
- [ ] Hermes Plugin 文档（参考 [[hermes-agent-plugin-development]]）

---

## 8. 第三方用户体验

### 安装
```bash
pip install omega-core
# 需要 Lean 4 + Mathlib（用户已有或按文档装）
# 不需要配置任何 API key、不需要 .env
git clone https://github.com/example/omega-plugin ~/.hermes/plugins/omega
# 重启 Hermes

# 然后在 QQ:
omega-prove "theorem hello : True := by"
```

### 不使用 Hermes 的用户（纯 CLI）
```bash
omega "theorem hello : True := by"
# 使用本地 Lean + Ollama（如果有）或 fallback 模板
```
