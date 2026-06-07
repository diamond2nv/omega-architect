# ACE + DSPy 启发下的 Omega 自进化证明管线

> 将 Agentic Context Engineering (ACE, ICLR 2026) 和 DSPy (ICLR 2024) 的
> 核心思想形式化地注入 Ω-Architect 证明系统。
>
> 论文：
>   - ACE: arXiv:2510.04618, ICLR 2026
>   - DSPy: arXiv:2310.03714, ICLR 2024
> 代码：
>   - ACE: https://github.com/ace-agent/ace
>   - DSPy: https://github.com/stanfordnlp/dspy

---

## 1. 当前系统的结构化缺陷

Omega 目前的 GoedelProver 自修正循环：

```
LLM prompt (hardcoded template)
  → proof attempt (Lean code)
  → T2 compile (lean-paper-plane)
  → error collection
  → self-correction prompt (hardcoded template)
  → next attempt
```

**三个根本问题：**

| # | 问题 | 症状 | 根因 |
|---|------|------|------|
| 1 | **提示词是硬编码字符串** | 换模型 / 换定理域就要手动调 | 无结构化提示词管理层 |
| 2 | **上下文不进化** | 修正轮 1 和轮 N 的 prompt 一样 | 无 playbook / memory |
| 3 | **无自动化化** | 不知道哪个 prompt 版本最好 | 无 metric-driven 编译 |

ACE 和 DSPy 分别解决 #2 和 #3。两者组合提供了一个完整方案。

---

## 2. ACE 核心思想与 Omega 映射

### 2.1 ACE 三角色架构

```
Generator                          Reflector                          Curator
───────────────────────────        ───────────────────────        ────────────────────────
产生推理轨迹                      Critique 轨迹提取教训            增删改 playbook bullet
↓                                 ↓                                 ↓
proof_attempt + result            lessons learned                  playbook delta update
```

### 2.2 ACE 的关键创新点

| 创新 | 描述 | 为何重要 |
|------|------|----------|
| **增量 Delta 更新** | 不重写整个 context，只追加/修改 bullet | 防止 context collapse |
| **结构化 Playbook** | bullet point + ID + helpful/harmful 计数器 | 可排序、可合并、可删除有害策略 |
| **Grow-and-Refine** | 先追加，后压缩（dedup 在 budget 超时触发） | 无信息丢失 |
| **无监督执行反馈** | 不需要人工标注，用 `correct/incorrect` 作为信号 | 可自动提升 |
| **轻量 Curator** | 非 LLM 的确定性合并逻辑，不消耗推理 token | 低成本 |

### 2.3 ACE Playbook 格式

```
## STRATEGIES & INSIGHTS
[str-00001] helpful=5 harmful=0 :: 先用 `nlinarith` 处理代数方程
[str-00002] helpful=3 harmful=1 :: `field_simp` 前先 `ring` 展开

## COMMON MISTAKES TO AVOID
[mis-00003] helpful=7 harmful=1 :: 不要忘记 `import Mathlib` 基本库
[mis-00004] helpful=4 harmful=0 :: `h : a = b` 用 `rw` 而非 `apply`

## FORMULA TEMPLATES
[fmt-00005] helpful=2 harmful=0 :: a^2 + b^2 = (a+b)^2 - 2ab
```

每个 bullet 有：
- **唯一 ID**（含 section slug，如 `str-`, `mis-`, `fmt-`）
- **helpful/harmful 计数器**（自动追踪该策略的收益）
- **内容**（实际策略/教训/公式）

---

## 3. DSPy 核心思想与 Omega 映射

### 3.1 DSPy 编程模型

```
Signature: "theorem_header : str → lean_code : str"
  ↓
Module: dspy.Predict(Signature)
  ↓
Optimizer: MIPROv2(metric=t2_pass_rate)
  ↓
Compiled Program: optimized instructions + demos
```

### 3.2 DSPy 的关键创新点

