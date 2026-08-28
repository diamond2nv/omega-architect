# Ω-Architect 硬件感知预算系统 v2

> 时间驱动预算 + 战略远程分配 + 国产模型联合

---

## 1. 动机

### 1.1 原有问题

OmegaRunner v1 使用硬编码的「每层预算上限」：

```python
LOCAL_DEFAULT = BudgetTier(
    max_tokens=100_000_000,  # ❌ 拍脑袋：12h 本地能跑完 1 亿 token？
    max_time_s=43200,
)
```

**三个根本问题：**

1. **无硬件基础** — 100M tokens 对 gemma4:26b 需要跑 **18 天**，但对本地 12h 会话没有任何约束力，token 上限永远先被时间上限截断
2. **硬编码限制系统发挥** — 如果 Qwen3 实际能跑 30 tok/s，12h 预算应该是 `30 × 43200 ≈ 1.3M tokens`，而非 100M
3. **远程 API 被平等消耗** — DeepSeek-v4-pro(hard) 和本地 ollama(easy) 走同一个预算，远程的高推理能力被日常 token 搜索稀释

### 1.2 核心理念

> 预算应该绑定到 **「给定硬件在给定时间内能产生多少推理」**，而非一个无物理意义的大整数。

```
effective_token_budget = min(safety_cap, remaining_time × measured_tok_s)
```

- **本地模型**：瓶颈是时间（免费的，只要够快就能产出更多）
- **远程 API**：瓶颈是成本（每 token 都要花钱，所以需要战略使用）

---

## 2. 架构

```
用户输入: 研究目标 + time_budget(2/4/8/12h)
         │
         ▼
    ┌─────────────────────────────────────┐
    │         OmegaRunner                 │
    │                                     │
    │  ┌─────────────────────────────┐    │
    │  │    ModelAllocator           │    │
    │  │  ├─ classify(header)        │    │
    │  │  │   EASY ──→ gemma4(最快)  │    │
    │  │  │   MEDIUM─→ deepseek-r1   │    │
    │  │  │   HARD ──→ local×3→远程  │    │
    │  │  ├─ record_outcome()        │    │
    │  │  └─ provenance_summary()    │    │
    │  └─────────────────────────────┘    │
    │                                     │
    │  ┌─────────────────────────────┐    │
    │  │    BudgetTracker            │    │
    │  │  ├─ time_based_dynamic      │    │
    │  │  │   = time×measured_tok_s  │    │
    │  │  ├─ adaptive_tok_s (加权)   │    │
    │  │  └─ dual_tier (local/remote)│    │
    │  └─────────────────────────────┘    │
    │                                     │
    │  ┌─────────────────────────────┐    │
    │  │    Benchmark Cache          │    │
    │  │  ~/.omega/benchmark.json    │    │
    │  │  ├─ deepseek-r1:8b = 31.9  │    │
    │  │  ├─ gemma4:26b    = 64.9   │    │
    │  │  └─ qwen3-coder:30b= 27.3  │    │
    │  └─────────────────────────────┘    │
    └─────────────────────────────────────┘
                       │
                       ▼
              Lean 定理证明文件
```

### 2.1 核心数据流

```
1. OmegaRunner.run("NV Hamiltonian", time=4h)
2.   → ModelAllocator.select_model(theorem)
3.      → classify() → ComplexityClass.MEDIUM
4.      → Allocation(model_id="ollama/deepseek-r1:8b")
5.   → BudgetTracker.check_token(time_remaining, tok_s)
6.      → effective = min(100M, 3600s × 31.9) = 114,840 tokens
7.   → GoedelProver.run(header, budget=effective)
8.   → BudgetTracker.consume(tokens, time)
9.      → adaptive_tok_s 加权更新
10.  → ModelAllocator.record_outcome(succeeded, attempts)
11.     → 如果失败 3 次，下次 HARD 定理升级到远程 DeepSeek-v4-pro
```

---

## 3. 模块详解

### 3.1 `omega/resource/benchmark.py` — 硬件探测

**自动检测 Windows ollama（WSL 网关 IP）：**

```python
def detect_ollama_url() -> str:
    gw = ip_route_show_default()  # e.g. localhost
    url = f"http://{gw}:11434"
    # curl /api/tags → 返回模型列表
```

