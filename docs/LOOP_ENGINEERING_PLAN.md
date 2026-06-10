# Ω-Architect Loop Engineering Plan

> **Doc version**: `0.1.0` — matches repo version bc6eca9
> **Last updated**: 2026-06-10 v0.2

> 从 Prompt Engineering 到 Loop Engineering 的范式转换
> 2026-06-10 | v0.2 — **消化 6 项批判性审查后重写**

---

## 修订记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.1 | 2026-06-10 | 初版 |
| v0.2 | 2026-06-10 | **消化 6 项审查修复 + 10 题分段开发集** |

---

## 1. 问题诊断

### 1.1 核心矛盾

当前 `omega-architect` 架构的核心断裂：

| 方向 | AGENTS.md 设计 | 实际代码 |
|------|---------------|---------|
| 编排 | 7 状态状态机 + delegate_task 子Agent | `orchestrator.py` 全 stub（6 个空壳 handler） |
| 证明 | 3 prover ensemble + skill 原语路由 | `passk.py` 单 prompt 发 API → 解析 → 编译 → 下一题 |
| 搜索 | leansearch/loogle 通过 MCP 工具调用 | 文本注入 prompt（实验证明无效） |
| 循环 | 蓝图精炼 + 假引理拆分 | 无真实循环，pass@k 碰运气 |

### 1.2 范式对比

```
Benchmark 范式（当前，不适合）:
  load N theorems
  for each theorem:
    1 prompt → parse → compile → log pass/fail
  report pass@k

Loop 范式（目标）:
  for each theorem:
    while not proved and budget remaining:
      think → tool_call(search) → code → compile → error → fix → recompile
  report proved_count
```

---

## 2. 三项核心假设变更（从 v0.1 修正）

### 变更 1：自底向上证明树，而非 LLM 蓝图 DAG

**v0.1 错误假设**：LLM 可以预先生成正确的子引理 DAG。
**v0.2 修正**：证明树 **自底向上生长**。

新流程：
1. 初始只有一个节点（定理本身）
2. Inner Loop 尝试证明 → 编译失败 → 从错误中提取缺失的引理 → 生长新节点
3. 新引理被证明后，父节点重试
4. LLM 只参与**节点证明**，不参与**节点生成**

这解决了旧架构中"假引理"的根因——LLM 不能预知证明需要什么子引理。

### 变更 2：编译作为局部 gate，而非 tool_call

**v0.1 错误假设**：`lean_run_code` 应该注册为 tool_call，让模型决定何时编译。
**v0.2 修正**：编译是纯本地确定的操作，不需要模型参与决策。

新设计：
```
模型输出 Lean 代码（content 字段）
      ↓
本地编译验证（lake env lean --stdin）
      ↓
通过 → 完成
失败 → 编译 error 注入下一轮 prompt → 模型修复
```

**收益**：每轮节省 1 次 API 往返（-50% API 调用量）。

### 变更 3：历史压缩 + Token 预算模型

**v0.1 错误假设**：20 轮消息历史可以无限增长。
**v0.2 修正**：引入 Token 预算和 checkpoint 压缩。

| 轮次范围 | 策略 |
|---------|------|
| last 2 轮 | 完整 `reasoning_content` + `content` |
| 3-5 轮前 | 只保留 `content`（去掉 reasoning） |
| 5 轮前 | 只保留 tool_call 结果摘要 |

---

## 3. Loop Engineering 总架构

### 3.1 三层循环

```
Outer Loop: Curriculum Scheduler
  (按 easy → medium → hard 顺序出题)
  [Budget tracker → 决定继续/停止]
          │
          ▼
  Middle Loop: Theorem Proving Loop
  (一道定理的完整解题生命周期)
  [单节点 → 证明 → 生长 → 编译 → 完成]
          │
          ▼
  Inner Loop: Tool-Calling Proof Cycle
  (每个引理的 thinking → search → code → compile → fix 循环)
  [token budget 内循环 → 用 tool_calls 搜索但编译做 gate]
```

