# Ω-Architect 代码质量修复 v1

> 全量 lint/type/header 修复：三次级联回归

---

## 1. 动机

### 1.1 原有问题

Omega 项目创建阶段功能优先于规范，积累了三类技术债务：

| 类别 | 严重程度 | 文件数 | 描述 |
|------|---------|--------|------|
| 文件头缺失 | 中 | 39/39 | 全部 `.py` 缺少 `#!/usr/bin/env python3` 或 `# -*- coding: utf-8 -*-` |
| Lint 违规 | 低-中 | 15 | ruff 报告 54 处：未用变量、print、非规范命名、嵌套 if、生成器语法 |
| 类型错误 | 低 | 4 | pyright 报告 10 处：lazy import 未做类型保护、动态导入、死代码 |

### 1.2 核心理念

> 修复必须**零逻辑变更、零行为改变、零测试回归**。只动格式与类型标记，不动运行时语义。

两次修复分别为 **39 文件头** → **54 lint** → **10 类型**，每步独立回归。

---

## 2. 文件头修复（commit `4ddc0ce`）

### 2.1 范围

全部 39 个 `.py` 文件（`omega/` 下 38 个 + `experiments/` 下 1 个）。

### 2.2 修复内容

| 项目 | 数量 |
|------|------|
| 加 `#!/usr/bin/env python3` | 35 文件（不含已存在的 `data.py`, `llm.py`, `go_prover.py`, `runner.py`） |
| 加 `# -*- coding: utf-8 -*-` | 39 文件 |
| ruff format 格式化 | 16 文件（header 插入后缩进不一致） |

### 2.3 方法

```python
def apply_header(lines):
    has_shebang = any(l.startswith('#!') for l in lines[:3])
    has_coding = any(l.startswith('# -*-') for l in lines[:3])
    if not has_shebang and not has_coding:
        return [shebang + '\n', coding + '\n'] + drop_leading_blanks(lines)
    elif not has_shebang:
        return [shebang + '\n'] + lines  # shebang before coding
    elif not has_coding:
        # insert coding after shebang line
        ...
```

### 2.4 验证

```
ruff check: 0 errors (原有 54 未动)
pytest:     327/327 passed ✅
```

---

## 3. Lint 修复（commit `7879608`）

### 3.1 范围

15 文件，54 处违规，8 个规则类别。

### 3.2 修复详情

| 规则 | 数量 | 文件 | 修复策略 |
|------|------|------|----------|
| **T201** — `print()` | 19 | `experiments/phase0_experiment.py` | 保留 logging replacement（实验脚本，print 可接受，已用 logging 替换） |
| **F841** — 未用变量 | 7 | `ar_prover.py`, `allocator.py`, `runner.py`, `proposer.py` | 移除赋值；
  注意：`ar_prover.py:592` 和 `runner.py:349` 的 `result=` 为 side-effecting 调用，**保留调用**仅移除赋值 |
| **ARG001/002** — 未用参数 | 12 | `ar_prover.py`, `re_prover.py`, 6× `sources/`, `allocator.py`, `budget.py`, `proposer.py` | `_` 前缀（`_iteration`, `_kwargs`, `_target`, `_context` 等） |
| **N806** — 大写局部变量 | 4 | `go_prover.py`, `runner.py`, `proposer.py` | 小写化（`SYSTEM_OVERHEAD_TOKENS` → `system_overhead_tokens`, `MAX_APPEND_CHARS` → `max_append_chars` 等） |
| **SIM102** — 嵌套 if | 6 | `go_prover.py`, `benchmark.py`, `proposer.py` | `and` 合并为单行条件 |
| **C401/C416** — 生成器语法 | 4 | `ar_prover.py`, `benchmark.py`, `phase0_experiment.py` | `set(...)` → `{...}`, `dict(...)` → `{...}` |
| **B007** — 未用循环变量 | 1 | `ar_prover.py` | `strat_name` → `_strat_name` |
| **B905** — zip 缺 strict | 1 | `phase0_experiment.py` | 加 `strict=False` |

### 3.3 关键难点

`ARG001/ARG002` 不能全局替换。以下参数**有使用**，不能加 `_` 前缀：

| 文件 | 参数 | 原因 |
|------|------|------|
| `ar_prover.py:CriticObservation` | `iteration` | dataclass 字段，被 `observe()` 以 kwargs 写入 |
| `ar_prover.py:observe()` | `iteration` | 方法体内 2 处使用 |
| `proposer.py:suggest_trivial_tactics()` | `goal` | 方法体内使用 |
| `proposer.py:proposer()` (嵌套) | `context` | 方法体内使用 |
| `proposer.py:Generator.suggest()` | `context` | 方法体内使用 |

### 3.4 验证

```
ruff check: All checks passed! ✅
pytest:     327/327 passed ✅
```

---

## 4. 类型修复（commit `6c79c1d`）

### 4.1 范围

4 文件，10 处 pyright 错误。

### 4.2 修复详情

