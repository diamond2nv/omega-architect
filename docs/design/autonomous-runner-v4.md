# Omega v4: Autonomous Research Runner — 2/4/8/12h 无人值守证明搜索

## 1. 问题的本质

当前 Omega 的每次 `run()` 只覆盖 **一个定理**，受限于：

```
max_tokens: 1_000_000      # 即使本地免费，也限制在 1M
max_attempts: 50           # 最多 50 次 T2 尝试
max_time_s: 300 (5min)     # 最多 5 分钟 / 定理
```

对于 "让它跑 12 小时看看能证明什么" 的场景，需要**全新架构**:

```
目前：  定理 A → KnowledgeProver.run(A) → 输出
                                   ↑ 单次执行 ~5min 封顶

需要：  研究目标
           ↓ 分解
        [定理A, 定理B, ..., 定理N]  ← 自动推导依赖关系
           ↓ 依次/并行
        KnowledgeProver.run(每个)
           ↓ 逐 theorem 报告进度
        12h 后: {成功: [A,B,E], 部分: [C], 失败: [...], 新发现: [...]}
```

## 2. 预算原则重构：本地 vs 远程

### 当前问题

```
默认 max_tokens=1M 对所有模型一样 —— 对 ollama 毫无意义
```

### 重构：双层预算

```toml
[budget.local]
# 本地模型（ollama, local/...）—— 零成本，墙钟时间约束
max_tokens = 100_000_000      # 1 亿 token，12h 够用
max_time_s = 43200            # 12 小时墙钟
max_attempts = 10000          # 最多 1 万次 T2 尝试
min_confidence = 0.2          # 本地便宜，门槛降低

[budget.remote]
# 付费模型（DeepSeek, Claude, ...）—— 金额约束
max_tokens = 5_000_000        # 500 万 token ≈ ~$1.40 DeepSeek
max_cost_usd = 2.00           # $2 上限 / 次
max_time_s = 3600             # 1 小时墙钟
min_confidence = 0.4          # 付费模型，提高门槛
```

**BudgetTracker 判断逻辑:**
```python
if tracker.is_free_model(model_id):
    # 使用 local budget
    _remaining_tokens = cfg.local.max_tokens      # 100M
    _remaining_time = cfg.local.max_time_s         # 12h
else:
    # 使用 remote budget
    _remaining_tokens = cfg.remote.max_tokens      # 5M
    _remaining_cost = cfg.remote.max_cost_usd      # $2
    _remaining_time = cfg.remote.max_time_s         # 1h
```

## 3. OmegaRunner: 多定理长时间运行

### 架构

```
OmegaRunner
├── config: BudgetConfig (local/remote 双层)
├── research_goal: str              # "形式化 GSNV NV 中心自旋哈密顿量"
├── theorem_queue: list[TheoremSpec] # 自动分解或手动指定的定理序列
├── results: list[TheoremResult]    # 每个定理的结果
│
├── run() → ResearchReport
│   ├── while time_budget_remaining:
│   │   ├── next_theorem = theorem_queue.pop()
│   │   ├── result = KnowledgeProver.run(next_theorem)
│   │   ├── if result.succeeded:
│   │   │     lemma_cache.add(result.proof)     # 后续定理可用
│   │   │     theorem_queue.depends_on(new_lemmas)  # 更新依赖
│   │   ├── report_progress()                    # 打印/日志
│   │   └── checkpoint_save()
│   └── return ResearchReport
│
├── checkpoint: JSON 序列化 (可中断/恢复)
└── progress_callback: 可选 (Hermes QQ/CLI 实时推送)
```

### TheoremSpec

```python
@dataclass
class TheoremSpec:
    """描述一个要证明的定理。"""
    header: str              # Lean4 定理头部
    description: str         # 自然语言描述
    priority: int            # 优先级 (1=最高)
    depends_on: list[str]    # 依赖的已证明定理名
    domain: str              # algebra / physics / quantum / optics
    expected_time_s: float   # 预期证明时间 (用于调度)
    status: str = "pending"  # pending / running / succeeded / failed
```

### Checkpoint 格式