### 3.2 Inner Loop 详细协议

```python
def inner_loop(theorem_header: str, token_budget: int = 32000) -> ProofResult:
    """
    关键设计决策（基于 6 项审查）:
    1. lean_run_code 不是 tool_call — 编译在本地作为 gate
    2. 每 3 轮压缩历史（去除旧 reasoning_content）
    3. 编译错误按 13 类分类检测死循环
    4. thinking mode = enabled（不关掉核心能力）
    """
    messages = [system_prompt, user_prompt]
    errors_seen = {}   # error_class → count
    
    for round in range(MAX_ROUNDS):
        # 每 3 轮压缩历史
        if round > 0 and round % 3 == 0:
            messages = compress_history(messages)
        
        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=messages,
            tools=SEARCH_TOOLS,      # 只有搜索/查目标工具
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        
        msg = response.choices[0].message
        messages.append(msg)
        
        # 处理 tool_calls（搜索/查目标 — 不包含编译）
        for tc in (msg.tool_calls or []):
            result = execute_tool(tc)
            messages.append({"role": "tool", ...})
        
        # 模型输出最终 Lean 代码 → 本地编译
        code = extract_lean_code(msg)
        compile_result = local_compile(code)
        
        if compile_result.verified:
            return ProofResult(success=True, code=code)
        
        # 编译错误分类 + 死循环检测
        err_class = classify_error(compile_result.errors[0])
        errors_seen[err_class] = errors_seen.get(err_class, 0) + 1
        
        if errors_seen[err_class] >= 3:
            # 同一类错误出现 3 次 → 标记 IRREDEEMABLE
            return ProofResult(success=False, error=f"stuck: {err_class}")
        
        # 注入编译错误 → 下一轮修复
        messages.append({
            "role": "tool",
            "tool_call_id": "compile_gate",
            "content": format_compile_error(compile_result)
        })
    
    return ProofResult(success=False, error="max_rounds")
```

### 3.3 编译错误分类（13 类）

| 类号 | 错误模式 | 检测方式 | 死循环阈值 |
|------|---------|---------|-----------|
| E01 | 未知标识符 `unknown identifier` | 正则 | 3 次 |
| E02 | 类型不匹配 `type mismatch` | 正则 | 3 次 |
| E03 | 语法错误 `syntax error` | 正则 | 3 次 |
| E04 | 宇宙约束 `universe` | 正则 | 3 次 |
| E05 | 函数预期 `function expected` | 正则 | 3 次 |
| E06 | 未使用变量 `unused` | 正则 | 3 次 |
| E07 | 无法合成 `failed to synthesize` | 正则 | 3 次 |
| E08 | 歧义 `ambiguous` | 正则 | 3 次 |
| E09 | 超时 `heartbeat` / `timeout` | 正则 | 2 次 |
| E10 | 内存 `memory` | 正则 | 2 次 |
| E11 | 循环依赖 `cyclic` | 正则 | 1 次 |
| E12 | 文件/IO `file` / `IO` | 正则 | 3 次 |
| E13 | 未知模式 `fallback` | catch-all | 5 次 |

---

## 4. DeepSeek API 正确用法

### 4.1 当前浪费 → 修正

| API 功能 | v0.1 状态 | v0.2 状态 | 用法 |
|---------|-----------|-----------|------|
| `thinking_mode` | ❌ 已禁用 | ✅ **启用** | `extra_body={"thinking": {"type": "enabled"}}` |
| `tool_calls` | ❌ 未用 | ✅ **用于搜索** | 注册 `lean_loogle`、`lean_search`、`lean_goal` |
| `multi_round` | ❌ 单轮 | ✅ **外循环** | 消息历史管理 + 错误注入 |
| `prefix_completion` | ❌ 未用 | ⚠️ 可选 | `beta` 功能，非必须 |
| `FIM completion` | ❌ 未用 | ❌ 暂不使用 | 适用场景窄（已知开头结尾） |
| `strict JSON mode` | ❌ 未用 | ⚠️ **Beta 测试中** | Phase 0 先测 strict+thinking 兼容性 |
| `lean_run_code` tool_call | — | ❌ **改为本地 gate** | 编译不经过 API，节省 50% 调用 |

