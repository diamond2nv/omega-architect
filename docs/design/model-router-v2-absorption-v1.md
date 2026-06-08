# ModelRouter v2: SquillaRouter 吸收整合设计

> 基于 opensquilla/opensquilla（Apache-2.0）SquillaRouter V4 Phase 3 的对比分析
> 目标：将 ML 路由和启发式后处理的成熟实践引入 Omega ModelRouter

---

## 1. 背景

### 1.1 Omega ModelRouter （当前）

`omega/resource/model_router.py`

```
模式: 纯规则（analyze_theorem_pattern + keyword matching）
分类: 3 层 (simple / medium / hard)
特征: 仅定理 header 文本（长度，关键字，模式置信度）
模型池: goedel/goedel-v2-8b, deepseek/deepseek-v4-flash, local/qwen3-coder:30b
路由策略: 优先级排序 + 可用性检查（API key / health endpoint）
历史: 成功率统计（per-model per-tier）
回退: "local/default" (template-only)
决策输出: model_id 字符串
```

### 1.2 SquillaRouter （opensquilla, Apache-2.0）

`opensquilla/src/opensquilla/engine/steps/squilla_router.py`
`opensquilla/src/opensquilla/squilla_router/v4_phase3.py`

```
模式:  ML 分类（LightGBM + ONNX MLP 融合）+ 7 层后处理
分类: 4 级 (R0/c0 → R3/c3)，4 种 thinking 模式 (T0-T3)，3 种 prompt 策略 (P0-P2)
特征: 390-dim 向量 = HC(51) + TFIDF(102) + context(10) + history(16) + BGE×3(192) + asst_HC(12) + continuation(2) + reasoning(5)
模型池: 可配置 tier_mapping + tier_registry（3-6 个模型）
路由策略: 2-model 融合（主 LGBM × MLP）× 7 层后处理
历史: routing_history 含完整轨迹（turn_index, route_class, difficulty, margin）
回退: DEFAULT_TEXT_TIER（c1）
决策输出: {tier, route_class, confidence, thinking_mode, prompt_policy, selected_model, savings_pct, ...}
```

### 1.3 许可证兼容性

| 项目 | 许可证 | 兼容性 |
|------|--------|--------|
| omega-architect | Apache-2.0 | ✅ |
| opensquilla | Apache-2.0 | ✅ （直接吸收，需保留版权声明） |

---

## 2. 核心差异对比

### 2.1 分类粒度

| 维度 | Omega ModelRouter | SquillaRouter |
|------|------------------|---------------|
| 复杂度等级 | 3 (simple/medium/hard) | 4 (R0-R3/c0-c3) |
| 推理深度 | 无 | T0(minimal)-T3(high)，从 margin+flags 派生 |
| 提示策略 | 无 | P0(compress)-P2(detail)，从 difficulty+flags 派生 |
| 成本控制 | 仅 cost_per_call 字段 | savings_pct + tier_price delta |
| 语言感知 | 无 | CJK 检测 → zh/en hint 本地化 |

### 2.2 特征工程

| Omega ModelRouter | SquillaRouter（390-dim） |
|------------------|-------------------------|
| `analyze_theorem_pattern` 置信度 | HC(51): 手造特征（长度、标点、数字密度等） |
| 关键字匹配 | TFIDF(102): SVD 降维后 TF-IDF |
| 长度判断 | Context(10): 轮次索引、上下文估计 token |
| — | 历史(16): 前轮 route_class, difficulty, trajectory |
| — | BGE × 3(192): BAAI/bge-small-zh-v1.5 embeddings 三通道（user/history/asst），PCA(64) |
| — | asst_HC(12): 助理回复手造特征 |
| — | continuation(2): 续写提示检测 |
| — | reasoning(5): 推理需求检测 |

### 2.3 决策流水线

**Omega ModelRouter 的选模型流程：**
```
theorem_header
  → analyze_theorem_pattern() → complexity (simple/medium/hard)
  → filter by preferred_tier + availability
  → sort by tier_priority + cost
  → return model_id
```

