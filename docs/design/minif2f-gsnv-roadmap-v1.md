# Omega MiniF2F & GSNV Formal Proof Roadmap

> 基于 Phase 0 实验数据的证据驱动迭代规划。
> 设计原则：先测量，再推断，后优化。
>
> 核心发现：**GoedelProver + ChatOllama + 真实 T2 完整管线已通**。
> 瓶颈不是基础设施，而是采样策略和模板机制的效率。

---

## Phase 0 实验报告（已完成）

### 实验设置

| 维度 | 值 |
|------|-----|
| 定理 | `mathd_algebra_141`: 给定 `a*b=180`, `2(a+b)=54`, 证 `a²+b²=369` |
| 模型 | qwen3-coder:30b via ChatOllama |
| T2 | lean-paper-plane (Mathlib) 真实编译 |
| 采样 | `num_samples=2`, `max_correction_rounds=1` |
| 耗时 | **199.4s** / 15 attempts / **SUCCESS** |
| 通过方式 | LLM 生成中间引理 `h5` → 模板 tactic `exact h5` 关闭证明 |

### 实验结果

```
Pass rate: 1/1 (100%) ✅
Total attempts: 15
Total T2 compiles: 15
Total errors: 26

Error type distribution:
  unsolved_goal         : 17 (65.4%) ████████████████████████████████
  syntax                :  6 (23.1%) ████████████
  other                 :  3 (11.5%) ██████
  type_mismatch         :  0          (LLM 理解类型)
  unknown_identifier    :  0          (LLM 理解 Mathlib API)
  unknown_tactic        :  0          (LLM 使用正确 tactic)
```

### 三个核心定量发现

> **⚠️ Caveat**: 当前 Phase 0 数据仅基于 **1 个定理**（`mathd_algebra_141`，代数域 easy）。
> 以下统计推断是**初步假设**，需 Step 4（50 定理 mini benchmark）验证泛化性。
> 特别是数论/组合/类型论域的定理可能有完全不同的错误分布。

**1. 管线可工作（最重要）**

ChatOllama → GoedelProver → 真实 T2 编译 → 证明通过。从 0/244 到可证明定理只需配置正确、样品足够。

**2. 65% 的错误是 "unsolved goals"**

这意味着 LLM 生成了**结构正确的 Lean 代码**——语法对、类型对、imports 对——只是证明没有走到底。这不是"模型不会用 Lean"，而是"模型写到一半停了"。

这比"模型生成垃圾"好得多：它意味着增加 `num_samples` 和一个更好的终止策略就能直接提升通过率。

**3. 成功的证明来自 LLM+模板的协作，而非单独一方**

```
LLM 生成: have h5 : a^2 + b^2 = 369 := by nlinarith
模板 tactic: exact h5
成功条件: h5 在作用域内
```

单一 LLM `have` 语句 + 模板 `exact` = 命题正确。这是系统设计成功的标志——LLM 负责"创造引理"，模板负责"机械闭合"。

---

## Phase 1 — MiniF2F T2 pass rate（5-7天，目标：根据实验动态设定）

### 理论基础：错误分布 → 策略优化

基于实验数据，每个错误类型对应不同的修复策略：

| 错误类型 | 占比 | 根因 | 修复策略 | 预期收益 |
|---------|------|------|---------|---------|
| `unsolved_goal` | 65% | 证明写到一半停下 | 1) 增加 `num_samples` (2→8) | +35% |
| | | | 2) 强制 LLM 输出完整 `:= by ...` block | +25% |
| | | | 3) 模板系统自动补 `done`/`exact` | +15% |
| `syntax (expected '{')` | 23% | `have h : P := by` 后缺块 | 1) Prompt 工程：`"always use := by { ... }"` | -20% |
| | | | 2) `_build_lean_code` 自动补 `{ }` | -3% |
| `other` | 12% | import 错位、rw pattern 不存 | 1) 错误注入 negative examples | -10% |
| | | | 2) `rw` 前先 `simp` 展开 | -2% |

**关键洞察**：65% + 23% = **88% 的错误是 LLM 写对了但没写完或格式不对**。这不像传统 ML 的"模型能力不够"，更像是"提示词/采样不够多"。

### 信息论下限

