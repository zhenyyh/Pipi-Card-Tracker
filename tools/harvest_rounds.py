# -*- coding: utf-8 -*-
"""把每一局结算里公开的两家手牌，存成带标签的样本。

为什么这条比「猜」靠谱
----------------------
`roundEnd.players[uid].mjhand` 里是**每家的终局手牌**，输家的也有 ——
这是游戏自己在结算面板上亮给你看的东西，不是暗牌。

于是每打完一局就自动多一条带答案的样本：中间发生过什么（底牌、首出、
过张、双方出牌序、门子、张数），最后两家手里剩什么。攒够几十局，
「猜牌」就从玄学变成统计：

  ① 校准先验：给定「第 N 手、墩剩多少、对面已经成几方」，他手牌通常是
     什么形状（几张孤张、几张对子）。用来给 AI 的候选集打权重。
  ② 枪毙错误假设：任何「他不可能有 X」的推论，都可以拿来对标签。
     实测已经枪毙一条 —— 见 docs/GAME_DATA.md「过张」那节。
  ③ 算上下界而不是点估计：
        确定他有 ⊇ 终局手牌（牌只会离开他手，不会凭空多出来）
        可能有   ⊆ 终局手牌 ∪ 他之后打出的牌 ∪ 他之后成门子用掉的牌

用法
----
    python tools/harvest_rounds.py                 # 扫 fixtures + data/stream_*.jsonl
    python tools/harvest_rounds.py a.jsonl b.jsonl
    python tools/harvest_rounds.py --out data/labels.jsonl

输出每行一局：
    {"tableid","playNum","winner","me","opp","myHand","oppHand",
     "oppPuts","oppMelds","passChi","bottom","deckLeft","oppZhuang","nPut"}
"""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)      # 让 tools/ 里也能 import pct.*


def _streams(argv):
    """取要扫的流文件。`--out` 后面那个是输出路径，不能当成输入。"""
    paths, skip = [], False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a in ("--out", "--from-state"):
            skip = True
            continue
        if a.startswith("--"):
            continue
        paths.append(a)
    if paths:
        return paths
    out = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "*.jsonl")))
    out += sorted(glob.glob(os.path.join(ROOT, "data", "stream_*.jsonl")))
    return out