**SquillaRouter V4 Phase 3 的 7 层后处理流水线：**
```
fused_probs(4)
  → Layer 1: argmax → base_class
  → Layer 2: margin_upgrade（margin < 0.15 → R↗R+1）
  → Layer 3: aux_downgrade（4-head aux: initial/maintain/upgrade/downgrade）
  → Layer 4: r1_rescue（R0 到 R1 的 gap < 0.20 → 提升到 R1）
  → Layer 5: under_routing_safety（R0/R1 且 heavy_prob < 0.45 → 提升到 R2）
  → Layer 6: flag_overrides（high_risk→min R2, debug+long→min R2, repo_arch→min R1）
  → Layer 7: sticky_tier（KV-cache 感知，避免频繁降级）
  → 派生: thinking_mode + prompt_policy + selected_model
```

### 2.4 启发式标志系统

SquillaRouter 的 6 个 flag，全部规则驱动，**零训练数据需求**：

| Flag | 触发条件 | 路由影响 |
|------|---------|---------|
| `high_risk` | 关键字匹配（中文/英文危险词） | 强制 ≥R2 |
| `long_context` | 字符>6000 或 代码块>1500 或 日志块>1500 或 文件引用≥2 或 context_tokens>2000 | 与 debug 联合触发 ≥R2 |
| `debug` | 关键字+模式匹配（调试相关） | 与 long_context 联合触发 ≥R2 |
| `repo_arch` | 关键字匹配（仓库/架构相关） | 强制 ≥R1 |
| `strict_format` | 关键字匹配（格式严格要求） | 触发 P2 策略 |
| `deep_conversation` | turn_index ≥ 4 | 强制 ≥R1 |

---

## 3. 可吸收的零训练成本特性

以下特性**不需要 ML 模型或训练数据**，可以直接复制或适配：

### P1. 启发式标志系统（高优先级）

从 `opensquilla/src/opensquilla/squilla_router/models/v4.2_phase3_inference/runtime_src/src/router/flags.py` 复制规则，适配到 Omega 的定理证明场景：

```python
# omega/resource/routing_flags.py （新文件，Apache-2.0, derived from opensquilla）
from __future__ import annotations

import re
from dataclasses import dataclass

@dataclass
class TheoremFlags:
    """定理证明场景的运行时标志，影响模型选择和后处理。"""
    long_proof: bool = False     # 证明长度 > 阈值 → 需要更大模型
    complex_type: bool = False   # 复杂类型签名 → 需要更强推理
    induction: bool = False      # 归纳证明 → 需要中等以上模型
    requires_library: bool = False  # 需要外部库 → 更长上下文
    multi_goal: bool = False     # 多个子目标 → 需更强推理
    error_correction: bool = False  # 已失败过 → 升级模型

    def min_tier(self) -> int:
        """根据标志推断最低 tier index。"""
        if self.complex_type or self.multi_goal:
            return 2  # mediun→hard
        if self.induction or self.requires_library:
            return 1  # simple→medium
        return 0
```

**标志提取：**
```python
def compute_theorem_flags(header: str, history: list | None = None) -> TheoremFlags:
    """从定理 header 和证明历史提取运行时标志。"""
    return TheoremFlags(
        long_proof=len(header) > 200,
        complex_type="→" in header or "∀" in header or "∃" in header,
        induction="induction" in pattern_hint.get("strategy", "")
            or "Nat.rec" in header,
        requires_library="import" in header.lower(),
        multi_goal=header.count("∧") > 1 or header.count("∧") > 1,
        error_correction=bool(history and any(h.get("succeeded") is False for h in history)),
    )
```

### P2. 后处理管道（高优先级）

参考 SquillaRouter 的 7 层后处理，设计 Omega 的 5 层后处理：

```python
def apply_omega_postprocess(
    tier: int,           # 0=simple, 1=medium, 2=hard
    flags: TheoremFlags,
    history: list | None,
    fallback_count: int,
) -> int:
    """5 层后处理：base → safety → flag → escalation → sticky"""

    # Layer 1: 安全网（complex_type/multi_goal 不能低于 medium）
    if flags.complex_type or flags.multi_goal:
        tier = max(tier, 1)

    # Layer 2: 标志覆盖
    if flags.long_proof and flags.complex_type:
        tier = max(tier, 2)  # 长证明+复杂类型 → 直接 HARD

    # Layer 3: 失败升级
    if fallback_count >= 2:
        tier = min(tier + 1, 2)  # 失败 ≥2 次 → 升一级，最多 hard

    # Layer 4: 黏性（KV-cache 感知）
    if history and len(history) > 0:
        last_tier = history[-1].get("tier", tier)
        if last_tier > tier:
            tier = last_tier  # 避免频繁降级

    return tier
```

### P3. 定价与成本追踪（中优先级）