**基准测试流程：**

1. 向 ollama generate API 发送标准定理证明 prompt（`add_comm`）
2. 测量 `eval_count / elapsed_s`
3. 3 次 trial 取平均
4. 缓存到 `~/.omega/benchmark.json`
5. 无 ollama 时使用保守 fallback 值

**缓存格式：**

```json
{
  "measured_at": "2026-06-07",
  "hardware": "RTX 4500 Ada (WSL→Windows ollama)",
  "models": {
    "deepseek-r1:8b": { "avg_tok_s": 31.9, "avg_tokens_per_call": 256 },
    "gemma4:26b":     { "avg_tok_s": 64.9, "avg_tokens_per_call": 256 }
  }
}
```

### 3.2 `BudgetTier.tok_s` — 时基动态预算

**新增字段：**

```python
@dataclass
class BudgetTier:
    max_tokens: int    # 安全上限（当 tok_s>0 时为软上限）
    tok_s: float       # 实测吞吐量（tok/s），0=传统静态模式
    
    def effective_token_budget(self, remaining_time_s: float = 0.0) -> int:
        if self.tok_s > 0 and remaining_time_s > 0:
            dynamic = int(remaining_time_s * self.tok_s)
            return min(self.max_tokens, dynamic)
        return self.max_tokens
```

**默认值（RTX 4500 Ada 实测）：**

| 维度 | 本地 (LOCAL_DEFAULT) | 远程 (REMOTE_DEFAULT) |
|------|---------------------|----------------------|
| max_tokens | 100M（安全上限，不会到达） | 5M |
| tok_s | **15.0**（保守估计） | 0（远程不计 tok/s） |
| max_time_s | 43,200 (12h) | 3,600 (1h) |
| max_cost_usd | $0 | $2.00 |
| max_attempts | 10,000 | 500 |

### 3.3 `BudgetTracker` — 自适应动态速率

**三重速率来源：**

1. **静态默认**：`LOCAL_DEFAULT.tok_s = 15.0`（最保守）
2. **benchmark 缓存**：若 `tok_s=0`，从 `~/.omega/benchmark.json` 加载
3. **运行时自适应**：每次 `consume()` 后计算加权平均

```python
def _compute_dynamic_tok_s(self) -> float:
    """最近 5 个样本加权双倍 + 更早样本等权"""
    recent = self._dynamic_samples[-5:]
    for i, (toks, ts) in enumerate(recent):
        w = 2.0 if i >= len(recent) - 3 else 1.0
        weighted_total += (toks / ts) * w
    return weighted_total / weight_sum
```

**summary 输出示例：**

```
BudgetTracker [local] — used / remaining
  tokens:        2,500 / ~114,840  (≤100,000,000 cap)
  cost (USD):    0.000000 / 0.000000  (0.0%)
  time (s):      12.25 / 43187.75  (0.0%)
  attempts:      5 / 10000  (0.1%)
  tok/s:         31.9 (adaptive)
```

### 3.4 `ModelAllocator` — 战略路由

**复杂度分类逻辑：**

```
                    theorem_header + domain
                           │
                    ┌──────┴──────┐
                    ▼             ▼
               domein∈           domain∉
              COMPLEX_DOMAINS?   COMPLEX_DOMAINS?
               YES (HARD)         │
                                  ▼
                          EASY_PATTERNS匹配?
                           YES (EASY) │ NO
                                      ▼
                              HARD_PATTERNS匹配?
                               YES (HARD) │ NO
                                          ▼
                                  长度>200? → HARD
                                          │ NO
                                          ▼
                                      MEDIUM(默认)
```

**路由策略：**

| 分类 | 模型 | tok/s | 理由 |
|------|------|-------|------|
| EASY | ollama/gemma4:26b | 64.9 | 最快, `simp`/`trivial` 秒出 |
| MEDIUM | ollama/deepseek-r1:8b | 31.9 | 好推理, 适合 lemma 搜索 |
| HARD (前3次) | ollama/qwen3.6:latest | 18.7 | 最大参数(36B), 深度推理 |
| HARD (≥4次, 蓝图) | deepseek/deepseek-chat (Pro) | ~50 | ¥3/M input, ¥6/M output, 策略设计 |
| HARD (续写) | deepseek/deepseek-v4-flash | ~80 | ¥1/M input, ¥2/M output, 大量续写 |

