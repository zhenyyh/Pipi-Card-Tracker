# -*- coding: utf-8 -*-
"""事件流 → 牌谱。两个独立来源互相校验，对不上就报错而不是猜。

为什么要两个来源
----------------
同一局的明牌可以从两处拿到：

  ① **live 快照**（`jsclient.data.sData.players[uid]`）
     客户端把明牌分成四个数组，天然带来源信息：
        mjpeng  碰 ｜ mjpeng1 偎 ｜ mjgang0 提 ｜ mjgang1 跑 ｜ mjchi 吃
  ② **事件流**（`MJPeng / MJGang / MJChi` 收包）
     带 `card` + `from`，但要靠 `from == 自己座位` 反推是提还是跑，
     而且 `MJGang.gang` 的编码（1/2/4/6）得另外解释。

两套算出来必须一样。不一样就是有一边错了 —— 直接报出来，别静默取信一边。
上一版就是只认 ②，把「偎」整组丢了，总牌守恒差 3 张，查了很久。

守恒公式（唯一可信的账）
------------------------
    80 - choupai = 各家手牌 + 各家弃牌堆 + 各家明牌 + 墩剩
                   live.hand   live.put     melds      pool - cardNext

逐家算「发牌 + 摸牌 = 手 + 弃 + 明」必然差几张，因为吃/碰/跑会把**对方的
弃牌**带进自己的明牌。只有全局守恒稳。
"""
from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import rules

POOL_TOTAL = rules.DECK_SIZE


# ---------------------------------------------------------------- 读入

def load(path: str) -> Tuple[List[dict], Optional[dict]]:
    """读事件流。支持三种：jsonl / {events,live} 整包 / ldmon eval --raw 的原始输出。"""
    with io.open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read().strip()
    if not txt:
        return [], None

    if not txt.startswith("{") and not txt.startswith("["):
        i = txt.find('{"ts"')
        if i < 0:
            i = txt.find("{")
        if i >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(txt[i:])
                if isinstance(obj, dict) and ("events" in obj or "live" in obj):
                    return list(obj.get("events") or []), obj.get("live")
            except ValueError:
                pass
        return _load_jsonl(txt), None

    try:
        obj = json.loads(txt)
    except ValueError:
        return _load_jsonl(txt), None

    if isinstance(obj, dict) and ("events" in obj or "live" in obj):
        return list(obj.get("events") or []), obj.get("live")
    if isinstance(obj, list):
        return obj, None
    return [], None


def _load_jsonl(txt: str) -> List[dict]:
    out = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------- 切局

def round_spans(events: List[dict]) -> List[Tuple[int, int]]:
    """按 roundEnd 切局，返回 [(start, end)]。

    `end` 要往后吃掉结算之后、下一局发牌之前的尾巴 —— `_live` 快照就排在
    roundEnd 后面（watch 是同一批写进去的），中间还可能夹几个 `updateInfo`。
    ★ 只延到 `mjhand` 为止：那是下一局的起点。以前只延「紧跟的 `_live`」，
      一旦中间夹了别的收包，本局的 live 就被切掉，明牌只能靠事件凑。
    """
    ends = [i for i, e in enumerate(events) if e.get("e") == "roundEnd"]
    spans = []
    prev_end = -1
    for idx in ends:
        start = prev_end + 1
        end = idx
        while end + 1 < len(events) and events[end + 1].get("e") not in ("mjhand", "roundEnd"):
            end += 1
        spans.append((start, end))
        prev_end = end
    return spans


