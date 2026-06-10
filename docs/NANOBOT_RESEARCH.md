# Nanobot 研究与借鉴分析

> **Doc version**: `0.1.0` — matches repo version
> **Last updated**: 2026-06-10

> **调研日期**: 2026-06-10  
> **目标**: 评估 nanobot 的 code-agent 架构能否改进我们的 Lean4 证明循环  
> **来源**: [github.com/HKUDS/nanobot](https://github.com/HKUDS/nanobot) (44k stars, MIT), [nanobot.wiki](https://nanobot.wiki/docs/0.2.1/advanced/python-sdk)

---

## 1. 关键规格对比

| 维度 | nanobot | Ω-Architect Inner Loop |
|------|---------|----------------------|
| 核心代码量 | ~4K 行 | ~2.5K 行 (omega/loop/) |
| 许可证 | MIT | Apache 2.0 |
| 代理循环 | `AgentRunner.run()` → `_run_core()` | `inner_loop()` |
| LLM 调用 | OpenAI-compatible provider | DeepSeek v4-pro (tool_calls + thinking) |
| 工具执行 | 异步, 支持并发, session-based | 同步, 串行, 单次 |
| 编译验证 | N/A (通用 agent) | `lean --stdin` 本地编译门 |
| 搜索工具 | MCP + web tools | lean-lsp-mcp (loogle/leansearch) |
| 上下文管理 | microcompact + tool_result_budget | compress_messages (每3轮) |
| 重试机制 | `_MAX_EMPTY_RETRIES=2`, `_MAX_LENGTH_RECOVERIES=3` | 无显式重试 |

## 2. 可借鉴的设计模式

### 2.1 上下文 Token 预算管理 (高价值)

nanobot 的 `_apply_tool_result_budget()` 在每次 LLM 调用前计算 context_window_tokens，**动态截断工具结果**。我们当前是每 3 轮固定压缩，不够精细。

**可复刻**: 在 `ds.send()` 前计算当前 messages 的 token 总量，超出预算时截断最旧的 tool_result。

### 2.2 微压缩 (Microcompact) (高价值)

nanobot 对旧工具结果做 summarize 替换（`_COMPACTABLE_TOOLS`），保留最近 10 条不压缩。我们固定每 3 轮全量压缩（丢弃旧 reasoning），粒度太粗。

**可复刻**: 只压缩最近 10 条以外的 tool_result，保留 reasoning_content 完整。

### 2.3 空内容重试 (中价值)

`_MAX_EMPTY_RETRIES=2` — 模型返回空/空白时重试。我们当前 `continue` 但不计数重试，可能导致无限循环。

**可复刻**: 在 `inner.py` 中加 `empty_retries` 计数器，超过 `max_empty_retries=3` 才终止。

### 2.4 Length 截断恢复 (中价值)

`_MAX_LENGTH_RECOVERIES=3` — 模型输出被截断 (finish_reason="length") 时自动追加 "continue" 提示。DeepSeek 有此情况。

### 2.5 Session-based Shell 执行 (高价值)

nanobot 的 `ExecTool` 支持 `yield_time_ms` 参数启动持久 shell 会话，适合需要多步交互的场景（如 `lake build`）。我们当前 `lean --stdin` 是单次调用。

**可复刻**: `CompileGate` 可改为 session-based `lake env` shell，避免每次启动进程开销。

### 2.6 Goal 延续模式 (低价值)

`goal_active_predicate` + `goal_continue_message` — 当目标未完成时自动追加延续提示。我们已经有 `continue` 机制，不需要。

### 2.7 安全守卫 (中价值)

nanobot 的 shell security 包括：
- deny patterns (rm -rf, dd, mkfs, shutdown)
- workspace path traversal 检测
- URL 检测 (SSRF guard)

**可复刻**: 为 `CompileGate` 和未来可能的通用 shell 执行添加安全守卫。

## 3. License 合规分析 ✅

| 项目 | 许可证 | 兼容性 |
|------|--------|--------|
| **nanobot** | MIT | ✅ 完全兼容 |
| **Ω-Architect** | Apache 2.0 | — |

**MIT → Apache 2.0 兼容性**: ✅ 无冲突
- MIT 是最宽松的许可证之一，允许"使用、复制、修改、合并、发布、分发、再许可和/或销售"
- Apache 2.0 项目可以包含 MIT 代码（MIT 条款不要求衍生作品也使用 MIT）
- **唯一义务**: 保留 MIT 版权声明（在 nanobot 代码文件的头部保留 "Copyright (c) 2025-present Xubin Ren and the nanobot contributors"）

**引用方式建议**:
```
# 直接复制代码片段 (< 10 行): 不需要版权声明（合理使用）
# 复制完整函数/文件: 保留文件头 MIT 版权声明 + 标注代码来源
# 参考设计后重新实现: 不需要版权声明，但建议提及灵感来源
# 文档引用: 自由引用，MIT 适用
```

## 4. 融合建议

### 4.1 高优先级 (立即采纳)

1. **Token 预算管理**: 在 `inner_loop` 中加入 token 预算检查，截断旧 tool_result 而非旧的 reasoning_content
2. **Microcompact**: 仅压缩 distance > 10 的 tool_result，保留最近推理上下文
3. **空内容重试**: 模型返回空时计数 retry，上限 3 次后终止

### 4.2 中优先级 (下一轮)

4. **Session-based CompileGate**: 改为持久 `lake env` shell 会话，减少每次编译的进程启动开销（当前 ~200ms/次）
5. **Length 恢复**: `finish_reason="length"` 时自动追加 "continue" prompt
6. **安全守卫**: 为 `compile_gate.py` 添加 deny pattern 检查

### 4.3 低优先级 (蓝图)

7. **并发工具**: 当前 MCP 搜索是串行的，可以改为并发（leansearch + loogle 同时查）
8. **Goal 延续**: 非必要，我们的循环已经有天然延续

## 5. 不采纳的部分

| nanobot 功能 | 不采纳原因 |
|-------------|-----------|
| Channels (Telegram/Slack/Discord) | 非相关 — Omega 是后端系统 |
| WebUI/Gateway | 非相关 |
| Memory/Long-term Dream | 已有 `DialogueCache` + `MemoryStore` |
| Image/voice generation | 非相关 |
| Plugin system | 过度工程化，不适合 2.5K 行的 loop |
| Provider routing | 我们只用一个 provider (DeepSeek) |

## 6. 总结

**技术价值**: nanobot 的 `AgentRunner` 在上下文管理和错误恢复方面有成熟的模式  
**License 风险**: 无 — MIT → Apache 2.0 完全兼容  
**建议方案**: **不 clone repo，只提取模式重新实现** — 这样无需处理版权声明，且代码量保持 2.5K 行  
**总 ROI**: 3-4 个高价值模式，预计 0.5 天实现，可提升 Inner Loop 稳定性和 token 效率