对 `unsolved_goal` 类错误，假设正确证明需要 LLM 连续生成 k 个正确决策。如果单步准确率 p，则完整证明概率 = p^k。

实验观测到的成功模式是：
- LLM 正确生成了 `have h5`（中间引理）← 一次采样
- 模板系统提供了 `exact h5` ← 不需要 LLM 完成

这表明**模板系统和 LLM 的协作降低了有效 k 值**。不需要 LLM 独立完成整个证明，只需要 LLM 覆盖"关键创造步骤"，模板覆盖"机械步骤"。

### 执行计划

#### Step 1: 修复语法错误（1天）

为目标：消除 23% 的 syntax 错误。

**Prompt 工程**：
```python
# proposer.py 中添加到 prompt
"IMPORTANT: Always close `:= by` blocks with a proper tactic. "
"Instead of:\n"
"  have h : P := by\n"
"  tactic\n"
"Use:\n"
"  have h : P := by\n"
"    tactic\n"
"  \n"
```
**`_build_lean_code` 防御**（拒绝而非修复不完整代码）：

```python
# go_prover.py _build_lean_code 中
# 如果 suggestion.tactic 以 := by 结尾（无后续内容），
# 这是一个不完整的证明——应当拒绝而不尝试编译
# 因为用 `sorry` 占位会让 T2 pass 但证明虚假
if re.search(r':=\s+by\s*$', suggestion.tactic):
    logger.warning("Rejecting incomplete suggestion: tactic ends with `:= by`")
    return None  # caller skips this suggestion
```

**同时**，`GoedelProver` 现在包含 `_contains_sorry()` 检测，任何包含 `sorry` 或
`admit` 的 T2 通过证明会设置 ``GoedelResult.contains_sorry = True``，
并使 ``result.succeeded`` 返回 ``False``：

```python
# go_prover.py — 已在代码中实现
def _contains_sorry(lean_code: str) -> bool:
    \"\"\"检查 Lean 代码是否包含 `sorry` 或 `admit` 关键词。
    排除注释中的匹配。
    \"\"\"
    text = re.sub(r"--[^\n]*", "", lean_code)          # 移除行注释
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)  # 移除块注释
    return bool(re.search(r"\bsorry\b|\badmit\b", text))
```

**验证指标**：syntax error 占比从 23% 下降到 <5%。

#### Step 2: 增加采样 + 减少模板浪费（1天）

**问题**：`GoedelProver(num_samples=2)` + `suggest_trivial_tactics` 产生约 8 个模板尝试，其中 `simp`, `rw [h]`, `norm_num` 等对复杂命题立即失败。每次模板尝试耗费 ~6s T2 编译且几乎不贡献信息。

**解决方案**：将 `num_samples` 从 2 提至 6，同时将模板尝试延迟到 LLM 尝试之后：

```python
# proposer.py make_llm_proposer 中
# 将模板 tactic 的优先级低于 LLM 输出
# 当前顺序：模板 → LLM（suggestions.extend）
# 改为：LLM → 模板
suggestions = llm_suggestions + template_suggestions
```

**验证指标**：
- 单定理通过率提升
- 模板尝试的"throughput"（每失败的模板尝试浪费多少时间）

#### Step 3: 分析而非缓存 T2 编译结果（1天）

**问题**：同一定理的不同尝试经常编译相同的 snippet。但简单的 `lru_cache` 按
`lean_code` 全文匹配，**实际命中率远低于预期**：

| 场景 | lean_code 示例 | 命中？ |
|------|--------------|-------|
| 同定理的 `simp` 尝试 × 2 | `"theorem A ... := by\\n  simp"` vs 相同字符串 | ✅ 命中 |
| **不同定理**的 `simp` 尝试 | `"theorem A ..."` vs `"theorem B ..."` | ❌ header 不同 |
| 同定理 `exact h3` vs `exact h5` | `"... exact h3"` vs `"... exact h5"` | ❌ 差 1 字符 |

估计实际命中率：**<5%**。20-30% 的原始估值过于乐观。

**替代方案**（推迟到 Phase 2 如果 Step 4 验证模板浪费显著）：

```python
# 缓存思路：按"去掉定理头的代码骨架"缓存
# 但骨架提取（删除具体标识符）是 NLP 级别的去重——1 天不够
# Phase 1 暂不实现，直接编译
```