def pick_live(events: List[dict], fallback: Optional[dict]) -> Optional[dict]:
    """本局自己的 live 优先（`_live` 事件），否则退回外部快照并校验它属于本局。

    ★ 校验不能用「live.playNum == 发牌 playNum」。结算后 live.playNum 已经是
      **下一局**编号了，拿它比必然误杀 —— 表现就是 playType/choupai/cardNext
      全变 None、明牌只能靠事件凑（于是把绞牌认成「?」）。2026-10-26 踩过。

    正确判据有两个，满足任一即可：
      ① 局中快照：live.playNum == 发牌那一刻的 playNum
      ② 结算后快照：live.playNum == roundEnd.tData.playNum（下一局编号）
         **且 cardNext 与本局结算的 cardNext 完全一致**（下一局发牌后会回到 40，
         靠它把「已经开了下一局」的快照挡掉）
    """
    own = [e["d"] for e in events if e.get("e") == "_live" and isinstance(e.get("d"), dict)]
    if own:
        return own[-1]
    if not fallback:
        return None

    deal_pn, end_pn, end_cn = None, None, None
    for e in events:
        nm, d = e.get("e"), (e.get("d") or {})
        td = d.get("tData") or {}
        if nm == "mjhand" and deal_pn is None and isinstance(td.get("playNum"), int):
            deal_pn = td["playNum"]
        if nm == "roundEnd":
            if isinstance(td.get("playNum"), int):
                end_pn = td["playNum"]
            if isinstance(td.get("cardNext"), int):
                end_cn = td["cardNext"]

    lp, lc = fallback.get("playNum"), fallback.get("cardNext")
    if not isinstance(lp, int):
        return fallback
    if isinstance(deal_pn, int) and lp == deal_pn:
        return fallback
    if isinstance(end_pn, int) and lp == end_pn and lc == end_cn:
        return fallback
    return None


def seat_map(events: List[dict], live: Optional[dict]) -> Dict[int, int]:
    uids: List[int] = []
    for e in events:
        u = ((e.get("d") or {}).get("tData") or {}).get("uids")
        if isinstance(u, list) and len(u) > len(uids):
            uids = [int(x) for x in u]
    if len(uids) < 2 and live and isinstance(live.get("uids"), list):
        if len(live["uids"]) > len(uids):
            uids = [int(x) for x in live["uids"]]
    return {u: i for i, u in enumerate(uids)}


# ---------------------------------------------------------------- 弃牌堆

def rebuild_puts(events: List[dict], seat_of: Dict[int, int]):
    """从 `newCard.mjput` 的增量重建弃牌堆 + 出牌时序。

    `newCard.mjput` 是 `curPlayer` 那一家的**弃牌堆快照**：打出的牌追加在
    尾部，被自己吃/偎/坎、或被对方吃/碰/跑掉就从快照消失。
    ⇒ 「这次快照 − 上次快照」的多重集差 = 这段时间他新打出的牌。

    返回值里的 `order` 可能多一张：结算那一刻**正摆在桌上等人反应**的牌也会
    出现在某些快照里（见 `drop_pending()`）。
    """
    piles: Dict[int, List[int]] = {}
    order: List[Tuple[int, int, int]] = []
    for e in events:
        if e.get("e") != "newCard":
            continue
        d = e.get("d") or {}
        snap, cur = d.get("mjput"), d.get("curPlayer")
        if not isinstance(snap, list):
            continue
        st = cur if isinstance(cur, int) else seat_of.get(cur)
        if st is None:
            continue
        left = list(piles.get(st, []))
        for c in snap:
            c = int(c)
            if c in left:
                left.remove(c)
            else:
                order.append((e.get("t", 0), st, c))
        piles[st] = [int(x) for x in snap]
    return piles, order


def drop_pending(order, live, puts: Optional[Dict[int, List[int]]] = None):
    """剪掉「摆牌待反应」的那张。

    `newCard.mjput` 里会混进一张**别人刚打出、正等自己反应**的牌
    （实测第 5 局座位1 的 mjput 尾是 110，而 live.lastPut=110/lastPutPlayer=0）。
    判据：`lastPut` 出现在**非 lastPutPlayer** 的弃牌堆里。

    ★ `puts` 也要一起剪 —— 以前只剪出牌时序，弃牌堆照旧留着那张，
      于是守恒稳定多 1 张（2026-10-26 四局里三局都是 61/60）。
      第 4 局：座0 弃牌堆里躺着 109，而 lastPut=109/lastPutPlayer=1，
      那张其实是座1 胡走的牌，服务端快照拍在移除之前。剪掉后 53+7=60 ✓。
    """
    if not live:
        return order, 0
    lp, lps = live.get("lastPut"), live.get("lastPutPlayer")
    if not isinstance(lp, int) or not isinstance(lps, int):
        return order, 0
    kept = [x for x in order if not (x[2] == lp and x[1] != lps)]
    n = len(order) - len(kept)
    if puts:
        for st in list(puts.keys()):
            if st == lps:
                continue                      # 自己打的那张本来就该留在他堆里
            arr = list(puts.get(st) or [])
            if lp in arr:
                arr.remove(lp)
                puts[st] = arr
                n += 1
    return kept, n


