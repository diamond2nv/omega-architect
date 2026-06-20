# 本地 GPU 增强 DeepSeek API 计划与路线图

> **状态**: 📋 计划 — 等待 WSL Hermes 系统 GPU 接力开发后实施
> **硬件**: NVIDIA RTX 4500 Ada (22.5 GB, ECC 扣减后)
> **主引擎**: DeepSeek-v4-flash/pro API (1M 上下文窗口)
> **当前环境**: CPU-only (无本地 GPU 时)

---

## 1. 架构总览：三层增强管道

```
用户请求
  │
  ├─[Level 1: 本地 vLLM 过滤层]──────────────┐
  │  Qwen2.5-7B (Q4_K_M) = ~6 GB             │
  │  简单/确定性任务→本地直接返回              │
  │  复杂/创造性任务→传 Level 2               │
  ├───────────────────────────────────────────┤
  │                                            │
  ├─[Level 2: RAG 增强层]──────────────────────┐
  │  bge-m3 ~2 GB  Embeds → 本地知识库检索     │
  │  BGE-reranker ~2 GB 精排 Top-5            │
  │  上下文字段注入 → 传给 DeepSeek API         │
  ├───────────────────────────────────────────┤
  │                                            │
  ├─[Level 3: DeepSeek API 主引擎]─────────────┤
  │  v4-flash / v4-pro                        │
  │  接收增强上下文 → 回答 → 返回用户           │
  └───────────────────────────────────────────┘
```

### 1.1 Level 1: vLLM 本地过滤层

**模型**: Qwen2.5-7B-Instruct (Q4_K_M, ~6 GB)
**端口**: `localhost:8000` (OpenAI 兼容 API)
**路由策略**:

| 场景 | 处理层 | 原因 |
|:-----|:-------|:-----|
| 简单格式化/翻译 | Level 1 | 7B 够用，0 cost |
| 日常分类/标签 | Level 1 | 7B 够用，0 cost |
| 代码格式化/lint | Level 1 | 7B 够用，0 cost |
| API 断网/限流 | Level 1 | 兜底 7B |
| 复杂推理/代码生成 | → Level 3 | 需 DeepSeek 能力 |
| 数学证明/论文写作 | → Level 3 | 需 DeepSeek 能力 |

**预估效果**: 30-50% 日常请求在 Level 1 消化，**0 API cost，<1s 延迟**。

### 1.2 Level 2: RAG 增强层

**组件**:
- `bge-m3` embedding: ~2 GB, 本地向量化 wiki/paper store
- `BGE-reranker-v2`: ~2 GB (按需加载), 精排 Top-5

**流程**:
```
用户提问
  → bge-m3 本地 embed (<10ms)
  → FAISS 内存索引检索 (~5ms)
  → BGE-reranker 精排 Top-5 (~50ms)
  → 增强上下文 + 问题 → DeepSeek API
  → 基于本地知识的精准回答
```

**知识源**: DokuWiki (190+ 页) + Paper Store (arXiv 论文) + 概念索引

### 1.3 Level 3: DeepSeek API 主引擎

不动层，但输入质量被 Level 2 大幅提升。

---

## 2. 显存分配

```
┌──────────────────────────────────────────────┐
│  NVIDIA RTX 4500 Ada  —  22.5 GB 可用        │
├──────────────────────────────────────────────┤
│  vLLM (Qwen2.5-7B Q4_K_M)        6.0 GB    ← 常驻
│  bge-m3 Embedding                 2.0 GB    ← 常驻
│  BGE-reranker v2                  2.0 GB    ← 按需加载
│  System/CUDA overhead             2.0 GB    │
├──────────────────────────────────────────────┤
│  空余可用                         10.5 GB    ← 临时任务
└──────────────────────────────────────────────┘
```

---

## 3. 技术实现方案

### 3.1 vLLM 部署

```bash
# 安装
pip install vllm

# 启动服务（常驻）
vllm serve Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4 \
  --port 8000 \
  --gpu-memory-utilization 0.35 \
  --max-model-len 8192 \
  --dtype auto

# 验证
curl http://localhost:8000/v1/models
```

### 3.2 Hermes Agent 集成

```yaml
# ~/.hermes/config.yaml 补充
custom_providers:
  local-vllm:
    base_url: http://localhost:8000/v1
    api_key: "not-needed"

# Cascade 规则：简单→本地，复杂→DeepSeek
```

### 3.3 Embedding 索引建立

```python
# 增量建立 wiki/paper store 向量索引
from sentence_transformers import SentenceTransformer
import faiss

model = SentenceTransformer("BAAI/bge-m3", device="cuda")
# 遍历 ~/wiki/ 建立 FAISS 索引
# 持久化到 ~/.omega/faiss_index/
```

---

## 4. 实施路线图

| 阶段 | 步骤 | 内容 | 预计工时 | 依赖 |
|:-----|:-----|:-----|:---------|:-----|
| **P0** | 1 | vLLM 部署 + OpenAI 兼容 API | 30min | GPU 环境 |
| | 2 | Hermes config vLLM provider + cascade | 15min | 步骤1 |
| | 3 | 验证 cascade: 简单请求本地, 复杂→API | 15min | 步骤2 |
| **P1** | 4 | bge-m3 部署 + 本地 embed 测试 | 30min | GPU 环境 |
| | 5 | 遍历 wiki/paper store 建 FAISS 索引 | 1h | 步骤4 |
| | 6 | BGE-reranker 部署 + 精排管道 | 30min | GPU 环境 |
| **P2** | 7 | RAG + vLLM + API 三端集成测试 | 1h | P0+P1 |
| | 8 | fallback 策略: GPU 掉线→纯 API 模式 | 30min | 步骤7 |
| **P3** | 9 | 跑一周指标，对比 token 节省和延迟 | 7天 | P2 |

---

## 5. 对比 LCLM 替代方案

| 维度 | LCLM Encoder+Decoder (4B) | 本方案 (vLLM+RAG) |
|:-----|:--------------------------|:-------------------|
| 输出质量 | 4B 本地 ≪ DeepSeek 数百B | 简单→本地/复杂→API，质量无损 |
| 核心价值 | 上下文压缩（已有 1M） | 减少 API 调用 + 注入本地知识 |
| VRAM 占用 | 13 GB (Encoder+Decoder) | 10 GB (vLLM+Embedding+Reranker) |
| 0 cost 请求 | 0%（全量生成） | 30-50%（本地消化简单请求） |
| RAG 增强 | 无 | 基于自有知识库的精排上下文 |
| 单次推理延迟 | >500ms (4B) | <100ms (7B Q4) |

---

## 6. Omega 集成点

```python
# omega/resource/gpu_enhancement.py (待创建)
class GPUEnhancementLayer:
    """
    本地 GPU 增强 DeepSeek API 的统一接口。

    方法:
    - route(theorem) -> "local" | "api" | "hybrid"
    - embed(text) -> vector (bge-m3)
    - rerank(query, candidates) -> ranked list
    - generate(prompt) -> Response (vLLM Qwen7B)
    """
```

## 7. 风险

| 风险 | 概率 | 影响 | 缓解 |
|:-----|:-----|:-----|:-----|
| 本地 GPU 掉线 | 中 | 降级到纯 API | fallback 到 `deepseek-api` backend |
| vLLM OOM | 低 | 服务 crash | `--gpu-memory-utilization 0.35` |
| bge-m3 索引过期 | 高 | 检索质量下降 | 增量索引 cron job |
| DeepSeek API 降级 | 低 | 全走本地 7B | 7B 兜底质量下降但不断服 |