def harvest(path):
    """扫一份流，每个 roundEnd 出一行。只用已落盘的字段，不做推断。"""
    me = None
    my_dealt = []          # 开局 mjhand 事件里的发牌序列（只有自己这一份是真的）
    put_by_uid = {}
    meld_by_uid = {}
    pass_chi = []
    bottom = None
    opened = False
    cur_key = None
    rows = []

    def flush(end_d):
        tdata = end_d.get("tData") or {}
        players = end_d.get("players") or {}
        if len(players) < 2:
            return
        hands = {}
        for u, v in players.items():
            h = v.get("mjhand") if isinstance(v, dict) else None
            if not isinstance(h, list):
                continue          # "<a0>" 之类的编码占位，不是手牌
            hands[str(u)] = [c for c in h if isinstance(c, int)]
        if len(hands) < 2 or not all(hands.values()):
            return
        uids = [str(u) for u in (tdata.get("uids") or [])]

        # `_live` 里未必带 me（旧流就没有）。用开局 mjhand 事件反推：
        # 那份发牌序列是「我」的，终局手牌和它交集最大的那个 uid 就是我。
        me_eff = me
        if me_eff is None and my_dealt:
            pool = {}
            for c in my_dealt:
                pool[c] = pool.get(c, 0) + 1
            best, best_hit = None, -1
            for u, h in hands.items():
                left = dict(pool)
                hit = 0
                for c in h:
                    if left.get(c, 0) > 0:
                        left[c] -= 1
                        hit += 1
                if hit > best_hit:
                    best, best_hit = u, hit
            me_eff = best

        opp = None
        if me_eff is not None:
            opp = next((u for u in uids if u != str(me_eff)), None)
        if opp is None:
            others = [u for u in hands if u != str(me_eff)]
            opp = others[0] if len(others) == 1 else None
        rows.append({
            "tableid": tdata.get("tableid"),
            "playNum": tdata.get("playNum"),
            "winner": tdata.get("winner"),
            "me": me_eff,
            "opp": opp,
            "zhuang": tdata.get("zhuang"),
            "choupai": tdata.get("choupai"),
            "cardNext": tdata.get("cardNext"),
            "deckLeft": (80 - (tdata.get("choupai") or 20) - (tdata.get("cardNext") or 0)),
            "myHand": hands.get(str(me_eff)) if me_eff is not None else None,
            "oppHand": hands.get(opp) if opp else None,
            "putsByUid": dict(put_by_uid),
            "oppMelds": meld_by_uid.get(opp, {}) if opp else None,
            "passChi": pass_chi,
            "bottom": bottom,
            "originWinNum": end_d.get("origWinNum"),
            "winNum": end_d.get("winNum"),
        })

    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            name, d = o.get("e"), (o.get("d") or {})
            td = d.get("tData") or {}

            # ★ mjhand = 发牌 = 新局开始。用它当换局信号，不要用 playNum：
            #   实测 roundEnd 里的 playNum 已经 +1（mjhand 报 3、roundEnd 报 4），
            #   拿局号做 key 会在结算那一刻把整局累计冲掉。
            if name == "mjhand":
                h = d.get("mjhand")
                if isinstance(h, list):
                    vals = [c for c in h if isinstance(c, int)]
                    if vals:
                        my_dealt = vals
                put_by_uid.clear()
                meld_by_uid.clear()
                pass_chi = []
                bottom = None
                opened = False
                continue
            if name == "_live":
                me = d.get("me") or me
                continue


            uid = d.get("uid")
            if name == "MJPut" and uid is not None and isinstance(d.get("mjput"), list):
                put_by_uid[str(uid)] = [int(c) for c in d["mjput"] if c]
            elif name in ("MJPeng", "MJChi", "MJGang") and uid is not None:
                slot = meld_by_uid.setdefault(str(uid), {})
                slot.setdefault(name, []).append(d.get("card") or d.get("mjchi"))

            if isinstance(d.get("mjpassChi"), list):
                vals = [int(c) for c in d["mjpassChi"] if c]
                if len(vals) > len(pass_chi):
                    pass_chi = vals

            if name == "newCard" and not opened:
                opened = True
                lp = d.get("lastPut")
                if isinstance(lp, int) and 1 <= lp <= 110:
                    bottom = lp

            if name == "roundEnd":
                flush(d)

    return rows