# ---------------------------------------------------------------- 明牌

def melds_from_live(live: Optional[dict]) -> List[dict]:
    """从 sData 的数组直接取明牌（来源明确，不用猜）。

    客户端一共给了 6 个数组（见 docs/FINDINGS.md 的字段表）：
        mjpeng 碰 / mjpeng1 偎 / mjgang0 提 / mjgang1 跑 / mjchi 吃 / mjlie 摆火
    `mjlie` 是**摆火**，单张、不计息，但占牌数 —— 少了它守恒就缺牌。
    """
    out: List[dict] = []
    if not live:
        return out
    for s in live.get("seats") or []:
        st = s.get("seat")
        for which in ("gang0", "gang1", "peng", "peng1", "chi", "lie"):
            arr = s.get(which)
            if not isinstance(arr, list) or not arr:
                continue
            if which == "lie":
                # 摆火：每个元素就是一张，逐张入账（不计息）
                for c in arr:
                    m = rules.meld_from_array("lie", [c])
                    if m:
                        m["seat"] = st
                        out.append(m)
            elif which == "chi":
                # 吃是 3 张一组平铺，可能多组
                for i in range(0, len(arr) - 2, 3):
                    m = rules.meld_from_array("chi", arr[i:i + 3])
                    if m:
                        m["seat"] = st
                        out.append(m)
            elif which in ("gang0", "gang1"):
                # gang 数组可能是 1 个代表值/组，也可能平铺
                per = 1 if len(arr) % 4 else 4
                for i in range(0, len(arr), per):
                    m = rules.meld_from_array(which, arr[i:i + per])
                    if m:
                        m["seat"] = st
                        out.append(m)
            else:
                per = 1 if len(arr) % 3 else 3
                for i in range(0, len(arr), per):
                    m = rules.meld_from_array(which, arr[i:i + per])
                    if m:
                        m["seat"] = st
                        out.append(m)
    return out


def melds_from_events(events: List[dict], seat_of: Dict[int, int]) -> List[dict]:
    """从 MJGang / MJPeng / MJChi 收包推明牌（用来跟 live 对账）。"""
    out: List[dict] = []
    band: Dict[Tuple[int, int], int] = {}     # (seat, card) -> out 下标，用于原地升级

    for e in events:
        nm, d = e.get("e"), (e.get("d") or {})
        st = seat_of.get(d.get("uid"))
        if st is None:
            continue
        if nm == "MJGang":
            c = int(d.get("card") or 0)
            if not c:
                continue
            origin = "self" if d.get("from") == st else "other"
            k = (st, c)
            if k in band:
                # 偎补到第 4 张 → 原地升级成提/跑。
                # ★ 必须连 xi 一起重算：只改 cards/kind 会把 3 张时的偎(3)/碰(1)
                #   胡息留在 4 张的提/跑身上，实测出现过「提 [4,4,4,4] xi=3」
                #   「跑 [2,2,2,2] xi=1」。2026-10-26 修。
                i = band[k]
                arr4 = [c] * 4
                name, xi, tip = rules.classify(arr4, origin)
                out[i].update(cards=arr4, kind=name, xi=xi, tip=tip, origin=origin)
                continue
            band[k] = len(out)
            # gang 编码 1=偎(3 张)，2/4/6=提或跑(4 张)；张数只影响展示，
            # 真正的牌型以 origin 为准（后面还要跟 live 对账）。
            n = 3 if int(d.get("gang") or 0) == 1 else 4
            arr = [c] * n
            name, xi, tip = rules.classify(arr, origin)
            out.append({"seat": st, "cards": arr, "kind": name, "xi": xi,
                        "tip": tip, "origin": origin, "from_array": "event"})
        elif nm == "MJPeng":
            c = int(d.get("card") or 0)
            if c:
                band[(st, c)] = len(out)
                out.append({"seat": st, "cards": [c] * 3, "kind": rules.KIND_PENG,
                            "xi": rules.XIXI[(rules.KIND_PENG, rules.is_big(c))],
                            "tip": "", "origin": "other", "from_array": "event"})
        elif nm == "MJChi":
            arr = [int(x) for x in (d.get("mjchi") or [])]
            orig = "self" if d.get("from") == st else "other"
            for i in range(0, len(arr) - 2, 3):
                grp = arr[i:i + 3]
                name, xi, tip = rules.classify(grp, orig)
                out.append({"seat": st, "cards": grp, "kind": name, "xi": xi,
                            "tip": tip, "origin": orig, "from_array": "event"})
    return out


