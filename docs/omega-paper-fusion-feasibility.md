# Ω-Architect 论文融合开发可行性分析

> **开发机**: i5-12450H CPU · 16GB RAM · **无 GPU** · DeepSeek API  
> **论文池**: QiMeng(2506.05007) · QiMeng-CPU-v2(2505.03195) · QiMeng-PRepair(2604.05963) · EvoKernel(2603.10846) · LCLM(2606.09659)  
> **现有代码**: LUFFY GRPO trainer(866行) · DialogueBuffer(368行) · OmegaPassKManager(935行) · BenchmarkSuite(422行)

---

## 一、本机能做 vs 不能做

### ✅ 能做（纯 CPU/API 开发）

| # | 模块 | 涉及文件 | 依赖 | 工作量 |
|:--|:-----|:---------|:-----|:-------|
| **P0** | EA-GRPO Reward 函数 | `luffy_mixed_trainer.py` | 无 | ~50行 |
| **P0** | fixp@k 评估指标 | `benchmark/suite.py` + `passk.py` | 无 | ~120行 |
| **P0** | line-level DEC 工具 | `search/passk.py` (新增 utility) | 无 | ~40行 |
| **P1** | EA-GRPO 命令行参数 | `cli/__init__.py` | 需 P0 先完成 | ~30行 |
| **P1** | Speculative edits 分析脚本 | `scripts/` (新增) | 无 | ~60行 |
| **P1** | MiniF2F 全量分类 + QA 基线 | `benchmark/` + `cli/` | DeepSeek API | ~100行 |
| **P2** | DialogueBuffer 域分类扩展 | `dialogue_buffer.py` | 无 | ~40行 |
| **P2** | Proof trajectory 聚类 | `learn/` (新增) | 无 | ~80行 |
| **P2** | 论文对比表自动生成 | `scripts/` | 数据分析 | ~50行 |

### ❌ 不能做（需 GPU）

| 功能 | 原因 | 替代方案 |
|:-----|:-----|:---------|
| LoRA 训练 | 需 ≥6GB VRAM + CUDA | 云 GPU / 合作方 |
| Goedel-Prover vLLM 推理 | 需 ≥24GB VRAM | DeepSeek API |
| Beam 批量生成（本地） | 需 GPU 并行编译 | 远程 API |
| QLoRA 4-bit 微调 | 需 CUDA + bitsandbytes | 代码写好，云端跑 |

**核心约束**: LUFFY 训练器的 On-Policy 部分（vLLM rollout）+ LoRA 更新**本机跑不动**。但 reward 设计、评估指标、数据管线全部可本机开发。

---

## 二、论文融合点优先级

### P0: EA-GRPO Reward（QiMeng-PRepair）

**现有代码** (`luffy_mixed_trainer.py:198-254`) 是 `compute_compile_reward()` stub——只返回 `±0.3/±0.5/±1.0`，**没有 edit cost 概念，没有 group-aware 惩罚**。EA-GRPO 直接替换。

```python
# 当前 (stub)
def compute_compile_reward(candidate_codes, theorem_ids):
    rewards = []
    for code in candidate_codes:
        if has_proof_fragment(code):
            rewards.append(0.3)     # ← 硬编码，无 edit cost
        else:
            rewards.append(-0.5)
    return torch.tensor(rewards)

# 目标 (EA-GRPO, 本机开发)
def compute_ea_grpo_reward(
    candidate_codes: list[str],
    buggy_code: str,
    group_accuracy_threshold: float = 0.5,
    edit_penalty_beta: float = 0.3,
) -> torch.Tensor:
    """
    论文: QiMeng-PRepair §2.4 Eq.5-6
    R_i = { 1 - T(G)·β·σ(z_i)  if correct
            0                    if incorrect
    z_i = (DEC_i - mean) / std   # group 内标准化
    T(G) = 1 if Acc_G ≥ α else 0 # 动态开关
    """
    # 1. compile gate
    compile_results = [gate.compile(code) for code in candidate_codes]
    
    # 2. line-level edit cost
    decs = [levenshtein_lines(buggy_code, code) for code in candidate_codes]
    
    # 3. group accuracy threshold
    group_acc = sum(1 for r in compile_results if r.success) / len(compile_results)
    penalty_on = float(group_acc >= group_accuracy_threshold)
    
    # 4. 标准化 + sigmoid 惩罚
    dec = torch.tensor(decs)
    z = (dec - dec.mean()) / (dec.std() + 1e-8)
    penalties = penalty_on * edit_penalty_beta * torch.sigmoid(z)
    
    rewards = torch.where(
        torch.tensor([r.success for r in compile_results]),
        1.0 - penalties,   # correct samples get edit penalty
        0.0,               # incorrect get zero
    )
    return rewards
```

**本机验证**: `python3 -c "from omega.learn.rl.luffy_mixed_trainer import compute_ea_grpo_reward; ..."` → 不依赖 GPU

---

### P0: fixp@k 评估指标（QiMeng-PRepair §2.2）

**集成点**: `OmegaPassKManager` (`passk.py`) 的 `PassKCandidate` 和 `PassKReport` 增加 edit cost 字段。

