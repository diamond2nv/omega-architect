# Step 2 计划 — MCTS 接入真实 Lean 证明循环 (2026-09-10)

> Step 1（`strategy_mcts.py` + `mcts_diagnosis.py`）已落地并通过 13 例测试。
> 本步把注入式三件套换成**真实实现**：LLM 生成 tactic 候选 → CompileGate 编译验证
> → 编译结果派生的价值。关联：`docs/omega-mcts-wiring-plan-2026-09.md`（三步接线）·
> `docs/omega-mcts-diagnosis-step1-plan-2026-09.md`

## 1. 环境前提（实测）

| 项 | 状态 |
|:--|:--|
| `lean` / `lake` / `elan` | ❌ **本机未安装**（`which` 全空） |
| `CompileGate` | ✅ 存在：`compile(lean_code) -> CompileResult{success, errors, error_class, line, diagnostics, elapsed_ms, cached}` |
| Lean LSP MCP | ⚙️ 经 `LEAN_LSP_MCP` 环境变量（默认 `~/.local/bin/lean-lsp-mcp`，项目默认 `~/lean-paper-plane`） |
| LLM 客户端 | ✅ `omega/llm.py` + `omega/loop/deepseek_client.py` |

→ **结论**：本机无法端到端真跑 Lean。因此 Step 2 交付**适配器层 + 可注入的编译回调**，
测试用 fake 编译器；真实端到端留待有 Lean 环境的机器（三机之一）。

> **⚠️ 计划迭代（2026-09-10，独立 review 后）**：原约束「不改 MCTSStrategy」被证明不可行——
> Step 2 的验收 T5（编译器异常/超时不得让搜索崩）本质上要求**搜索核自己**包住三个注入 callable。
> 已改核（每处调用 try/except + 降级为可诊断状态），并随 review 一并修掉：死胡同烧预算/重复调
> LLM/anytime 未实现/假成功/诊断假阳性等 7 个 major。细节见 `omega/engine/strategy_mcts.py` 模块
> docstring 与 `tests/test_mcts_strategy.py` 的回归段落。

## 2. 三个适配器（新增 `omega/engine/lean_adapters.py`）

对齐 Step 1 的三件套签名，**不改 MCTSStrategy**：

| 适配器 | 对应注入点 | 职责 | 依赖 |
|:--|:--|:--|:--|
| `LeanActionGenerator` | `action_generator: (ProofState) -> list[ProofAction]` | 由 LLM 生成 tactic 候选（k 个，含置信度）；可选先经 P0 Search Aggregator 召回 lemma | `omega/llm.py`（或注入 callable） |
| `CompileGateTransition` | `state_transition: (ProofState, ProofAction) -> ProofState` | 把 action 追加进 code → 调 `compile_fn` → 返回带 `errors`/`error_class`/`is_terminal` 的新状态 | `compile_fn`（默认 CompileGate；**可注入 fake**） |
| `CompileDistanceEvaluator` | `evaluator: (ProofState) -> float` | 价值 = 编译通过 1.0；否则由**错误类别 + 剩余目标数 + 错误行位置**折算（**廉价、无 rollout** —— 即 AlphaZero 路线的本地版） | `ProofState` 字段（无需 LLM） |

**关键设计**：三者都接受**注入**（LLM callable / compile_fn），所以生产与测试共用实现，
只在注入物上分叉（与 Step 1 同一哲学）。

## 3. 组装（一个入口函数）

```python
def build_lean_mcts(
    *,
    llm_call=None,            # None -> 用 omega.llm
    compile_fn=None,          # None -> 用 CompileGate().compile
    max_iterations: int = 32,
    c: float = 1.4,
) -> MCTSStrategy:
    """Wire MCTS onto the Lean proof loop (all collaborators injectable)."""
```

## 4. 测试策略（无 Lean、无 LLM）

| 测试 | 内容 |
|:--|:--|
| T1 | `CompileGateTransition`：注入 fake `compile_fn`（按 code 内容返回成功/指定错误类别）→ 断言状态字段（errors/error_class/is_terminal/depth） |
| T2 | `CompileDistanceEvaluator`：给定"编译通过" → 1.0；"类型不匹配@行 3 + 2 个 goal" → 落在 (0,1) 且**有序**（错误越轻分越高） |
| T3 | `LeanActionGenerator`：注入 fake LLM → 断言解析出 k 个 ProofAction（含置信度），且**非法输出被安全过滤**（空/超长/非 tactic） |
| T4 | 端到端（fake compile）：MCTS 在"两步可证"的假定理上解出，且诊断视图有 `stuck_nodes`/`blind_spots` |
| T5 | 回归：任何 fake 编译器异常（抛错/超时模拟）→ **不得让 MCTS 崩**（降级为低价值状态 + 记入 error_heat） |

## 5. 验收标准

- [ ] 三个适配器实现 + 注入点与 Step 1 签名完全一致（不改 MCTSStrategy）
- [ ] T1–T5 全绿；`fake` 编译器可复现三类错误（类型/未定义符号/未完成目标）
- [ ] `build_lean_mcts()` 在 fake 编译下端到端跑通并输出诊断
- [ ] 编译器**异常/超时**不导致搜索崩溃（降级 + 可诊断）
- [ ] ruff `--no-fix` / pyright / pytest 全绿
- [ ] 文档：真实 Lean 端到端的**运行条件**（需 lean+lake 的机器 + `LEAN_LSP_MCP` 或用 CompileGate 的本地 t2_real 回调）

## 6. 风险

| 风险 | 对策 |
|:--|:--|
| 本机无 Lean → "接线完成"无法端到端证明 | 明确区分"适配器已交付 + fake 验证"与"真实端到端未验证"；给出到有 Lean 机器的运行清单 |
| LLM tactic 候选质量低 → 搜索空转 | 诊断视图会直接暴露（盲区/卡点），Step 3 用其数据训练价值模型 |
| CompileGate 每次编译 ~秒级 → 迭代预算受限 | 依赖其内置缓存（500 条）；后续可加"同 code 快速短路" |
| 适配器把 LLM 调用放进搜索内 → token 成本 | 与 `BudgetTracker` 对齐（Step 3 收口）；候选数 k 可配 |
