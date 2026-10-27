# -*- coding: utf-8 -*-
"""记牌器状态机：把「事件流 + 快照」折成对方门子与后续出牌的对照表。

核心问题
--------
记牌器要的是「对方成这个门子之后打出了哪张」。这两件事在数据里是分开的：
  门子 —— 事件 MJChi/MJPeng/MJGang（也同步存在于 sData 的 6 个数组）
  出牌 —— 事件 MJPut
所以只能按**事件到达顺序**配对，不能靠轮询快照（快照里被吃走的牌会消失，
顺序也丢了）。

★ 但 `MJPut` 不是每手都发（仓库 README 也记了这点，实测只抓到一半）。
  所以出牌要双保险：
    ① 事件流 MJPut —— 有就用，顺序最准
    ② 弃牌堆快照差分 —— 事件漏掉的那张从 sData 的 mjput 增量里补
  两边用 Counter 去重，不会记两遍。

口径（对着 4 份 fixture 验过）
------------------------
`from` 是**座位号**，不是 uid：
  MJPeng  from == 自己座位 → 偎（自己摸的第 3 张），否则 → 碰（接别人打的）
  MJGang  from == 自己座位 → 提（4 张自己摸齐），否则 → 跑（第 4 张来自别人）
这两条和 sData 的 mjpeng1 / mjgang0 数组一一对应，可以互相校验。

门子会升级
----------
碰 3 张被别人打第 4 张 → 跑；偎 3 张自己摸到第 4 张 → 提。
升级时**原位替换**，原来配好的 after 保留 —— 否则整张表会错位。

门子类型不是只有「吃」
------------------
mjchi 数组里装两种东西：一句话（顺子）和**绞牌**（一对 + 一张同点异大小写），
所以类型判定走 rules.classify，别一律当「吃」。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from . import rules

KIND_NAME = {
    "chi": "吃",
    "peng": "碰",
    "wei": "偎",
    "ti": "提",
    "pao": "跑",
    "lie": "摆",
    "jiao": "绞",
}
CN2EN = {
    rules.KIND_CHI: "chi",
    rules.KIND_JIAO: "jiao",
    rules.KIND_PENG: "peng",
    rules.KIND_WEI: "wei",
    rules.KIND_TI: "ti",
    rules.KIND_PAO: "pao",
    rules.KIND_LIE: "lie",
}


class Meld:
    __slots__ = ("kind", "cards", "after", "after_at", "t", "syn", "owner")

    def __init__(self, kind: str, cards: List[int], t: int = 0, syn: bool = False,
                 owner: str = "opp"):
        self.kind = kind
        self.cards = list(cards)
        self.after: Optional[int] = None
        self.after_at = 0
        self.t = t
        self.syn = syn          # True = 从快照补的，还等着事件来认领
        self.owner = owner      # "me" | "opp"：门子是谁的

    @property
    def card(self) -> int:
        return self.cards[0] if self.cards else 0

    def to_ui(self) -> Dict[str, Any]:
        return {"kind": KIND_NAME.get(self.kind, self.kind),
                "cards": self.cards, "after": self.after}

    def __repr__(self) -> str:
        return "<%s %s -> %s>" % (self.kind, self.cards, self.after)


def _added(prev: List[int], cur: List[int]) -> List[int]:
    """多重集差，保留 cur 里的相对顺序（新增通常追加在尾部）。"""
    pc = Counter(prev)
    out = []
    for c in cur:
        if pc[c] > 0:
            pc[c] -= 1
        else:
            out.append(c)
    return out


class Tracker:
    """一局的记牌状态。换局（playNum 变）自动重置。"""

    def __init__(self) -> None:
        self.reset()
        self._round_key: Any = None

    def reset(self) -> None:
        self.me: Optional[int] = None
        self.opp: Optional[int] = None
        self.my_seat: Optional[int] = None
        self.opp_seat: Optional[int] = None
        self.uids: List[int] = []
        self.melds: List[Meld] = []
        self.pending: Optional[Meld] = None
        self.puts: List[int] = []
        self.first_put: Optional[int] = None
        self.bottom: Optional[int] = None
        self.in_game = False
        self.t_state: Optional[int] = None
        self.left: List[int] = []
        self.winner: Optional[int] = None
        self.play_num: Optional[int] = None
        self.tableid: Any = None
        self._prev_put: List[int] = []
        self._primed = False
        self._seats: list = []
        self._ai_my_seat: Optional[int] = None
        self.choupai = 0
        self._card_next = 0
        self._card_next_known = False
        self._cur_player = None
        self._last_put = None
        self._visible_actions = None
        self._last_put_player = None
        self._last_draw_player = None
        self._room_rules: Dict[str, Any] = {}
        self._deck_left_known = False
        self._opp_all_puts: List[int] = []
        self.timeline: List[Dict[str, Any]] = []   # 回合流水：谁在什么时候做了什么
        self.zhuang: Optional[int] = None      # 庄家座位号
        self.opp_is_zhuang = False
        self.dealt: List[int] = []      # 发牌顺序（未排序）
        self.bottom_self: Optional[int] = None   # 自己当庄时的底牌
        self.bottom_ev: Optional[int] = None     # 开局 newCard 的 lastPut（最准）
        self._opened = False                     # 本局是否已见过第一条 newCard

    # ------------------------------------------------------------------ 身份
    def _identify(self, live: Dict[str, Any]) -> None:
        me = live.get("me")
        uids = [int(u) for u in (live.get("uids") or [])]
        if not uids:
            return
        self.uids = uids
        self.me = int(me) if me else self.me
        if self.me in uids:
            self.my_seat = uids.index(self.me)
            for i, u in enumerate(uids):
                if u != self.me:
                    self.opp = u
                    self.opp_seat = i
                    break
        elif self.opp is None:
            self.opp = uids[0]
            self.opp_seat = 0

    def _seat_of(self, uid: Any) -> Optional[int]:
        try:
            return self.uids.index(int(uid))
        except (ValueError, TypeError):
            return None

    def _opp_seat_data(self, live: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for s in live.get("seats") or []:
            if s.get("seat") == self.opp_seat:
                return s
        return None

    # ------------------------------------------------------------------ 事件
    def feed(self, live: Dict[str, Any], evs: List[Dict[str, Any]]) -> None:
        if not live or live.get("none"):
            self.in_game = False
            self._ai_my_seat = None
            return

        key = (live.get("tableid"), live.get("playNum"))
        if self._round_key is not None and key != self._round_key:
            self.reset()
        self._round_key = key

        self._identify(live)
        self._seats = live.get("seats") or []
        # AI 的座位必须来自本次快照，不能沿用旧身份或猜测。
        self._ai_my_seat = None
        try:
            uid = int(live.get("me"))
            uids = [int(u) for u in live.get("uids") or []]
            if uid and uids.count(uid) == 1:
                self._ai_my_seat = uids.index(uid)
        except (TypeError, ValueError):
            pass
        self.choupai = live.get("choupai") or 0
        self._card_next = live.get("cardNext") or 0
        self._card_next_known = isinstance(live.get("cardNext"), int)
        self._deck_left_known = (isinstance(live.get("choupai"), int) and
                                 isinstance(live.get("cardNext"), int))
        self._cur_player = live.get("curPlayer")
        self._visible_actions = live.get("visibleActions")
        self._last_put = live.get("lastPut")
        self._last_put_player = live.get("lastPutPlayer")
        self._last_draw_player = live.get("lastDrawPlayer")
        self._room_rules = {k: live.get(k) for k in ("huziType", "limitScore", "playType")}
        self.t_state = live.get("tState")
        self.tableid = live.get("tableid")
        self.play_num = live.get("playNum")
        self.left = list(live.get("leftCards") or [])
        self.winner = live.get("winner")
        # ★ 局中状态：3=出牌 4=等人出牌 5=**等某人响应（可胡/可碰/可吃）** 8=胡牌确认
        #   tState=5 是最容易踩的坑：对面出现「胡」按钮时就是 5，
        #   之前不在列表里，被当成结算直接清空了（用户实测「打着打着就本局已结束」）。
        IN_GAME_STATES = (3, 4, 5, 8)
        self.in_game = self.t_state in IN_GAME_STATES

        # 只要不是「局中」，一律清空（结算 6 / 平局 -1 / 其他中间态）
        if not self.in_game:
            self._clear_round()
            return

        # 庄家：tData.zhuang 是**座位号**。
        # 口径（用户要求）：只有对家是庄，底牌和首出才有意义 —— 那是庄家先亮/先出的牌。
        # 对家非庄时这两个位置留空，别拿自己的牌冒充对方的。
        z = live.get("zhuang")
        self.zhuang = int(z) if isinstance(z, int) else None
        self.opp_is_zhuang = (self.zhuang is not None and self.zhuang == self.opp_seat)

        # 底牌：先看醒牌(xingCard)，没有就用财牌数组(caiPaiArr)的第一个。
        # 只有对家是庄才记 —— 那时底牌才是「对方的」信息。
        self.bottom = None

        # ★ 配对策略：差分定「这一轮到底该记几张」，事件定「顺序」。
        #   不能各自独立记账 —— 胡牌那一下事件会重发，实测会出现 [1,4,105,4]。
        opp = self._opp_seat_data(live)
        cur_put = [int(c) for c in (opp.get("put") or [])] if opp is not None else []

        if not self._primed:
            # 第一轮：_prev_put 还是空的，全量快照会把历史出牌都算成"新增"。
            # 这时只能按事件配额走，历史牌本来就没有可配的门子。
            self._primed = True
            quota = Counter()
            for e in evs:
                d = e.get("d") or {}
                if e.get("e") == "MJPut" and opp is not None:
                    try:
                        if int(d.get("uid") or 0) == self.opp:
                            quota[int(d.get("card") or 0)] += 1
                    except (TypeError, ValueError):
                        pass
            quota.pop(0, None)
        else:
            quota = Counter(_added(self._prev_put, cur_put))

        # ① 按事件顺序走：门子立即建行，出牌消耗配额
        for e in sorted(evs, key=lambda x: x.get("t") or 0):
            self._on_event(e, quota, opp)

        # ② 配额没用完的，从差分补（事件漏发的那些）
        for c, n in list(quota.items()):
            for _ in range(n):
                self._on_put(c, 0)
        quota.clear()

        self._prev_put = cur_put

        opp = self._opp_seat_data(live)
        if opp is not None:
            self._opp_all_puts = [int(c) for c in (opp.get("put") or [])]

        self._sync_from_snapshot(live)

        # ★ 底牌必须在**事件处理之后**算：开局第一条 newCard 的 lastPut 才是底牌，
        #   放在事件前面算的话那时还是空的。
        self._compute_bottom(live)

        # ★ 结算立即清空：上一局的门子/出牌/底牌/首出一律不留。
        #   用户要求「这局结束马上清空，不记录任何数据」——所以连快照也不补。
        if self.t_state == 6:
            self.melds.clear()
            self.puts.clear()
            self.first_put = None
            self.bottom = None
            self.pending = None
            self._prev_put = []

    def _clear_round(self) -> None:
        self.timeline = []
        self.melds.clear()
        self.puts.clear()
        self.first_put = None
        self.bottom = None
        self.pending = None
        self._prev_put = []
        # ★ 补：这些同样是「局内」状态。漏清会导致下一局显示上一局的底牌，
        #   而且 _opened=True 会让新局的 newCard 不再更新底牌（永不刷新）。
        self.bottom_ev = None
        self.bottom_self = None
        self._opened = False
        self.dealt = []
        self._opp_all_puts = []

    def _on_event(self, e: Dict[str, Any], quota: Optional[Counter] = None,
                  opp_snapshot: Optional[Dict[str, Any]] = None) -> None:
        """处理一条事件。出牌要消耗 quota 里的一份额度，用完的不记（重发）。"""
        name = e.get("e")
        d = e.get("d") or {}

        # ★ mjhand：发牌事件。d.mjhand 是**未排序的发牌顺序**（sData 里那份是
        #   排过序的，所以之前从 sData 取 hand[20] 永远取不到底牌）。
        #   庄家 21 张、闲家 20 张，多出来的第 21 张就是开局亮出来的底牌。
        # ★ newCard：每摸一张都会发。**本局第一条**的 lastPut 就是庄家亮出来的
        #   底牌 —— 实测对面庄那局，newCard{"lastPut":10,"mjput":[],"cardNext":41}
        #   正好对上「底牌十」，紧接着的 MJPut 才是首出。
        if name == "newCard":
            if not self._opened:
                self._opened = True
                lp = d.get("lastPut")
                try:
                    iv = int(lp)
                    if 1 <= iv <= 110:
                        self.bottom_ev = iv
                except (TypeError, ValueError):
                    pass
            return

        if name == "mjhand":
            hand = d.get("mjhand") or []
            try:
                cards = [int(c) for c in hand if c]
            except (TypeError, ValueError):
                cards = []
            self.dealt = cards
            if len(cards) > 20:
                # 自己当庄：发牌序列的最后一张 = 底牌
                self.bottom_self = cards[20]
            return
        uid = d.get("uid")
        if uid is None or self.opp is None:
            return
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return
        if uid not in (self.me, self.opp):
            return
        owner = "me" if uid == self.me else "opp"

        # ★ 回合流水：喂给 AI 的「过程」——不只是最终状态，还有先后顺序
        if name in ("MJPut", "MJPeng", "MJChi", "MJGang"):
            try:
                _c = int(d.get("card") or 0)
            except (TypeError, ValueError):
                _c = 0
            ent = {"n": len(self.timeline) + 1, "who": owner}
            if name == "MJPut":
                ent["act"] = "put"
            else:
                # 按事件名称记原始动作；不能用上一条门子的 pending 猜类型。
                ent["act"] = {"MJPeng": "peng_or_wei", "MJChi": "chi_or_jiao",
                              "MJGang": "gang"}[name]
            ent["card"] = _c
            self.timeline.append(ent)
        try:
            if int(uid) != self.opp:
                return
        except (TypeError, ValueError):
            return

        t = e.get("t") or 0
        seat = self._seat_of(uid)

        if name == "MJPeng":
            card = int(d.get("card") or 0)
            source = d.get("from")
            if card and source is not None and opp_snapshot is not None:
                kind = "wei" if source == seat else "peng"
                key = "peng1" if kind == "wei" else "peng"
                # 请求或尚未落地的事件不算成牌；后续快照会补齐成功的门子。
                if card in (opp_snapshot.get(key) or []):
                    self._add(kind, [card] * 3, t)
            return

        if name == "MJChi":
            cards = [int(c) for c in (d.get("mjchi") or []) if c]
            actual = (opp_snapshot or {}).get("chi") or []
            if len(cards) >= 3 and any(Counter(cards[:3]) == Counter(actual[i:i + 3])
                                       for i in range(0, len(actual) - 2, 3)):
                self._add_chi(cards[:3], t)
            return

        if name == "MJGang":
            card = int(d.get("card") or 0)
            if card:
                kind = "ti" if d.get("from") == seat else "pao"
                self._add(kind, [card] * 4, t)
            return

        if name == "MJPut":
            card = int(d.get("card") or 0)
            if not card:
                return
            if quota is None:
                self._on_put(card, t)
                return
            if quota.get(card, 0) > 0:
                quota[card] -= 1
                if quota[card] <= 0:
                    del quota[card]
                self._on_put(card, t)
            # 配额用完 = 这张是重发，跳过

    def _add_chi(self, cards: List[int], t: int) -> None:
        """吃数组里可能是顺子，也可能是绞牌，交给 rules 判。"""
        try:
            name, _xi, _tip = rules.classify(cards, "other")
            kind = CN2EN.get(name, "chi")
        except Exception:
            kind = "chi"
        self._add(kind, cards, t)

    def _add(self, kind: str, cards: List[int], t: int, owner: str = "opp") -> None:
        """新增一个门子。**只 append，不合并、不重排**。

        用户要求：
        1. 门子按出现顺序排列，位置不能跳
        2. 手里 3 张（偎/坎）之后又进一张同值的（提），要**单独占一行**，
           不能把原来的 3 张并成 4 张 —— 那样会丢掉「中间那一次成型」的信息
        """
        # 完全相同的门子不重复记（事件重发 / 快照重复补）
        for m in self.melds:
            if m.kind == kind and m.cards == cards:
                if m.syn and m.after is None:
                    m.syn = False
                    m.t = t or m.t
                    self.pending = m
                return
        self.melds.append(Meld(kind, cards, t, owner=owner))
        self.pending = self.melds[-1]

    def _on_put(self, card: int, t: int) -> None:
        if self.first_put is None and self.opp_is_zhuang:
            self.first_put = card
        self.puts.append(card)
        if self.pending is not None:
            self.pending.after = card
            self.pending.after_at = t
            self.pending = None

    # -------------------------------------------------------------- 快照兜底
    def _sync_from_snapshot(self, live: Dict[str, Any]) -> None:
        """钩子丢了时事件流会断，用 sData 数组把门子补齐。

        只补「没有的」，不覆盖已有的 —— 已有的是带 after 的，快照里没有这个信息。
        """
        s = self._opp_seat_data(live)
        if s is None:
            return

        have = {}
        for m in self.melds:
            have.setdefault((m.kind, m.card), m)

        def ensure(kind: str, cards: List[int]) -> None:
            if not cards:
                return
            if kind in ("chi", "jiao"):
                for m in self.melds:
                    if m.kind == kind and m.cards == cards:
                        return
                self.melds.append(Meld(kind, cards, 0, True))
                return
            # 只看完全相同的；同值不同牌型（偎 vs 提）是两回事，各占一行
            if (kind, cards[0]) in have:
                return
            self.melds.append(Meld(kind, cards, 0, True))

        for c in s.get("gang0") or []:
            ensure("ti", [int(c)] * 4)
        for c in s.get("gang1") or []:
            ensure("pao", [int(c)] * 4)
        for c in s.get("peng") or []:
            ensure("peng", [int(c)] * 3)
        for c in s.get("peng1") or []:
            ensure("wei", [int(c)] * 3)
        chi = [int(c) for c in (s.get("chi") or [])]
        for i in range(0, len(chi) - 2, 3):
            cards3 = chi[i:i + 3]
            try:
                name, _xi, _tip = rules.classify(cards3, "other")
                kind = CN2EN.get(name, "chi")
            except Exception:
                kind = "chi"
            ensure(kind, cards3)
        for c in s.get("lie") or []:
            ensure("lie", [int(c)])

        # 快照补进来的门子没有 pending（事件没认领过），它**之后**打出的牌就配不上。
        # 挂载时已经成型的门子，after 本来就拿不到（历史事件没抓），但接下来的
        # 一手应该能接上 —— 让最后这个还没配出牌的门子成为 pending。
        if self.pending is None and self.melds:
            last = self.melds[-1]
            if last.after is None:
                self.pending = last


    def _compute_bottom(self, live: Dict[str, Any]) -> None:
        """底牌 = 庄家多抓的那张（第 21 张）。

        ★ 只有「对面是庄」才记：自己庄时那张是我自己的牌，对记牌器没意义。
        优先级：开局第一条 newCard 的 lastPut（实测最准）> 快照里的候选。
        """
        if not self.opp_is_zhuang:
            self.bottom = None
            return

        bottom = None
        if self.bottom_ev:
            bottom = self.bottom_ev
        if bottom is None:
            opp = self._opp_seat_data(live)
            if opp:
                hand = opp.get("hand") or []
                if len(hand) > 20:
                    try:
                        cand = int(hand[20])
                        if 1 <= cand <= 110:
                            bottom = cand
                    except (TypeError, ValueError):
                        pass
        if bottom is None:
            xc = live.get("xingCard")
            if isinstance(xc, int) and xc > 0:
                bottom = xc
        if bottom is None:
            for c in (live.get("caiPaiArr") or []):
                try:
                    iv = int(c)
                except (TypeError, ValueError):
                    continue
                if iv > 0:
                    bottom = iv
                    break
        self.bottom = bottom


    # ------------------------------------------------------------------- 统计
    def summary(self) -> Dict[str, Any]:
        """一局的完整记录（换局/退出时落盘用）。"""
        return {
            "tableid": self.tableid,
            "playNum": self.play_num,
            "me": self.me,
            "opp": self.opp,
            "zhuang": self.zhuang,
            "oppZhuang": bool(self.opp_is_zhuang),
            "bottom": self.bottom,
            "first": self.first_put,
            "winner": self.winner,
            "melds": [{"kind": m.kind, "cards": m.cards, "after": m.after}
                      for m in self.melds],
            "puts": list(self.puts),
            "left": list(self.left),
        }

    def remaining(self) -> Dict[int, int]:
        """每个牌值还剩几张没露过面（含墩里 + 对手手牌）。

        一副 80 张，每种牌 4 张。已见 = 双方手牌 + 明牌 + 弃牌。
        choupai 抽走的那部分无从得知，所以算出来的「还剩」可能略大于真实池子，
        这是记牌器的固有误差，不是 bug。
        """
        seen: Counter = Counter()
        for st in self._seats:
            for c in st.get("hand") or []:
                seen[int(c)] += 1
            for c in st.get("put") or []:
                seen[int(c)] += 1
            for c in st.get("chi") or []:
                seen[int(c)] += 1
            for c in st.get("lie") or []:
                seen[int(c)] += 1
            for key, n in (("peng", 3), ("peng1", 3), ("gang0", 4), ("gang1", 4)):
                for c in st.get(key) or []:
                    seen[int(c)] += n

        out: Dict[int, int] = {}
        for base in range(1, 11):
            for v in (base, base + 100):
                out[v] = max(0, 4 - seen.get(v, 0))
        return out

    # ------------------------------------------------------------------- 输出
    def ui_state(self) -> Dict[str, Any]:
        rows = []
        ended = not self.in_game    # 非局中一律算「本局已结束」
        if not ended:
            for m in self.melds:
                rows.append(m.to_ui())
        return {
            "inGame": bool(self.in_game),
            "ended": ended,
            "tState": self.t_state,
            "bottom": self.bottom,
            "first": self.first_put,
            "oppZhuang": bool(self.opp_is_zhuang),
            "zhuang": self.zhuang,
            "rows": rows,
            "left": self.left,
            "winner": self.winner,
            "remain": self.remaining(),
            "oppPuts": list(self._opp_all_puts),           # 对手已打出的牌（确定值）
            "deckLeft": self.deck_left(),                  # 墩里还剩几张（精确）
        }

    def deck_left(self) -> int:
        """墩里还剩几张。

        池 = 80 - choupai（默认抽 20 → 60），发牌指针 cardNext 之前都已派发，
        剩下没发的就是 60 - cardNext。精确值，不依赖任何推算。
        """
        total = 80 - (self.choupai or 20)
        cn = self._card_next or 0
        return max(0, total - cn)

    def _hand_analysis(self) -> Dict[str, Any]:
        """替 AI 把牌型算清楚：门子数、对子/坎、能凑的句、还差几方。

        模型自己数牌容易错、还爱反复推演，把确定值算好直接给它判断。
        """
        me_s = None
        for seat in self._seats:
            if seat.get("uid") == self.me:
                me_s = seat
                break
        hand = [int(c) for c in ((me_s or {}).get("hand") or []) if isinstance(c, int)]
        cnt: Counter = Counter(hand)
        meld_n = len(self.melds)

        pairs = sorted([c for c, n in cnt.items() if n == 2])
        triples = sorted([c for c, n in cnt.items() if n >= 3])

        # 能凑的「一句话」：一二三 / 二七十（大小字各一套）
        runs = []
        for base in ((1, 2, 3), (2, 7, 10)):
            for off in (0, 100):
                trio = [b + off for b in base]
                if all(cnt.get(c, 0) > 0 for c in trio):
                    runs.append(trio)

        return {
            "meldCount": meld_n,                      # 已成型门子数
            "needMelds": max(0, 7 - meld_n),          # 还差几方到胡
            "pairs": pairs,                            # 对子（可碰/偎）
            "triples": triples,                        # 坎（可提/跑）
            "possibleRuns": runs,                      # 手里能凑出的一句话
            "singleCount": sum(1 for n in cnt.values() if n == 1),   # 孤张数量
            "seen": self._seen_cards(),
        }

    def _seen_cards(self) -> Dict[int, int]:
        """已经露面的牌各几张（我的手牌 + 双方门子 + 双方弃牌）。

        对手暗牌、墩里剩余、抽走的 20 张都未知，所以这只是「已确认已现」，
        用来判断某张牌还剩几张可等（存活率）。
        """
        seen: Counter = Counter()
        for seat in self._seats:
            for c in seat.get("hand") or []:
                if isinstance(c, int):
                    seen[c] += 1
            for c in seat.get("put") or []:
                if isinstance(c, int):
                    seen[c] += 1
            for c in seat.get("chi") or []:
                if isinstance(c, int):
                    seen[c] += 1
            for key, n in (("peng", 3), ("peng1", 3), ("gang0", 4), ("gang1", 4)):
                for c in seat.get(key) or []:
                    if isinstance(c, int):
                        seen[c] += n
        # 每种牌最多 4 张，超过说明数据冲突，夹到 4 避免误导 AI
        return {c: min(n, 4) for c, n in seen.items() if n}

    def ai_state(self) -> Dict[str, Any]:
        """只向 AI 提供当前快照中能确认的信息，不发送推测或暗牌。"""
        def cards(value):
            if not isinstance(value, list):
                return None
            if any(type(c) is not int or not (1 <= c <= 10 or 101 <= c <= 110)
                   for c in value):
                return None
            return list(value)

        def snapshot_melds(seat, keys):
            if not seat:
                return None
            known = seat.get("knownMeldFields")
            if isinstance(known, dict):
                if any(known.get(key) is not True for key in keys):
                    return None
            elif seat.get("meldsKnown") is False:
                return None
            arrays = {key: cards(seat.get(key)) for key in keys}
            if any(v is None for v in arrays.values()):
                return None
            if "chi" in arrays and len(arrays["chi"]) % 3:
                return None
            melds = []
            for key, kind, size in (("peng", "peng", 3), ("peng1", "wei", 3),
                                    ("gang0", "ti", 4), ("gang1", "pao", 4)):
                for card in arrays.get(key, []):
                    melds.append({"kind": kind, "cards": [card] * size})
            chi = arrays.get("chi", [])
            for i in range(0, len(chi), 3):
                group = chi[i:i + 3]
                kind = CN2EN.get(rules.classify(group, "other")[0])
                if kind not in ("chi", "jiao"):
                    return None
                melds.append({"kind": kind, "cards": group})
            for card in arrays.get("lie", []):
                melds.append({"kind": "lie", "cards": [card]})
            return melds

        # gs_install.js 按 uids 数组下标生成 seat，必须同时核对 uid 和 seat。
        seat_by_index = {}
        duplicates = set()
        for seat in self._seats:
            try:
                index = seat.get("seat")
                if (type(index) is int and 0 <= index < len(self.uids)
                        and int(seat.get("uid")) == self.uids[index]):
                    if index in seat_by_index:
                        duplicates.add(index)
                    else:
                        seat_by_index[index] = seat
            except (AttributeError, TypeError, ValueError):
                pass
        for index in duplicates:
            seat_by_index.pop(index, None)
        my_seat = self._ai_my_seat
        me_s = seat_by_index.get(my_seat)
        if me_s is None:
            my_seat = None
        opp_indices = [i for i in seat_by_index if i != my_seat]
        opp_s = seat_by_index[opp_indices[0]] if my_seat is not None and len(opp_indices) == 1 else None

        my_hand = cards(me_s.get("hand")) if me_s and me_s.get("handKnown") is not False else None
        my_melds = snapshot_melds(me_s, ("peng", "peng1", "gang0", "gang1", "chi", "lie"))
        # 对手的偎、提可能含暗牌；只发送公开的碰、跑、吃、摆。
        opp_melds = snapshot_melds(opp_s, ("peng", "gang1", "chi", "lie"))
        my_put = cards(me_s.get("put")) if me_s and me_s.get("putKnown") is not False else None
        opp_put = cards(opp_s.get("put")) if opp_s and opp_s.get("putKnown") is not False else None

        return {
            "me": {
                "hand": my_hand,
                "handKnown": my_hand is not None,
                "puts": my_put,
                "melds": my_melds,
                "meldsKnown": my_melds is not None,
            },
            "opp": {
                "puts": opp_put,
                "visibleMelds": opp_melds,
            },
            "deckLeft": max(0, 80 - self.choupai - self._card_next) if self._deck_left_known else None,
            "zhuang": self.zhuang,
            "tState": self.t_state,
            "curPlayer": self._cur_player,
            "mySeat": my_seat,
            "tableid": self.tableid,
            "playNum": self.play_num,
            "cardNext": self._card_next if self._card_next_known else None,
            "lastPut": self._last_put,
            "lastPutPlayer": self._last_put_player,
            "lastDrawPlayer": self._last_draw_player,
            # 按钮可见即本人有选择（tState=4 也会弹按钮，不能只看 tState=5）
            "responsePlayer": (my_seat if isinstance(self._visible_actions, list)
                              and self._visible_actions else None),
            "availableActions": (sorted(set(self._visible_actions) & {"peng", "hu", "pass", "eat"})
                                if isinstance(self._visible_actions, list)
                                and self._visible_actions else None),
            "analysis": self._hand_analysis(),
            "roomRules": dict(self._room_rules),
        }

    def debug(self) -> str:
        parts = []
        for m in self.melds:
            parts.append("%s%s→%s" % (KIND_NAME.get(m.kind, m.kind), m.cards, m.after))
        return " | ".join(parts) or "(无门子)"
