#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ProofErrorMemory — 跨定理错误模式记忆（JSONL 持久化）。

从 Agora pattern_memory 借鉴的模式:
  {error_signature → proven_fix_template}

以 JSONL 行日志格式持久化，零 API 成本。

用法:
    mem = ProofErrorMemory()
    hint = mem.lookup(error_msg, category)
    if hint:
        # 注入到反馈中
    mem.record(error_msg, category, fix, theorem_name)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.loop.error_memory")

# ── 配置常量 ──────────────────────────────────────────────

MAX_ENTRIES = 2000          # 超过后 LRU 裁汰
MIN_FIX_LENGTH = 20         # 低于此长度的 fix 不记录
LOOKUP_SCAN_TAIL = 100      # lookup 扫最后 N 行（再往前太旧不相关）
SUCCESS_RATE_EPSILON = 0.01 # 避免除零

# 归一化时移除的通用词（不影响错误签名）
STOP_TOKENS = frozenset({
    "the", "at", "has", "are", "was", "were", "been",
    "this", "that", "these", "those", "its", "their",
    "file", "line", "column", "char", "position",
    "type", "term", "value", "kind",
})

# Jaccard fallback 阈值
JACCARD_THRESHOLD = 0.50


# ── 标准化函数 ────────────────────────────────────────────


def _normalize_error(msg: str) -> str:
    """归一化错误消息：去行号/位置/Unicode/数字 → lower → token 排序。
    目的是让 "type mismatch at line 42" 和 "type mismatch at line 99" 映射到同一签名。
    """
    text = re.sub(r'\bline\s+\d+\b', '', msg)
    text = re.sub(r'\bcol(?:umn)?\s+\d+\b', '', text)
    text = re.sub(r':\d+:\d+', '', text)
    text = re.sub(r'\d+', '', text)  # 所有裸数字
    # 去 Unicode 装饰
    text = text.replace('⊢', '|-').replace('ℕ', 'Nat').replace('ℤ', 'Int').replace('ℝ', 'Real')
    text = text.replace('→', '->').replace('↔', '<->')
    # 去符号
    text = re.sub(r"[`'\"\\@#$%^&*(){}[\]|;:,.<>?/~`!]", ' ', text)
    text = text.lower().strip()
    # 分词 + 去停用词（含单字符）
    tokens = [t for t in text.split() if t not in STOP_TOKENS and len(t) > 1]
    tokens.sort()
    return " ".join(tokens)


def _error_signature(msg: str) -> str:
    """错误消息的模糊签名（SHA256[:12]）。"""
    return hashlib.sha256(_normalize_error(msg).encode()).hexdigest()[:12]


def _jaccard_similarity(a: str, b: str) -> float:
    """token 集合的 Jaccard 相似度。零模型依赖，纯 set 运算。"""
    if not a or not b:
        return 0.0
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# ── 数据模型 ──────────────────────────────────────────────


