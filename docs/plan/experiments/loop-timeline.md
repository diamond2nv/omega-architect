---
plan_id: loop-experiment-timeline-v1
title: "2天 Loop 实验时间线 + 架构变化追踪"
version: 1.0
date: 2026-06-12
status: active
---

# Loop 实验 2 天演化史

## 时间线

```
Day 1 (2026-06-11)
──────────────────────────────────────────────────────────────────────
13:30  Run7/V3      4/10  outer.py + inner_loop (64轮, proof_sketch)
                          ↑ 首次 full-width 运行，V3 基线

14:45  Run8/Beam    timeout  beam=3 重跑失败题 → 超时

17:03  E1           3/10   ErrorMemory v1 + 预算 $0.04 限制
                            ↑ loop 独立运行（不再走 outer.py）

17:46  E2(best)     5/10   顺序 + 512轮 + 无收敛检测
                            ↑ 当前最高记录

18:xx  E2(multi)    5/9    E2 复跑，缺 1 题

23:22  E3           4/10   beam=3 + 候选融合 + 收敛检测
                            ↑ 新功能 + 死循环修复，没提升

23:32  Multi-Stage  5/10   按难度差异化（easy beam=1, hard beam=5）
                            ↑ 成本 3x，通过率不变

Day 2 (2026-06-12)
──────────────────────────────────────────────────────────────────────
00:15  P0 (rerun)   4/10   验证 E2(best) 是否可复现 → 否，40-50%
00:33  P1 (pro)     0/4    deepseek-v4-pro 攻 4 道 never-pass
00:51  P2 (Goedel)  0/3    WSL vLLM 本地推理 → 4096 context 瓶颈
01:33  Domain Prompt 0/4   注入 lemma 名 → 证明瓶颈不在 prompt
```

## Loop 架构变化点（按实验顺序）

### InnerLoopConfig 字段的逐次变更

| 实验 | 新增/修改字段 | 目的 | 效果 |
|------|-------------|------|------|
| Run7 | (基线) | — | — |
| E1 | `error_memory=True` | 引入错误记忆 | hit_rate=0% |
| E2 | `dead_loop_detection=False` | 关闭收敛检测 | ⬆ 5/10 最高记录 |
| E3 | `parallel_candidates=3` | beam search | ❌ 没帮助 |
| E3 | `candidate_fusion=True` | 多候选融合 | ❌ 没帮助 |
| E3 | `convergence_window=5→3` | 减少误判 | ❌ 反而少了 5 轮 |
| Multi | 按难度差异化 | 节省 easy 资源 | time↓ 但 cost↑ |
| P0 | 复现 E2(best) | 验证可复现性 | 40% 不可完全复现 |

### 代码新增（这两天）

| 文件 | 实验引入 | 行数 | 当前状态 |
|------|---------|:----:|:--------:|
| `omega/loop/error_memory.py` | E1 | 394 | 🔴 hit_rate=0% |
| `omega/loop/deepseek_client.py` | Run7 | 400+ | 🟢 稳定 |
| `omega/loop/compile_gate.py` | Run7 | 250+ | 🟢 稳定 |
| `omega/loop/verifier.py` | Run7 | 150+ | 🟢 稳定 |
| `omega/loop/inner.py` | Run7 | 1247 | 🟡 需整合 P1 |
| `omega/loop/runner.py` | Run7 | 267 | 🟢 稳定 |

## 实验数据汇总

| 实验 | 模型 | 配置 | 通过率 | 成本 | 时间 | 死循环 |
|:----:|:----:|:----|:-----:|:----:|:----:|:-----:|
| Run7/V3 | flash | 64轮, 旧架构 | 40% | $0.21 | 58.5min | — |
| E1 | flash | +ErrorMemory, $0.04 | 30% | $0.075 | 18.8min | 7 |
| E2(best) | flash | 顺序+512轮 | **50%** | $0.19 | 41.7min | 5 |
| E3 | flash | beam=3+融合 | 40% | $0.11 | 28.4min | 6 |
| Multi-Stage | flash | 差异化策略 | 50% | $0.58 | 27.7min | 5 |
| P0 (rerun) | flash | 同 E2(best) | 40% | $0.17 | 29.4min | 6 |
| P1 (pro) | pro | 4 never-pass | 0% | $0.66 | 46.9min | — |
| P2 (Goedel) | goedel | vLLM 4096 | 0% | $0 | 9.2min | — |
| Domain | flash | 注入 lemma | 0% | $0.10 | 17.6min | — |

## 关键决策节点

```
E2(best) 50% ──→ E3 40%       ← beam+融合 不如 纯顺序
                    │
                    ▼
            结论：架构创新不提升通过率
                    │
                    ▼
            P1 pro 0/4          ← 模型不是瓶颈
            P2 Goedel 0/3       ← 本地 GPU 4096 瓶颈
            Domain Prompt 0/4   ← prompt 不是瓶颈
                    │
                    ▼
            结论：40-50% 是 deepseek-v4-flash 天花板
                    │
                    ▼
            战略转向：
              1. 修复已知 bug（ConvergenceTracker, BudgetTracker）
              2. 基础设施增强（P0 搜索, P1 分类）
              3. 第三方 CLI 重设计
```

## 当前代码状态速查

```
正在使用的路径（我们实验跑的）:
omega/loop/
├── inner.py              ✅ 稳定，需整合 P1
├── runner.py             ✅ 稳定
├── compile_gate.py       ✅ 稳定
├── deepseek_client.py    ✅ 稳定
├── verifier.py           ✅ 稳定
├── errors.py             ✅ 稳定
├── error_memory.py       🔴 将替换为 P1 Layer 2
├── compress.py           ✅ 可用
├── memory_processor.py   ✅ 可用
├── dialogue_cache.py     ✅ 可用
├── outer.py              🟡 未测试
├── mcp_client.py         🟡 MCP 客户端
├── mcp_sync.py           🟡 MCP 同步
├── file_pipeline.py      🟡 实验性
└── persistent_shell.py   🟡 实验性

未使用的旧模块:
omega/prover/
├── go_prover.py          🟡 仅 P2 实验调用过
├── ar_prover.py          🔴 未使用
├── re_prover.py          🔴 未使用
└── ensemble.py           🔴 未使用

omega/search/
├── proposer.py           🔴 未使用
├── blueprint.py          🔴 未使用
├── channels.py           🔴 未使用
├── curriculum.py         🔴 未使用
├── lean_search.py        🟡 将替换为 P0
└── error_classifier.py   🟡 将扩展为 P1 Layer 1
```
