# Omega-Architect 优化 Plan V2（基于 Review 修正）

## P0（重构）：异步并发多源聚合搜索

### 架构

```
用户/模型搜索请求
        │
        ▼
┌─ Search Aggregator ─────────────────────────────────────┐
│  async gather(timeout_per_source=10s)                    │
│                                                         │
│  ┌────────────────────────────────────────────────────┐ │
│  │  Cache Layer (JSONL, ~/.cache/omega/search_cache/) │ │
│  │  key: sha256(normalize_query)                      │ │
│  │  val: {results, source, timestamp}                  │ │
│  │  TTL: 3600s (dev), 300s (prod)                     │ │
│  │  Fallback: if 3/4 sources timeout, use cache        │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  Concurrent Sources:                                     │
│  ├── leansearch.net          (remote, timeout=10s)      │
│  ├── loogle.lean-lang.org    (remote, timeout=10s)      │
│  ├── Local BM25 Index        (local wiki + paper store) │
│  └── Local Embedding Search  (vector, via QMD/SQLite)   │
│                                                         │
│  Post-processing:                                        │
│  1. Dedup by lemma name / URL                           │
│  2. Score fusion (normalized per-source rank)           │
│  3. Merge + dedup → top-k results                      │
│  4. Cache the merged result                             │
└─────────────────────────────────────────────────────────┘
        │
        ▼
  格式化的搜索结果 → LLM
```

### 实现要点

```python
# omega/search/aggregator.py
class SearchAggregator:
    def __init__(self):
        self.sources = [
            LeanSearchSource(timeout=10),
            LoogleSource(timeout=10),
            LocalBM25Source(),
            LocalEmbeddingSource(),
        ]
        self.cache = JSONLCache("~/.cache/omega/search_cache/")
    
    async def search(self, query: str) -> SearchResult:
        cached = self.cache.lookup(query)
        # 并发执行所有 source
        results = await asyncio.gather(
            *[s.search(query) for s in self.sources],
            return_exceptions=True
        )
        # 过滤超时/失败
        valid = [r for r in results if not isinstance(r, Exception)]
        if not valid and cached:
            return cached  # 降级使用缓存
        merged = self._merge(valid)
        self.cache.store(query, merged)
        return merged
```

### 国内网络缓存策略

| 场景 | 行为 |
|:----|:------|
| 首次搜索，网络正常 | 远程搜索 + 写入缓存 |
| 网络超时，缓存命中 | 返回缓存（1 小时内） |
| 网络超时，缓存未命中 | 返回本地 BM25 结果 + 标记"remote unavailable" |
| 开发调试 | `SEARCH_CACHE_ONLY=1` 强制仅用缓存 |

---

## P1：基于错误分级的多层次差异化反馈

### 三层架构