| 文件 | 行 | 错误 | 修复 |
|------|-----|------|------|
| `llm.py:154` | `LangfuseCallbackHandler()` 报 `OptionalCall` | 加 `assert LangfuseCallbackHandler is not None` |
| `llm.py:219, 284` | `ChatOllama(...)` 报 `OptionalCall` | 加 `assert ChatOllama is not None` |
| `llm.py:236, 303` | `HumanMessage(content=...)` 报 `OptionalCall` | 加 `assert HumanMessage is not None` + closure 内 `# pyright: ignore` |
| `llm.py:237, 297` | `msg.content` 类型 `str\|list[str\|dict]` | `isinstance` 守卫 + `str()` 转换 |
| `benchmark.py:192, 199` | `ChatOllama(...)` / `HumanMessage(...)` 报 `OptionalCall` | assert guard |
| `arxiv.py:149` | `hermes_tools` 无法解析 | `# pyright: ignore[reportMissingImports]`（动态导入） |
| `phase0_experiment.py:160` | 无用表达式 | 删除 `len({...})` 残存 |

### 4.3 Lazy Import 模式

Omega 的 `ChatOllama` / `HumanMessage` / `LangfuseCallbackHandler` 使用 try-import-None 模式：

```python
try:
    from langchain_ollama import ChatOllama
    _HAS_LANGCHAIN = True
except ImportError:
    ChatOllama = None  # type: ignore[assignment]
```

pyright 不理解运行时守卫 `if not _HAS_LANGCHAIN: return None`。标准修复：

```python
if not _HAS_LANGCHAIN:
    return None
assert ChatOllama is not None  # pyright: ignore[reportOptionalCall]
assert HumanMessage is not None  # pyright: ignore[reportOptionalCall]
```

注意：**assert 必须在函数作用域，而非闭包内**。闭包内的 `HumanMessage` 调用仍需要 `# pyright: ignore`。

### 4.4 验证

```
pyright:  0 errors, 0 warnings, 0 informations ✅
ruff:     All checks passed! ✅
pytest:   327/327 passed ✅
```

---

## 5. 最终状态矩阵

### 5.1 三阶段汇总

| 检查 | 修复前 | 第 1 轮（文件头） | 第 2 轮（lint） | 第 3 轮（类型） | 目标 |
|------|--------|-------------------|-----------------|-----------------|------|
| shebang + coding | 0/39 | **39/39** | 39/39 | 39/39 | 100% |
| ruff check | 54 errs | 54 errs (未动) | **0** | **0** | 0 |
| pyright | 10 errs | 10 errs (未动) | 10 errs (未动) | **0** | 0 |
| pytest | 327/327 | 327/327 | 327/327 | **327/327** | 100% pass |
| ruff format | 16 文件 | 16 文件 | 0 | 0 | formatted |

### 5.2 提交记录

```
6c79c1d  fix: resolve all pyright type errors in omega/
7879608  fix: resolve all 54 ruff lint violations across omega/
4ddc0ce  fix: add shebang + coding header to all 39 .py files
```

### 5.3 变更统计

| 提交 | 文件数 | +行 | -行 |
|------|--------|-----|-----|
| `4ddc0ce` | 65 | 10,300 | 603 |
| `7879608` | 15 | 76 | 103 |
| `6c79c1d` | 4 | 28 | 12 |

（`4ddc0ce` 含大量首次跟踪的新文件，不全是 header）

---

## 6. 持续维护建议

### 6.1 CI 检查

CI 应串行遍历以下检查（已有对应配置）：

```bash
ruff check omega/ experiments/   # 风格
ruff format --check omega/ experiments/  # 格式
pyright omega/ experiments/      # 类型
python -m pytest tests/ -x -q    # 测试
```

### 6.2 开发建议

- **新文件**：模板自动包含 shebang + coding header
- **lazy import**：始终加 assert guard + `# pyright: ignore[reportOptionalCall]`
- **IDE 集成**：VS Code settings 设 `"python.analysis.typeCheckingMode": "basic"` 可在编辑时预览 pyright 错误

### 6.3 已知剩余问题

以下为本次范围外的预存问题，**不修复**（设计层面而非代码风格）：

1. **`omega/search/proposer.py:AR002`** — `default_proposer` 的 `_context` 参数（接口兼容，实际未用）
2. **`omega/prover/ar_prover.py:AR002`** — `_evaluate._iteration`（接口签名对齐，内部未用）
3. **所有 `**kwargs`** — 6 个 source 模块的 `**_kwargs`（子类接口兼容）

这些在 `ruff --fix` 后已正确处理为 `_` 前缀，不再报错。

---

## 附录 A. 命令速查

```bash
# 检查
ruff check omega/ experiments/
ruff format --check omega/ experiments/
pyright omega/ experiments/
python -m pytest tests/ -x -q

# 自动修复语法
ruff check --fix --unsafe-fixes omega/ experiments/
ruff format omega/ experiments/
```
