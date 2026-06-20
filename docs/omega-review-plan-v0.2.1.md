# Ω-Architect 项目 Review + 分步开发计划

> **版本**: v0.2.1  
> **日期**: 2026-06-20  
> **开发机约束**: Intel i5-12450H (8核/12线程) · 16GB RAM · **无 GPU** · DeepSeek API

---

## 一、当前状态 Review

### 1.1 核心指标

| 维度 | 状态 | 详情 |
|:-----|:-----|:------|
| **架构完整性** | ✅ L1-L4全部实现 | 资源抽象/证明引擎/编排器/策略学习 |
| **Python 源码** | 176 文件 | 含 omega-core + omega + omega-plugin + CLI |
| **测试覆盖** | 725 用例 | **619 pass · 15 fail · 29 skip · 12 error** |
| **AGENTS.md** | ✅ 344 行 | 完整架构文档 + 决策记录 |
| **Whitepaper** | ✅ 7章 | Quarto 排版，PDF ~1.9MB |
| **Partner Proposal** | ✅ 新增 | `docs/omega-partner-proposal.md` |
| **MiniF2F 基线** | 4/10 (40%) | Easy 3/3 ✅, Medium 1/4, Hard 0/3 |

### 1.2 15 项测试失败——全部可修复

**原因 1: json_repair 缺失**（7 个失败 → 4+2+1）
```
omega/llm.py:464 → import json_repair → ModuleNotFoundError
```
影响: `test_generate_fn.py`(4), `test_learn_policy.py`(2), `test_playbook.py`(1)
✅ 修复: `uv pip install json_repair`

**原因 2: Lean 项目 `lean-paper-plane` 不存在**（6 个失败）
```
Project directory not found: /home/shenli/lean-paper-plane
```
影响: `test_t2_real.py` 全部 6 个测试
✅ 修复: `mkdir ~/lean-paper-plane && cd ~/lean-paper-plane && lake init lean-paper-plane`

**原因 3: 环境依赖不完整**（2 个 error 模块）
```
test_luffy_trainer.py: 缺少 peft
test_model_router_v2.py: 缺少 sklearn/svd
```
✅ 修复: 标记为 `@pytest.mark.skipif(no_gpu, ...)` — CPU-only 机器本就不需要 LoRA/ML 路由

### 1.3 对本机的约束分析（关键）

| 模块 | GPU 需求 | 本机可行? | 替代方案 |
|:-----|:---------|:----------|:---------|
| DeepSeek API (flash/pro) | ❌ 不依赖 | ✅ | 主 LLM 后端 |
| Lean 4 + Mathlib | ❌ 不依赖 | ✅ | 本地编译门 |
| lean-lsp-mcp | ❌ 不依赖 | ✅ | MCP 搜索 |
| Goedel-Prover-V2 vLLM | ~24GB VRAM | ❌ | 仅云 GPU |
| LUFFY GRPO LoRA 训练 | ~6-24GB VRAM | ❌ | 仅云 GPU |
| GPU layer (detector/backends) | CUDA 设备 | ⛔ 不可用 | CPU-only 回退 |
| Batch benchmark (vLLM) | ~24GB VRAM | ❌ | DeepSeek API 替代 |

**结论**: 本机是 **API→CPU-only** 架构。所有 GPU/本地推理路径仅在云 GPU 可用时启用。

---

## 二、分步开发计划

### 2.1 快速修复阶段（预期 ~2 小时）

#### Task 1: 安装缺失依赖

```bash
cd ~/Documents/Gitlab/forgejo-self-host/omega-architect
uv pip install json_repair
```

**验证**: `python3 -m pytest tests/test_generate_fn.py -q` → 10 passed

#### Task 2: 创建 Lean 测试项目

```bash
mkdir -p ~/lean-paper-plane
cd ~/lean-paper-plane
lake init lean-paper-plane
lake build  # 验证 Mathlib 可访问
```

**验证**: `python3 -m pytest tests/test_t2_real.py -q` → 17 passed

#### Task 3: 标记 GPU 依赖测试为 skip

修改 `tests/conftest.py` 或对应 test 文件:

```python
# 在 tests/conftest.py 或 test 文件中
import pytest
import subprocess

def has_gpu():
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

no_gpu = pytest.mark.skipif(not has_gpu(), reason="No GPU available")
```

**目标**: `python3 -m pytest tests/ -q` → 0 failed

---

### 2.2 基础设施加固阶段

#### Task 4: Plan Layer 增加 CPU-only 模式

**文件**: `omega/plan/__init__.py` → `PlanManager.resolve()`

在 `PlanManager` 增加 `mode="cpu-only"` 预设:

```python
def resolve(self, tier="production", mode=None):
    if mode is None:
        # auto-detect: CPU-only machine
        mode = "cpu-only" if not self._detect_gpu() else "gpu"
    
    if mode == "cpu-only":
        return ExecutionPlan(
            budget=BudgetPlan(tier),
            gpu=GPUPlan("cpu-only"),  # 跳过 GPU 依赖
            lean=PathPlan(),
            mode_router="dfs-only",    # 只启用 DFS (不需要 GPU 采样)
            model_backend="deepseek-api-only"  # 无 vLLM
        )
```

**验证**: `python3 -c "from omega.plan import PlanManager; p = PlanManager.resolve('development', 'cpu-only'); print(p.gpu.mode)"` → `cpu-only`

#### Task 5: GPU detector 增加 CPU-only 分支检测

**文件**: `omega/gpu_layer/detector.py`

```python
def detect():
    if not shutil.which("nvidia-smi"):
        return {"available": False, "reason": "no-nvidia-smi"}
    ...
```

