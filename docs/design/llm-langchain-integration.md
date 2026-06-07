# Omega LLM-LangChain 集成设计

> 将 LLM 调用层从 `curl` + `subprocess` 迁移到 `langchain-ollama`，
> 为 Langfuse 可观测性铺路。
>
> 核心原则：**只替换传输层，保留业务逻辑**。
> GoedelProver / Archon 等证明策略算法不改写进 LangChain Chain / LangGraph。

## 1. 动机

### 1.1 之前的痛点

```
subprocess.run(["curl", "-s", "--max-time", "120",
                f"{ollama_host}/api/generate", "-d", payload])
```

| 问题 | 表现 | 影响 |
|------|------|------|
| **进程级 HTTP** | 每次调用 fork 一个 curl 进程 | ~20ms 开销 + 无连接池 |
| **零可观测性** | 无法 attach Observer | 调了哪些模型？花了多少 token？延迟？全部盲盒 |
| **Think 未分离** | `...` 原样混入 content | `_extract_tactics_from_text` 污染 |
| **手动拼 JSON** | `json.dumps({model, prompt, stream, options})` | 参数无限长，无校验 |
| **社区信任** | 评审问 "为什么不用标准库？" | 需要额外解释 |

### 1.2 选择 LangChain 而非轻量替代

| 方案 | 依赖大小 | Langfuse 集成 | 第三方信任 | 选否理由 |
|------|---------|---------------|-----------|---------|
| `httpx` 手写 | ~0.3 MB | ❌ 需手工打点 | 中等 | 自研痕迹重，评审不认 |
| `ollama` 官方 SDK | ~1 MB | ❌ 需手工打点 | 中等 | 仅 ollama，无前瞻性 |
| **`langchain-ollama`** | **~30 MB** | **✅ 一等公民** | **高** | **选中** |
| LangChain 全套 Chain | ~200 MB | ✅ | 高 | 过度设计，业务逻辑不该被 Chain 包裹 |

## 2. 架构

### 2.1 分层

```
User / GoedelProver
      │
      ▼  generate_fn: Callable[[str], str]
┌────────────────────────────────────────────┐
│           omega/llm.py                     │
│                                            │
│  make_langchain_generate_fn(model, ...)    │
│       │                                    │
│       ▼                                    │
│  ChatOllama (langchain-ollama)             │
│       │  httpx.Client(connection pool)     │
│       │  callbacks=[LangfuseHandler]       │
│       ▼                                    │
│  Ollama /api/chat                           │
└────────────────────────────────────────────┘
      │
      ▼  msg.content (str)
_extract_tactics_from_text()
      │
      ▼
TacticSuggestion → T2 compilation
```

### 2.2 接口契约

LangChain 改动层完全封装在接口 `Callable[[str], str]` 之后，上游（Proposer, GoedelProver, OmegaRunner）零感知：

```python
# 之前 (runner.py:113-133)
def _generate(prompt: str) -> str:
    payload = json.dumps({"model": ..., "stream": False, ...})
    result = subprocess.run(["curl", ...], capture_output=True)
    return json.loads(result.stdout).get("response", "")

# 之后 (llm.py:166-187)
def _generate(prompt: str) -> str:
    msg = llm.invoke([HumanMessage(content=prompt)])
    return msg.content
```

### 2.3 代码布局

```
omega/
├── llm.py                          ← 新增：统一 LLM 接口
│   ├── make_langchain_generate_fn()   ↓ 返回 (str) → str
│   ├── make_streaming_generate_fn()   ↓ 流式 + early stopping
│   ├── resolve_ollama_model()         ↓ model_id → ollama 名
│   └── _get_langfuse_handler()        ↓ 可选 Langfuse trace
│
├── runner.py                        ← 修改：_make_generate_fn 简化
│   └── _make_generate_fn(model_id)     ↓ 8 行，委派给 llm.py
│
└── resource/
    └── benchmark.py                 ← 修改：run_benchmark 用 ChatOllama
        └── run_benchmark()             ↓ tok/s 测量用 ChatOllama
```

