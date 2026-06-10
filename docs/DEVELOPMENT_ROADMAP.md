# Ω-Architect Development Roadmap

> **Doc version**: `0.1.0` — matches repo version
> **Last updated**: 2026-06-10

> **Status**: Active development — Inner Loop v0.3  
> **Last Updated**: 2026-06-10  
> **Model**: deepseek-v4-pro (DeepSeek API)  
> **Target**: MiniF2F 244题 → 95%+ pass rate

---

## 1. Architecture Overview

```
User Query
    │
    ▼
┌─────────────────────────────────────────────────┐
│  analyze_query (delegate_task)                  │
│  → parse formal target + type signature         │
└─────────────────────┬───────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│           Inner Loop (核心)                      │
│                                                   │
│  ┌──────────┐    ┌──────────┐    ┌────────────┐  │
│  │ Cadence  │───▶│   Gate   │───▶│  Feedback  │  │
│  │ (retry)  │    │ (compile)│    │ (error→fix)│  │
│  └──────────┘    └──────────┘    └────────────┘  │
│       │               │               │          │
│       ▼               ▼               ▼          │
│  ┌──────────┐    ┌──────────┐    ┌────────────┐  │
│  │ DeepSeek │    │ Compile  │    │ MCP Tools  │  │
│  │ API +    │    │ Gate     │    │ (leansearch│  │
│  │ tool_call│    │ (local)  │    │  loogle    │  │
│  └──────────┘    └──────────┘    │  multi_att)│  │
│                                  └────────────┘  │
│                                                   │
│  Converged? → return proof                        │
│  Stuck?     → return with diagnosis               │
│  Max rounds? → return partial result              │
└─────────────────────┬───────────────────────────┘
                      │
                      ▼
              ┌──────────────┐
              │ Dialogue     │
              │ Cache (JSONL)│
              └──────────────┘
```

## 2. Roadmap

### Phase 0: Foundation ✅ (2026-06-03 ~ 2026-06-10)

| Component | Status | Details |
|-----------|--------|---------|
| DeepSeek SDK integration | ✅ | `omega/loop/deepseek_client.py` — tool_calls + thinking mode |
| Compile Gate | ✅ | `omega/loop/compile_gate.py` — local `lean --stdin` + cache (SHA256) |
| Error Classifier | ✅ | `omega/loop/errors.py` — 13 error classes, pattern matching |
| Convergence Tracker | ✅ | `omega/resource/tracker.py` — epoch-level stuck/diverging detection |
| Budget Tracker | ✅ | `omega/resource/budget.py` — $2 cap / 5M tokens / 300s / 50 attempts |
| MCP Client | ✅ | `omega/loop/mcp_client.py` — persistent lean-lsp-mcp connection |
| Inner Loop v0.1 | ✅ | 10题 4/10 (40%) pass, $0.63 total |
| MiniF2F 10题抽样 | ✅ | Easy 3/3 ✅, Medium 1/4 ✅, Hard 0/3 ✅ |

### Phase 1: Search & Loop Optimization ✅ (2026-06-10)

| Fix | Status | Bug | Impact |
|-----|--------|-----|--------|
| `tools or _DEFAULT_TOOLS` | ✅ | `[]` falsy → 搜索限制完全无效 | 搜索次数从 5-6 → 2 |
| 搜索限制+强制写代码 | ✅ | 模型无限搜索不写代码 | IMO 1959 P1: stuck → **proved** (4r $0.02) |
| MCP PATH 注入 | ✅ | 子进程找不到 `lake` | LSP code_actions 可用 |
| `lean_run_code` 工具 | ✅ | 模型无法 MCP 编译验证 | 工具列表完整 |
| 子引理生长 | ✅ | unknown identifier 只记日志不处理 | 自动注入子引理 prompt |
| MCP 错误优雅处理 | ✅ | code_actions 挂死整个 loop | try/except 包裹 |
| loogle 本地缓存 | ✅ | symlink v4.30.0 → rc1 绕过下载 | 本地 loogle 可用但不稳定 |

### Phase 2: Adaptive Strategy & Caching ✅ (2026-06-10)

