# Phase C 重做：Ω-Architect 系统修复与防御性重构

## 审核摘要

7 个已验证的设计缺陷。修复优先级 = 影响面 × 修复成本。

| # | 缺陷 | 影响 | 修复成本 | 优先级 |
|---|------|------|---------|-------|
| 1 | T2 编译：`lean --stdin` 无 Mathlib 上下文 | T2=100% false negative | 中 | P0 |
| 2 | GenerateFn str→str 信息瓶颈：markdown fence 后处理失败 | 10-20% 浪费 attempt | 低 | P0 |
| 3 | 超时未传播：prove() 无超时，QQ 30s 后用户看到错误但后台继续 | 用户体验崩溃 | 低 | P0 |
| 4 | T1 验证是烟雾弹：不检查定理-证明对应 | 统计污染 | 低 | P1 |
| 5 | Ensemble 内 GoedelProver 无 generate_fn | 模板模式输出弱 | 低 | P1 |
| 6 | 无增量学习/错误分类 | 跨定理效率低 | 高 | P2 |
| 7 | `_bridge.py` 访问私有属性 `_goedel.proposer` | 运行时风险 | 低 | P1 |

## 修复设计

### P0-1: T2 编译重构

```
                       ┌──────────────────┐
       lean_code ──▶   │ T2 Compiler      │
                       ├──────────────────┤
                       │ 1. MCP lean_lsp  │  ← 优先，热启动
                       │    lean_build    │    项目上下文
                       │ 2. CLI lake env  │  ← 备用，带项目
                       │    lean --stdin  │    lean-paper-plane
                       │ 3. 缓存上次编译  │  ← 防重复编译
                       │    结果 (LRU)    │
                       └──────┬───────────┘
                              ▼
                       T2Result(verified, errors, elapsed_ms)
```

**实现**：
- 优先 `ctx.dispatch_tool("terminal", ...)` 调用 MCP `lean_lsp/lean_build`
- 备用：`cd lean-paper-plane && lake env lean --stdin`（利用已有 Mathlib 缓存）
- 编译缓存：`functools.lru_cache(maxsize=128)`，key = `lean_code` 哈希
- 错误分类：正则匹配 `unknown identifier` / `type mismatch` / `unsolved goals` / `timeout` / `missing import`

### P0-2: GenerateFn 结构化

**现状**：`(prompt: str) → str` — 自由文本输出，需要后处理 strip fence。

**改为**：

```python
PROVER_SCHEMA = {
    "type": "object",
    "properties": {
        "lean_code": {"type": "string", "description": "Full Lean 4 proof code"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "tactic": {"type": "string", "description": "Primary tactic used"},
        "notes": {"type": "string", "description": "Any notes about the proof"},
    },
    "required": ["lean_code", "confidence"],
}

result = ctx.llm.complete_structured(
    instructions="...",
    input=[{"type": "text", "text": prompt}],
    json_schema=PROVER_SCHEMA,
    temperature=0.1,
    max_tokens=2048,
    purpose="omega.proof.generate",
)
```

**Fallback**：如果 `complete_structured` 返回纯文本（`content_type == "text"`），则回退到旧版 text→code 解析。

### P0-3: 超时传播

```
/omega-prove "theorem ..." --timeout 60
    │
    ▼
prove(timeout_s=60)
    │
    ├── GoedelProver.run(timeout_s)  ← 每轮检查已耗时
    │       │
    │       └── ctx.llm.complete(timeout=55)  ← 预留 5s 给 Lean 编译
    │
    └── 超时 → 返回 {"succeeded": False, "summary": "Timeout (60s)"}
```

### P1-4: T1 验证增强

当前 T1 只检查括号平衡和 `sorry` 存在。新增检查：

1. **定理-证明结构对齐**：证明的 must `theorem header :=` 后跟证明块（`by ...`）
2. **`:=` 完整性**：定理头必须有 `:=` 且后面有内容
3. **引用验证**：证明中引用的标识符（`simp`、`omega`、`induction` 等）不能是拼写错误
4. **比验证前更严格的拦截**：`:= by\n  -- empty body` 不通过

### P1-5: GoedelProver 注入 generate_fn

`EnsembleProver` 新增 `generate_fn` 参数，传递给内部的 GoedelProver。

### P1-7: 移除私有属性访问

`OmegaBridge` 不再直接访问 `_goedel.proposer.generate_fn`。改为在 EnsembleProver init 时注入。

## 测试策略

每个修复点必须：
1. `pytest` 通过
2. `ruff check` 零错误
3. `pyright` 零新增类型错误
4. 结构评审：是否在错误路径上仍安全（fail-closed）

## 已知未修复（P2 以后）

- 真正的增量学习（跨 session lemma cache）
- Proof sketch 持久化
- 并行三路 Ensemble（当前为串行）