### 4.2 工具定义（仅用于搜索）

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lean_goal",
            "description": "Get current Lean proof goal at a position in the file",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "line": {"type": "integer"}
                },
                "required": ["file_path", "line"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lean_loogle",
            "description": "Search Mathlib by type signature: e.g. '?a + ?b = ?b + ?a'",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lean_search",
            "description": "Semantic search in Mathlib by natural language description",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"],
                "additionalProperties": False
            }
        }
    }
]
```

---

## 5. 10 题分段开发集

### 5.1 选择标准

- **Easy（3 题）**：单 binder + 典型结构（归纳/代数求解/模运算）
- **Medium（4 题）**：多 binder + 基本不等式/AMC
- **Hard（3 题）**：IMO/AIME + 多步推理

全部来自 MiniF2F `test` split（244 题）。

### 5.2 题目清单

| # | 难度 | 名称 | 定理 | 期望 | 备注 |
|---|------|------|------|------|------|
| 1 | 🔵 Easy | `mathd_numbertheory_3` | `(∑ x in range 10, (x+1)²) % 10 = 5` | 简单算术 + `simp` | 0 个外部依赖 |
| 2 | 🔵 Easy | `induction_12dvd4expnp1p20` | `∀ n:ℕ, 12 ∣ 4^(n+1) + 20` | `induction` + `simp` | 单 binder，典型归纳 |
| 3 | 🔵 Easy | `mathd_algebra_33` | `(x y z:ℝ) → 2x=5y → 7y=10z → (35x-24z)/(...)=?` | `field_simp` + `ring` | 代数求解 |
| 4 | 🟡 Medium | `amc12a_2020_p10` | `(n:ℕ) → 1<n → log₂(...)?` | 对数运算 | AMC 风格 |
| 5 | 🟡 Medium | `algebra_sqineq_unitcircatbpabsamblt1` | `(a b:ℝ) → a²+b²=1 → |a-b|≤1` | 不等式 + `nlinarith` | |
| 6 | 🟡 Medium | `amc12_2001_p5` | 乘积/组合问题 | `calc` + 数论 | AMC 12 2001 P5 |
| 7 | 🟡 Medium | `algebra_amgm_sum1toneqn_prod1tonleq1` | `(a:ℕ→NNReal) → ∑a_i / n ≥ (∏a_i)^(1/n)` | AM-GM 不等式 | 需要 `Real` 库 |
| 8 | 🔴 Hard | `imo_1959_p1` | `(n:ℕ) → 0<n → gcd(21n+4,14n+3) = 1` | Euclid 算法 + `Nat.gcd` | IMO 1959 P1 |
| 9 | 🔴 Hard | `aime_1983_p1` | `(x y z w:ℕ) → x^? ...` | 指数 + 素因数分解 | AIME 1983 P1 |
| 10 | 🔴 Hard | `imo_1992_p1` | `(p q r:ℤ) → 1<p<q<r → ...` | 方程求解 + 整除 | 历史难题（之前实验失败） |

### 5.3 增量验证策略

```
Phase 0a: 只跑 Problem 1（最简单的，确保管线通）
Phase 0b: 跑 Easy 3 题（验证基础能力）
Phase 0c: 跑 Medium 4 题（验证不等式/多 binder）
Phase 0d: 跑 Hard 3 题（验证复杂推理）
Phase 1:  10 题全部通过 T2 编译
```

每一阶段验证通过后，再进入下一阶段。绝不在基础设施未稳时跳级。

---

## 6. 实施计划（Phase 0-1）

### Phase 0a — 最小可行管线（0.5 天）

**目标**：1 题（mathd_numbertheory_3）端到端 Inner Loop 跑通。

| # | 任务 | 文件 | 交付标准 |
|---|------|------|---------|
| 0a.1 | 创建 `omega/loop/` 包 | `omega/loop/__init__.py` | 可以 `import omega.loop` |
| 0a.2 | 编译错误分类器 | `omega/loop/errors.py` | 13 类正则，`test_errors.py` 通过 |
| 0a.3 | 本地编译 gate | `omega/loop/compile_gate.py` | 调用 `lake env lean --stdin`，返回结构化诊断 |
| 0a.4 | DeepSeek tool_calls 客户端（v4-pro） | `omega/loop/deepseek_client.py` | **测试 strict + thinking 兼容性**；返回流式 tool_calls |
| 0a.5 | 历史压缩器 | `omega/loop/compress.py` | 每 3 轮丢弃旧 reasoning，保持 ≤8K token |
| 0a.6 | 单题端到端测试 | `tests/test_loop/test_e2e_p1.py` | `mathd_numbertheory_3` 通过 Inner Loop → T2 编译 |

### Phase 0b — Easy 3 题（0.5 天）

| # | 任务 | 交付标准 |
|---|------|---------|
| 0b.1 | 运行 Problems 1-3 | 3/3 通过 T2 |
| 0b.2 | 失败分析 | 记录每题的：轮数、token 消耗、错误类型分布 |
| 0b.3 | 识别通用漏洞 | 检查是否所有题都在 `errors_seen` 阈值内完成 |

### Phase 0c — Medium 4 题（1 天）

| # | 任务 | 交付标准 |
|---|------|---------|
| 0c.1 | 运行 Problems 4-7 | ≥3/4 通过 T2 |
| 0c.2 | `nlinarith`/`positivity` 等 tactic 兼容性 | 确保不等式题有正确的导入 |
| 0c.3 | IRREDEEMABLE 检测验证 | 确保真的死循环能正确终止（不浪费 token） |

### Phase 0d — Hard 3 题（1 天）

| # | 任务 | 交付标准 |
|---|------|---------|
| 0d.1 | 运行 Problems 8-10 | ≥2/3 通过 T2 |
| 0d.2 | 多引理自底向上证明树 | 第 10 题（imo_1992_p1）可能需要生长 3+ 子引理 |
| 0d.3 | 验证 `compress_history` 效果 | 对比有无压缩的 token/轮数比值 |

### Phase 1 — 工具完善 + 跨题缓存（1 天）

| # | 任务 | 交付标准 |
|---|------|---------|
| 1.1 | 证明归档 + 策略模式匹配 | 已证定理的 `(binder_types, target_shape)` → 缓存策略 |
| 1.2 | Cluster runner | 并行运行 10 题，JSONL 日志 |
| 1.3 | 结果分析报告 | 10 题通过率、平均轮数、token 成本 |
| 1.4 | 对比 v0 旧管线 | pass@k 56% vs loop solve count |
| 1.5 | 识别 Phase 2 瓶颈 | 分析 10 题失败模式 → 决定下一步优先级 |

---

## 7. 验证标准矩阵

| 阶段 | 度量 | 目标 | 硬截止 |
|------|------|------|--------|
| 0a | 1 题通过 T2 | ✅ | 最多 50 token |
| 0b | 3 题通过率 | ≥ 3/3 | 最多 30K token/题 |
| 0c | 7 题通过率 | ≥ 5/7 | 最多 50K token/题 |
| 0d | 10 题通过率 | ≥ 7/10 | 最多 100K token/题 |
| 1 | 10 题通过率 | ≥ 8/10 | 无额外 token 限制 |
| 1 | vs baseline(pass@k 56%) | 胜出 | 解题数 > 6/10 |

---

## 8. 6 项审查修复对照表

| # | 审查裂缝 | v0.1 问题 | v0.2 修复 | 状态 |
|---|---------|-----------|-----------|------|
| ① | Token 增长失控 | 20 轮 × 历史增长 → 200K+ tokens | 每 3 轮 `compress_history` | 📝 待实现 |
| ② | lean_run_code 2× API 浪费 | tool_call 每轮 2 次 API 往返 | 编译变为本地 gate | 📝 待实现 |
| ③ | strict + thinking 兼容性 | 计划中用 strict 但没验证 | Phase 0a 先测试；不行就降级 | 🔬 待测试 |
| ④ | LLM 蓝图产生假引理 | LLM 生成 DAG → 类型错误 | 自底向上证明树，LLM 不生成节点 | 📝 待实现 |
| ⑤ | 跨定理缓存缺失 | 每题从头开始 | 策略模式匹配 + Proof Archive | 📝 Phase 1 |
| ⑥ | 死循环检测不完全 | 字符串相等 → 遗漏振荡 | 13 类 error 分类 + 同类 3 次触发 | 📝 待实现 |

---

## 9. 代码废弃计划

| 文件 | 处理 |
|------|------|
| `omega/search/passk.py` | 保留（Phase 1 对比 baseline） |
| `omega/search/matlas_cache.py` | **删除** — Matlas 搜索已证明无效 |
| `omega/search/lean_search.py` | 代码保留 — 搜索函数改由 tool_calls 调用 |
| `omega/agent/orchestrator.py` | 暂保留 | 
| `omega/search/blueprint.py` | **标记 deprecated** — 用自底向上证明树替代 |
| `scripts/run_blueprint_benchmark.py` | 标记 deprecated — 用 `omega/loop/runner.py` 替代 |
|---

## 9. 硬件资源与并行加速策略

### 9.1 可用资源

| 资源 | 规格 | 用途 |
|------|------|------|
| CPU | 32 核 (64 HT) | 编译 gate 并行、数据处理 |
| RAM | 46 GB (40 GB free) | 多定理上下文、汇编缓存 |
| GPU | RTX 4500 Ada 24GB | **保留给其他任务** — Inner Loop 不使用 GPU |
| Mathlib 缓存 | 6.7 GB / 8109 oleans @ `lean-paper-plane/.lake/packages/mathlib/` | 编译加速 |
| 编译命令 | `lake env lean --stdin` (~2.5s/定理) | 通过 `t2_real.py` 调用 |

**设计原则**：Inner Loop 只依赖 DeepSeek API（I/O bound）+ 本地编译（CPU bound）。GPU 完全不占用，留给 GSNV/CUDA 任务。

### 9.2 编译 gate 加速策略

#### 策略 1：编译结果缓存（Proof Archive）

```python
class CompileCache:
    """缓存已编译定理的 hash → (success, errors, olean_path)。"""
    
    def __init__(self, cache_dir: str = "~/.cache/omega/compile/"):
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._db = self._load_db()  # SQLite or JSON
    
    def _code_hash(self, code: str) -> str:
        """对 Lean 代码取 hash（去除注释/空白后）。"""
        normalized = self._normalize(code)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]
    
    def get(self, code: str) -> CompileResult | None:
        h = self._code_hash(code)
        return self._db.get(h)
    
    def put(self, code: str, result: CompileResult):
        h = self._code_hash(code)
        self._db[h] = result
        self._save_db()