| 创新 | 描述 | 为何重要 |
|------|------|----------|
| **声明式签名** | `输入字段 → 输出字段` 的自然语言类型规约 | 管道替换模型无需重写 prompt |
| **自动优化器** | MIPROv2 (贝叶斯)、GEPA (进化)、BootstrapFewShot | 替代人工手调提示词 |
| **Metric-driven** | 自动搜索最大化 metric 的指令+样例组合 | 可量化、可复现 |
| **BootstrapFewShot** | 从成功 traces 自动构建 few-shot 样例 | 自循环改进 |
| **编译时优化** | 优化发生在 pipeline 构建时，而非运行时 | 零推理开销 |

### 3.3 DSPy 优化器选择

```
问题类型                         推荐优化器              适用场景
──────────────────────────────────────────────────────────
指令措辞不准确                   GEPA / COPRO           单步 prompt 润色
指令+样例都不对                  MIPROv2                多步 pipeline 联合优化
已有一些成功样例                 BootstrapFewShot        有 teacher trace
完全从零开始                     BootstrapFewShotWithRandomSearch  小样本启动
```

---

## 4. 融合设计：Omega Proof Playbook System (OPPS)

### 4.1 架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│  Omega Proof Playbook System (OPPS)                                │
│                                                                     │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────────────┐  │
│  │ Proposer    │───→│ T2 Compile   │───→│ ErrorAnalyzer         │  │
│  │ (Generator) │    │ (Reflector)  │    │ (Extract Lessons)     │  │
│  └──────┬──────┘    └──────────────┘    └───────────┬───────────┘  │
│         │                                           │              │
│         │    ┌──────────────────────────────┐       │              │
│         └────│  PlaybookManager (Curator)   │←──────┘              │
│              │  ┌────────────────────────┐  │                      │
│              │  │ Playbook (structured   │  │                      │
│              │  │ bullet points + meta)  │  │                      │
│              │  └────────────────────────┘  │                      │
│              └──────────────────────────────┘                      │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  DSPy Optimizer Layer (offline compile)                     │   │
│  │  ┌──────────┐  ┌───────────┐  ┌───────────────────────┐    │   │
│  │  │ MIPROv2  │  │   GEPA    │  │ BootstrapFewShot      │    │   │
│  │  │ (prompt+ │  │ (refine   │  │ (build demos from     │    │   │
│  │  │  demos)  │  │  prompt)  │  │  successful traces)    │    │   │
│  │  └──────────┘  └───────────┘  └───────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 组件设计

#### 4.2.1 Playbook — 结构化上下文

```python
@dataclass
class PlaybookBullet:
    """单个 playbook 条目。"""
    bullet_id: str          # e.g. "str-00001"
    section: str            # "strategies" / "mistakes" / "templates"
    content: str            # 实际策略文本
    helpful: int = 0        # 成功引用次数
    harmful: int = 0        # 失败引用次数
    created_epoch: int = 0
    last_used_epoch: int = 0

@dataclass
class Playbook:
    """定理证明的策略 playbook。"""
    bullets: list[PlaybookBullet] = field(default_factory=list)
    max_tokens: int = 8000   # ACE: 80k budget, Omega: 8k (更小的证明上下文)
    next_id: int = 1

    def render(self) -> str:
        """渲染为结构化文本，注入 proposer prompt。"""
        ...

    def add_or_update(self, bullet: PlaybookBullet):
        """ACE 风格：delta update，不重写。"""
        ...

    def deduplicate(self, threshold: float = 0.85):
        """语义去重（ACE: bulletpoint_analyzer）。"""
        ...

    def prune(self):
        """删除 harmful >> helpful 的条目。"""
        ...
```

#### 4.2.2 ErrorAnalyzer — Reflector 角色

将原始 T2 diagnostics 提取为可行动的 playbook bullet。

```
输入: T2 compile diagnostics (Lean error messages)
输出: list[PlaybookBullet]  // 成功/失败教训

方法: 非 LLM 规则匹配（轻量）+ 关键错误模式 LLM 提炼

错误模式 → 对应策略:
  unsolved_goal         → "将大目标分解为子目标"
  syntax '{' expected   → "始终用 := by { ... } 包裹证明块"  
  unknown identifier    → "检查 import 和 open 语句"
  type mismatch         → "rewrite 前后类型不一致"
```