def _meld_sig(m: dict) -> Tuple[int, int, int, str]:
    """对账用的签名：座位 + 点数 + 张数 + 牌型。大小写混的组按点数合并。"""
    pts = sorted(rules.pt(c) for c in m["cards"])
    return (m["seat"], pts[0] if pts else 0, len(m["cards"]), m["kind"])


# ---------------------------------------------------------------- 主还原

def _meld_key(m: dict) -> Tuple[int, int, int]:
    """(座位, 点数, 张数) —— 不带牌型。同一个位置不可能是两种牌型。"""
    pts = sorted(rules.pt(c) for c in m["cards"])
    return (m["seat"], pts[0] if pts else 0, len(m["cards"]))


def merge_melds(live_m: List[dict], ev_m: List[dict], warn: List[str]) -> List[dict]:
    """live 优先，事件只补 live 里没有的组。

    为什么不能直接用其中一个：
      - **只有 live**：老版本的 dump 字段名是 `gang`（提+跑拼在一起）、
        没有 `peng1`，偎和跑就整组丢了。
      - **只有事件**：胡牌那一下 `MJGang` 会重发，凭空多出 3 张。
    所以以 live 为骨架，事件按 (座位, 点数, 张数) 补齐；牌型冲突单独报。
    """
    out = list(live_m)
    have = {_meld_key(m): m for m in out}
    added, conflict = [], []
    for m in ev_m:
        k = _meld_key(m)
        if k in have:
            if have[k]["kind"] != m["kind"]:
                conflict.append((k, have[k]["kind"], m["kind"]))
            continue
        out.append(m)
        have[k] = m
        added.append((k, m["kind"]))
    if conflict:
        warn.append("★ 牌型冲突（live vs 事件）：%s" % conflict)
    if added:
        warn.append("★ 这些明牌 live 里没有，由事件补上：%s" % added)
    if live_m and not added and not conflict:
        warn.append("明牌：live 与事件两个来源一致（%d 组）" % len(out))
    return out