**当前决策**：Phase 1 不实现 T2 缓存。模板浪费通过 Step 2（排序
LLM→模板）来减少频率，而非缓存。如果 Step 4 数据显示模板尝试占比
>40%，则在 Phase 2 引入去骨架缓存。

#### Step 4: 跑 50 定理 mini benchmark（1天）

选择 50 个 MiniF2F 定理覆盖：
- 代数 (15)
- 数论 (15)
- 组合/不等式 (10)
- 分析 (10)

记录每定理的：
- pass/fail
- error type 分布
- 耗时
- 通过的方式（LLM 完整证明 / LLM+模板 / 模板单独）

#### Step 5: 动态目标设定 + 模型切换策略（1天）

基于 Step 4 的数据，进行 **模型切换门禁判断**：

```python
# 根据 50 定理 pass rate 自动决策下一步策略
if step4_pass_rate < 5%:
    # ⚠️ 模型不够强：qwen3-coder:30b 无法胜任
    options = [
        ("deepseek-r1:8b",     "更快但可能更弱，1天对比实验"),
        ("qwen3.6:latest",     "36B dense, 可能更好，2天基准"),
        ("API: deepseek-v4-pro", "更强但需预算 $0.28/M tok，0.5天集成"),
    ]
    action = "运行模型对比实验，选择最佳 → 重启 Phase 1"
    phase_1_restart = True
    target_pass_rate = "待定（取决于最佳模型）"

elif step4_pass_rate >= 30%:
    # ✅ 模型足够，策略优化
    action = "增加 num_samples (6→12) + correction_rounds (2→3)"
    target_pass_rate = "60% 可能可达"
    phase_1_restart = False

else:  # 5% ≤ pass_rate < 30%
    # ⚠️ 模型能力临界
    action = """策略优化 + 模型对比同时进行：
      1. 增加 num_samples (6→12), correction_rounds (2→3)
      2. 并行运行 deepseek-r1:8b 对比实验
      3. 取两者通过率之和"""
    target_pass_rate = f"max({step4_pass_rate*2}%, optimistic)"
    phase_1_restart = False
```

回答以下问题：

1. **天花板在哪？** — qwen3-coder:30b + 当前策略的通过率上界
2. **瓶颈是模型还是策略？** — 如果错误仍以 `unsolved_goal` 为主，说明需要更多采样或更精准的 prompt
3. **60% 是否可达？** — 如果 step 4 通过率 >30%，则在增加 `num_samples=12` + `correction_rounds=3` 后 60% 可能可达

**输出**：一份基准报告，包含每个定理的 error taxonomy、pass rate 精算、以及修正后的 Phase 1 目标。

### Phase 1 验证标准

```python
VERIFICATION_MATRIX = {
    "syntax_error_rate": {"target": "<5%", "measure": "errors_with_syntax / total_errors"},
    "avg_t2_cache_hit_rate": {"target": ">20%", "measure": "cache_hits / total_compiles"},
    "attempt_efficiency": {"target": "<8s/attempt", "measure": "total_time / n_attempts"},
    "llm_vs_template_ratio": {"target": ">60% LLM-sourced passes", 
                               "measure": "passes_via_llm / total_passes"},
}
```

---

## Phase 2 — 可观测性与系统鲁棒性（3天）

### 前置条件

Phase 1 完成后，MiniF2F 基准通过率已知且策略已验证。Phase 2 专注于：
1. 缩短迭代周期（开发者 debug 体验）
2. 降低运维风险（Ollama 断连、T2 死锁等）
3. 提供跨 session 的证明传播能力

### 2.1 实时监控面板

**当前状态**：`OmegaRunner` 已有 `progress_callback`，但输出到 stdout/回调函数，无持久化。

**目标**：运行时状态实时可查，支持 2/4/8h 无人值守时故障恢复。

