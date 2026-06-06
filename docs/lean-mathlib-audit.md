# Lean 4 / Mathlib 环境审计报告

> 审计时间：2026-06-06
> MCP 配置：`~/.hermes/config.yaml` → lean_lsp

## 环境现状

| 组件 | 状态 | 路径 |
|------|------|------|
| **lean 二进制** | ✅ 4.29.1 (stable) | `~/.elan/toolchains/stable/bin/lean` |
| **elan** | ✅ 已安装 | `~/.elan/bin/elan` |
| **lake** | ✅ 已安装 | `~/.elan/bin/lake` |
| **lean-lsp-mcp** | ✅ 已安装 | `~/.local/bin/lean-lsp-mcp` (uv 包) |

## Mathlib 状况

### 之前（已修复）

```
MCP 指向: lean-playground
Mathlib:   ❌ 未安装 (lakefile 无 require, packages: [])
lean_run_code 错误: "unknown module prefix 'Mathlib'"
```

### 现在（审计后）

```
MCP 指向: lean-paper-plane ← 已修正 ✅
Mathlib:  ✅ 已构建 (8,108 olean, 7.1GB)
           git: https://github.com/leanprover-community/mathlib4.git
           工具链: leanprover/lean4:v4.30.0
           其他依赖: aesop, batteries, Cli, Qq, plausible, proofwidgets,
                     LeanSearchClient, importGraph
```

**审计发现**: `lean-paper-plane` 项目已完整编译 Mathlib（8,477 oleans 总）。之前的 MCP 配置错误地指向了 `lean-playground`（一个空项目），导致 `lean_run_code` 无法解析 `import Mathlib`。

### 修复动作

1. ✅ `~/.hermes/config.yaml` 中 `LEAN_PROJECT_PATH` 已从 `/home/shenli/lean-playground` 改为 `/home/shenli/lean-paper-plane`
2. ✅ 原 lean-playground 空项目保留（不含 Mathlib）
3. ⚠️ MCP 服务器需**重启会话**后才使用新路径（当前会话仍连旧进程）

## 启动性能

WSL 环境下首次加载 Mathlib（8,108 olean 从 D 盘读取）较慢，但 MCP `lean_run_code` 在 MCP 进程存活期间保持缓存，后续调用很快。

## 已验证的定理（无需 Mathlib 的纯 Lean）

| 定理 | T1 | T2 | 说明 |
|------|----|----|------|
| add_zero | ✅ | ✅ | Nat 归纳 |
| zero_add_fixed | ✅ | ✅ | calc 证明 |
| true_trivial | ✅ | ✅ | trivial |
| and_comm_example | ✅ | ✅ | `∧` 可交换 |
| or_comm_example | ✅ | ✅ | `∨` 可交换 |
| simple_identity_example | ✅ | ✅ | `x+x = 2*x` |

## 下一步

重启 Hermes 后，MCP 将使用 `lean-paper-plane` 项目，届时可直接编译含 `import Mathlib` 的定理。

参考：
- `lean-paper-plane/PaperPlane.lean` — 已包含使用 `nlinarith`/`ring` 的示例定理
- `lean-paper-plane/.lake/packages/mathlib/` — Mathlib 本地仓库（8,108 olean）
- 之前 `mod_two_example` 和 `mathlib` 定理编译失败，重启后应可正常工作