def build(events: List[dict], live: Optional[dict] = None, which: Optional[int] = None) -> dict:
    """还原一局。which: None=最后一局，1/2/3…=第几局，-1=倒数第一局。"""
    warn: List[str] = []
    spans = round_spans(events)
    if spans:
        idx = len(spans) - 1 if which is None else (which - 1 if which > 0 else len(spans) + which)
        idx = max(0, min(idx, len(spans) - 1))
        s, e = spans[idx]
        events = events[s:e + 1]
        warn.append("共 %d 局，取第 %d 局（%d 条事件）" % (len(spans), idx + 1, len(events)))
    live = pick_live(events, live)
    seat_of = seat_map(events, live)
    uids = [u for u, _ in sorted(seat_of.items(), key=lambda kv: kv[1])]
    nseat = len(uids) or 2

    r: Dict[str, Any] = {"uids": uids, "seat_of": seat_of, "nseat": nseat, "warn": warn}

    # ---- 发牌 / 终局手牌 ----
    for e in events:
        if e.get("e") == "mjhand":
            h = (e.get("d") or {}).get("mjhand")
            if isinstance(h, list) and h:
                r["deal"] = [int(x) for x in h]
                break

    final_hand: Dict[int, List[int]] = {}
    if live:
        for s in live.get("seats") or []:
            final_hand[s["seat"]] = [int(x) for x in (s.get("hand") or [])]
        r["card_next"] = live.get("cardNext")
        r["choupai"] = live.get("choupai")
        r["limit"] = live.get("limitScore")
        r["play_type"] = live.get("playType")
        r["huzi_type"] = live.get("huziType")
        r["init_hand"] = live.get("initHandNum")
        r["cai"] = live.get("caiPaiArr") or {}
        r["cai_zuan"] = live.get("caiPaiZuanNumArr") or {}
        r["cai_cnt"] = {s["seat"]: (s.get("caiPaiCount") or {}) for s in live.get("seats") or []}

    # ---- 结算 ----
    end = None
    for e in events:
        if e.get("e") == "roundEnd":
            end = e.get("d") or {}
    if end:
        td = end.get("tData") or {}
        winner, last_draw = td.get("winner"), td.get("lastDrawPlayer")
        r["end"] = {
            "winner": winner, "tState": td.get("tState"),
            "zhuang": td.get("zhuang"),
            "winNum": end.get("winNum"), "origWinNum": end.get("origWinNum"),
            "multiple": end.get("multiple"),
            "last_put": td.get("lastPut"), "last_draw_player": last_draw,
            # 自摸判据：最后一张牌是自己从墩上摸的。别用 mjdesc 里有没有「自摸」，
            # 那是名堂列表，不一定写全（第 1 局写了、第 4 局是接炮所以为空）。
            "self_draw": (isinstance(winner, int) and winner == last_draw),
            "cards": [int(x) for x in (end.get("cards") or []) if isinstance(x, int)],
            "players": {},
        }
        for uid, p in (end.get("players") or {}).items():
            if not isinstance(p, dict):
                continue
            st = seat_of.get(int(uid)) if str(uid).isdigit() or isinstance(uid, int) else None
            if st is None:
                continue
            r["end"]["players"][st] = {
                k: p.get(k) for k in ("winone", "winall", "huNum", "paoNum",
                                      "tiNum", "fireNum", "selfDrawNum")
            }
            r["end"]["players"][st]["mjdesc"] = [str(x) for x in (p.get("mjdesc") or [])]
            h = p.get("mjhand")
            if (not final_hand.get(st)) and isinstance(h, list) and h:
                final_hand[st] = [int(x) for x in h if isinstance(x, int)]
        for k, src in (("card_next", "cardNext"), ("choupai", "choupai"), ("limit", "limitScore")):
            if r.get(k) is None and isinstance(td.get(src), int):
                r[k] = td[src]
    elif live:
        # roundEnd 不在缓冲里（arm 之后才开局、或者缓冲被 clear 过）。
        # 不猜结算字段，只把 live 自己的状态摆出来。
        r["live_state"] = {"tState": live.get("tState"),
                           "winner": live.get("winner"),
                           "playNum": live.get("playNum")}
    r["final_hand"] = final_hand

    # ---- 弃牌堆 ----
    puts, order = rebuild_puts(events, seat_of)
    if live:
        live_put = {s["seat"]: [int(x) for x in (s.get("put") or [])]
                    for s in live.get("seats") or []}
        if any(live_put.get(st) for st in range(nseat)):
            for st in range(nseat):
                a, b = len(puts.get(st) or []), len(live_put.get(st) or [])
                if a != b:
                    warn.append("座位%d 弃牌：事件增量 %d 张 / live %d 张，按 live 修正" % (st, a, b))
            puts = live_put
    order, dropped = drop_pending(order, live, puts)
    if dropped:
        warn.append("出牌时序/弃牌堆剪掉 %d 张「摆牌待反应」" % dropped)
    r["put"], r["put_order"] = puts, order

    # ---- 明牌：两个来源对账 ----
    ml = melds_from_live(live)
    me = melds_from_events(events, seat_of)
    r["melds_live"], r["melds_event"] = ml, me
    r["melds"] = merge_melds(ml, me, warn)

    # ---- 胡息：明牌 + 手牌门子（两半都要算）----
    r["xi_meld"] = {st: sum(m["xi"] for m in r["melds"] if m["seat"] == st)
                    for st in range(nseat)}

    # ★ 非自摸（接炮）时，`lastPut` 那张是**别人打的**。它和手上两张同值凑成的
    #   是「碰」（大 3 / 小 1），不是「坎」（大 6 / 小 3）。胡牌瞬间的 `mjhand`
    #   含刚胡进来的那张，所以这一门必须降级 —— 第 4 局就是靠这条从 18 落到 15。
    foreign = set()
    if r.get("end") and not r["end"].get("self_draw"):
        lp = r["end"].get("last_put")
        if isinstance(lp, int):
            foreign.add(lp)
    r["foreign"] = sorted(foreign)
    r["xi_hand"] = {}
    for st, h in (r.get("final_hand") or {}).items():
        if h:
            w = (r.get("end") or {}).get("winner")
            r["xi_hand"][st] = rules.hand_xi(h, foreign=(foreign if st == w else None))
    r["xi_total"] = {}
    for st in range(nseat):
        a = r["xi_meld"].get(st, 0)
        b = (r["xi_hand"].get(st) or {}).get("total", 0)
        r["xi_total"][st] = a + b

    # ---- 第二次对账：红字数 → 名堂倍率 → winNum ----
    # 胡息对得上只说明「门子算对了」，名堂对得上才说明「这局读懂了」。
    # 红字要数**该玩家全部牌**（明牌 + 终局手牌），因为名堂看的是整副胡牌。
    r["red_n"] = {}
    for st in range(nseat):
        cards = [c for m in r["melds"] if m["seat"] == st for c in m["cards"]]
        cards += list((r.get("final_hand") or {}).get(st) or [])
        r["red_n"][st] = rules.red_count(cards)
    r["fan"] = None
    w = (r.get("end") or {}).get("winner")
    if isinstance(w, int) and w >= 0:
        pred, hits = rules.fan_win(r["end"].get("origWinNum"), r["red_n"].get(w, 0),
                                   r["end"].get("self_draw"), nseat)
        actual = r["end"].get("winNum")
        r["fan"] = {"seat": w, "pred": pred, "actual": actual, "hits": hits,
                    "ok": (pred == actual)}

    # ---- 守恒 ----
    r["pool"] = (POOL_TOTAL - r["choupai"]) if r.get("choupai") else \
        ((r.get("init_hand") or 20) * nseat + 20)
    r["audit"] = _audit(r)
    return r


def _audit(r: dict) -> dict:
    cn = r.get("card_next")
    rows = []
    for st in range(r["nseat"]):
        rows.append({
            "seat": st,
            "hand": len((r.get("final_hand") or {}).get(st) or []),
            "put": len((r.get("put") or {}).get(st) or []),
            "meld": sum(len(m["cards"]) for m in r["melds"] if m["seat"] == st),
        })
    tot = sum(x["hand"] + x["put"] + x["meld"] for x in rows)
    rest = (r["pool"] - cn) if (r.get("pool") and isinstance(cn, int)) else None
    known = all(x["hand"] > 0 for x in rows)
    # ★ 别再试图用「自摸就减 1」去凑 round_kan 那张差额 —— 试过，round_v3 也是
    #   自摸并且 lastDrawPlayer==winner，但它的 cardNext 是准的，一减就错。
    #   round_kan 差的 1 张仍未定性（见 docs/FINDINGS.md「未闭合」）。
    return {"rows": rows, "total": tot, "rest": rest, "rest_raw": rest,
            "pool": r.get("pool"),
            "self_draw": bool((r.get("end") or {}).get("self_draw")),
            "hand_known": known,
            "ok": bool(rest is not None and known and tot + rest == r["pool"])}
