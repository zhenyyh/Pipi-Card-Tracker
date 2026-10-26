# -*- coding: utf-8 -*-
"""回归测试：python -m pytest tests -q   或   python tests/test_rules.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pct import replay, rules  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def test_card_names():
    assert rules.cname(1) == "一"
    assert rules.cname(10) == "十"
    assert rules.cname(101) == "壹"
    assert rules.cname(106) == "陸"          # 大写六是繁体
    assert rules.cname(110) == "拾"


def test_red_cards():
    # isRed() 实测：二七十，大小写都红，6 种 × 4 = 24
    reds = [c for c in range(1, 111) if 1 <= c % 100 <= 10 and rules.is_red(c)]
    assert sorted(reds) == [2, 7, 10, 102, 107, 110]
    assert reds.__len__() * 4 == 24


def test_classify_shapes():
    assert rules.classify([5] * 4, "self")[0] == "提"
    assert rules.classify([5] * 4, "other")[0] == "跑"
    assert rules.classify([5] * 3, "self")[0] == "偎"
    assert rules.classify([5] * 3, "other")[0] == "碰"
    assert rules.classify([103, 104, 105], "other")[0] == "吃"
    # 绞牌：一对 + 一张同点异大小写（不是「搭」，邵阳规则叫绞牌）
    assert rules.classify([105, 105, 5], "self")[0] == "绞"
    assert rules.classify([2, 102, 2], "other")[0] == "绞"


def test_seq_case_matters():
    """特殊顺的胡息分大小写：小 3 / 大 6。以前一张表不分，全给 6 是错的。"""
    assert rules.seq_xi((1, 2, 3), 0) == 3        # 一二三 小
    assert rules.seq_xi((1, 2, 3), 1) == 6        # 壹贰叁 大
    assert rules.seq_xi((2, 7, 10), 0) == 3       # 二七十 小
    assert rules.seq_xi((2, 7, 10), 1) == 6       # 贰柒拾 大
    assert rules.seq_xi((5, 6, 7), 0) == 0        # 普通顺 0
    # classify 也必须认 2-7-10（跨度 8，不是连号）
    n, x, _ = rules.classify([2, 7, 10], "other")
    assert (n, x) == ("吃", 3)
    assert rules.classify([102, 107, 110], "other")[1] == 6


def test_special_seq():
    # 壹贰叁（大字）
    name, xi, _ = rules.classify([101, 102, 103], "other")
    assert name == "吃" and xi == 6


def test_xixi_table():
    assert rules.XIXI[("提", 1)] == 12 and rules.XIXI[("提", 0)] == 9
    assert rules.XIXI[("跑", 1)] == 9 and rules.XIXI[("跑", 0)] == 6
    assert rules.XIXI[("偎", 1)] == 6 and rules.XIXI[("偎", 0)] == 3
    assert rules.XIXI[("碰", 1)] == 3 and rules.XIXI[("碰", 0)] == 1


def test_round_v3_conservation():
    """第 5 局：48 张在场 + 墩剩 12 = 60 = 80 - choupai(20)。"""
    ev, live = replay.load(os.path.join(FIX, "round_v3.jsonl"))
    r = replay.build(ev, live)
    a = r["audit"]
    assert a["hand_known"] and a["ok"], a
    assert a["total"] + a["rest"] == 60


def test_round_v3_melds():
    """偎和跑必须都在 —— 这是上一版丢 3 张的根因。"""
    ev, live = replay.load(os.path.join(FIX, "round_v3.jsonl"))
    r = replay.build(ev, live)
    kinds = {m["kind"] for m in r["melds"] if m["seat"] == 1}
    assert "跑" in kinds, kinds
    assert "偎" in kinds, kinds
    # 座位1 明牌合计 13 张（跑4 + 吃3 + 吃3 + 偎3）
    assert sum(len(m["cards"]) for m in r["melds"] if m["seat"] == 1) == 13


def test_round_v3_xi():
    """明牌 18 胡 + 手牌 0 胡 = 18，与游戏自己的 origWinNum=18 一致。

    ★ 以前只算明牌那一半，手牌有坎/顺子的局必然对不上（round2 明牌 11 而
    origWinNum=20）。完整公式是「明牌息 + 手牌门子息」。
    """
    ev, live = replay.load(os.path.join(FIX, "round_v3.jsonl"))
    r = replay.build(ev, live)
    assert r["xi_meld"][1] == 18, r["xi_meld"]
    assert r["xi_hand"][1]["total"] == 0
    assert r["xi_total"][1] == r["end"]["origWinNum"] == 18


def test_split_hand():
    """坎（起手 3 张）只能从手牌算 —— 服务端不发事件、不进任何数组。"""
    # 坎 + 顺 + 将：叁叁叁 是一坎，肆伍陸 一句，二二 是将
    s = rules.split_hand([3, 3, 3, 4, 5, 6, 2, 2])
    assert [c for c, _x in s["kan"]] == [3], s
    assert len(s["seq"]) == 1 and sorted(s["seq"][0][0]) == [4, 5, 6], s
    assert s["pair"] == [[2, 2]]
    assert s["rest"] == []

    # 特殊顺 贰柒拾 是 6 胡
    s2 = rules.split_hand([102, 107, 110])
    assert s2["seq"] and s2["seq"][0][1] == 6, s2

    # 将本身不产生胡息
    assert rules.hand_xi([2, 2])["total"] == 0

    # 坎按开关计息：邵阳表里没有坎，长沙/二人表里有 大6 小3
    assert rules.hand_xi([103, 103, 103], count_kan=False)["kan_xi"] == 0
    assert rules.hand_xi([103, 103, 103], count_kan=True)["kan_xi"] == 6
    assert rules.hand_xi([3, 3, 3], count_kan=True)["kan_xi"] == 3


def test_lie_is_counted():
    """摆火（mjlie）单张、不计息，但必须进守恒 —— 漏了守恒就缺牌。"""
    m = rules.meld_from_array("lie", [105])
    assert m["kind"] == "摆" and m["xi"] == 0 and m["cards"] == [105]


def test_conservation_formula():
    """墩剩 = (80 - choupai) - cardNext。两局实证。

    choupai 的官方口径：二人精简「随机抽掉牌池里 20 张」（help.txt 第 27 条），
    所以本局牌池 = 80 - 20 = 60，cardNext 是池内已发到的游标。
    """
    ev, live = replay.load(os.path.join(FIX, "round_v3.jsonl"))
    r = replay.build(ev, live)
    assert r["choupai"] == 20 and r["pool"] == 60
    assert r["pool"] - r["card_next"] == r["audit"]["rest"] == 12


def test_round_kan_xi():
    """★ 有坎的局：坎计息，实测钉死。

    座位1 手牌两坎（坎拾 大字 / 坎一 小字），明牌 碰捌3 + 二七十3 + 六七八0，
    手牌 坎拾6 + 坎一3 + 壹贰叁6 + 三四五0 = 15。合计 21 == origWinNum。
    坎按 0 计只有 9~12，怎么都对不上 —— 所以本玩法计坎息，大 6 小 3。
    """
    ev, live = replay.load(os.path.join(FIX, "round_kan.jsonl"))
    r = replay.build(ev, live)
    hx = r["xi_hand"][1]
    assert [x for _c, x in hx["detail"]["kan"]] == [3, 6], hx["detail"]["kan"]
    assert hx["total"] == 15, hx
    assert r["xi_meld"][1] == 6
    assert r["xi_total"][1] == r["end"]["origWinNum"] == 21
    # 明牌 3 组 + 手牌 4 方 = 7 方门子，正好够胡
    assert len([m for m in r["melds"] if m["seat"] == 1]) == 3
    assert hx["menzi"] == 4


def test_round_kan_melds():
    """live 与事件两个来源必须一致（这局 live 是结算后的快照，
    以前被 playNum 校验误杀，导致明牌只能靠事件凑）。"""
    ev, live = replay.load(os.path.join(FIX, "round_kan.jsonl"))
    r = replay.build(ev, live)
    kinds = sorted(m["kind"] for m in r["melds"])
    assert kinds == ["偎", "吃", "吃", "碰", "碰", "绞"], kinds
    # playType 是**整数** 111000110（bitmask），不是字符串 —— 别拿 '%s' 的直觉比
    assert r.get("choupai") == 20 and r.get("play_type") == 111000110, (
        "choupai=%r play_type=%r" % (r.get("choupai"), r.get("play_type")))


def test_hand_kind_depends_on_origin():
    """★ 手上的 3 张同值：自己摸齐的是「坎」（大6/小3），别人打来的第 3 张是「碰」（大3/小1）。

    help.txt 第 4 条：「碰牌：当别人打或摸的牌自己手中有一对，则可以碰牌」。
    胡牌瞬间的 mjhand 里含刚胡进来的那张，接炮时它和手上两张凑成的只能是碰。
    """
    hand = [109, 109, 109, 101, 101, 101, 7, 8, 9]
    # 不指定来源 ⇒ 109 也按坎算（6），合计 12
    assert rules.hand_xi(hand)["total"] == 12
    # 109 是别人打的 ⇒ 降级为碰（大 3），合计 9
    hx = rules.hand_xi(hand, foreign={109})
    assert hx["total"] == 9, hx
    assert [x for _c, x in hx["detail"]["peng"]] == [3]
    assert [x for _c, x in hx["detail"]["kan"]] == [6]
    # 门子数不变（碰也是 1 方）
    assert hx["menzi"] == rules.hand_xi(hand)["menzi"] == 3
    # 小字碰只值 1
    assert rules.hand_xi([5, 5, 5], foreign={5})["total"] == 1


def test_round4_xi():
    """★ 接炮的那局：18 vs 15 差 3 的谜底。

    手牌 9 张 [109×3, 101×3, 7,8,9]，明牌 碰伍3 + 吃三四五0 + 吃一二三3 + 吃六七八0 = 6。
    lastPut=109 且 lastDrawPlayer≠winner（接炮）⇒ 109 那路按**碰**算 3，不是坎 6：
        6(明) + 6(坎壹) + 0(顺789) + 3(碰玖) = 15 == origWinNum
    按坎算会得 18，差 3 —— 之前一直以为是「小字一二三不计息」，被 round_v3
    （明牌吃大字壹贰叁计 6、orig 18 精确对上）证伪了。
    """
    ev, live = replay.load(os.path.join(FIX, "round_multi4.jsonl"))
    r = replay.build(ev, live, which=4)
    e = r["end"]
    assert e["winner"] == 1 and e["self_draw"] is False, e
    assert e["last_put"] == 109
    assert r["foreign"] == [109]
    assert r["xi_meld"][1] == 6
    assert r["xi_hand"][1]["total"] == 9
    assert r["xi_total"][1] == e["origWinNum"] == 15


def test_round4_conservation():
    """★ 守恒多 1 张的真因：`lastPut` 那张「已被拿走」的牌还留在别人的弃牌堆里。

    第 4 局座0 弃牌堆末尾躺着 109，而 lastPut=109/lastPutPlayer=1（座1 胡走的）。
    服务端快照拍在移除之前 ⇒ 剪掉后 53 + 7 = 60 ✓。
    以前 drop_pending 只剪出牌时序、不剪弃牌堆，于是稳定 61/60。
    """
    ev, live = replay.load(os.path.join(FIX, "round_multi4.jsonl"))
    r = replay.build(ev, live, which=4)
    a = r["audit"]
    assert a["ok"], a
    assert a["total"] + a["rest"] == a["pool"] == 60
    assert 109 not in (r["put"].get(0) or []), r["put"]


def test_round4_all_rounds_xi():
    """四局里两个胡牌局都要对平：第 1 局 21/21（自摸，同 round_kan）、第 4 局 15/15。"""
    ev, live = replay.load(os.path.join(FIX, "round_multi4.jsonl"))
    got = {}
    for i in (1, 4):
        r = replay.build(ev, live, which=i)
        w = r["end"]["winner"]
        got[i] = (r["xi_total"][w], r["end"]["origWinNum"])
    assert got == {1: (21, 21), 4: (15, 15)}, got


def test_fan_win_rules():
    """名堂倍率（娄底放炮罚，官方线上规则页）。

    ★ 是**乘算**，不是加法 —— 邵阳剥皮才是「自摸 +10 胡」。
    实测 round_v3：18 胡、红 11 张、自摸 ⇒ 18×2(红胡)×2(自摸) = 72 == winNum。
    """
    # 自摸 ×2
    assert rules.fan_win(21, 8, True)[0] == 42
    # 放炮不翻倍
    assert rules.fan_win(15, 3, False)[0] == 15
    # 红胡(10≤红<13) ×2 叠自摸 ×2 = ×4
    assert rules.fan_win(18, 11, True)[0] == 72
    # 红 1 张 → 一点红 ×2
    assert rules.fan_win(16, 1, False)[0] == 32
    # 红 0 张 → 乌胡，固定 100
    assert rules.fan_win(16, 0, False)[0] == 100
    # 红 ≥13（二人/三人）→ 十三红，固定 100
    assert rules.fan_win(16, 13, False)[0] == 100
    # 胡息正好 30 → 三十胡，固定 100
    assert rules.fan_win(30, 5, False)[0] == 100
    # 胡息正好 20 → 卡胡 ×2
    assert rules.fan_win(20, 5, False)[0] == 40


def test_round_v3_fan():
    """第二次对账：红字数 → 名堂倍率 → winNum 也要对得上。"""
    ev, live = replay.load(os.path.join(FIX, "round_v3.jsonl"))
    r = replay.build(ev, live)
    f = r["fan"]
    assert f and f["ok"], f
    assert r["red_n"][f["seat"]] == 11
    assert f["pred"] == f["actual"] == 72
    names = [k for k, _m, _w in f["hits"]]
    assert names == ["红胡", "自摸"], names


def test_all_rounds_double_closed():
    """所有胡牌局的两个锚点都要闭合：胡息 == origWinNum，且名堂倍率 == winNum。"""
    import glob
    bad = []
    for p in glob.glob(os.path.join(FIX, "*.jsonl")):
        ev, live = replay.load(p)
        n = len(replay.round_spans(ev)) or 1
        for i in range(1, n + 1):
            r = replay.build(ev, live, which=i)
            e = r.get("end") or {}
            win = e.get("winner")
            if not isinstance(win, int) or win < 0:
                continue
            if r["xi_total"].get(win) != e.get("origWinNum"):
                bad.append((os.path.basename(p), i, "xi", r["xi_total"].get(win), e.get("origWinNum")))
            if not (r.get("fan") or {}).get("ok"):
                bad.append((os.path.basename(p), i, "fan", (r.get("fan") or {}).get("pred"), e.get("winNum")))
    assert bad == [], bad


def test_draw_round():
    """荒庄局：能跑通，且不把缺手牌误报成守恒失败。"""
    ev, live = replay.load(os.path.join(FIX, "round_draw.jsonl"))
    r = replay.build(ev, live)
    assert r["end"]["winner"] == -1
    assert r["audit"]["ok"] is False      # 手牌没拿到
    assert r["audit"]["hand_known"] is False


def test_no_crash_on_empty():
    r = replay.build([], None)
    assert r["melds"] == []


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print("  ✓ %s" % name)
        except AssertionError as e:
            bad += 1
            print("  ✗ %s  %s" % (name, e))
        except Exception as e:                     # noqa: BLE001
            bad += 1
            print("  ✗ %s  异常 %r" % (name, e))
    print("\n通过 %d / 失败 %d" % (len(fns) - bad, bad))
    sys.exit(1 if bad else 0)