#### 4.2.3 PlaybookManager — Curator 角色

```python
class PlaybookManager:
    """管理 playbook 的增删改查和上下文集成。"""

    playbook: Playbook
    tm: TemplateManager  # DSPy 模板管理层

    def update_from_result(self, theorem, attempt, t2_result):
        """T2 编译后：提取教训 → 创建/更新 bullet。"""
        lessons = ErrorAnalyzer.extract(attempt, t2_result)
        for lesson in lessons:
            self.playbook.add_or_update(lesson)
        self.playbook.prune()

    def inject_into_prompt(self, prompt_template: str) -> str:
        """将 playbook 注入 proposer prompt。"""
        return prompt_template.replace(
            "{{PLAYBOOK}}", self.playbook.render()
        )

    def compile(self, dataset, metric, optimizer="MIPROv2"):
        """DSPy compile: 优化 prompt template + demo selection。"""
        ...
```

#### 4.2.4 TemplateManager — DSPy 模板管理层

```
class ProposerSignature(dspy.Signature):
    """Prove a Lean theorem given its header."""
    theorem_header: str = dspy.InputField()
    playbook_context: str = dspy.InputField()      # ← 新增
    lean_code: str = dspy.OutputField()

class ProposerModule(dspy.Module):
    def __init__(self):
        self.proposer = dspy.Predict(ProposerSignature)
    
    def forward(self, theorem_header, playbook_context):
        return self.proposer(
            theorem_header=theorem_header,
            playbook_context=playbook_context,
        )

# 优化器调用
def t2_pass_rate(prediction, gold):
    """Metric: T2 compile success."""
    return 1.0 if compile(prediction.lean_code).success else 0.0

optimizer = dspy.MIPROv2(metric=t2_pass_rate)
compiled_proposer = optimizer.compile(
    ProposerModule(),
    trainset=train_theorems,
    valset=val_theorems,
)
```

### 4.3 新提示词模板（ACE playbook 注入版）

```text
You are a Lean 4 theorem prover assistant.

Prove the following theorem:

{theorem_header}

=== Proof Strategy Playbook ===
{playbook_context}
==============================

Available imports:
{imports}
```

对比当前（硬编码、无 playbook）：

```text
<|im_start|>system
Prove the given Lean 4 theorem using the available environment.
<|im_end|>
```

---

## 5. 实现计划

### Phase A: ACE Playbook 基础 (3-4天)

| 任务 | 文件 | 关键方法 |
|------|------|----------|
| A1 | `omega/prover/playbook.py` | `Playbook`, `PlaybookBullet`, `PlaybookManager` 数据结构 |
| A2 | `omega/prover/reflector.py` | `ErrorAnalyzer`：T2 diagnostics → playbook lessons |
| A3 | `omega/prover/curator.py` | 更新 logic：dedup / prune / helpful+harmful 计数 |
| A4 | 集成到 GoedelProver | `_prove_theorem` 调用 PlaybookManager |

**验证**: 单定理跑通，playbook 正确积累教训

### Phase B: DSPy 模板层 (2-3天)

| 任务 | 文件 | 关键方法 |
|------|------|----------|
| B1 | `omega/prover/template_manager.py` | `Signature` 定义 + `dspy.Module` 封装 |
| B2 | `omega/prover/compiler.py` | MIPROv2/GEPA compile loop |
| B3 | MiniF2F 编译 | 先用 50 定理做 train set，metric = T2 pass |

**验证**: MIPROv2 找出的 prompt 比默认 manual prompt 在 val set 上高 10%+

### Phase C: 自适应 Playbook + 自动编译 (2天)

| 任务 | 文件 | 关键方法 |
|------|------|----------|
| C1 | 跨定理 playbook 共享 | 批次跑完后整合全局 playbook |
| C2 | 定时重编译 | 每 100 定理或每周触发一次 DSPy compile |
| C3 | playbook 快照 | `~/.omega/playbooks/` 版本化 |

**验证**: 20→100→500 定理，playbook 增长后 pass rate 单调提升

---

