"""ThreeLayerClassifier 的 ``source`` 契约与 ``stats`` 闭合 —— 审计页 §9.3 回归。

修复前实测: 兜底路径 (``category=OTHER``, ``confidence=0.0``) 把 ``source`` 标成
``"layer2"``, 但 Layer 2 恰恰是因为置信度不足而**拒绝**了这条输入 —— 即
**「无层决策」被记为「L2 决策」**，溯源字段说谎。空输入路径还把自己标成 ``"layer1"``。

同时该兜底路径不递增任何层计数，于是 ``stats`` 的 ``total`` 与各层计数**不闭合**：
实测 12 条样本里 2 条凭空从分层统计里消失（``{'layer1': 10, 'layer2': 0,
'layer3': 0, 'total': 12}``）。

判据 1（0-token 优先律）要防的正是「机械层给不出确定性答案、记录却说某层给了」。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.classifier.vote import (  # noqa: E402
    SOURCE_NONE,
    ErrorContext,
    ThreeLayerClassifier,
)

#: 让 L1/L2 都不可能达标 ⇒ 必定走兜底路径（阈值 >1.0 是确定性复现手段）。
NEVER = 1.1


def _clf(**kw) -> ThreeLayerClassifier:
    """0-token 分类器：关掉 LLM judge（不联网、不花 token）。"""
    kw.setdefault("enable_llm_judge", False)
    return ThreeLayerClassifier(**kw)


class TestSourceContract:
    def test_empty_input_is_none(self):
        r = _clf().classify(ErrorContext(error_msg=""))
        assert r.source == SOURCE_NONE, "空输入没有任何层决策，不得冒认 layer1"
        assert r.category == "OTHER"
        assert r.confidence == 0.0

    def test_whitespace_input_is_none(self):
        assert _clf().classify(ErrorContext(error_msg="   \n")).source == SOURCE_NONE

    def test_forced_fallthrough_is_none_not_layer2(self):
        clf = _clf(l1_threshold=NEVER, l2_threshold=NEVER)
        r = clf.classify(ErrorContext(error_msg="qwerty asdf zxcv"))
        assert r.source == SOURCE_NONE, "Layer 2 拒绝了它，就不该记成 layer2 决策"
        assert r.category == "OTHER"
        assert r.confidence == 0.0

    def test_layer1_decision_still_labeled_layer1(self):
        """修契约不能把真正的 L1 决策也改掉。"""
        r = _clf().classify(ErrorContext(error_msg="unknown identifier 'foo'"))
        assert r.source == "layer1"
        assert r.confidence > 0.0


class TestStatsClosure:
    def test_fallthrough_is_counted(self):
        clf = _clf(l1_threshold=NEVER, l2_threshold=NEVER)
        for _ in range(3):
            clf.classify(ErrorContext(error_msg="qwerty asdf zxcv"))
        s = clf.stats
        assert s["total"] == 3
        assert s["unclassified"] == 3
        assert s["layer1"] == s["layer2"] == s["layer3"] == 0

    def test_empty_input_is_counted(self):
        clf = _clf()
        clf.classify(ErrorContext(error_msg=""))
        assert clf.stats["unclassified"] == 1

    def test_total_equals_sum_of_layers(self):
        """核心不变量: total == 各层 + unclassified（修复前会漏 2 条）。"""
        clf = _clf()
        clf.classify(ErrorContext(error_msg=""))                       # 空输入 ⇒ none
        clf.classify(ErrorContext(error_msg="unknown identifier 'foo'"))  # ⇒ layer1
        clf.classify(ErrorContext(error_msg="qwerty asdf zxcv"))        # ⇒ l1/l2/none
        s = clf.stats
        assert s["total"] == 3
        assert s["total"] == (
            s["layer1"] + s["layer2"] + s["layer3"] + s["unclassified"]
        ), f"计数不闭合: {s}"

    def test_stats_exposes_unclassified_key(self):
        assert "unclassified" in _clf().stats