| Feature | Status | Details |
|---------|--------|---------|
| Adaptive strategy hints | 🔧 | 8 类错误→策略映射，代码就绪但默认关闭（实测可能误导）|
| Auto `aesop` fallback | 🔧 | MCP multi_attempt 自动尝试 aesop/simp/nlinarith |
| Dialogue Cache | ✅ | `~/.cache/omega/proof_dialogues/proofs.jsonl` — 5 entries |
| SFT export | ✅ | `cache.export_sft()` — problem+solution pairs |
| Regression test | ✅ | Easy 3/3 ✅, total $0.046 |

### Phase 3: Stability & Coverage 🔜 (Next)

| Priority | Task | Expected Impact |
|----------|------|-----------------|
| P0 | IMO 1959 P1 稳定性（当前 PASS 不稳定） | 解决 API 一致性差异 |
| P0 | AM-GM 证明策略（stuck after compile) | 需要更好的 `nlinarith` 引导 |
| P1 | MCP `lean_multi_attempt` 集成到模型工具 | 让模型自主使用 auto-tactics |
| P1 | Medium 4 题回归测试 | 验证不退化 |
| P2 | `max_search_rounds` 自适应（按难度） | Easy=1, Medium=2, Hard=3 |

### Phase 4: Full Coverage 🔮

| Goal | Approach | Reference |
|------|----------|-----------|
| MiniF2F 244题 >95% | Loop Engineering + search→reasoning layer | Goedel-Architect: 99.2% (2606.06468) |
| Blueprint DAG generation | delegate_task 分解子目标 | Omega State Machine (AGENTS.md) |
| Self-play iteration | 成功对话 → few-shot prompt → 改进 | Dialogue Cache |
| Logprobs + beam search | GPU 后端 (vLLM) | RTX 4500 Ada 24GB |

## 3. Current Benchmarks

### MiniF2F Dev Set (10题抽样)

| # | Problem | Difficulty | Phase 0 | Phase 1 | Phase 2 | Notes |
|---|---------|-----------|---------|---------|---------|-------|
| P1 | mathd_numbertheory_3 | Easy | ✅ 1r | — | ✅ 2r | native_decide |
| P2 | induction_12dvd4expnp1p20 | Easy | ✅ 8r | — | ✅ 2r | induction+simp |
| P3 | mathd_algebra_33 | Easy | ✅ 1r | — | ✅ 2r | field_simp+nlinarith |
| P4 | amc12a_2020_p10 | Medium | ❌ search_loop | ❌ | — | MCP搜索待优化 |
| P5 | algebra_sqineq_unitcircatbpabsamblt1 | Medium | ✅ 6r | — | — | 不等式推理 |
| P6 | amc12_2001_p5 | Medium | ❌ search_loop | ❌ | — | MCP搜索待优化 |
| P7 | algebra_amgm_sum1toneqn_prod1tonleq1 | Medium | ❌ compile | ❌ | ❌ | 需 nlinarith 策略 |
| P8 | imo_1959_p1 | Hard | ❌ stuck | ✅ 4r | ❌(不稳定) | API 一致性待解决 |
| P9 | aime_1983_p1 | Hard | ❌ search_loop | ❌ | — | MCP搜索待优化 |
| P10 | imo_1992_p1 | Hard | ❌ search_loop | ❌ | — | MCP搜索待优化 |

**Phase 0 Final**: 4/10 (40%), $0.63  
**Phase 1-2**: Easy 3/3 ✅ (100%), Medium 1/4 (25%), Hard 0/3 (0%)

## 4. Key Design Decisions

### 4.1 Loop Engineering > Prompt Engineering
- 核心思路：循环设计（Cadence/Gate/Feedback 三层）替代 prompt 工程
- 模型自主调用 tool_calls 搜索 + 编译迭代
- 拒绝做 pass@k 批量调用（token 浪费）

### 4.2 MCP Integration
- 使用 `lean-lsp-mcp`（streamable-http）获得真实 Mathlib 引理
- MCP 子进程需注入 `~/.elan/bin` 到 PATH
- 本地 loogle 需要 `v4.30.0-rc1` toolchain（symlink 到已安装的 v4.30.0）