## 6. 预期收益

| 指标 | 当前 | 预期 | 依据 |
|------|------|------|------|
| MiniF2F T2 pass rate | 0% | 40-60% | ACE AppWorld +10.6% baseline; DSPy 多篇论文 25-40% 提升 |
| 收敛所需轮次 | ~15 attempts/定理 | 3-5 attempts | Playbook 减少重复错误 |
| 跨定理复用 | 0 | 40-60% lessons共享 | MiniF2F 244 定理中 60%+ 共享代数策略 |
| 换模型的适应时间 | 2-3天手调 | 1h DSPy compile | GEPA 论文: 75x 更便宜, 2x 更可靠 |
| 上下文退化 | 每轮重写, 信息流失 | 增量更新, 信息累积 | ACE: context collapse 完全消除 |

### 6.1 特别适用于 Omega 的 ACE/DSPy 特性

| 特性 | 为何特别适合定理证明 |
|------|--------------------|
| **无监督执行反馈** | T2 compile 的 pass/fail 是天然的人类级别监督信号，不需要人工标注 |
| **增量更新** | 证明策略不会互相矛盾（不像 agent 行为），bullet 可安全积累 |
| **BootstrapFewShot** | 成功的 proof trace 可直接作为 few-shot 样例 |
| **结构化 playbook** | 定理公式（代数/数论/组合）可分类存储，Domain-specific |
| **低开销 curator** | T2 编译本身 2.5s — 加 curator 的非 LLM 逻辑几乎 0 额外延迟 |

---

## 7. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Playbook 增长失控 | 中 | 高 | 设置 hard token budget（ACE 默认 80k, Omega 建议 8k） |
| DSPy 编译耗时过长 | 低 | 低 | 只在前 50 定理上小批量 compile；全量 weekly |
| 有害 bullet 降低性能 | 中 | 中 | harmful 计数 + 自动 prune（harmful > helpful × 2) |
| ACE 的三角色增加延迟 | 低 | 中 | Reflector 复用 T2 输出，不额外 LLM 调用；Curator 纯逻辑 |
| DSPy 需要 dspy 依赖 | 中 | 低 | 可选依赖 (`pip install dspy`)；无 dspy 时降级为纯 ACE 模式 |

---

## 8. 与现有设计的关系

| 现有组件 | ACE/DSPy 替換/增强 | 兼容性 |
|----------|-------------------|--------|
| `GoedelProver.self_correct_prompt` | → `PlaybookManager.inject_into_prompt()` | 向后兼容，可降级 |
| `GoedelProver._extract_tactics_from_text` | → 保留 | 独立功能 |
| `GoedelProver` 循环 (`n_samples`, `correction_rounds`) | → 保留外层循环 + 内层使用 Playbook | 自然增强 |
| `RethlasProver.blueprint_decompose` | → Playbook 可为 blueprint 提供子目标模板 | 可独立叠加 |
| `ArchonProver.ProgressCritic` | → reflector 可复用 ProgressCritic 的 CONVERGING/CHURNING 状态 | 互补 |

---

## 9. 一句话总结

> **ACE 解决"策略不会积累"——T2 失败的每个 error 都变成一个永久的、带计量的 playbook bullet。**
> **DSPy 解决"prompt 不会优化"——MIPROv2 用贝叶斯搜索找到最大化 T2 pass rate 的提示词和样例组合。**
> **两者组合：系统第一次跑 = 硬编码，第十次跑 = 有针对性的、经过优化的、积累了全部经验的智能管道。**

```
Phase A (ACE playbook)       Phase B (DSPy compile)        Phase C (自适应)
┌─────────────────┐          ┌─────────────────────┐       ┌─────────────────┐
│ T2 失败 → bullet │  ──→    │ MIPROv2: 找最佳 prompt│ ──→  │ playbook 跨定理共享│
│ T2 成功 → helpful++│       │ BootstrapFewShot     │      │ 定时重编译      │
│ playbook 注入 prompt│     │ 全量 compile         │      │ 版本化快照      │
└─────────────────┘          └─────────────────────┘       └─────────────────┘
```

