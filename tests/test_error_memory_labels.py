"""ProofErrorMemory 标签语义 (success / failure) —— 判据 4「离线固化律」门禁。

回归背景 (2026-09-10, WSL 回源审计 [[zero-token-diagnosis-layer-audit-2026]]):

    修复前 `record()` 接受 `success: bool = True` 但**从不使用该参数**, 于是
    `ErrorRecord.success_count / failure_count` 恒为 `1 / 0`, 后果三连:

      (a) `success_rate()` 恒为 1.0 ⇒ `prune()` 的"保留每 signature 最高
          success_rate"退化为"保留最早一条"; `lookup()` 的 `rate > best[0]`
          在所有候选间恒为平局 ⇒ **最早记录的候选胜出**。
      (b) `inner.py` 的"首次出现"分支把**刚刚编译失败的 code** 当作 fix 记录,
          之后又被 `lookup()` 以 `"Proven fix: ..."` 回注进 LLM 提示
          ⇒ **自污染回路**: 提示词宣称"已验证的修复", 实际是必然失败的代码。
      (c) 轨迹只有正例、没有负例 ⇒ 判据 4「轨迹 schema 须自带诊断标签
          (否则离线学的是噪声)」被违反 —— 任何在 `success_rate` 上训练的
          离线模型学到的都是常数。

    修复 = 让 `record()` 尊重 `success`, 并在"首次出现"分支显式传
    `success=False`。负例的 `_calc_rate` 为 0.0, 不会胜过 `lookup()` 的
    初始哨兵 `best[0] == 0.0`, 因此**无需改 lookup() 即天然被过滤**,
    同时负例照常落盘供离线训练 —— 两个目标同时达成。
"""
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.loop.error_memory import ProofErrorMemory  # noqa: E402

ERR = "type mismatch: expected Real, got Int"
FIX_OK = "insert natCast to convert the Int literal"      # >= MIN_FIX_LENGTH (20)
FIX_BAD = "the attempted code that just failed to compile"  # >= 20, 但是负例


@pytest.fixture
def mem():
    d = tempfile.mkdtemp()
    m = ProofErrorMemory(cache_dir=d)
    m.clear()
    yield m
    shutil.rmtree(d, ignore_errors=True)


# ══════════════════════════════════════════════════════════
# 1. 标签语义: success 参数须真正生效
# ══════════════════════════════════════════════════════════

class TestLabelSemantics:
    def test_default_is_verified_fix(self, mem):
        mem.record(ERR, "TYPE_MISMATCH", FIX_OK, theorem_name="t")
        rec = mem.read_all()[0]
        assert rec.success_count == 1
        assert rec.failure_count == 0
        assert rec.success_rate() == 1.0

    def test_failure_sample_labeled(self, mem):
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t", success=False)
        rec = mem.read_all()[0]
        assert rec.success_count == 0
        assert rec.failure_count == 1
        assert rec.success_rate() == 0.0

    def test_success_rate_has_variance(self, mem):
        """判据 4 的可跑断言: 标签必须有信息量 (不能恒为 1.0)。"""
        mem.record(ERR, "TYPE_MISMATCH", FIX_OK, theorem_name="t")
        mem.record("unsolved goals: ⊢ P", "UNSOLVED_GOAL", FIX_BAD,
                   theorem_name="t2", success=False)
        rates = {r.success_rate() for r in mem.read_all()}
        assert rates == {0.0, 1.0}, f"标签退化为常数, 离线只能学噪声: {rates}"


# ══════════════════════════════════════════════════════════
# 2. lookup 不得把失败样本当 "Proven fix" 回注
# ══════════════════════════════════════════════════════════

class TestLookupFiltersNegativeSamples:
    def test_pure_failure_not_returned(self, mem):
        """只有失败样本时, lookup 必须落空 (否则就是自污染回路)。"""
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t", success=False)
        assert mem.lookup(ERR, "TYPE_MISMATCH") is None

    def test_verified_fix_beats_earlier_failed_attempt(self, mem):
        """自污染回归: 失败尝试先落盘, 之后才记录成功修复 —— 必须返回成功那个。"""
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t", success=False)
        mem.record(ERR, "TYPE_MISMATCH", FIX_OK, theorem_name="t")
        got = mem.lookup(ERR, "TYPE_MISMATCH")
        assert got is not None
        assert "natCast" in got, f"返回了失败样本当修复: {got!r}"

    def test_verified_fix_beats_later_failed_attempt(self, mem):
        """顺序颠倒也必须返回成功那个 (胜出靠 rate, 不靠落盘顺序)。"""
        mem.record(ERR, "TYPE_MISMATCH", FIX_OK, theorem_name="t")
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t2", success=False)
        got = mem.lookup(ERR, "TYPE_MISMATCH")
        assert got is not None
        assert "natCast" in got, f"返回了失败样本当修复: {got!r}"


# ══════════════════════════════════════════════════════════
# 3. 标签须持久化 (JSONL 往返不丢)
# ══════════════════════════════════════════════════════════

class TestPersistence:
    def test_label_survives_roundtrip(self, mem):
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t", success=False)
        mem2 = ProofErrorMemory(cache_dir=str(mem.cache_dir))
        rec = mem2.read_all()[0]
        assert rec.failure_count == 1
        assert rec.success_count == 0

    def test_hit_rate_unchanged_by_negative_sample(self, mem):
        """负例既不该被返回, 也不该计入 hit。"""
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t", success=False)
        assert mem.lookup(ERR, "TYPE_MISMATCH") is None
        assert mem.hit_rate() == 0.0


# ══════════════════════════════════════════════════════════
# 4. prune 须保留已验证修复而非失败尝试
# ══════════════════════════════════════════════════════════

class TestPruneKeepsVerified:
    def test_prune_prefers_verified_over_failed(self, mem, monkeypatch):
        # prune() 在 len(records) <= MAX_ENTRIES 时直接 return 0 —— 收窄阈值
        # 才能走到真实的裁剪判定路径 (否则本测试测的是空操作)。
        monkeypatch.setattr("omega.loop.error_memory.MAX_ENTRIES", 0)
        mem.record(ERR, "TYPE_MISMATCH", FIX_BAD, theorem_name="t", success=False)
        mem.record(ERR, "TYPE_MISMATCH", FIX_OK, theorem_name="t")
        mem.prune()
        recs = mem.read_all()
        assert len(recs) == 1, "同 signature 应被折叠成一条"
        assert recs[0].success_count == 1
        assert recs[0].failure_count == 0