```json
{
    "research_goal": "形式化 NV center spin Hamiltonian",
    "started_at": "2026-06-06T10:00:00Z",
    "elapsed_s": 3600,
    "budget_remaining": { "tokens": 95000000, "time": 39600, ... },
    "theorems": [
        {
            "header": "theorem spin_1_hamiltonian ...",
            "status": "succeeded",
            "proof_file": ".omega/checkpoints/proof_spin_1.lean",
            "elapsed_s": 120,
            "attempts": 3
        },
        {
            "header": "theorem zeeman_splitting ...",
            "status": "running",
            "elapsed_s": 45,
            "attempts": 2
        },
        {
            "header": "theorem nv_center_d_T ...",
            "status": "pending"
        }
    ],
    "discoveries": [
        "发现 lemma: spin_basis_rotation 可在多个定理复用"
    ]
}
```

## 4. 四个研究领域的定理地图

### GSNV (钢轨裂纹 NV 检测)

```
NV 中心自旋物理:
  1. spin_1_hamiltonian     ℋ = D·S_z² + γ·B·S
  2. zeeman_splitting       E_±1 = D ± γB_z
  3. nv_odmr_lineshape      S(ω) = ... (洛伦兹线形)
  4. stress_strain_coupling  ΔD = d∥·ε_zz + d⊥·(ε_xx + ε_yy)
  5. magnetic_dipole_field   B_(crack)(r) = μ₀/4π · (3(m·r̂)r̂ - m)/r³

裂纹检测理论:
  6. crack_mfl_signal        ∇×H = J_free, ∇·B = 0
  7. snr_optimization        SNR = C·√(n_NV·T₂*) · B_signal / √(Δt)
```

### WGM (回音壁模式)

```
 1. wgm_resonance_condition  2π·R·n_eff = m·λ
 2. wgm_quality_factor       Q = ω·τ = λ/Δλ
 3. kerr_nonlinearity        n = n₀ + n₂·I
 4. coupling_efficiency      κ = κ_ext / (κ₀ + κ_ext)
```

### 光频梳

```
 1. kerr_comb_dynamics        ∂_t A = -κ/2·A + i·D·A + i·γ·|A|²·A + √κ·A_in  (LLE)
 2. soliton_existence          soliton 解存在的参数条件
 3. repetition_rate           f_rep = v_g / (2π·R)
 4. ceo_phase                 Δφ_CEO 稳定性条件
```

### 光钟

```
 1. clock_transition          ΔE = h·ν_clock
 2. stability_analysis        σ_y(τ) = (Δν/ν₀) / √(N·τ)
 3. systematic_shifts         黑体辐射、引力红移、DC Stark 的 qunatified 表达式
```

## 5. 实现路线

### Phase A: 双层预算 (this session)

```
omega/resource/config.py → BudgetConfig.local, BudgetConfig.remote
omega/resource/budget.py → BudgetTracker 双轨判断
```

### Phase B: OmegaRunner (this session)

```
omega/runner/
├── __init__.py
├── spec.py          # TheoremSpec, ResearchGoal
├── runner.py        # OmegaRunner — 长时间运行 loop
├── checkpoint.py    # JSON checkpoint 读写
└── report.py        # ResearchReport, progress 格式化
omega/cli/ → omega run "formally verify NV Hamiltonian" --time 12h
```

### Phase C: 定理地图

```
omega/theorems/
├── gsnv.py          # GSNV 定理序列
├── wgm.py           # WGM 定理序列  
├── frequency_comb.py # 光频梳定理序列
└── optical_clock.py  # 光钟定理序列
```

### Phase D: Demo / 第三方接入

```
omega demo --target gsnv --time 4h
  → 自动加载 GSNV 定理地图
  → 每 30min 输出 progress report
  → 12h 后可查看 checkpoint 和最终报告
```

## 6. 对第三方 demo 的意义

| 场景 | 配置 | 效果 |
|------|------|------|
| 快速验证 | `--time 2h --model ollama` | 2 小时看到 pipeline 能否跑通 |
| 复现 SOTA | `--time 8h --model deepseek` | 用付费模型尝试复现 miniF2F |
| 理论探索 | `--time 12h --target gsnv` | 聚焦 GSNV 理论形式化 |
| 团队演示 | `--demo --time 4h --progress qq` | 每定理通过 → QQ 自动推送进度 |