### 3.6 预算量化

**DeepSeek 官方定价（2026年6月，4月降价后永久价）：**

| 模型 | 输入 (缓存未命中) | 缓存命中 | 输出 |
|------|:---:|:---:|:---:|
| v4-Flash | ¥1/M | ¥0.02/M | ¥2/M |
| v4-Pro | ¥3/M | ¥0.025/M | ¥6/M |

注：2026年4月 DeepSeek 全线降价至 1/10。Pro 优惠期过后永久价调整为原价 1/4（¥3/M）。

**完整定理证明管线成本（GoedelProver: 8 samples × 3 rounds = 32 LLM calls/attempt）：**

| 场景 | LLM calls | 输入 tokens | Flash 成本 | Pro 成本 |
|------|:---------:|:----------:|:----------:|:--------:|
| Easy（1次尝试）| 32 | 0.1M | **¥0.2** | ¥0.7 |
| Medium（5次尝试）| 160 | 1.9M | **¥2.4** | ¥7.2 |
| Hard（20次续写）| 640 | 32M | **¥34** | **¥102** |
| Very Hard（50次）| 1,600 | 80M | ¥85 | ¥254 |

**当前配置（国产合规 DeepSeek 版）：**

| 预算项 | 原值 | 调整后 | 覆盖范围 |
|-------|:---:|:-----:|---------|
| Pro 预算 | $0.50 | **$10.00** (¥72) | ~24M tokens Pro 蓝图 |
| Flash 预算 | $1.50 | **$5.00** (¥36) | ~36M tokens Flash 续写 |
| 总预算/定理 | $2.00 | **$15.00** (¥108) | 10x 安全余量 |
| **GSNV 完整形式化 (60 lemmas)** | — | **~¥3,000** | 全自动 12h 运行 |

**vs 人工成本对比：**
- PhD 学生 1 个月工资：¥8,000-15,000
- 人工形式化 1 个定理：¥2,000-5,000（3-5天）
- Omega 自动形式化 60 定理：**¥3,000**（12h 自主任证）
- 效率提升：**100-1000x**

**升级机制：**

```
HARD 定理
  ├── 第 1 次：本地 qwen3.6（尝试）
  ├── 第 2 次：本地 qwen3.6（再尝试）
  ├── 第 3 次：本地 qwen3.6（最后一次）
  └── 第 4+ 次：远程 DeepSeek-v4-pro（仅生成蓝图，不是全证明）
        └── 远程 $2 用完 → 回退本地 qwen3.6
```

### 3.5 溯源追踪（Provenance）

每次定理证明结果被记录：

```python
ProvenanceRecord(
    theorem_header="theorem add_comm ...",
    model_id="ollama/gemma4:26b",
    complexity=ComplexityClass.EASY,
    succeeded=True,
    elapsed_s=5.2,
    n_attempts=1,
    tokens_consumed=500,
    cost_usd=0.0,
    blueprint_used=False,
)
```

**运行结束时输出：**

```
============================================================
ModelAllocator Provenance
============================================================
Local:  ✅ 8 / ❌ 2  (80%)
Remote: ✅ 1 / ❌ 0  (100%)
Remote cost: $0.0034

Recent records (last 5):
  ✅🖥 theorem add_comm (a b : Nat) : a + b = b + a :=  (5s, 1 att)
  ✅🖥 theorem mul_comm (a b : Nat) : a * b = b * a :=  (12s, 2 att)
  ❌🖥 theorem spectral_theorem ...  (120s, 5 att)
  ❌🖥 theorem spectral_theorem ...  (95s, 4 att)
  ✅🌐 theorem spectral_theorem ...  (45s, 1 att, blueprint)
```

---

## 4. CLI 接口

### 4.1 `omega benchmark`

```bash
$ omega benchmark
============================================================
Ω-Architect Hardware Benchmark
============================================================
Measured at: 2026-06-07 09:09:38
Hardware:    RTX 4500 Ada (WSL→Windows ollama)

Model                          tok/s   tok/call    12h tokens
--------------------------------------------------------------
deepseek-r1:8b                  31.9        256     1,378,080
qwen3-coder:30b                 27.3        256     1,179,360
qwen3.6:latest                  18.7        256       807,840
gemma4:26b                      64.9        256     2,803,680
```