```python
# 新增 omega/monitor.py
class ExperimentMonitor:
    """持久化运行时监控。
    
    将 progress_callback 的事件写入 JSONL 文件，
    支持 WebSocket/文件轮询两种读取方式。
    """
    def __init__(self, log_path: str = "~/.omega/monitor/"):
        self._path = Path(log_path).expanduser()
        self._path.mkdir(parents=True, exist_ok=True)
    
    def on_progress(self, event: ProgressEvent) -> None:
        """写入 JSONL 行。"""
        entry = {
            "timestamp": datetime.now(ISO).isoformat(),
            "theorem": event.theorem_header[:80],
            "status": event.status,
            "attempts": event.n_attempts,
            "elapsed_s": event.elapsed_s,
            "budget_pct": f"{event.budget_remaining_pct:.1f}%",
        }
        with open(self._path / "run.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
```

### 2.2 健康检查

**问题**：当前 `_make_generate_fn` / `make_langchain_generate_fn` 对 Ollama 断连无感知。断开 5+ 分钟可能浪费在静默失败上。

**目标**：在每次 `GoedelProver.run()` 前做轻量健康检查。

```python
# omega/llm.py 中新增
def check_ollama_health(base_url: str = DEFAULT_OLLAMA_HOST) -> bool:
    """Ping Ollama health endpoint. Returns False if unreachable."""
    import httpx
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False
```

### 2.3 跨定理证明传播

**当前状态**：`KnowledgeProver` 已有 `use_cache=True`，但缓存仅在单次 `run()` 内生效。跨不同定理的证明不能共享。

**存储方案选择**：JSONL vs SQLite

| 维度 | JSONL（`omega/data.py` 现有） | SQLite |
|------|-----------------------------|--------|
| 读性能 | O(n) 扫描 (n<10000 可接受) | O(1) B-tree 索引 |
| 写方式 | append-only，无锁冲突 | INSERT，支持并发 |
| 与现有数据层一致性 | ✅ 统一 | ❌ 第三种存储格式 |
| 架构复杂度 | 低（已有 RecordWriter） | 中（需初始化 schema） |
| 适合场景 | 实验记录、流式处理 | 高频随机访问缓存 |

**决策**：Phase 2 使用 **JSONL**（与实验数据层统一）。如果后续缓存规模
>10万条，迁移到 SQLite。理由：
1. 缓存记录数 = 已证明定理数 ≤ 244（MiniF2F）+ 自定义定理 ≤ 1000
2. O(n) 扫描在 1000 条级别是亚毫秒级
3. 统一数据格式降低维护成本

```python
# omega/data.py — 扩展 RecordWriter/Reader（已在代码中）
# 缓存文件：~/.omega/proof_cache.jsonl
# 每行一个 ProofRecord，append-only
```

```python
class ProofCache:
    """持久化证明缓存 (JSONL 后端)。
    
    在 GoedelProver 找到通过 T2 的证明后自动写入，
    在 Proposer 的 `prove` 流程前通过 theorem_id 查询。
    """
    def __init__(self, path: str = "~/.omega/proof_cache.jsonl"):
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
    
    def lookup(self, theorem_id: str) -> str | None:
        """扫描 JSONL，返回第一个匹配的 lean_code。"""
        if not self._path.exists():
            return None
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("theorem_id") == theorem_id and record.get("succeeded"):
                    return record.get("proof_preview") or ""
        return None
    
    def save(self, theorem_id: str, lean_code: str) -> None:
        """追加一条缓存记录。"""
        with RecordWriter(self._path) as w:
            w.write(ProofRecord(
                theorem_id=theorem_id,
                succeeded=True,
                proof_preview=lean_code[:200],
                source="proof_cache",
            ))
```

### 2.4 Langfuse Trace 深度集成

**当前**：Langfuse 通过 ChatOllama callback 自动捕获 LLM 调用级别的事件。
所有 CallbackHandler 共享同一个 Langfuse 客户端单例（通过 ``omega/llm._get_langfuse_client()``）。

**目标**：增加 GoedelProver 级别（含 T2 编译）的 span，**确保 T2 span
与 LLM span 在同一个 trace 树中**。

**关键设计决策**：T2Tracer 通过 ``_get_langfuse_client()`` 获取 Langfuse 客户端，
ChatOllama 的 ``CallbackHandler`` 通过 ``_get_langfuse_handler()`` 创建。
两者各自管理自己的客户端实例，但从相同的 ``LANGFUSE_PUBLIC_KEY`` 环境变量读取。
在 Langfuse 服务端，trace 通过 ``trace_id`` 关联，只要使用同一 project key
就属于同一个 trace 树。