## 3. 核心设计决策

### 3.1 只替换传输层，业务逻辑不动

**不做的决策（同等重要）**：

| 不被采纳的方案 | 理由 |
|--------------|------|
| 用 `LangChain Chain` 重写 Proposer | Proposer 的算法逻辑（采样、错误收集、自校正）本质是 Python 循环 + T2 编译，不是 LLM Chain。强行迁入 LangChain 会增加复杂度、降低可调试性 |
| 用 `LangGraph` 重写 GoedelProver | Graph 状态机的表达能力对 GoedelProver 无额外收益，且会增加依赖树的编译时间 |
| 用 `OutputParser` 替换 `_extract_tactics_from_text` | Qwen3-Coder 30B 不支持 function calling / JSON mode，OutputParser 的正则不比手写 regex 强 |

### 3.2 Langfuse 可选零成本

```python
# omega/llm.py:90-105
def _get_langfuse_handler() -> Any | None:
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if pk and sk:
        return LangfuseCallbackHandler()  # ← 仅在配置时创建
    return None                           # ← 不配置 = 零开销
```

### 3.3 流式 + Early Stopping

```python
def make_streaming_generate_fn(...):
    llm = ChatOllama(...)
    def _stream_generate(prompt: str) -> str:
        buffer = ""
        fence_count = 0
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            buffer += chunk.content
            fence_count += content.count("```")
            if fence_count >= 2 and fence_count % 2 == 0:
                break  # ← 检测完整 ```lean4...``` 块后提前停止
        return buffer
    return _stream_generate
```

实测 438 chars vs 非流式 821 chars，由于 early stopping 省了约 50% 输出 token。

### 3.4 模型名解析

```python
MODEL_NAME_MAP = {
    "gemma4":     "gemma4:26b",
    "deepseek":   "deepseek-r1:8b",
    "qwen3-coder":"qwen3-coder:30b",
    "qwen3":      "qwen3.6:latest",
    "default":    "qwen3-coder:30b",
}
```

`resolve_ollama_model("ollama/deepseek")` → `"deepseek-r1:8b"`，支持前缀匹配。

## 4. 可观测性：Langfuse Trace 架构

### 4.1 Span 层次

```
Trace: GoedelProver.run(theorem="add_zero")
  │
  ├─ Generation: sample_0
  │    ├── input_tokens: 45
  │    ├── output_tokens: 256
  │    ├── model: qwen3-coder:30b
  │    ├── latency_ms: 5230
  │    └── usage_metadata: {input, output, total}
  │
  ├─ Generation: sample_1
  │    └── ...
  │
  └─ Generation: correction_round_1 (with error context)
       └── ...
