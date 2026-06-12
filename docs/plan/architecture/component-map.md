---
plan_id: architecture-overview-v1
title: "Ω-Architect Current Architecture & Integration Map"
version: 0.1
date: 2026-06-12
status: active
---

# Current Architecture & Integration Map

## Module Map (omega/)

```
omega/
├── runner.py               # OmegaRunner — 主入口
├── cli/                    # CLI (typer-based)
│
├── loop/                   # ← CORE: Inner/Outer proof loop
│   ├── inner.py            #   Inner Loop (proof → compile → fix → repeat)
│   ├── outer.py            #   Outer Loop (replan on failure)
│   ├── runner.py           #   LoopExperiment (batch runner)
│   ├── compile_gate.py     #   Lean compiler wrapper
│   ├── deepseek_client.py  #   DeepSeek API client
│   ├── error_memory.py     #   🔴 ErrorMemory (to be replaced by P1)
│   ├── errors.py           #   CompileErrorClass taxonomy
│   ├── verifier.py         #   VerifierAgent (post-compile check)
│   ├── compress.py         #   Context compression (memory mgmt)
│   └── memory_processor.py #   LLM-based history summarization
│
├── prover/                 # Prover 三件套
│   ├── go_prover.py        #   GoedelProver — 主证明引擎
│   ├── ar_prover.py        #   ArchonProver — 架构搜索证明器
│   ├── re_prover.py        #   RethlasProver — Rethlas 证明器
│   ├── ensemble.py         #   EnsembleProver — 三合一投票
│   ├── compiler.py         #   编译器接口
│   └── cache.py            #   证明缓存
│
├── search/                 # 搜索与提议
│   ├── lean_search.py      #   🔴 lean-lsp-mcp wrapper (to be P0)
│   ├── proposer.py         #   Proposer — 证明策略提议
│   ├── blueprint.py        #   Blueprint DAG
│   ├── channels.py         #   搜索通道
│   ├── curriculum.py       #   课程学习
│   ├── passk.py            #   pass@k 计算
│   ├── error_classifier.py #   🔴 当前错误分类器 (to be P1)
│   ├── matlas_cache.py     #   🔴 MCP atlas cache (to be P0 cache)
│   └── tree.py             #   搜索树
│
├── verify/                 # 验证层
│   ├── t1_llm.py           #   T1: LLM 语义验证
│   ├── t2_lean.py          #   T2: Lean 编译器验证
│   ├── t2_mcp.py           #   T2: MCP 在线验证
│   └── t2_real.py          #   T2: 真实编译回调
│
├── resource/               # 资源与路由
│   ├── budget.py           #   BudgetTracker (🔴 tool_call 去重 needed)
│   ├── config.py           #   配置加载
│   ├── model_registry.py   #   模型注册
│   ├── model_router.py     #   模型路由 (ML-based tier prediction)
│   ├── routing_flags.py    #   路由标志
│   ├── tracker.py          #   🔴 ConvergenceTracker (fix needed)
│   ├── allocator.py        #   资源分配器
│   ├── pricing.py          #   定价
│   ├── benchmark.py        #   基准测试资源
│   └── lean_config.py      #   Lean 工具链发现
│
├── research/               # 研究知识库
│   ├── knowledge.py        #   知识库接口
│   ├── prover.py           #   研究证明器
│   └── sources/            #   数据源
│       ├── arxiv.py        #   arXiv 论文
│       ├── hfpclawer.py    #   HF 论文爬取
│       ├── kiwix.py        #   Kiwix 离线百科
│       ├── leancode.py     #   Lean 代码库
│       ├── paperstore.py   #   论文存储
│       └── wiki.py         #   Wiki 接口
│
├── plan/                   # 执行计划
│   ├── allocator.py        #   分配
│   ├── budget_plan.py      #   预算计划
│   ├── execution.py        #   执行计划
│   ├── gpu_plan.py         #   GPU 计划
│   └── path_plan.py        #   路径计划
│
├── benchmark/              # 基准测试
│   └── suite.py
│
├── gpu_layer/              # GPU 层
│   ├── backends.py         #   vLLM/Ollama/Transformers 后端
│   ├── detector.py         #   硬件检测
│   └── scheduler.py        #   GPU 调度器
│
├── agent/                  # Agent 层
│   ├── orchestrator.py     #   Orchestrator
│   └── message_log.py      #   消息日志
│
├── llm.py                  # LLM 通用接口
├── logger.py               # 日志系统
└── data.py                 # 数据工具
```

## Benchmarks

```
benchmarks/
├── minif2f/                # MiniF2F 基准
│   ├── analyze_results.py  #   结果分析
│   ├── run_benchmark.py    #   运行基准
│   └── results/            #   结果数据
├── putnambench/            # PutnamBench
├── matholympiadbench/      # Math Olympiad
└── lean-project/           # 自定义 Lean 项目
```

## Plan → Module Integration

| Plan | New/Modified Files |
|------|-------------------|
| P0 | `omega/search/aggregator.py`, `omega/search/sources/*.py`, `omega/search/cache.py` |
| P1 | `omega/classifier/*.py`, extend `omega/search/error_classifier.py` |
| P2 Fix | `omega/resource/tracker.py` (ConvergenceTracker) |
| P3 Fix | `omega/resource/budget.py` (BudgetTracker) |
| P4 | `omega/research/sources/` (paper store pipeline) |
| CLI | `omega/cli/__init__.py` (add `prove`, `bench`, `search`, `doctor` commands) |

## Third-Party CLI → Module Routing

```
omega search "..."
  → omega/search/aggregator.py (P0)

omega prove "..."
  → omega/runner.py → omega/loop/inner.py → P1 feedback

omega bench minif2f
  → omega/benchmark/suite.py → omega/loop/runner.py

omega doctor
  → omega/resource/lean_config.py + omega/gpu_layer/detector.py

omega config set ...
  → omega/resource/config.py
```