### 4.3 Compile as Local Gate
- 编译是本地 gate（`lake env lean --stdin`），非 tool_call
- 节省一次 API 往返，直接注入错误反馈

### 4.4 Search Limit
- `max_search_rounds=2`：允许 2 轮搜索工具调用，之后强制写代码
- 强制后只保留 `lean_multi_attempt` 和 `lean_run_code`（移除搜索工具）

### 4.5 定理头必须从数据集严格加载
- 手写容易出错（曾因错误定理头浪费一次实验）

## 5. File Map

```
omega-architect/
├── omega/
│   ├── loop/
│   │   ├── inner.py              ← 核心 Inner Loop (搜索限制 + 自适应策略 + aesop fallback)
│   │   ├── deepseek_client.py    ← DeepSeek API 封装 (tool_calls + thinking模式)
│   │   ├── compile_gate.py       ← 本地 Lean 编译门 (缓存+错误分类)
│   │   ├── errors.py             ← 13 类错误分类器
│   │   ├── compress.py           ← 历史压缩 (每3轮丢弃旧 reasoning)
│   │   ├── mcp_client.py         ← MCP 异步客户端 (lean-lsp-mcp wrapper)
│   │   ├── mcp_sync.py           ← MCP 同步封装 (后台线程)
│   │   └── dialogue_cache.py     ← 成功对话缓存 (JSONL, SFT导出)
│   ├── resource/
│   │   ├── budget.py             ← 预算跟踪 ($2 cap)
│   │   └── tracker.py            ← 收敛检测 (converged/stuck/diverging)
│   └── verify/
│       └── t2_real.py            ← 真实 Lean 编译回调
├── docs/
│   ├── DEVELOPMENT_ROADMAP.md    ← 本文件
│   └── LOOP_ENGINEERING_PLAN.md  ← 旧版计划 (已归档)
├── AGENTS.md                     ← 状态机架构 (已引用本文件)
└── scripts/
    └── experiments/              ← 旧蓝图实验日志
```

## 6. How to Run

```bash
# 单题测试
cd /home/shenli/Gitlab/Agentic4Sci/omega-architect
python3 -c "
from omega.loop.inner import inner_loop, InnerLoopConfig
from omega.loop.mcp_sync import PersistentMcpClient
from omega.resource.budget import BudgetTracker
import os, json, re

os.environ['DEEPSEEK_API_KEY'] = open('/dev/stdin').read().strip()  # or use shell env

mcp = PersistentMcpClient()
mcp.initialize()

path = '.../dataset/minif2f.jsonl'
with open(path) as f:
    problems = [json.loads(l) for l in f if l.strip()]
formal = problems[0]['formal_statement']
formal = re.sub(r'∑\s+(\w+)\s+in\s+Finset', r'∑ \1 ∈ Finset', formal)

r = inner_loop(formal, theorem_name=problems[0]['name'],
               config=InnerLoopConfig(), budget=BudgetTracker(), mcp=mcp)
print('Proved!' if r.success else f'Stuck: {r.termination}')
"

# 查看缓存
python3 -c "
from omega.loop.dialogue_cache import DialogueCache
cache = DialogueCache()
print(f'Entries: {cache.count()}')
for p in cache.list_proofs():
    print(f'  {p[\"theorem_name\"]}: {p[\"rounds\"]}r \${p[\"cost_usd\"]:.4f}')
"
```

## 7. Glossary

| Term | Meaning |
|------|---------|
| Inner Loop | 单定理证明的 agentic loop: 搜索→写代码→编译→反馈→迭代 |
| Gate | 编译门 — 本地 Lean 编译校验，非 tool_call |
| Cadence | 指数退避 + jitter 重试逻辑 |
| Convergence Tracker | 检测 proof_length 和 error_count 的收敛/发散 |
| MCP | Model Context Protocol — lean-lsp-mcp 提供搜索工具 |
| Dialogue Cache | 成功对话 JSONL 缓存，可用于微调或 few-shot |
| `max_search_rounds` | 允许搜索工具调用的轮次上限，超限后强制写代码 |
| Adaptive Strategy | 根据错误类型注入针对性策略提示（默认关闭） |