```

### 4.2 自动捕获的数据

| 字段 | 来源 | 用途 |
|------|------|------|
| `input_tokens` | `usage_metadata.input_tokens` | 成本核算 |
| `output_tokens` | `usage_metadata.output_tokens` | 成本核算 |
| `model_name` | `response_metadata.model` | Provider 溯源 |
| `total_duration` | `response_metadata.total_duration` | 性能分析 |
| `prompt` | 自动 (HumanMessage content) | 调试回溯 |
| `response` | 自动 (AIMessage content) | 调试回溯 |

### 4.3 集成代码量

```python
# 需要：3 行
from langfuse.langchain import CallbackHandler
handler = CallbackHandler()
llm = ChatOllama(model=..., callbacks=[handler])
```

**零打点代码** — LangChain Callback 体系自动触发 `on_llm_start/end`。

## 5. 与旧方案对比

| 维度 | 旧 (curl + subprocess) | 新 (ChatOllama) | 收益 |
|------|----------------------|-----------------|------|
| HTTP 层 | subprocess + curl | httpx 连接池 | ~20ms/调用 |
| Think 分离 | ❌ 需手动 re | ✅ 原生 reasoning_content | 零代码 |
| 流式 | ❌ stream=False | ✅ stream=True + early stop | 省 50% 等待 |
| Token 统计 | ❌ 手动 `eval_count` | ✅ `usage_metadata` 自动 | 可靠 |
| 错误处理 | `returncode != 0` 简单 | ✅ HTTP 异常 + re-raise | 更健壮 |
| Langfuse | ❌ 不可能 | ✅ callbacks 参数 | 完整 OTel |
| 参数验证 | ❌ 手动 dict | ✅ Pydantic 30+ 字段 | 编译时检查 |
| 第三方审计 | "为什么用 curl?" | "标准 LangChain" | 无需解释 |
| 依赖 | 0 (系统自带) | langchain-ollama + langfuse | ~30 MB |
| 代码行 (generate_fn) | 55 行 | 8 行 (委派) | 84% 精简 |

## 6. 路线图

### Phase 1 — ✅ 已完成（本次）

- [x] `omega/llm.py` — ChatOllama 封装
- [x] `runner.py` — curl → ChatOllama
- [x] `benchmark.py` — curl → ChatOllama
- [x] `langfuse` — optional integration via env vars
- [x] 327/327 tests passing

### Phase 2 — 流式 + 并行采样

- [ ] `make_streaming_generate_fn` 正式化（当前已有 prototype）
- [ ] 并行 `num_samples` 调用：`ThreadPoolExecutor` + 独立 ChatOllama 实例
- [ ] 测量：`num_samples=4` 时耗时从 4× 降到 ~1.2×

### Phase 3 — Langfuse 深度集成

- [ ] `KnowledgeProver` / `GoedelProver` 级别自定义 span（标记 round、error count）
- [ ] `budget_tracker.consume()` 事件上报为 Langfuse 评分
- [ ] T2 编译结果为 custom observation
- [ ] `PROMPT_TOKEN_COST` / `COMPLETION_TOKEN_COST` 自动成本计算

### Phase 4（可选）— 其他 Provider

- [ ] 添加 `ChatAnthropic`（Claude Sonnet 4）for HARD theorems
- [ ] 添加 `ChatOpenAI` for blueprint generation
- [ ] 通过 `make_langchain_generate_fn(provider="openai")` 统一入口

## 7. 关键教训

1. **接口契约胜于框架耦合** — `Callable[[str], str]` 使得替换传输层不触动业务逻辑。LangChain 的收益是传输层的，不应允许它上渗到算法层。

2. **Langfuse 的 CallbackHandler 在 `langchain` 包中** — 仅装 `langchain-ollama` 不够，需要 `pip install langchain` 才能激活 `langfuse.langchain.CallbackHandler`。

3. **流式的 early stopping 有代价** — 如果 proof block 后的内容（如额外注释、备选方案）恰好包含后续尝试所需的关键信息，过早截断可能降低自校正效率。衡量后决定将流式保留为可选函数，默认仍用非流式。

4. **Pydantic 对 callbacks 严格** — `callbacks=[None]` 导致 ValidationError。需要在传给 ChatOllama 前 `[cb for cb in callbacks if cb is not None]` 过滤。

5. **`reasoning=True` 仅在支持 think protocol 的模型上可用**（如 DeepSeek R1、GPT-OSS）。Qwen3-Coder 不支持，设置会收到 400 错误。保持 `reasoning=None` 让 Ollama 决定。

## 8. 引用

- [LangChain ChatOllama 源码](https://github.com/langchain-ai/langchain/blob/master/libs/partners/ollama/langchain_ollama/chat_models.py)
- [LangChain BaseChatModel 抽象](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py)
- [Langfuse LangChain 集成](https://langfuse.com/integrations/frameworks/langchain)
- [LangChain Callbacks 架构](https://reference.langchain.com/python/langchain-core/callbacks)