SquillaRouter 的 `_compute_savings` + `lookup_price` 模式。

### P4. 语言感知行为（低优先级）

CJK 检测 → 为 Omega 的双语场景选择中文/英文 prompt 策略。

### P5. Trivial ACK 检测（低优先级）

短确认消息直接分配到最便宜的模型。

---

## 4. 需 ML 训练的差异化特性

### M1. BGE Embedding + PCA 的语义复杂度特征

SquillaRouter 最核心的差异化：BGE-small-zh-v1.5 的 512-dim 语义嵌入降维到 64-dim PCA，三通道捕捉用户意图 + 历史 + 前次回复的语义上下文。这需要：
- 标注数据（定理 header → 正确 tier 标签）
- PCA 拟合数据（约 1000+ 定理样本）
- ONNX 部署（可选，提升推理速度）

**Omega 落地路径：**
```python
# Phase 1: 只用手造特征（HC, TFIDF）
# Phase 2: 用 sentence-transformers 替换 BGE → 定理语义
# Phase 3: 训练 LightGBM 分类器
```

### M2. 融合模型（LightGBM + ONNX MLP）

双模型融合：LGBM 做结构化特征，MLP 做语义特征 → alpha-weighted 加权平均。这需要训练管线。

### M3. 辅助分类头（Auxiliary Head）

4 类的辅助分类（initial/maintain/upgrade/downgrade），用于 `_apply_aux_downgrade`。需要时序训练数据。

---

## 5. 吸收实施计划

### Phase A：启发式后处理 + 评估（~1 天，无训练数据）

1. 创建 `omega/resource/routing_flags.py`（TheoremFlags + compute_theorem_flags）
2. 修改 `omega/resource/model_router.py`：集成 5 层后处理管道
3. 添加成本节约追踪（`savings_pct` 统计）
4. 添加 trivial_ack 快速路径

### Phase B：定价感知 + 本地化（0.5 天）

1. 实现 `lookup_price` 从 pricing.py 获取 input/output/cached 单价
2. 路由器自动计算 `savings_pct` 和每轮成本
3. 为双语提示添加 `prompt_hint_locale` 检测

### Phase C：特征管道 + 分类器训练（~3 天，需标注数据）

1. 构建手造特征提取（51-dim：长度、符号密度、类型复杂度、注解密度...）
2. 构建 TFIDF + SVD（102-dim）
3. 用 `BAAI/bge-small-zh-v1.5` 提取定理语义（3×64-dim 三通道）
4. 训练 LightGBM 分类器 → ONNX 导出
5. 集成 InferenceCore 到 ModelRouter

### Phase D：持续优化（按需）

1. 辅助分类头训练
2. 历史轨迹特征化
3. 在线 A/B 测试框架

---

## 6. 许可证合规要求

吸收 opensquilla 代码（Apache-2.0）的合规步骤：

1. **每个复制文件**头部添加：
   ```python
   # Copyright 2025 OpenSquilla Authors
   # SPDX-License-Identifier: Apache-2.0
   #
   # Derived from opensquilla/opensquilla/src/opensquilla/squilla_router/
   # models/v4.2_phase3_inference/runtime_src/src/router/flags.py
   ```

2. **引用或修改的核心模块**需列出：
   - `flags.py` → `compute_theorem_flags` 核心思路
   - `postprocess.py` → 7 层管道架构
   - `predictor.py` → thinking_mode / prompt_policy 派生规则
   - `v4_features.py` → BGE 特征提取模式
   - `squilla_router.py` → routing_history 管理 + deferred commit

3. 在 Omega README 添加 THIRD_PARTY.md 或 NOTICE 文件。

---

## 7. 预期效果

| 指标 | 当前 Omega ModelRouter | Phase A 后 | Phase C 后 |
|------|-----------------------|-----------|-----------|
| 路由准确率（简单→简单） | ~60%（规则估计） | ~75%（加标志覆盖） | ~90%（ML 分类器） |
| 错误路由导致的 token 浪费 | ~30% | ~15% | ~5% |
| 离线推理命中率 | 仅有可用性检查 | 标志驱动升级 | 全特征推理 |
| 成本透明度 | 无 | savings_pct + 每轮成本 | 全链路审计 |
| 多语言支持 | 无 | 中文/英文 prompt 自适应 | 同左 |
| 持续学习 | 无（需模型可用） | 已决策历史可审计 | 可增量再训练 |