```python
# 新增到 passk.py
def compute_edit_cost(buggy_code: str, fixed_code: str) -> float:
    """Line-level Levenshtein distance / total lines.
    论文: QiMeng-PRepair §2.2 Eq.3 (DEC)
    """
    buggy_lines = buggy_code.splitlines()
    fixed_lines = fixed_code.splitlines()
    # Levenshtein on line sequences
    return edit_distance(buggy_lines, fixed_lines) / max(len(buggy_lines), 1)

@dataclass
class FixKCandidate(PassKCandidate):
    """Extends PassKCandidate with edit cost and fixp score."""
    edit_cost: float = 0.0
    dec_ratio: float = 0.0       # DEC(candidate) / DEC(golden)
    fixp_score: float = 0.0      # 1 if correct and dec_ratio ≤ p
```

**本机验证**: `python3 -c "from omega.search.passk import compute_edit_cost; assert compute_edit_cost('a\nb', 'a\nc') == 0.5"`

---

### P1: MiniF2F 全量分类 + EA-GRPO 对比基线

**使用 DeepSeek API 在 CPU 上运行**。不需要 GPU。

| 子任务 | 文件 | 本机可行? |
|:-------|:-----|:----------|
| 244题难度自动分类 | `benchmark/suite.py` 新增 `classify_tier()` | ✅ 纯规则 |
| naive GRPO baseline | `luffy_mixed_trainer.py` (已有) | ✅ 需要过一次 vLLM 逻辑… |
| EA-GRPO 对比实验 | 新增 `scripts/run_ea_grpo_comparison.py` | ✅ API |
| 可视化对比图 | `scripts/` (matplotlib) | ✅ CPU |

⚠️ **注意**: GRPO 训练本身需要 GPU（vLLM rollout + LoRA 更新）。但**评估管线 + 结果分析 + 图表**全在本机。

---

### P1: Speculative Edits 分析（QiMeng-PRepair §2.5）

纯数学验证 + 脚本。公式 `T ∝ (1-(1-DEC)^(K+1))/DEC`。

```python
# scripts/analyze_speculative_edits.py (本机跑)
def throughput_speedup(edit_cost: float, draft_window: int = 5) -> float:
    """Predict speculative decoding throughput factor."""
    return (1 - (1 - edit_cost) ** (draft_window + 1)) / edit_cost

# 对比: naive GRPO (DEC=0.6) vs EA-GRPO (DEC=0.2)
# naive: T ∝ (1-0.4^6)/0.6 ≈ 1.66
# EA:    T ∝ (1-0.8^6)/0.2 ≈ 4.37
# Speedup: 4.37/1.66 ≈ 2.63x
```

---

### P2: Proof Trajectory 聚类（EvoKernel §3.1 inspiration）

EvoKernel 的 Q-value retrieval 思想 → omega 的 DialogueBuffer 扩展。

```python
# dialogue_buffer.py 新增
def cluster_trajectories(self, n_clusters=8):
    """对历史轨迹进行聚类，生成域原型 (EvoKernel-inspired)."""
    from sklearn.cluster import KMeans  # CPU-only
    features = self._extract_features()  # 特征: n_rounds, error_types, edit_cost
    clusters = KMeans(n_clusters=n_clusters).fit(features)
    return clusters
```

⚠️ **依赖**: `pip install scikit-learn`（CPU-only，本机无问题）

---

### P2: 论文实验对比表自动生成

| 指标 | baseline (当前) | EA-GRPO (预期) | 意义 |
|:-----|:---------------|:---------------|:-----|
| pass@1 | ~40% (MiniF2F 10题) | ~40%+ (reward 不影响 pass) | 正确率不变 |
| fix₁@1 | 无 | **+20-40%** (vs naive GRPO) | 精确修复率↑ |
| DEC | ~0.5+ (naive GRPO) | ~0.2-0.3 | 编辑量↓ |
| 推理吞吐 | baseline | **+77%** (Verilog 论文数据) | 推理加速 |

---

## 三、推荐开发顺序

```
Week 1:  P0 核心算法（CPU-only, 0依赖）
  Day 1-2:  levenshtein_lines() + DEC utility + 测试
  Day 3-4:  compute_ea_grpo_reward() → LUFFY trainer
  Day 5:    fixp@k → PassKManager + BenchmarkSuite

Week 2:  P1 评估管线（DeepSeek API）
  Day 1-2:  MiniF2F 244题全量分类 + 难度谱
  Day 3-4:  scripts/run_ea_grpo_comparison.py (API baseline)
  Day 5:    scripts/analyze_speculative_edits.py

Week 3:  P2 扩展（CPU-only）
  Day 1-2:  DialogueBuffer 域分类 + trajectory 聚类
  Day 3-4:  论文对比表 + 可视化脚本
  Day 5:    CLI `--edit-aware` 参数 + 集成测试
```

**所有 P0-P2 开发完全在 i5-12450H 上完成，不需要任何 GPU 设备。**

需要云 GPU 的只有：
- LUFFY 实际 GRPO 训练（LoRA on Goedel-Prover）
- vLLM 批量 beam 搜索
- 论文最终实验数据采集

但这些可以在代码开发完成后，租一次云 GPU 集中跑（~300元/A100×1天）。