```

#### 策略 2：preamble 预编译

Inner Loop 中，同一题的编译之间只有 proof 代码变化，import 部分不变。

```python
# 每次 inner loop 编译时，使用缓存的前言部分
# 如果只改了 proof 内容，import 部分不需要重编译
# Lean 自身已经做了增量编译，但 lake env 每次都是新进程
# 优化：如果 error 出在后半部分代码，只发送 diff 重编译
```

#### 策略 3：编译错误重定向

`lake env lean --stdin` 的输出包含行号+列号。直接解析为 13 类错误格式，无需额外处理。已存在的 `parse_lean_diagnostics()` 函数可直接复用。

### 9.3 并行加速策略

#### 三层并行度

```
层 1 — 题间并行 (theorem-level):
  多个定理同时进行 Inner Loop（各自独立 API 调用）
  max_theorems = 4 (基于 API rate limit + token budget)
  实现: concurrent.futures.ThreadPoolExecutor(max_workers=4)

层 2 — 编译并行 (compile-level):
  多篇待编译代码并行调用 lake env lean --stdin
  max_compiles = 8 (32 核留 75% 余量)
  实现: concurrent.futures.ProcessPoolExecutor(max_workers=8)
        或 asyncio + subprocess（避免 GIL）

层 3 — 子引理并行 (sub-lemma-level):
  自底向上证明树中，同一深度的无依赖引理可并行证明
  通过 Proof Archive 同步状态