@dataclass
class ErrorRecord:
    """一条错误→修复记录（对应 JSONL 的一行）。"""
    signature: str          # _error_signature 的 12 字符 hash
    category: str           # CompileErrorClass.value
    raw_error: str          # 原始第一条错误消息（审计用）
    fix_template: str       # 已验证的修复模式（compact）
    theorem_class: str      # 自动分类: number_theory / algebra / ...
    theorem_name: str       # 首次记录的定理
    success_count: int = 1
    failure_count: int = 0
    timestamp: str = ""     # ISO datetime，留空自动填充

    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total

    def to_jsonl(self) -> str:
        d = {k: v for k, v in self.__dict__.items()}
        d["_type"] = "record"
        if not d.get("timestamp"):
            d["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "ErrorRecord":
        return cls(
            signature=d.get("signature", ""),
            category=d.get("category", ""),
            raw_error=d.get("raw_error", ""),
            fix_template=d.get("fix_template", ""),
            theorem_class=d.get("theorem_class", ""),
            theorem_name=d.get("theorem_name", ""),
            success_count=d.get("success_count", 1),
            failure_count=d.get("failure_count", 0),
            timestamp=d.get("timestamp", ""),
        )


# ── 记忆类 ────────────────────────────────────────────────


class ProofErrorMemory:
    """JSONL 持久化的错误模式记忆。
    
    读取: lookup 从文件尾部向前扫描 LOOKUP_SCAN_TAIL 行，找同分类+同签名的最高成功率。
    写入: record 原子 append 一行到文件末尾。
    裁剪: 超过 MAX_ENTRIES 时，保留每 signature 最高 success_rate 的 entry。
    """

    def __init__(self, cache_dir: str = "~/.cache/omega/error_memory/"):
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.cache_dir / "signatures.jsonl"
        self._hit_count: int = 0
        self._lookup_count: int = 0

    # ── 公共接口 ──────────────────────────────────────

    def lookup(self, error_msg: str, category: str) -> str | None:
        """查找已知修复。先精确签名匹配，fallback Jaccard 相似度。"""
        self._lookup_count += 1
        if not error_msg or not category:
            return None

        sig = _error_signature(error_msg)
        best: tuple[float, str, int] = (0.0, "", -1)

        try:
            lines = self._read_tail(LOOKUP_SCAN_TAIL)
        except Exception as e:
            logger.debug("ErrorMemory read failed: %s", e)
            return None

        for line_num, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("_type") != "record":
                continue
            if rec.get("category") != category:
                continue

            # Phase 1: exact signature match
            if rec.get("signature") == sig:
                rate = self._calc_rate(rec)
                if rate > best[0]:
                    best = (rate, rec.get("fix_template", ""), line_num)
                continue

            # Phase 2: Jaccard fallback (only if no exact match yet)
            if best[0] == 0.0:
                stored_error = rec.get("raw_error", "")
                if stored_error:
                    j = _jaccard_similarity(_normalize_error(error_msg),
                                            _normalize_error(stored_error))
                    if j >= JACCARD_THRESHOLD:
                        rate = self._calc_rate(rec) * (0.5 + 0.5 * j)  # 加权：Jaccard 越高越可信
                        if rate > best[0]:
                            best = (rate, rec.get("fix_template", ""), line_num)

        if best[1]:
            self._hit_count += 1
            logger.debug("ErrorMemory HIT: sig=%s cat=%s fix=%s (rate=%.2f)",
                         sig, category, best[1][:60], best[0])
            return best[1]

        return None

    def record(
        self,
        error_msg: str,
        category: str,
        fix: str,
        theorem_name: str = "",
        theorem_class: str = "",
        success: bool = True,
    ) -> None:
        """记录 {错误 → 修复} 映射。

        ``success`` 决定这条记录是**已验证修复**还是**失败样本**:
        - ``True``  → ``success_count=1``: 可作检索先验, ``lookup()`` 会返回。
        - ``False`` → ``failure_count=1``: 负例, ``_calc_rate`` 得 0.0,
          ``lookup()`` 的 ``rate > best[0]`` 判据不会选中它 —— 只进离线训练集。
        """
        if not error_msg or not fix or len(fix) < MIN_FIX_LENGTH:
            return

        sig = _error_signature(error_msg)
        rec = ErrorRecord(
            signature=sig,
            category=category,
            raw_error=error_msg[:200],
            fix_template=fix[:200],
            theorem_class=theorem_class or _auto_classify(theorem_name),
            theorem_name=theorem_name,
            success_count=1 if success else 0,
            failure_count=0 if success else 1,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        try:
            with open(self._path, "a") as f:
                f.write(rec.to_jsonl() + "\n")
        except OSError as e:
            logger.warning("ErrorMemory record failed: %s", e)
            return

        logger.debug("ErrorMemory record: sig=%s cat=%s fix=%s", sig, category, fix[:60])
        self._lazy_prune()

    def hit_rate(self) -> float:
        if self._lookup_count == 0:
            return 0.0
        return self._hit_count / self._lookup_count

    def clear(self) -> None:
        """清空所有记忆（测试用）。"""
        if self._path.exists():
            self._path.unlink()
        self._hit_count = 0
        self._lookup_count = 0

    # ── 内部方法 ──────────────────────────────────────

    def _read_tail(self, n: int) -> list[str]:
        if not self._path.exists() or self._path.stat().st_size == 0:
            return []
        lines: list[str] = []
        try:
            with open(self._path, "r") as f:
                all_lines = f.readlines()
            lines = all_lines[-n:]
        except OSError:
            pass
        return [l.strip() for l in lines if l.strip()]

    def _calc_rate(self, rec: dict) -> float:
        succ = rec.get("success_count", 1)
        fail = rec.get("failure_count", 0)
        total = succ + fail
        if total == 0:
            return 0.0
        return succ / total

    def _lazy_prune(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                count = sum(1 for _ in f)
        except OSError:
            return
        if count > MAX_ENTRIES * 1.2:
            removed = self.prune()
            logger.info("ErrorMemory lazy prune: removed %d entries", removed)

    def read_all(self) -> list[ErrorRecord]:
        if not self._path.exists():
            return []
        records: list[ErrorRecord] = []
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("_type") == "record":
                            records.append(ErrorRecord.from_dict(d))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return records

    def prune(self) -> int:
        records = self.read_all()
        if len(records) <= MAX_ENTRIES:
            return 0
        best_by_sig: dict[str, ErrorRecord] = {}
        for r in records:
            existing = best_by_sig.get(r.signature)
            if existing is None or r.success_rate() > existing.success_rate():
                best_by_sig[r.signature] = r
        kept = list(best_by_sig.values())
        try:
            with open(self._path, "w") as f:
                for r in kept:
                    f.write(r.to_jsonl() + "\n")
        except OSError as e:
            logger.warning("ErrorMemory prune failed: %s", e)
            return 0
        removed = len(records) - len(kept)
        logger.info("ErrorMemory prune: %d->%d entries (removed %d)",
                     len(records), len(kept), removed)
        return removed

    def stats(self) -> dict[str, Any]:
        records = self.read_all()
        if not records:
            return {"total": 0, "categories": {}, "top_hits": [], "hit_rate": self.hit_rate()}
        cats: dict[str, int] = {}
        for r in records:
            cats[r.category] = cats.get(r.category, 0) + 1
        top = sorted(records, key=lambda r: r.success_count, reverse=True)[:5]
        return {
            "total": len(records),
            "categories": cats,
            "hit_rate": self.hit_rate(),
            "top_hits": [
                {"sig": r.signature, "cat": r.category, "ok": r.success_count,
                 "fail": r.failure_count, "fix": r.fix_template[:60]}
                for r in top
            ],
        }


def _auto_classify(theorem_name: str) -> str:
    if not theorem_name:
        return "unknown"
    name = theorem_name.lower()
    if any(k in name for k in ("algebra", "mathd_algebra", "sqineq", "amgm")):
        return "algebra"
    if any(k in name for k in ("number", "dvd", "prime", "mod", "int", "nat")):
        return "number_theory"
    if any(k in name for k in ("combin", "catalan", "choose", "perm")):
        return "combinatorics"
    if any(k in name for k in ("geometry", "angle", "triangle", "circle")):
        return "geometry"
    if any(k in name for k in ("calc", "ineq", "sum", "prod")):
        return "calculus"
    if any(k in name for k in ("ime", "imo", "aime", "amc")):
        return "contest"
    return "other"


if __name__ == "__main__":
    mem = ProofErrorMemory(cache_dir="/tmp/test_em")
    mem.clear()

    mem.record("type mismatch: expected Nat, got Int at line 42",
               "TYPE_MISMATCH", "use `natCast` to convert Int to Nat",
               theorem_name="mathd_algebra_33")
    mem.record("unknown identifier 'foo_bar' at line 42",
               "UNKNOWN_IDENT", "check spelling or open the import",
               theorem_name="aime_1983_p1")

    # Test 1: fuzzy match (line 99 vs line 42)
    h1 = mem.lookup("type mismatch: expected Nat, got Int at line 99", "TYPE_MISMATCH")
    print(f"MATCH:   {h1 is not None} -> {h1}")

    # Test 2: category isolation
    h2 = mem.lookup("type mismatch: expected Nat, got Int at line 99", "SYNTAX_ERROR")
    print(f"ISOLATE: {h2 is None}")

    # Test 3: persistence
    mem2 = ProofErrorMemory(cache_dir="/tmp/test_em")
    h3 = mem2.lookup("type mismatch: expected Nat, got Int at line 42", "TYPE_MISMATCH")
    print(f"PERSIST: {h3 is not None} -> {h3}")

    # Test 4: stats
    import json
    print(f"STATS:   {json.dumps(mem.stats(), indent=2)}")