def from_state(st):
    """从一份 `pct_state.json`（常驻采集器快照）抽一行标签。

    为什么需要这条路：`roundEnd` 的 `players[uid].mjhand` 是个嵌套对象，
    采集器只保留标量和数组（限 30），**嵌套对象会被丢掉** —— 实机流里的
    `roundEnd.players` 是空的。但结算那一刻 `sData.players[uid].mjhand`
    两家都会填满（实测 tState=6 时对手 17 张、本人 15 张），而 seats 快照
    本来就是数组，能落盘。

    所以：**结算快照比 roundEnd 事件更可靠**，这是拿标签的首选通路。
    """
    seats = st.get("seats") or []
    if len(seats) < 2:
        return None
    hands, puts, melds = {}, {}, {}
    for s in seats:
        uid = str(s.get("uid"))
        if not s.get("handKnown"):
            return None
        h = [c for c in (s.get("hand") or []) if isinstance(c, int)]
        if not h:
            return None
        hands[uid] = h
        puts[uid] = [c for c in (s.get("put") or []) if isinstance(c, int)]
        melds[uid] = {k: s.get(k) for k in ("peng", "peng1", "gang0", "gang1", "chi", "lie")
                      if s.get(k)}
    me = str(st.get("me")) if st.get("me") is not None else None
    uids = [str(u) for u in (st.get("uids") or [])]
    if me is None and uids:
        me = uids[0]
    opp = next((u for u in uids if u != me), None) if me else None
    if opp is None:
        return None

    # 每条标签自带一次守恒校验：对不上的局要能被一眼挑出来，
    # 而不是等训练时才发现标签是错的。
    try:
        from pct.tracker import Tracker
        t = Tracker()
        t.feed(st, [])
        c = t.conservation()
        cons = {"pool": c["pool"], "sum": c["sum"], "diff": c["diff"],
                "ok": c["ok"], "clipped": c["clipped"],
                "unknown": [u["seat"] for u in c["unknown"]]}
    except Exception as e:            # 守恒算不了不该挡住标签本身
        cons = {"error": str(e)[:120]}

    return {
        "tableid": st.get("tableid"),
        "playNum": st.get("playNum"),
        "winner": st.get("winner"),
        "me": me,
        "opp": opp,
        "zhuang": st.get("zhuang"),
        "choupai": st.get("choupai"),
        "cardNext": st.get("cardNext"),
        "deckLeft": (80 - (st.get("choupai") or 20) - (st.get("cardNext") or 0)),
        "myHand": hands.get(me),
        "oppHand": hands.get(opp),
        "putsByUid": puts,
        "oppMelds": melds.get(opp, {}),
        "passChi": next((s.get("passChi") for s in seats
                         if str(s.get("uid")) == me), None),
        "bottom": st.get("xingCard") if isinstance(st.get("xingCard"), int) and st.get("xingCard") > 0 else None,
        # 结算时墩里剩的牌会公开（实测 15 张，正好 = 60 - cardNext）
        "leftCards": [c for c in (st.get("leftCards") or []) if isinstance(c, int)],
        "conservation": cons,
        "originWinNum": None,
        "winNum": None,
    }


def main(argv):
    out_path = None
    if "--out" in argv:
        i = argv.index("--out")
        out_path = argv[i + 1]

    rows = []
    if "--from-state" in argv:
        sp = argv[argv.index("--from-state") + 1]
        with open(sp, encoding="utf-8") as f:
            st = json.load(f)
        row = from_state(st)
        if row:
            rows.append(row)
            print("%-40s 1 局带标签（结算快照）" % os.path.basename(sp))
        else:
            print("✗ 这份快照拿不到标签：需要 tState=6 且两家 mjhand 都有内容")
    else:
        for p in _streams(argv):
            got = harvest(p)
            if got:
                print("%-40s %d 局带标签" % (os.path.basename(p), len(got)))
            rows.extend(got)

    # 同一局可能出现在多份流里（round_multi4 就是拼接出来的），按「局 + 双方手牌」去重
    seen, uniq = set(), []
    for r in rows:
        sig = (r.get("tableid"), r.get("playNum"), tuple(r.get("myHand") or []),
               tuple(r.get("oppHand") or []))
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(r)
    if len(uniq) != len(rows):
        print("去重：%d → %d 局" % (len(rows), len(uniq)))
    rows = uniq

    if not rows:
        print("没有可用样本：需要同时带 roundEnd.players[uid].mjhand 的流")
        return 1

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("已写 %s（%d 行）" % (out_path, len(rows)))
    else:
        print(json.dumps(rows[-1], ensure_ascii=False, indent=1))

    # 一个立刻能看的统计：对手手牌里孤张/对子的形状
    shape = {}
    for r in rows:
        h = r.get("oppHand") or []
        c = {}
        for x in h:
            c[x] = c.get(x, 0) + 1
        sig = tuple(sorted(c.values(), reverse=True))
        shape[sig] = shape.get(sig, 0) + 1
    print("对手终局手牌形状分布（%d 局）：" % len(rows))
    for sig, n in sorted(shape.items(), key=lambda kv: -kv[1]):
        print("   %s  ×%d" % (sig, n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
