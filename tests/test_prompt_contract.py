# -*- coding: utf-8 -*-
"""提示词契约的回归：代码算好的排序 / 吃法 / 猜牌字段，不能被改回「让模型自己拍脑袋」。

为什么会有这个文件
------------------
用素材重写提示词之后，实测模型两次犯同一类错：
  1. 出牌在 7 和 106 之间来回摇摆（同档位没有定序规则）
  2. 说「吃 3 拆不成句」来给 pass 找理由，而手里 1、2 明明能成 一二三

这两件事都是**可枚举的事实**，不该交给模型自由心证。现在由 `_hand_analysis()`
算好 `putOrder` / `eatOptions`，模型只负责在 blacklist 之外挑第一个。
这个文件锁住这两条产出，防止以后又把排序退回提示词里。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pct import ai  # noqa: E402
from pct.tracker import Tracker  # noqa: E402


def _live(hand, last_put=9, opp_put=None, **kw):
    d = {
        "me": 1, "uids": [1, 2], "tState": 3, "curPlayer": 0, "zhuang": 1,
        "cardNext": 42, "choupai": 20, "lastPut": last_put, "lastPutPlayer": 1,
        "lastDrawPlayer": 0, "tableid": "t", "playNum": 1,
        "huziType": 2, "limitScore": 15,
        "seats": [
            {"seat": 0, "uid": 1, "handKnown": True, "hand": hand, "put": []},
            {"seat": 1, "uid": 2, "handKnown": False, "hand": [], "put": opp_put or []},
        ],
    }
    d.update(kw)
    return d


def _analysis(hand, **kw):
    t = Tracker()
    t.feed(_live(hand, **kw), [])
    return t._hand_analysis()


# ------------------------------------------------------------------ 出牌排序
def test_put_order_excludes_runs_and_pairs():
    """成句里的牌、对子里的牌，都不能进 putOrder。"""
    a = _analysis([1, 2, 3, 5, 5, 7, 101, 102, 103, 106, 108, 109, 110])
    order = a["putOrder"]
    for c in (1, 2, 3, 101, 102, 103, 5):
        assert c not in order, (c, order)


def test_put_order_prefers_seen_more():
    """a 档：已现 2~3 张的先打。

    注意 seen 含自己手牌：只拿一个 7 时 seen[7]=1，和 106 同级，这时边张 110 反而先走。
    要让 7 进 a 档，必须「桌面已经出现过一张 7」—— 也就是对手打过。
    """
    a = _analysis([7, 106, 108, 109, 110], opp_put=[7])
    assert a["seen"][7] == 2, a["seen"]
    assert a["putOrder"][0] == 7, a["putOrder"]


def test_put_order_edge_before_middle():
    """b 档：同为已现 0~1 张时，边张（110）先于中张（106）。"""
    a = _analysis([106, 108, 110])
    assert a["putOrder"][0] == 110, a["putOrder"]


def test_put_order_small_before_big():
    """c 档：同为边张时小字优先（10 先于 110）。"""
    a = _analysis([10, 110])
    assert a["putOrder"][0] == 10, a["putOrder"]


def test_put_order_is_deterministic():
    """同一手牌连算 5 次，结果必须完全一致（否则 AI 会跟着飘）。"""
    hand = [1, 2, 3, 5, 5, 7, 101, 102, 103, 106, 108, 109, 110]
    first = _analysis(hand)["putOrder"]
    for _ in range(4):
        assert _analysis(hand)["putOrder"] == first


# ------------------------------------------------------------------ 合法吃法
def test_eat_options_finds_run():
    """手里 1、2，lastPut=3 → 一定有一二三 这手吃。"""
    a = _analysis([1, 2, 4, 5, 6, 7], last_put=3)
    assert [3, 1, 2] in a["eatOptions"] or [3, 2, 1] in a["eatOptions"], a["eatOptions"]


def test_eat_options_empty_when_really_cannot():
    """手里没有能配的牌，eatOptions 必须是空 —— 这时 pass 才是对的。"""
    a = _analysis([5, 6, 101, 102, 103, 106], last_put=3)
    assert a["eatOptions"] == [], a["eatOptions"]


def test_eat_options_finds_jiao():
    """绞：手里两张大字同值，小字那一张打过来也能成绞。"""
    a = _analysis([102, 102, 5, 6, 7, 8], last_put=2)
    assert [2, 102, 102] in a["eatOptions"], a["eatOptions"]


# -------------------------------------------------------------------- 猜牌
def test_read_accepts_hypothesis():
    r = ai._clean_read({"plan": "对手连打小字，可能做大字", "conf": "中", "cards": [104, 105]})
    assert r == {"plan": "对手连打小字，可能做大字", "conf": "中", "cards": [104, 105]}


def test_read_rejects_assertion():
    """「他手里有 X」是编造 —— 服务端根本不下发对手手牌。"""
    assert ai._clean_read({"plan": "对手手里有一对九", "conf": "高"}) is None
    assert ai._clean_read({"plan": "他一定有拾", "conf": "高"}) is None


def test_read_conf_defaults_low():
    assert ai._clean_read({"plan": "对手在做红胡"})["conf"] == "低"


def test_read_rejects_bad_shape():
    assert ai._clean_read(None) is None
    assert ai._clean_read({"plan": ""}) is None
    assert ai._clean_read({"plan": "x" * 40}) is None


# ---------------------------------------------------------------- 数字转汉字
def test_num_cn_reads_chinese():
    """数量按中文读法：十五 / 二十 / 六十 / 一百零五。"""
    from pct.cn import num_cn
    assert [num_cn(n) for n in (0, 7, 10, 11, 15, 20, 60, 105)] == [
        "零", "七", "十", "十一", "十五", "二十", "六十", "一百零五"]


def test_card_values_use_big_chinese():
    """101~110 是「大壹~大拾」这一张牌，不是数量一百零五。"""
    from pct.cn import one
    assert [one(v) for v in (1, 10, 101, 105, 110)] == ["一", "十", "壹", "伍", "拾"]


def test_to_cn_text_mixed():
    from pct.cn import to_cn_text
    assert to_cn_text("吃3成句，还差6方") == "吃三成句，还差六方"
    assert to_cn_text("对手打9/105") == "对手打九/伍"
    assert to_cn_text("墩剩15张") == "墩剩十五张"


def test_digit_by_digit_reading_collapses():
    """模型把 101 逐位念成「壹零壹」时，收敛回单字「壹」。"""
    from pct.cn import to_cn_text
    assert to_cn_text("不拆壹零壹门子") == "不拆壹门子"
    assert to_cn_text("不拆一一零门子") == "不拆拾门子"


def test_reason_and_read_have_no_arabic_digits():
    """模型写阿拉伯数字时，validate 兜底转成汉字再吐给面板。"""
    st = {"mode": "put", "tState": 4, "mySeat": 1, "curPlayer": 1,
          "me": {"hand": [1, 2, 2, 106, 7, 7, 7]}}
    out = ai.validate(st, {"action": "put", "card": 106,
                           "reason": "留三张，还差6方",
                           "read": {"plan": "对手打9/105", "conf": "低"}})
    assert out["action"] == "put", out
    assert "6" not in out["reason"] and "六" in out["reason"], out["reason"]
    assert out["read"]["plan"] == "对手打九/伍", out["read"]


# ---------------------------------------------------------------- 多张推荐
def _put_state(hand):
    return {"mode": "put", "tState": 4, "mySeat": 1, "curPlayer": 1,
            "me": {"hand": hand}}


def test_multi_cards_pass_through():
    """多张候选：首张等于 card 时原样带出。"""
    st = _put_state([1, 2, 2, 106, 7, 7, 7])
    out = ai.validate(st, {"action": "put", "card": 106, "reason": "留对子",
                           "cards": [106, 1, 2]})
    assert out["cards"] == [106, 1, 2], out


def test_multi_cards_dropped_when_first_differs():
    """首张和 card 不一致 = 模型自相矛盾，候选整块丢掉，只留首选。"""
    st = _put_state([1, 2, 2, 106, 7, 7, 7])
    out = ai.validate(st, {"action": "put", "card": 106, "reason": "留对子",
                           "cards": [1, 2]})
    assert out["action"] == "put" and "cards" not in out, out


def test_multi_cards_sanitized():
    """候选里不合法的条目要滤掉（不在手里 / 3 张同值 / 重复 / 超 4 张）。"""
    st = _put_state([1, 2, 2, 106, 7, 7, 7])
    out = ai.validate(st, {"action": "put", "card": 106, "reason": "留对子",
                           "cards": [106, 106, 999, 7, 1, 2, 2]})
    assert out["cards"] == [106, 1, 2], out


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print("  ✓ %s" % name)
        except Exception as e:
            bad += 1
            print("  ✗ %s: %s" % (name, e))
    print("\n通过 %d / 失败 %d" % (len(tests) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