**验证**: `python3 -c "from omega.gpu_layer.detector import detect; print(detect())"` → `{'available': False, ...}`

#### Task 6: ModeRouter 增加 CPU-only 模式

**文件**: `omega/engine/router.py`

CPU-only 下跳过 Hybrid (需要 GPU beam)，只保留 DFS:

```python
def select_mode(config):
    if config.cpu_only:
        return "dfs"  # DFS 只需要 DeepSeek API
    ...
```

---

### 2.3 QA 验证阶段

#### Task 7: MiniF2F 10 题基线 + 诊断记录

```bash
cd ~/Documents/Gitlab/forgejo-self-host/omega-architect
python3 -m omega.cli prove-bench --benchmark minif2f --subset 10
```

记录:
- 成功率（当前 4/10 = 40%）
- 每个定理的 round 数 / token 消耗 / $ cost
- 失败定理的错误类型分布
- 与 SOTA 对比表

#### Task 8: 修复已知的 3 个 Medium/Hard 问题

已有诊断:

| 定理 | 难度 | 问题 | 策略 |
|:-----|:-----|:-----|:-----|
| amc12a_2020_p10 | Medium | search_loop — MCP 搜索超限 | 降低 max_search_rounds 或调整搜索阈值 |
| algebra_amgm_sum1... | Medium | compile 后 stuck — 需 nlinarith 引导 | 自适应策略注入 nlinarith 提示 |
| imo_1959_p1 | Hard | API 一致性不稳定（有时 pass 有时 fail） | 添加 retry + temperature 衰减逻辑 |

---

### 2.4 性能优化与文档阶段

#### Task 9: 缓存和收敛检测优化

- `ErrorMemory` 的跨定理学习复用
- `DialogueCache` 的 SFT 导出管线修复
- `ConvergenceTracker` 在 CPU-only 模式的阈值调整

#### Task 10: 更新 AGENTS.md

更新内容:
- CPU-only 模式使用说明
- 测试状态表（排除 GPU 依赖的测试）
- MiniF2F 基线
- 常用命令速查

---

## 三、分步执行计划（按优先级）

```
P0 优先级 ────────────────────────────────────────────────────────────
Task 1: uv pip install json_repair                    [~2分钟]
Task 2: 创建 lean-paper-plane 项目                      [~5分钟]
Task 3: 标记 GPU 依赖测试为 skip                        [~15分钟]
──────────────────────────────────────────────────────────────────────
Tests 回归: 725 pass / 0 fail                          [目标状态]

P1 优先级 ────────────────────────────────────────────────────────────
Task 4+5+6: CPU-only 模式                                    [~40分钟]
  · PlanManager 增加 cpu-only preset
  · gpu_layer/detector 检测
  · ModeRouter 跳过 GPU 路径
──────────────────────────────────────────────────────────────────────
CPU-only 模式下端到端可运行

P2 优先级 ────────────────────────────────────────────────────────────
Task 7: MiniF2F 10题基线 + 诊断                            [~30分钟]
Task 8: 修复 3 个已知 Medium/Hard 问题                     [~1-2小时]
──────────────────────────────────────────────────────────────────────
MiniF2F 基线 10 题: 4/10 → 目标 6/10

P3 优先级 ────────────────────────────────────────────────────────────
Task 9: 缓存 + 收敛检测优化                                 [~30分钟]
Task 10: 更新 AGENTS.md + 使用文档                           [~20分钟]
──────────────────────────────────────────────────────────────────────
文档完整 + 优化到位
```

---

## 四、关键约束与风险

### 4.1 本机 vs 云 GPU 的分工

| 工作 | 本机 (CPU) | 云 GPU (合作方/临时租用) |
|:-----|:-----------|:-----------------------|
| 日常开发/测试/调试 | ✅ | — |
| MiniF2F 244 题全量分类 | ✅ (脚本) | — |
| DeepSeek API 推理 | ✅ | — |
| EA-GRPO Reward 集成编码 | ✅ | — |
| LoRA 训练 + vLLM 推理 | — | ✅ |
| GRPO 多轮训练实验 | — | ✅ |
| Batch Benchmark (300+题) | — | ✅ |
| 论文实验验证 | — | ✅ |

### 4.2 不做的（YAGNI）

1. **本地 GPU 层** — 无需在 CPU-only 机器上调试 GPU 代码
2. **Auto-Loop 元搜索** — 仅限于 P5，需先完成 P3-P4
3. **多机分布式训练** — 超出 MVP 范围
4. **Research 模块（arxiv/wiki/hfpclawer）** — 功能完整但不优先

### 4.3 已知风险

| 风险 | 概率 | 影响 | 缓解 |
|:-----|:-----|:-----|:-----|
| DeepSeek API 不稳定 | 中 | 高 — 卡住开发 | CLI 增加 `--retry` flag；缓存已成功的对话 |
| Lean 版本兼容 | 低 | 中 — 回归失败 | 锁定 v4.30.0 + Mathlib 8109 |
| 16GB RAM 不足 | 低 | 中 — OOM | Lean 编译通常 <2GB；DeepSeek API 无本地内存开销 |
| MCP 连接问题 | 低 | 中 — 搜索工具不可用 | 有 fallback 到纯 prompt 推理 |

---

## 五、下一步具体行动

读完 review 后，告诉我：

1. **OK 直接开工** — 按 P0→P1→P2→P3 顺序执行
2. **先修 `json_repair` 和 Lean 项目** — 快速让 15 个测试通过
3. **先写 CPU-only 模式** — 再一起跑测试
4. **跳过基础设施，直接跑 MiniF2F 基线** — 看看 10 题现状再定