```python
# omega/llm.py — 已在代码中实现
from langfuse import Langfuse, get_client

_LANGFUSE_CLIENT = None  # singleton

def _get_langfuse_client() -> Langfuse | None:
    \"\"\"共享的 Langfuse 客户端单例。\"\"\"
    global _LANGFUSE_CLIENT
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        return None
    try:
        _LANGFUSE_CLIENT = get_client()  # 优先复用
    except Exception:
        _LANGFUSE_CLIENT = Langfuse(pk, sk, host)
    return _LANGFUSE_CLIENT
```

**T2Tracer 实现**：

```python
# omega/verify/t2_tracer.py
from omega.llm import _get_langfuse_client

class T2Tracer:
    \"\"\"T2 编译事件的 Langfuse span。
    
    使用 ``_get_langfuse_client()`` 创建的 Langfuse 客户端单例。
    与 ChatOllama CallbackHandler 使用同一 ``LANGFUSE_PUBLIC_KEY``，
    确保 LLM trace 和 T2 trace 在 Langfuse 服务端属于同一项目。
    \"\"\"
    def trace_compile(
        self, theorem: str, lean_code: str, result: T2Result, elapsed_s: float
    ) -> None:
        client = _get_langfuse_client()
        if client is None:
            return  # Langfuse 未配置，静默跳过
        span = client.span(
            name="t2_compile",
            input={"theorem": theorem[:80], "lean_code_len": len(lean_code)},
            output={"verified": result.verified, "n_errors": len(result.errors),
                    "contains_sorry": result.contains_sorry if hasattr(result, 'contains_sorry') else False},
            metadata={"elapsed_s": elapsed_s},
        )
        span.end()
```

### Phase 2 验证标准

```python
VERIFICATION_MATRIX = {
    "monitor_file_exists": {"target": "run.jsonl created after each runner session"},
    "health_check_detects_down": {"target": "check_ollama_health() returns False when ollama stopped"},
    "proof_cache_hit": {"target": "same theorem header → cache hit → 0 LLM calls"},
    "langfuse_t2_span": {"target": "T2 compile events visible in Langfuse UI"},
}
```

---

## Phase 3 — GSNV 理论形式化（独立研究轨道，4-8 周）

### 前置条件

Phase 1 证明 MiniF2F 管线稳定（无需 60%，>20% 即可表明系统可用）。
Phase 2 证明系统可 debug 和恢复。

Phase 3 是一个**独立的数学研究项目**，与 Phase 1-2 并行不阻塞。

### 3.1 NV Hamiltonian 形式化（2-4 周）

形式化目标（⚠️ 以下 Lean 代码为设计草图，未经编译测试——详见 Issue #6）：

```lean4
import Mathlib
open Complex

-- 自旋-1 算符 (S_x, S_y, S_z)
--  TODO: 验证 Matrix (Fin 3) (Fin 3) ℂ 的乘法是否满足自旋对易关系
def Sx : Matrix (Fin 3) (Fin 3) ℂ := ...
def Sy : Matrix (Fin 3) (Fin 3) ℂ := ...
def Sz : Matrix (Fin 3) (Fin 3) ℂ := ...

-- NV 基态哈密顿量：H = D * Sz² + E * (Sx² - Sy²)
def nv_hamiltonian (D E : ℝ) : Matrix (Fin 3) (Fin 3) ℂ :=
  D • (Sz * Sz) + E • (Sx * Sx - Sy * Sy)

-- 塞曼项：H_Z = γ * B · S
--  TODO: Vector ℝ 3 的选择：使用 Fin 3 → ℝ 还是 ℝ × ℝ × ℝ？
def zeeman_term (γ : ℝ) (B : Vector ℝ 3) : Matrix (Fin 3) (Fin 3) ℂ := ...

**交付物**：
- 自旋-1 算符的矩阵表示
- NV 基态哈密顿量的定义
- 能级分裂定理（本征值 → ODMR 共振频率）
- ZFS 温度依赖理论（D(T) = D₀ + α·T²）

**验证**：通过 Lean 计算本征值并与解析解比对。

### 3.2 Dang Van 疲劳准则（3-6 周）

```lean4
-- 应力张量
structure StressTensor where
  σ : Matrix (Fin 3) (Fin 3) ℝ
  symmetric : σ = σᵀ

