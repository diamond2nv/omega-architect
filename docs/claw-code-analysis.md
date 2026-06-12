# Claw Code 分析报告

> 来源: https://github.com/instructkr/claw-code (fork of ultraworkers/claw-code)  
> DeepMind 链接 (https://deepmind.com/instructkr/claw-code): **404，不存在**

## 项目性质

Claw Code 是 **Claude Code agent 架构的 clean-room Rust 重实现**。不是纯 Python 项目——Python 代码是 porting audit 工具。

## 代码构成

| 语言 | 文件数 | LOC | 比例 | 用途 |
|:----|:-----:|:---:|:----:|:-----|
| **Rust** | 101 | **70,171** | 66.1% | 主 CLI agent harness |
| Python | 78 | 2,763 | 70.2% | Porting audit / 参考镜像 |
| JSON | 68 | 2,592 | 56.4% | 工具/命令快照数据 |
| Markdown | 47 | 3,710 | — | 文档 |
| **总计** | 357 | **76,650** | — | |

## 核心架构

### Rust crate 结构

```
crates/
├── rusty-claude-cli/    ← 主 CLI 入口
├── runtime/              ← Agent runtime (file_ops, trident loop)
│   ├── file_ops.rs      文件操作
│   └── trident.rs       三叉戟循环（plan → exec → review）
├── api/                  ← API 客户端 (Anthropic / OpenAI-compatible)
├── tools/                ← 工具定义 (Bash, FileRead, FileEdit, MCP)
├── plugins/              ← 插件系统
├── memory-index/         ← 向内存/embedding 索引
│   ├── chunk.rs, db.rs, embed.rs, ingest.rs
│   ├── search.rs, qdrant_index.rs
└── mock-anthropic-service/ ← 测试 mock 服务器
```

### Python 源码 (`src/`) —— 不是 agent 本体

Python `src/` 是一个 **porting audit 工作区**，作用：

1. **镜像 Claude Code 的 TypeScript 接口快照**（存于 `src/reference_data/*.json`）
2. **追踪 Rust 端对 Claude Code 命令/工具的迁移进度**
3. **提供 CLI 诊断命令**：`summary`, `parity-audit`, `command-graph`, `tool-pool`

```python
# 核心模式：JSON 快照 → PortingModule 镜像 → 进度追踪
# src/tools.py: load_tool_snapshot() 从 tools_snapshot.json 加载
# src/commands.py: load_command_snapshot() 从 commands_snapshot.json 加载
# src/parity_audit.py: 比较 Python 工作区 vs TypeScript 归档
```

**Python 不是可运行的 agent——它是个仪表盘。**

### 哲学架构（PHILOSOPHY.md）

三体系统：
```
OmX (oh-my-codex)      → 工作流编排层
clawhip                → 事件/通知路由器 (Discord-based)
OmO (oh-my-openagent)  → 多 agent 协调

人类接口：Discord（不是终端）
```

## 与 Omega-Architect 的对比

| 维度 | Claw Code | Omega-Architect |
|:----|:----------|:----------------|
| 语言 | Rust + Python audit | **纯 Python** |
| Agent loop | Trident (plan→exec→review) | Inner Loop (proof→compile→fix) |
| 工具系统 | 工具池 + 权限上下文 | MCP tools + CompileGate |
| 上下文策略 | prompt cache + 历史管理 | ConvergenceTracker + MemoryProcessor |
| 模型后端 | Anthropic / OpenAI-compatible | **DeepSeek API** + Goedel local |
| 人类接口 | Discord 通知 | QQ Bot / 终端 |
| 记忆系统 | memory-index (向量数据库) | ErrorMemory (JSONL) |
| 坐标系统 | OmO 多 agent 协调 | loop/ 三层架构 |

## 值得参考的设计

| 设计 | 位置 | 对 Omega 的启示 |
|:----|:-----|:----------------|
| **工具池权限系统** | `src/permissions.py` | Omega 的 MCP 工具缺乏细粒度权限控制 |
| **Prompt Cache** | `rust/crates/api/src/prompt_cache.rs` | 减少 API 调用的重复上下文 |
| **Trident 循环** | `runtime/trident.rs` | Plan → Exec → Review 三阶段——比纯迭代式的 Inner Loop 更结构化 |
| **Worker 生命周期状态机** | `tools/src/lane_completion.rs` | Omega 的 loop/ 三层架构有类似但不完整的生命周期 |
| **Green-ness 合约** | ROADMAP.md | 严格的自洽验证 (Green=编译+测试通过)，Omega 的 CompileGate + Verifier 类似 |
| **Mock 服务测试** | `mock-anthropic-service/` | Omega 缺少 mock DeepSeek API 的测试框架 |
| **Parity Audit** | `src/parity_audit.py` | Omega 缺少 porting/迁移进度追踪 |
| **事件原生架构** | `runtime/src/lane_events.rs` | 结构化事件流替代日志爬取，Omega 的 httpx 日志可以借鉴 |

## DeepWiki 补充说明 (https://deepwiki.com/instructkr/claw-code)

DeepWiki 页面确认并补充了以下内容：

### "Clawable" 三大约束
1. **spawning** — agent 能 spawn 子 agent
2. **ready_for_prompt** — 系统就绪信号
3. **blocked** — 显式的阻塞/空闲状态

### Green-ness 合约
| 等级 | 含义 |
|:----:|:------|
| **Green** | 编译通过 + 全部测试通过 |
| Yellow | 编译通过，可能有测试失败 |
| Red | 无法编译 |

### 事件原生架构
取代"日志爬取"方式，改为结构化事件管道（`lane_events.rs`），事件通过 Discord 投递而非 tmux 终端。

### UltraWorkers 生态
| 组件 | 角色 | 仓库 |
|:----|:------|:-----|
| **OmX** | 编排层 (高层面任务规划) | oh-my-openagent |
| **clawhip** | 事件集成层 (claw 事件 → Discord) | clawhip |
| **claw-code** | 执行层 (Rust claw 二进制) | ultraworkers/claw-code |

## 值得注意的局限性

1. **不是 Python 项目** — 不能直接交互或集成到 Omega
2. **强依赖 Anthropic API** — 本地模型支持有限
3. **Python 端仅为 audit** — 不是可运行的 agent
4. **DeepMind 链接失效** — `deepmind.com/instructkr/claw-code` 返回 404
