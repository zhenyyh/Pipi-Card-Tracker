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
        self.pass_chi: List[int] = []   # MJPass.mjpassChi 原样留档（语义未坐实，只记录）
        self.bottom_self: Optional[int] = None   # 自己当庄时的底牌
        self.bottom_ev: Optional[int] = None     # 开局 newCard 的 lastPut（最准）
        self._opened = False                     # 本局是否已见过第一条 newCard

        # ---- 读牌信号：对手「摸牌→出牌」的思考时长 --------------------------
        # 数据来源：newCard 每摸一张都会发（走 netCallBack，带 Date.now() 时间戳），
        # 所以「摸到牌的那一刻」是可观测的；下一次 MJPut 到达时两者相减，
        # 就是对手从拿到牌到点出牌的纯思考时间。
        # 注意：_on_event 每轮会全量重放最近 240 条事件，必须用事件序号 q 去重，
        # 不能直接累加，否则同一次出牌会被算很多遍。
        self._think_seq = 0                      # 已处理到的事件序号
        self._think_key = (0, 0)                 # 去重游标 (时间戳, 序号)
        self._draw_t = 0                         # 最近一次摸牌的时刻（ms）
        self._last_put_t = 0                     # 上一张牌打出的时刻（gap 兜底用）
        self._gap: List[int] = []                # 「上一张牌→对手出牌」间隔样本
        self._think: Dict[str, List[int]] = {"opp": []}   # 思考时长样本（ms）

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
                    if self.opp is not None and self.opp != u:
                        # 换对手了：旧的思考时长样本属于上一个人，留着会污染判断
                        self._think["opp"].clear()
                        self._gap.clear()
                        self._last_put_t = 0
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
        self.pass_chi = []
        self._opp_all_puts = []

    def _track_think(self, e: Dict[str, Any], name: str) -> None:
        """记录对手「摸牌→出牌」的间隔，作为读牌信号。

        newCard 每摸一张都会发（走 netCallBack，带 Date.now() 时间戳），
        所以「摸到牌的那一刻」是可观测的；下一次对手 MJPut 到达时相减，
        就是他从拿到牌到点出牌的纯思考时长（不含网络往返的猜测成分）。

        ★ 必须去重：_on_event 每轮会把最近 240 条事件全量重放。用 (t, q) 组合做游标——
          重放时这两个值不会超过已处理的最大值；队列清空重开时 t 继续往前走，
          游标自然越过，两种情形都能正确处理（只看 q 会被「重放＝序号倒退」骗到）。
        """
        try:
            seq = int(e.get("q") or 0)
            t = int(e.get("t") or 0)
        except (TypeError, ValueError):
            return
        if not seq or not t:
            return
        key = (t, seq)
        if key <= self._think_key:
            return
        self._think_key = key

        if name == "newCard":
            self._draw_t = t
            return
        if name != "MJPut" or self.opp is None:
            return
        try:
            uid = int((e.get("d") or {}).get("uid") or 0)
        except (TypeError, ValueError):
            return
        # —— 兜底信号：上一张牌到对手这张牌的间隔（含摸牌，总是可用）——
        gap = t - self._last_put_t if self._last_put_t else 0
        self._last_put_t = t
        if uid == self.opp and 200 <= gap <= 60000:
            self._gap.append(gap)
            if len(self._gap) > 12:
                self._gap.pop(0)
        if uid != self.opp:
            # 自己出牌：之前那次摸牌作废，否则对手随后碰/吃直接出牌会被误算
            self._draw_t = 0
            return
        if not self._draw_t:
            return
        dt = t - self._draw_t
        self._draw_t = 0                 # 一次摸牌只对应一次出牌
        # 200ms 以下多是 UI 动画或事件重发；60s 以上是挂机/掉线，都不算思考
        if 200 <= dt <= 60000:
            samples = self._think["opp"]
            samples.append(dt)
            if len(samples) > 12:
                samples.pop(0)

    def _note_pass(self, d: Dict[str, Any]) -> None:
        """把 `mjpassChi` 原样留档 —— 这是全工程唯一一份「过张」证据。

        `MJPut` / `MJPass` 的负载里都带这个数组：一张牌被打出去、没人吃碰胡，
        它就进这个列表。语义上等于「这张牌对某一家没用」，是**能削弱对手手牌
        假设的公开证据**，比思考时长硬。

        ★ 只记录，不进推断：实测同一局里不同事件带回的版本长度和内容对不上
          （座位 0 的 `MJPut` 带的是全局列表，座位 1 的只含自己打过的），
          到底按全局还是按座位算还没坐实。没坐实的口径进 AI 就是编造，
          所以这里只取最长的那一份留档，等 `tools/probe_pass.py` 把语义定下来
          再接推断层。
        """
        pc = d.get("mjpassChi")
        if not isinstance(pc, list):
            return
        vals = []
        for c in pc:
            try:
                iv = int(c)
            except (TypeError, ValueError):
                return
            if 1 <= iv <= 110:
                vals.append(iv)
        if len(vals) > len(self.pass_chi):
            self.pass_chi = vals

    def _on_event(self, e: Dict[str, Any], quota: Optional[Counter] = None,
                  opp_snapshot: Optional[Dict[str, Any]] = None) -> None:
        """处理一条事件。出牌要消耗 quota 里的一份额度，用完的不记（重发）。"""
        name = e.get("e")
        d = e.get("d") or {}
        self._track_think(e, name)
        self._note_pass(d)

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
            # ★ caiPaiArr 在旧流里是 {uid: 牌值} 的字典，直接 for 会遍历出 uid，
            #   int("5513337") > 0 就把 uid 当成底牌记下（实测旧 fixture 踩到）。
            #   字典只取 values，数组照旧。
            cai = live.get("caiPaiArr")
            if isinstance(cai, dict):
                cai = list(cai.values())
            for c in (cai or []):
                try:
                    iv = int(c)
                except (TypeError, ValueError):
                    continue
                if 1 <= iv <= 110:
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
            "passed": list(self.pass_chi),
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

    # --------------------------------------------------------------- 总牌守恒
    # 每一条都是踩出来的，不要删：
    #
    # ① 池不是 80。二人房 choupai=20 把 20 张抽走，池 = 60。
    #    拿 80 去对账差 20，会误判成「采集漏了」。
    # ② **已易主的那张要剪掉**。胡牌那张从出牌方的 put 转进赢家手牌，
    #    但 put 里不会把它移除 —— 于是它同时算进「赢家手牌」和「出牌方弃牌」，
    #    凭空多 1 张。实测第 10 局：lastPut=105 在对手 put=[9,101,105] 里，
    #    同时也在赢家手牌里，61 就是这 1 张。判据见 _clip_reowned()。
    # ③ 偎(peng1) / 提(gang0) / 跑(gang1) 在数组里**一个元素 = 一组**，不是一张：
    #    peng1 一组 3 张，gang0 / gang1 一组 4 张。
    #    把 [109,107] 当 2 张算，会少 4 张。
    # ④ 吃 / 绞 在 mjchi 里 3 张一组，按 len // 3 拆。
    # ⑤ 摆火 mjlie 一个元素 = 1 张（单张亮出）。
    # ⑥ 局中只有本人 hand 是真的；对手 handN 可能是 0。缺项要报 unknown，
    #    不能拿 0 当真实值去凑 —— 凑出来的守恒是假的。
    # ⑦ 只有结算（tState=6）两家 hand 都有值，这时才能算完整账。
    # ⑧ deck_left 要 clamp 到 0：旧房间 cardNext 可能超过池。
    MELD_UNIT = {"peng": 3, "peng1": 3, "gang0": 4, "gang1": 4, "lie": 1}

    def _seat_cards(self, seat: Dict[str, Any]) -> Dict[str, Any]:
        """一家的「手 / 弃 / 明」三堆各几张，按上面 ③④⑤⑥ 的口径。"""
        hand = seat.get("hand") or []
        melds = 0
        for key, unit in self.MELD_UNIT.items():
            melds += len(seat.get(key) or []) * unit
        chi = seat.get("chi") or []
        melds += (len(chi) // 3) * 3
        # 空手牌 / 字段不存在 / 明确标记未知，三件事要分开：
        #   显式 False        → 未知
        #   显式 True         → 已知（哪怕 0 张，胡完把手牌全打进明牌是合法的）
        #   没有这个键（旧快照）→ 有内容就算已知，空的按未知（分不清 0 和没读到）
        flag = seat.get("handKnown")
        if flag is False:
            known = False
        elif flag is True:
            known = True
        else:
            known = len(hand) > 0
        return {
            "hand": len(hand),          # 手牌张数
            "handKnown": known,
            "puts": len(seat.get("put") or []),
            "melds": melds,
        }

    def _clip_reowned(self):
        """找出「换了主人却被数两遍」的那张牌。返回 [(牌值, 座位号)]。

        判据（不猜）：`lastPut` 出现在**赢家手牌**里 —— 说明这张是胡牌那张，
        已经被赢家拿走了。既然如此，它就不可能还留在任何人的弃牌堆里；
        谁家的 put 里还有它，就从谁家剪掉一张。

        这条同时覆盖 README 里更早记下的那个变体
        「lastPut 出现在非 lastPutPlayer 的堆里（已易主）」：
        自摸时 lastPut 就是赢家自己摸的那张，不进任何人 put，自然不会剪。
        """
        lp = self._last_put
        if not isinstance(lp, int) or not (1 <= lp <= 110):
            return []
        if self.winner is None or self.winner < 0:
            return []

        # 赢家手牌里有没有这张（多重集比较，同值多张时只剪对应张数）
        win_seat = None
        for s in self._seats:
            if s.get("seat") == self.winner:
                win_seat = s
                break
        if win_seat is None:
            return []
        hand = [int(c) for c in (win_seat.get("hand") or []) if isinstance(c, int)]
        if lp not in hand:
            return []

        out = []
        for s in self._seats:
            if s.get("seat") == self.winner:
                continue                      # 赢家自己的 put 不会有这张
            if lp in [int(c) for c in (s.get("put") or []) if isinstance(c, int)]:
                out.append((lp, s.get("seat")))
        return out

    def _reowned_candidates(self):
        """哪些牌**可能**也「已经易主」—— 只报，不剪。

        除了胡牌那张，理论上吃 / 碰 / 绞 拿走的牌也可能留在出牌方的 put 里，
        同样会被数两遍。但目前**只有胡牌这条有实证**（第 10 局 105、multi4 的 109），
        吃碰这条没有干净的实机样本可判 —— round_kan 差 1 且剪不掉，就是这个形状。

        所以这里列出来供人判断，**不参与对账**。等攒到一局「吃/碰 之后正好对不上」
        的实机数据，再决定要不要把它们也剪掉。
        """
        seen, out = set(), []
        for a in self._seats:
            puts = [int(c) for c in (a.get("put") or []) if isinstance(c, int)]
            if not puts:
                continue
            for b in self._seats:
                if b.get("seat") == a.get("seat"):
                    continue
                groups = []
                for key, unit in (("peng", 3), ("peng1", 3), ("gang0", 4), ("gang1", 4)):
                    for c in (b.get(key) or []):
                        groups.append((key, int(c)))
                chi = [int(c) for c in (b.get("chi") or []) if isinstance(c, int)]
                for i in range(0, len(chi) - 2, 3):
                    groups.append(("chi", chi[i]))
                for kind, card in groups:
                    if kind == "chi":
                        hit = [c for c in chi if c in puts]
                        if not hit:
                            continue
                        card = hit[0]
                    elif card not in puts:
                        continue
                    key = (card, a.get("seat"), b.get("seat"))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"card": card, "from": a.get("seat"),
                                "to": b.get("seat"), "kind": kind})
        return out

    def conservation(self) -> Dict[str, Any]:
        """总牌守恒：手牌 + 弃牌 + 明牌 + 墩剩 = 池。

        返回的是**可对账的诊断**，不是一句 True/False：
        `diff == 0` 才算对得上；`unknown` 非空说明这一局根本算不了完整账
        （局中、或对手 hand 没下发），此时不要拿 diff 下结论。
        """
        pool = 80 - (self.choupai or 20)
        deck = max(0, pool - (self._card_next or 0))
        clips = self._clip_reowned()

        seats, unknown = [], []
        for s in self._seats:
            c = self._seat_cards(s)
            clipped = sum(1 for _card, seat_no in clips if seat_no == s.get("seat"))
            if not c["handKnown"]:
                # 手牌没下发 ≠ 手牌 0 张。局中对手就是这样，记成未知而不是 0。
                unknown.append({"seat": s.get("seat"), "why": "hand 未下发"})
            seats.append({
                "seat": s.get("seat"),
                "uid": s.get("uid"),
                "hand": c["hand"],
                "handKnown": c["handKnown"],
                "puts": c["puts"],
                "putsClipped": c["puts"] - clipped,   # 剪掉已易主那张之后
                "melds": c["melds"],
                "clipped": clipped,
            })

        total = sum(x["hand"] + x["putsClipped"] + x["melds"] for x in seats) + deck
        return {
            "pool": pool,
            "deckLeft": deck,
            "seats": seats,
            "clipped": clips,
            "candidates": self._reowned_candidates(),
            "sum": total,
            "diff": total - pool,
            "ok": (total == pool) and not unknown,
            "unknown": unknown,
        }

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

        # ---- 真孤张排序：把「先打哪张」算成一个确定顺序，别让模型自己排 ----
        # 模型排过两次，两次都在 7 和 106 之间摇摆，还拆过对子。排序是确定性的
        # 机械活，交给代码；模型只负责在 blacklist 之外挑第一个。
        seen = self._seen_cards()
        run_members = set()
        for r in runs:
            run_members.update(r)
        singles = [c for c, n in cnt.items()
                   if n == 1 and c not in run_members and c not in triples]

        def _order_key(c: int):
            num = c - 100 if c >= 100 else c
            seen_n = seen.get(c, 0)
            return (
                0 if seen_n >= 2 else 1,      # a. 已现 2~3 张的先打（剩得少，留着无用）
                0 if num in (1, 10) else 1,   # b. 边张先于中张（中张连接性好）
                0 if c < 100 else 1,          # c. 小字先于大字（宁小莫大）
                c,                             # d. 同档取牌值最小 —— 保证结论稳定
            )

        # ---- 合法吃法：也在代码里算好 ----
        # 实测模型会编「吃 3 拆不成句」来给 pass 找理由，而手里 1、2 明明能成 一二三。
        # 能吃不能吃是可枚举的事实，不该交给模型自由心证。
        eat_options = []
        lp = self._last_put
        if isinstance(lp, int) and 1 <= lp <= 110:
            num = lp - 100 if lp >= 100 else lp
            off = 100 if lp >= 100 else 0
            for base in ((1, 2, 3), (2, 7, 10)):
                if num in base:
                    others = [b + off for b in base if b != num]
                    if all(cnt.get(c, 0) > 0 for c in others):
                        eat_options.append([lp] + others)
            # 绞：手里两张「同点异大小写」，再来一张同值的就能成绞
            for c in (lp + 100, lp - 100):
                if 1 <= c <= 110 and cnt.get(c, 0) >= 2:
                    eat_options.append([lp, c, c])

        return {
            "meldCount": meld_n,                      # 已成型门子数
            "needMelds": max(0, 7 - meld_n),          # 还差几方到胡
            "pairs": pairs,                            # 对子（可碰/偎）
            "triples": triples,                        # 坎（可提/跑）
            "possibleRuns": runs,                      # 手里能凑出的一句话
            "singleCount": sum(1 for n in cnt.values() if n == 1),   # 孤张数量
            "putOrder": sorted(singles, key=_order_key),  # 出牌顺序（已排好，直接用）
            "eatOptions": eat_options,                 # 合法吃法（空 = 真吃不了）
            "seen": seen,
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
            "oppThink": self._think_summary(),
            "roomRules": self._room_rules_explained(),
        }

    def _think_summary(self) -> Optional[Dict[str, Any]]:
        """对手出牌前的耗时（秒）。

        两个来源，优先精确的：
        - src="draw"：从「对手摸到牌」算到「他出牌」，纯思考时间
        - src="gap" ：从「上一张牌」算起，含摸牌过程，只在 draw 样本不足时兜底

        样本少于 2 次返回 None —— 单个样本分不出「他平时就快」还是「这次碰巧快」，
        给出去只会让模型过度解读。宁可没有，不可误导。
        """
        think = list(self._think.get("opp") or [])
        if len(think) >= 2:
            src, s = "draw", think
        elif len(self._gap) >= 2:
            src, s = "gap", list(self._gap)
        else:
            return None
        return {
            "src": src,
            "last": round(s[-1] / 1000.0, 1),
            "avg": round(sum(s) / len(s) / 1000.0, 1),
            "recent": [round(x / 1000.0, 1) for x in s[-5:]],
            "n": len(s),
        }

    def _room_rules_explained(self) -> Dict[str, Any]:
        """把房间原始参数翻译成 AI 看得懂的口径。

        ★ 原样发 {"huziType":2,"limitScore":15,"playType":10000110} 等于没发——
        模型不认识这三个裸数字，而它们恰恰决定了「胡息要够多少才能胡」和
        「放炮要扣多少分」。含义来自 docs/FINDINGS.md §3.4 的客户端考证。
        """
        raw = dict(self._room_rules)
        huzi = raw.get("huziType")
        limit = raw.get("limitScore")
        # 只翻译已考证确定的部分；拿不准的保留原值，不臆测含义。
        name = {2: "娄底放炮罚"}.get(huzi)
        out: Dict[str, Any] = {
            "ruleName": name or "未考证(huziType=%s)" % huzi,
            "minHuXi": limit if type(limit) is int else None,
        }
        if raw:
            out["raw"] = raw
        return out

    def debug(self) -> str:
        parts = []
        for m in self.melds:
            parts.append("%s%s→%s" % (KIND_NAME.get(m.kind, m.kind), m.cards, m.after))
        return " | ".join(parts) or "(无门子)"