-- 偏应力
def deviatoric (S : StressTensor) : StressTensor := ...

-- 静水应力
def hydrostatic (S : StressTensor) : ℝ := (S.σ 0 0 + S.σ 1 1 + S.σ 2 2) / 3

-- Dang Van 准则：max(τ(t) + α·p(t)) ≤ τ₀
def dang_van_criterion (τ t : ℝ → ℝ) (α τ₀ : ℝ) : Prop :=
  ∀ t, τ t + α • p t ≤ τ₀
```

**交付物**：
- 连续介质力学基本类型的 Lean 定义
- Dang Van 准则的形式化
- 钢轨接触疲劳的 Meso-scale 模型

**验证**：与 COMSOL 数值解比对。

### 3.3 裂纹磁偶极模型（2-4 周）

```lean4
-- 磁标势
noncomputable def magnetic_potential (m : Vector ℝ 3) (r : Vector ℝ 3) : ℝ :=
  (m · r) / (4 * π * |r|³)

-- 表面裂纹泄漏场
def leakage_field (crack_width depth : ℝ) (position : Vector ℝ 3) : Vector ℝ 3 := ...
```

**交付物**：
- 点偶极、线偶极、面裂纹的磁标势
- 泄漏场 B_x/B_z 分量的解析表达式
- SNR 模型（光子散粒噪声 + 自旋投影噪声）

### Phase 3 验证标准

```lean4
VERIFICATION_MATRIX = {
    "nv_hamiltonian_compiles": {"target": "`lake build` passes for NV_Hamiltonian.lean"},
    "dang_van_types_defined": {"target": "StressTensor, deviatoric, hydrostatic all compile"},
    "crack_field_correct": {"target": "Bx formula matches analytic solution from literature"},
}
```

---

## 整体验证框架

### 每次变更的测试门禁

```bash
# 提交前必须通过:
python -m pytest tests/ -x -q          # 327 tests
python experiments/phase0_experiment.py  # Phase 0 regression
ruff check omega/                       # lint
```

### 每 Phase 的可交付物检查

| Phase | 必须可交付 | 可选可交付 |
|-------|-----------|-----------|
| **1** | MiniF2F benchmark report (.json) | 错误类别 CSV |
| | Error type distribution chart | 采样效率分析 |
| | Pass rate vs num_samples 曲线 | |
| **2** | `run.jsonl` 监控文件 | Web UI |
| | `check_ollama_health()` 函数 | Grafana dashboard |
| | `ProofCache` 实现 | |
| **3** | NV Hamiltonian 定义 .lean | 本征值解析解 |
| | Dang Van 类型定义 | 疲劳极限定理 |
| | 裂纹场解析式 | SNR 数字孪生 |

---

## 引用

- GoedelProver 架构: `omega/prover/go_prover.py`
- ChatOllama 集成: `omega/llm.py`
- MiniF2F Benchmark: `benchmarks/minif2f/run_benchmark.py`
- 真实 T2 编译: `omega/verify/t2_real.py`
- Langfuse 集成: `docs/design/llm-langchain-integration.md`
- Phase 0 实验数据: `~/.omega/experiments/phase0_experiment.json`
- 数据层: `omega/data.py`

### 相关论文

| 论文 | 链接 | 与 Omega 关系 |
|------|------|-------------|
| **Limit of RLVR** — Yue et al., Tsinghua LeapLab, NeurIPS 2025 Best Paper Runner-Up | [arXiv:2504.13837](https://arxiv.org/abs/2504.13837) | 证明 RLVR 仅提升采样效率而非推理能力。Omega 的 GoedelProver 应优先增加 num_samples 而非训练 RL 模型 |
| **LongTraceRL** — Lin et al., Tsinghua KEG, 2026 | [arXiv:2605.31584](https://arxiv.org/abs/2605.31584) | Rubric 过程奖励可直接映射到 Omega 的 T2 error 分类：unsolved_goal/syntax 等作为"推理步骤质量"信号。JSONL 数据格式兼容 datasets 库 |
