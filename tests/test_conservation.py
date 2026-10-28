# -*- coding: utf-8 -*-
"""总牌守恒的回归：60 张必须对得上，对不上要能说清是哪一类的账错了。

核心那条（第 10 局实测）
------------------------
`lastPut=105`、`lastPutPlayer=1`、`winner=0`。
105 既在对手弃牌堆 `[9,101,105]` 里，又在赢家手牌里 —— 放炮胡那张换了主人，
但弃牌堆不会把它移除，于是被数了两遍，凭空多 1 张（61）。

这条不是新发现，README 的口径速查里早就记了
「弃牌堆要剪掉 lastPut 出现在非 lastPutPlayer 堆里那张（已易主）」，
但一直没落到代码里。现在落到 `Tracker.conservation()`。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pct.tracker import Tracker  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


def _live(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if o.get("e") == "_live":
                return o["d"]
    raise AssertionError("%s 里没有 _live" % name)


def _state(name):
    """直接读一份 `pct_state.json` 快照（不是事件流）。"""
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        st = json.load(f)
    st.pop("ev", None)          # 事件队列不参与守恒
    return st


def _mk(seats, **kw):
    """捏一份最小 live 快照。seats 里只给需要的键。"""
    d = {"tState": 6, "choupai": 20, "cardNext": 40, "winner": -1,
         "uids": [1, 2], "me": 1, "seats": seats, "lastPut": -1}
    d.update(kw)
    return d


# --------------------------------------------------------------- 实测两局
def test_round10_clips_discard_win():
    """第 10 局：放炮胡，105 在对手 put 和赢家手牌里各出现一次。"""
    t = Tracker()
    t.feed(_state("live_round10.json"), [])
    c = t.conservation()
    assert c["pool"] == 60, c["pool"]
    assert c["clipped"] == [(105, 1)], c["clipped"]
    assert c["diff"] == 0, c
    assert c["ok"] is True, c


def test_multi4_clips_discard_win():
    """multi4 的结算局：109 同理，剪掉后 60。"""
    t = Tracker()
    t.feed(_live("round_multi4.jsonl"), [])
    c = t.conservation()
    assert c["clipped"] == [(109, 0)], c["clipped"]
    assert c["diff"] == 0, c
    assert c["ok"] is True, c


def test_huang_zhuang_balances_without_clip():
    """荒庄局：winner=-1、墩子抽干(cardNext=60)、双方各留 7 张，一次都不用剪。

    这同时把「吃 / 碰 会不会也重复计数」钉死了 —— 本局座 0 有三组吃、座 1 有 14 张明牌。
    如果吃掉的牌还留在出牌方弃牌堆里，这里必然多 3 张以上；实测差 0，
    说明吃/碰时会从对方弃牌堆移除，**只有胡牌那张不会**。
    """
    t = Tracker()
    t.feed(_state("live_round2.json"), [])
    c = t.conservation()
    assert c["clipped"] == [], c["clipped"]
    assert c["diff"] == 0, c
    assert c["ok"] is True, c


# ------------------------------------------------------------------ 边界
def test_self_draw_does_not_clip():
    """自摸：lastPut 是赢家自己摸的，不进任何人弃牌堆 → 不该剪。

    剪错方向比不剪更糟：会把本来对得上的账剪成 -1。
    """
    seats = [
        {"seat": 0, "uid": 1, "hand": [1, 2, 3], "put": [9], "handKnown": True},
        {"seat": 1, "uid": 2, "hand": [5, 6, 7], "put": [8], "handKnown": True},
    ]
    # 池 60，墩剩 60-40=20；两家合起来只有 3+1+3+1=8，凑不满，只看「剪没剪」
    t = Tracker()
    t.feed(_mk(seats, lastPut=5, winner=1, cardNext=40), [])
    c = t.conservation()
    assert c["clipped"] == [], c["clipped"]


def test_hand_empty_but_known_is_not_unknown():
    """胡完手牌全部打进明牌时 hand=[]，但 handKnown=True —— 这是「真的 0 张」。"""
    seats = [
        {"seat": 0, "uid": 1, "hand": [], "put": [], "handKnown": True},
        {"seat": 1, "uid": 2, "hand": [1, 2], "put": [], "handKnown": True},
    ]
    t = Tracker()
    t.feed(_mk(seats), [])
    c = t.conservation()
    assert c["unknown"] == [], c["unknown"]


def test_opponent_hand_missing_is_unknown_not_zero():
    """局中对手 hand 不下发。记成「未知」，不能当 0 张去凑守恒。"""
    seats = [
        {"seat": 0, "uid": 1, "hand": [1, 2, 3], "put": [], "handKnown": True},
        {"seat": 1, "uid": 2, "hand": [], "put": [], "handKnown": False},
    ]
    t = Tracker()
    t.feed(_mk(seats, tState=3), [])
    c = t.conservation()
    assert [u["seat"] for u in c["unknown"]] == [1], c["unknown"]
    assert c["ok"] is False


def test_meld_units():
    """偎/提/跑 一个元素 = 一组，不是一张。吃按 3 张一组。

    把 peng1=[109,107] 当 2 张算会少 4 张 —— 这条错过的账看起来像「采集漏了」。
    """
    seats = [
        {"seat": 0, "uid": 1, "hand": [], "put": [], "handKnown": True,
         "peng": [4], "peng1": [109, 107], "gang0": [6], "gang1": [7],
         "chi": [1, 2, 3, 4, 5, 6], "lie": [9]},
        {"seat": 1, "uid": 2, "hand": [], "put": [], "handKnown": True},
    ]
    t = Tracker()
    t.feed(_mk(seats), [])
    c = t.conservation()
    # 碰 3 + 偎 3*2 + 提 4 + 跑 4 + 吃 3*2 + 摆 1 = 24
    assert c["seats"][0]["melds"] == 24, c["seats"][0]["melds"]


def test_candidates_are_reported_not_clipped():
    """吃/碰 拿走的牌只报候选，不参与对账 —— 同值牌有 4 张，光看值分不清是不是同一张。"""
    seats = [
        {"seat": 0, "uid": 1, "hand": [1], "put": [10], "handKnown": True},
        {"seat": 1, "uid": 2, "hand": [2], "put": [], "handKnown": True,
         "chi": [10, 102, 103]},
    ]
    t = Tracker()
    t.feed(_mk(seats), [])
    c = t.conservation()
    assert c["clipped"] == [], c["clipped"]
    assert any(x["card"] == 10 and x["kind"] == "chi" for x in c["candidates"]), c["candidates"]


def test_cardnext_over_pool_clamps():
    """旧房间 cardNext 可能超过池，墩剩要夹到 0，不能出负数。"""
    t = Tracker()
    t.feed(_mk([], cardNext=99), [])
    assert t.deck_left() == 0


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