### 4.2 `omega run`

```bash
# 2 小时快速验证
omega run "Formalize Maxwell equations" --time 2

# 12 小时深度证明搜索
omega run "Prove NV center spin Hamiltonian" --time 12

# 从 checkpoint 恢复
omega run "Continue last session" --resume ~/.omega/checkpoints/checkpoint_20260606.json
```

---

## 5. 国产模型联合策略

### 5.1 本地模型分工

| 角色 | 模型 | 优势 |
|------|------|------|
| 快速扫描 | gemma4:26b (64.9 tok/s) | EASY 定理秒过，routine 过滤 |
| 常规推理 | deepseek-r1:8b (31.9 tok/s) | lemma 搜索、中等推理 |
| 代码生成 | qwen3-coder:30b (27.3 tok/s) | TPS 好，擅长 Lean 代码 |
| 深度推理 | qwen3.6:latest (18.7 tok/s) | HARD 定理首次尝试，36B 参数 |

### 5.2 远程模型定位

DeepSeek-v4-pro 仅用于：
- HARD 定理的 **证明蓝图设计**（3 次本地失败后）
- **RESEARCH 任务的领域综合**（新领域定理的分解策略）
- 不用于：routine 证明、lemma 搜索、simple rewrite

### 5.3 成本估算

```
12h 本地推理 ≈ $0 (全部 ollama)
远程仅：HARD 定理 × 蓝图请求
  假设 5 个 HARD 定理 × 1 蓝图/定理 × 2000 token/蓝图
  ≈ 10,000 tokens @ DeepSeek-v4-pro
  ≈ $0.0042
```

---

## 6. 文件清单

| 文件 | 新增/修改 | 行数 | 功能 |
|------|----------|------|------|
| `omega/resource/benchmark.py` | **新增** | 277 | 硬件自动探测 + tok/s 基准测试 + 缓存 |
| `omega/resource/allocator.py` | **新增** | 390 | 复杂度分类 + 战略路由 + 溯源追踪 |
| `omega/resource/config.py` | 修改 | 527+ | BudgetTier.tok_s + effective_token_budget() |
| `omega/resource/budget.py` | 重写 | 230 | 时基动态预算 + 自适应加权速率 |
| `omega/resource/__init__.py` | 修改 | 44+ | 导出新模块 |
| `omega/runner.py` | 修改 | 430+ | 集成 allocator + 溯源 + 动态 tok/s |
| `omega/cli/__init__.py` | 修改 | 438+ | `omega benchmark` 命令 |
| `tests/test_benchmark.py` | **新增** | 101 | 22 个 benchmark 测试 |
| `tests/test_allocator.py` | **新增** | 191 | 23 个 allocator 测试 |
| `tests/test_resource.py` | 修改 | 265+ | 适配时基预算断言 |

---

## 7. 经验教训

1. **不要硬编码"大数字"做预算上限** — 100M tokens 看着慷慨实际无约束力，还误导人。预算应来自 `time × rate`。
2. **硬件可测量就测量** — 3 次 `curl POST` 到 ollama 就能得到 tok/s，比任何估计都准确。
3. **远程 API 是推理引擎，不是 token 工厂** — 按 token 单价算远程成本，按战略价值分配远程调用。
4. **自适应速率比静态估计好** — 首次 benchmark 后缓存，运行中通过 `consume()` 反馈不断修正动态 tok/s。
5. **国产模型各有所长** — gemma4 快但浅，deepseek-r1 中等，qwen3.6 慢但深。把不同的定理复杂度交给不同的模型。

---

## 8. 未来优化方向

- [ ] **Multi-GPU 感知** — 如果有多卡，每卡 tok/s 不同，需分卡调度
- [ ] **定理历史成功率统计** — 根据历史正确/失败比例调整路由
- [ ] **远程 API 竞价** — 多个远程模型（DeepSeek/Claude/Qwen API）按性价比选择
- [ ] **运行时自适应学习** — 不仅调整 tok/s，还调整 escalation_threshold（如果某定理 3 次本地全失败，下次直接远程）
- [ ] **Benchmark 增量更新** — 每次运行后自动更新 tok/s 缓存（而非仅 benchmark 命令更新）
