# T2 MCP 集成 — 在线编译回调

当前 MCP `lean_run_code` 在核心 Lean 库上工作正常，但**不包含 Mathlib**。
Mathlib 依赖需 `lake build`（从中国大陆下载极慢）。

## 工作状态

- **纯 Lean 定理**：T2 编译通过 ✅（5/7 = 71.4%）
- **Mathlib 定理**：需要 Mathlib lake 项目（网络受限暂未解决）
- **替代方案**：使用 Goedel-Prover-V2 或 Real-Prover 的自定义编译管线