```
Compile Error
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Rule-Based Classifier (微秒级)                │
│  ─────────────────────────────────────                   │
│  classify_diagnostics() → 13 种 CompileErrorClass       │
│  + 正则模式匹配（已知错误模板）                           │
│  + 位置就近分析（错误行号 vs 修改行号）                   │
│  例: "unknown identifier 'X'" → UNKNOWN_IDENT           │
│      "type mismatch" + line 42 = last edit line 40 →    │
│      高概率是这次修改引入的                               │
│  输出: {class, confidence, suggested_fix}                │
└──────────────────────┬──────────────────────────────────┘
                       │ confidence < 0.7?
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 2: NLP Algorithm Spectrum (毫秒级)               │
│  ─────────────────────────────────────                   │
│  多算法并行，软投票:                                      │
│                                                         │
│  ├── Token Jaccard: 归一化错误消息的 token 集合相似度     │
│  │   (已有 ErrorMemory 的基础，但只用作一层投票)          │
│  │                                                      │
│  ├── Edit Distance: 编辑距离匹配已知失败模式               │
│  │   例: "unknown identifier 'Nat.dvd_add_right'"        │
│  │   vs "unknown identifier 'Nat.dvd_add_left'"          │
│  │   → 编辑距离 4 → 同类错误                             │
│  │                                                      │
│  ├── BM25 Retrieval: 从历史错误库检索 top-3 相似案例       │
│  │   例: 当前错误 "type mismatch: ℕ ≠ ℝ"                │
│  │   → 检索到历史案例 "algebra_sqineq 的 ℕ/ℝ 转换"       │
│  │   → 推荐 `norm_cast` 或 `Nat.cast`                    │
│  │                                                      │
│  ├── Embedding Similarity: 语义级匹配                    │
│  │   例: "expression has type ℕ but ℝ was expected"      │
│  │   vs "cannot apply ℕ to ℝ operation"                  │
│  │   → 语义相似 → 同一类 cast 问题                       │
│  │                                                      │
│  投票机制:                                               │
│    3/4 算法同意 "cast_problem" → confidence=0.85         │
│    2/4 同意 → confidence=0.60                           │
│    1/4 同意 → 降级到 Layer 3                             │
└──────────────────────┬──────────────────────────────────┘
                       │ confidence < 0.7?
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 3: LLM-as-Judge (秒级, deepseek-v4-flash)        │
│  ─────────────────────────────────────                   │
│  Judge Prompt（Lean4 数学代码模式微调）:                  │
│                                                         │
│  System: You are a Lean4 proof debugging expert.         │
│  Given a theorem and its compile error, classify the     │
│  error nature and suggest 2-3 specific fix strategies.   │
│                                                         │
│  Output JSON:                                            │
│  {                                                        │
│    "error_class": "type_cast_needed",                    │
│    "confidence": 0.9,                                     │
│    "fix_strategies": [                                    │
│      "use `norm_cast` to lift ℕ to ℝ",                   │
│      "add `Nat.cast` before the operation"                │
│    ],                                                     │
│    "key_lemma": "Nat.cast_add"                           │
│  }                                                        │
│                                                         │
│  设计约束:                                                │
│  - 仅当 Layer 1+2 都低置信度时触发                         │
│  - 异步执行，不阻塞主循环                                   │
│  - 结果缓存（同一 +error 模式无需重复 judge）              │
│  - 引入 judge_accuracy 追踪：judge 的 fix 是否真的解决了    │
│    问题？ → 自动调整 confidence weight                     │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│  最终反馈融合                                              │
│  ──────────────────────                                  │
│  - 高置信度 (0.85+) → 注入精准提示                         │
│    例: "Try `norm_cast` to resolve ℕ/ℝ mismatch"          │
│  - 中置信度 (0.6-0.85) → 注入提示 + 保留原始错误           │
│    例: "Possible cast issue. Try `norm_cast`. Full error:" │
│  - 低置信度 (<0.6) → 注入原始错误 + 通用策略                │
│    例: "Compilation error. Try a different approach."     │
└─────────────────────────────────────────────────────────┘
```

### JSONL 错误缓存（复用 P0 的缓存模式）

```python
# ~/.cache/omega/error_cache/signatures.jsonl
{
  "signature": "a1b2c3d4e5f6",
  "error_class": "type_mismatch",
  "theorem_domain": "inequality",
  "fix_strategy": "norm_cast",
  "success_count": 3,
  "failure_count": 1
}
```

这不是 ErrorMemory 的替代——ErrorMemory 存的是"错误→代码修复"，而这个是"错误→策略分类"，是跨定理通用的。

---

## 修订后完整 Plan

| 优先级 | 改进 | 工作量 | 关键设计 |
|:-----:|:-----|:------|:---------|
| **P0** | Async Multi-Source Search | 2 天 | async concurrent + JSONL cache + BM25 + embedding |
| **P1** | 3-Layer Error Classification | 3 天 | 规则层 + NLP 谱系 + LLM-Judge 投票 |
| **P2** | 修复 ConvergenceTracker | 0.5 天 | 取消单调假设 → 改用"无新策略尝试"检测 |
| **P3** | 修复 BudgetTracker | 0.5 天 | tool_call 去重，attempt = round 而非 API call |
| **P4** | Paper Store Ingest Pipeline | 1 天 | 3-pass + 人工审核 gate |

### 开工顺序

```
Day 1:    修复 ConvergenceTracker + BudgetTracker（先止血）
Day 2-3:  P0 Async Search（核心基础设施）
Day 4-6:  P1 3-Layer Error Classification（核心能力提升）
Day 7:    P4 Paper Store Pipeline（周边增强）
```

要按这个计划开工吗？
