# 四系统交叉对比分析：Omega-Architect 改进 Plan

## 系统全景

| 维度 | Omega-Architect | Claw Code (Python) | Agora (ICML 2026) | LLM-Wiki |
|:----|:----------------|:--------------------|:-------------------|:----------|
| **目标** | Lean 定理证明 agent | Claude Code 架构审计 | 分布式协议 bug 检测 | 个人知识库管理 |
| **语言** | Python | Python (audit) | Python | Python |
| **框架** | DeepSeek API + MCP | Claude API + 工具池 | Claude Agent SDK | Ollama + Qwen3 |
| **记忆** | ErrorMemory (JSONL) | 无（仅镜像） | **MCP Memory Server** | wiki 文件系统 |
| **搜索** | leansearch/loogle | 命令/工具索引匹配 | 无（外部测试） | **BM25+向量+重排序** |
| **循环** | Inner Loop (单 agent) | Trident (3阶段) | **双 agent (策略+测试)** | 3-pass ingest pipeline |
| **自愈** | 无（编译失败→重试） | 无 | **Self-Healing (5次)** | lint --fix |
| **知识库** | 手动 paper store | 无 | **PatternMemory MCP** | **LLM 自动生成 wiki** |

---

## 四个系统对 Omega 最有价值的可借鉴设计

### 1. 从 Agora 借鉴：结构化 MCP Memory Server

Agora 的 **MCP Memory Server** 是最直接可复用的设计：

```
Agora 架构:
┌─ MCP Memory Server ─────────────────────┐
│  PatternMemory  (bug patterns → fixes)  │ ← Omega 的 ErrorMemory 类似但太简陋
│  RepoKnowledge  (repo structure)        │ ← Omega 没有的
│  TestHistory    (test results)          │ ← Omega 没有的
└─────────────────────────────────────────┘
```

**Omega 现状**: `ErrorMemory = JSONL 文件 + SHA256 签名匹配`，hit_rate=0%

**改进方案（P0，1天）**:
```
omega/memory/
├── mcp_server.py        ← 新的 MCP Memory Server（替代 JSONL）
│   ├── PatternMemory    ← 错误模式 → 修复模板（当前 ErrorMemory）
│   ├── ProofMemory      ← 成功证明 → 策略（新增）
│   └── LeanKnowledge    ← Mathlib lemma → 用法（新增）
└── memory_client.py     ← 供 inner_loop 调用的客户端
```

通过 MCP 协议暴露，任何 agent（DeepSeek API）都可以查询/写入。比 JSONL 活跃得多。

### 2. 从 Agora 借鉴：双 Agent 架构

```
Agora:                        Omega 现状:
┌─ Strategy Agent ──────┐     ┌─ Inner Loop ──────────┐
│  Extended Thinking    │     │  Single LLM call       │
│  Analyze protocol    │     │  Write code → compile  │
│  Generate attack     │     │  Fix errors → repeat   │
└────────┬─────────────┘     └────────────────────────┘
         │ (handoff)
         ▼
┌─ TestGen Agent ──────┐
│  Write test code     │
│  Execute & validate  │
│  Report results      │
└──────────────────────┘
```

**Omega 改进**: 将 Inner Loop 拆为 `Planning Agent`（策略生成+lemma 检索）和 `Coding Agent`（代码生成+编译验证）。两个 agent 共享一个 context。

### 3. 从 LLM-Wiki 借鉴：3-Pass Ingest Pipeline

```
LLM-Wiki:                     Omega Paper Store 现状:
Pass 1 (Think):               手动：
  提取实体/概念/摘要             web_search → ensure_paper
Pass 2 (File):                → 手动写 entities/concepts
  创建/合并页面                 → 手动建 wikilinks
Pass 3 (Link):                → 无自动索引
  建立 [[wikilinks]] 交叉引用
```

**改进**: 给 `ensure_paper` 加上 LLM 后处理 pipeline，自动提取实体、构建概念关联、重建搜索索引。这已在 deepwiki-research skill 里有基础。

### 4. 从 LLM-Wiki 借鉴：Hybrid Search

```
LLM-Wiki:                     Omega 现状:
BM25 (keyword)                leansearch/loogle（外部 API）
+ 向量嵌入 (embedding)        无本地搜索
+ LLM 重排序                  无融合检索
= 3级混合搜索
```

**Omega 改进**: 给 wiki/paper store 加 QMD 搜索或类似的本地搜索引擎，支持 BM25 + embedding + rerank 的混合检索，不依赖外部 leansearch API。

### 5. 从 Agora 借鉴：Self-Healing Reflection Loop

```
Agora:                        Omega:
try:                           compile → if fails → retry
    execute test
except Exception as e:        无结构化错误分析
    analyze error              无递减策略切换
    choose different strategy  无最大重试上限约束
    retry (max 5)
```

**Omega 改进**: 编译失败后不只重试，而是先做错误分类→策略切换→目标调整。当前 `_strategy_hint_for_error()` 已有雏形，但未接入循环逻辑。

### 6. 从 Claw Code 借鉴：结构化 Tool Pool 权限

```
Claw Code Python:             Omega:
tools.py → PORTED_TOOLS       无显式工具注册表
permissions.py → 按名称/前缀过滤  tools 在 DeepSeek 端定义
filter_tools_by_permission()   本地无控制
```

**Omega 改进**: 加一层本地工具权限管理，对不同定理类型限制不同搜索工具（如 inequality 题禁止 search，强制用 nlinarith）。

---

## 优先级建议（基于投入产出比）

| 优先级 | 改进 | 来源 | 工作量 | 预期收益 |
|:-----:|:-----|:-----|:------|:---------|
| **P0** | **MCP Memory Server**（替代 JSONL ErrorMemory） | Agora | ⭐ 半天 | 替代 hit_rate=0% 的 JSONL |
| **P1** | **Paper Store Ingest Pipeline**（自动实体提取+wikilinks） | LLM-Wiki | ⭐ 1天 | 解决手动维护 wiki 的痛点 |
| **P2** | **Hybrid Search**（BM25+embedding+rerank for wiki） | LLM-Wiki | ⭐⭐ 2天 | 不依赖外部 leansearch |
| **P3** | **双 Agent 架构**（Strategy + Coding 分离） | Agora | ⭐⭐⭐ 3天 | 针对 hard 题可能有提升 |
| **P4** | **Self-Healing Loop**（结构化错误→策略切换） | Agora | ⭐⭐ 2天 | 减少死循环 |
| **P5** | **工具权限管理**（per theorem type） | Claw Code | ⭐ 半天 | 防止 medium 题误用 search |

**推荐先做 P0+P1**：MCP Memory Server 替代无用的 ErrorMemory + Paper Store 自动化。这两项加起来 1.5 天，直接解决当前两个最大痛点（记忆零命中、wiki 手动维护）。