```

#### 资源预留规则

| 资源 | 使用上限 | 预留 |
|------|---------|------|
| CPU 编译线程 | max_workers=8 (25% of 32) | 其余 75% 留系统+其他 |
| 内存 | ≤ 16 GB | 留 30 GB 给其他任务 |
| GPU | **不使用** | 全部 24 GB 保留 |
| API 并发 | ≤ 4 题同时 | DeepSeek rate limit ~100 RPM |

#### Backpressure 机制

```python
class ResourceController:
    MAX_COMPILE_QUEUE = 16
    MAX_THEOREMS = 4
    MAX_MEM_GB = 16
    
    def can_start_theorem(self) -> bool:
        return (
            len(self.active_theorems) < self.MAX_THEOREMS
            and len(self.compile_queue) < self.MAX_COMPILE_QUEUE
            and self.current_memory_gb() < self.MAX_MEM_GB
        )
    
    def wait_for_slot(self, timeout: int = 300):
        """阻塞直到有空闲资源或超时。"""
        while not self.can_start_theorem():
            time.sleep(1)
            timeout -= 1
            if timeout <= 0:
                raise ResourceTimeout("No slot available")
```

### 9.4 与现有 `t2_real.py` 的集成

现有 `real_compile_callback()` 已经完整实现了：
- `lake env lean --stdin` 调用
- `parse_lean_diagnostics()` 输出解析
- Mathlib 缓存路径自动发现（通过 `omega.toml [lean]`）

```
编译 gate 直接复用 make_real_compile_callback():
    from omega.verify.t2_real import make_real_compile_callback
    
    compile_fn = make_real_compile_callback(timeout=60)
    result = compile_fn(lean_code)
    # result.diagnostics → 13 类错误分类
    # result.exit_code → 0 = 通过
```

不需要重写编译逻辑，只需要包装错误分类器和缓存。

---

## 10. 并行加速验证标准

| 检查项 | 目标 | 测量方式 |
|--------|------|---------|
| 4 题并行时 wall-time ≤ 串行 30% | ≤ 30% | 同样 4 题先串行后并行 |
| 编译 gate 峰值 CPU ≤ 25% | ≤ 25% | `top -bn1` 采样 |
| GPU 内存占用 = baseline | 0 额外 | `nvidia-smi` 前后对比 |
| 总内存 ≤ 16 GB | ≤ 16 GB | `free -m` |
| Backpressure 触发正常 | 队列 ≥ 16 时暂停 | 单元测试 |

---

## 附录：Phase 0a 文件清单

```
omega/loop/
├── __init__.py          # 包声明
├── errors.py            # 13 类编译错误分类器
├── compile_gate.py      # 本地 lean --stdin 编译
├── deepseek_client.py   # DeepSeek v4-pro tool_calls + thinking
├── compress.py          # 历史压缩
├── inner.py             # Inner Loop 主循环
└── runner.py            # 单题运行器 + 日志

tests/test_loop/
├── __init__.py
├── test_errors.py       # 13 类正则测试
├── test_compile_gate.py # 编译 gate 集成测试（需 Lean 环境）
└── test_e2e_p1.py       # Problem 1 端到端
```
