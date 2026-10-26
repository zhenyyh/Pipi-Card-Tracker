# -*- coding: utf-8 -*-
"""牌值 / 牌型 / 胡息 —— 全部按实测口径，不猜。

牌值编码
--------
`组别 × 100 + 点数`，点数 1..10。

    1..10    小字：一 二 三 四 五 六 七 八 九 十
    101..110 大字：壹 贰 叁 肆 伍 陸 柒 捌 玖 拾

20 种 × 4 张 = 80 张。★ 大写「六」游戏里显示成繁体「陸」。

红张
----
`jsclient.phz.isRed()` 逐一问出来的：**二七十，大小写都红**，6 种 × 4 = 24 张。

牌型
----
提 4 张（全在自己手上，明晃晃摊开）
跑 4 张（3 张在手 + 别人打出第 4 张）
偎 3 张（手上 2 张 + 自己摸到第 3 张，**只有 1 张明、2 张红底牌背**）
碰 3 张（别人打出 + 手上 2 张）
吃 3 张（同大小写连号）
坎 3 张（自己凑齐的 3 张同值，服务端不发事件）
绞 3 张（一对 + 一张同点异大小写，0 胡；规则里叫「绞牌」）

★ 判「提 vs 跑」「碰 vs 偎」**不要靠 from 猜**。客户端 sData 里本来就是
六个独立数组（见 docs/FINDINGS.md）：

    mjpeng   → 碰（接别人的）      mjpeng1  → 偎（自己摸的）
    mjgang0  → 提（4 张全在手）    mjgang1  → 跑（3 + 1）
    mjchi    → 吃 + 绞牌           mjlie    → 摆火（单张，0 胡）

所以本模块的 `classify()` 只做纯形状判定，来源（self/other）由调用方从
**哪个数组来的**直接给出，而不是靠 `from` 字段反推。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 基础

XIAO = "一二三四五六七八九十"
DA = "壹贰叁肆伍陸柒捌玖拾"

RED_POINTS = (2, 7, 10)          # isRed() 实测：二七十 大小写都红
DECK_SIZE = 80

# 胡息：(牌型, 是否大字) -> 胡息
XIXI = {
    ("提", 1): 12, ("提", 0): 9,
    ("跑", 1): 9,  ("跑", 0): 6,
    ("偎", 1): 6,  ("偎", 0): 3,
    ("碰", 1): 3,  ("碰", 0): 1,
}

# 特殊顺子：贰柒拾 / 壹贰叁。★ 胡息**分大小写**，这是实测钉死的（见 docs/FINDINGS.md）。
# help.txt 第 15 条原话：「一句话小 1、2、3 或 2、7、10 有 3 胡息，大 1、2、3 或
# 2、7、10 有 6 胡息」—— 小字 3，大字 6。以前一张表不分大小写一律给 6 是错的。
SEQ_SPECIAL = {
    (2, 7, 10): (3, 6),      # (小字, 大字)
    (1, 2, 3): (3, 6),
}

# 坎：起手就有 3 张同值牌，**服务端不发任何事件、也不进 sData 的任何数组**，
# 只能从终局手牌里算。
#
# ★ 是否计息：邵阳剥皮的文本自相矛盾（胡牌条件段没列坎，胡息表里又有）。
#   2026-10-26 15:29 实测定案 —— 座位1 手牌两坎（坎拾 大字 / 坎一 小字），
#   明牌 3+3+0 + 手牌 6+3+6+0 = 21 == origWinNum。坎按 0 计只有 9~12，
#   怎么都对不上；所以**本玩法计坎息，大 6 小 3**。
KAN_XI = {
    1: 6,
    0: 3,
}
COUNT_KAN = True


def cname(c) -> str:
    """牌值 → 单字名。"""
    try:
        c = int(c)
    except (TypeError, ValueError):
        return str(c)
    if c <= 0:
        return "—"
    grp, pt = divmod(c, 100)
    if 1 <= pt <= 10:
        return (XIAO if grp == 0 else DA)[pt - 1]
    return "?%d" % c


def cnames(lst, sep: str = "") -> str:
    return sep.join(cname(x) for x in (lst or []))


def pt(c) -> int:
    """点数（1..10），丢掉大小写。"""
    return int(c) % 100


def is_big(c) -> int:
    return 1 if int(c) >= 100 else 0


def is_red(c) -> bool:
    return pt(c) in RED_POINTS


def red_count(lst) -> int:
    return sum(1 for c in (lst or []) if is_red(c))


def sort_cards(cards):
    """按 点数 → 大小写 排序，牌谱读起来顺眼。"""
    return sorted((int(c) for c in cards), key=lambda x: (pt(x), is_big(x)))


# ---------------------------------------------------------------- 牌型判定

KIND_TI, KIND_PAO = "提", "跑"
KIND_WEI, KIND_PENG = "偎", "碰"
KIND_CHI, KIND_KAN = "吃", "坎"
KIND_DA = "搭"          # 旧名，保留做兼容；规则里叫「绞牌」，见 KIND_JIAO
KIND_LIE, KIND_JIANG = "摆", "将"
KIND_JIAO = "绞"


def seq_key(cards):
    """3 张能组成门子就返回 (点数序列)，否则 None。要求同大小写。

    连号（差 2）+ 两个特殊顺 贰柒拾 / 壹贰叁。
    ★ 贰柒拾 跨度是 8，**不是连号** —— 只按「差 2」判会把它当垃圾丢掉，
      而它恰恰是最常见的 6 胡门子。这个 bug 同时存在于 classify() 和
      split_hand()，2026-10-26 一局实测抓出来的。
    """
    if len(cards) != 3:
        return None
    if len(set(is_big(c) for c in cards)) != 1:
        return None
    p = sorted(pt(c) for c in cards)
    if len(set(p)) != 3:
        return None
    key = tuple(p)
    if p[2] - p[0] == 2 or key in SEQ_SPECIAL:
        return key
    return None


def seq_xi(key, big: int) -> int:
    """特殊顺的胡息：小字 3 / 大字 6。普通顺 0。"""
    pair = SEQ_SPECIAL.get(tuple(key))
    if not pair:
        return 0
    return pair[1] if big else pair[0]


def classify(group, origin: str | None = None):
    """判定一组明牌。

    group  3 或 4 张牌值
    origin 'self' = 自己摸成（提/偎/坎）｜'other' = 接别人打的（碰/跑）

    返回 (牌型, 胡息, 说明)。`origin` 只在「4 张」和「3 张同值」时用来分
    提/跑、偎/碰；如果调用方是从 `mjgang0/mjgang1` 这类**确定的数组**取来的，
    可以直接传 None 并自己覆盖牌型，这里不会瞎猜。
    """
    g = [int(x) for x in (group or [])]
    if not g:
        return ("?", 0, "空")

    same = len(set(g)) == 1                        # 连大小写都一样
    same_pt = len(set(pt(x) for x in g)) == 1      # 点数一样、大小写可能混

    if len(g) >= 4 and same_pt:
        b = 1 if any(is_big(x) for x in g) else 0
        name = KIND_TI if origin == "self" else KIND_PAO
        return (name, XIXI[(name, b)], "%s字%s" % ("大" if b else "小", name))

    if len(g) == 3 and same:
        b = is_big(g[0])
        name = KIND_PENG if origin == "other" else KIND_WEI
        return (name, XIXI[(name, b)], "%s字%s" % ("大" if b else "小", name))

    if len(g) == 3 and same_pt:
        # 同点数、大小写混：一对 + 一张异大小写 = **绞牌**（help.txt 第 5 条），
        # 是合法门子但 0 胡息。三张大小写都不同不会出现（只有大小两套）。
        nbig = sum(is_big(x) for x in g)
        return (KIND_JIAO, 0, "绞牌 %s（%d大%d小，0胡）"
                % (cnames(sort_cards(g)), nbig, 3 - nbig))

    key = seq_key(g)
    if key:
        b = is_big(g[0])
        hx = seq_xi(key, b)
        tip = "%s字顺 %s" % ("大" if b else "小", cnames(sort_cards(g)))
        if hx:
            tip += "（特殊顺 %d胡）" % hx
        return (KIND_CHI, hx, tip)

    return ("?", 0, "认不出：%s" % cnames(g))


# 数组来源 → 牌型。客户端 sData 里是四个独立数组，这个映射是硬编码的事实，
# 不依赖 `from` 字段，也不需要 `MJGang.gang` 的编码。
ARRAY_KIND = {
    "peng":   (KIND_PENG, "other"),   # 碰：接别人打的
    "peng1":  (KIND_WEI,  "self"),    # 偎：自己摸到的第 3 张
    "gang0":  (KIND_TI,   "self"),    # 提：4 张全在手
    "gang1":  (KIND_PAO,  "other"),   # 跑：3 张在手 + 别人打的第 4 张
    "chi":    (KIND_CHI,  "other"),   # 吃
    "lie":    (KIND_LIE,  "self"),    # 摆火：吃牌时把手里同样的牌摊出来
}


def meld_from_array(which: str, cards):
    """从「哪个数组」直接定牌型，胡息按牌型 + 大小写查表。

    服务端有时只发 1 个代表值（实测 `gang: [10]` 就是），所以张数不足时
    按牌型补齐成 3 或 4 张，牌面就是那一张重复。

    `lie`（摆火）是**单张**，不补张、不计息，但**必须计入总牌守恒**——
    漏掉它守恒就会缺牌，这是上一版没算的一处。
    """
    arr = [int(c) for c in (cards or [])]
    if not arr:
        return None
    kind, origin = ARRAY_KIND.get(which, ("?", None))
    if kind == KIND_LIE:
        b = 1 if any(is_big(x) for x in arr) else 0
        return {"kind": KIND_LIE, "cards": arr, "xi": 0,
                "tip": "摆火（%s，不计息）" % cnames(sort_cards(arr)),
                "origin": origin, "from_array": which}

    want = 4 if kind in (KIND_TI, KIND_PAO) else 3
    if len(arr) == 1 and want:
        arr = arr * want

    b = 1 if any(is_big(x) for x in arr) else 0
    if kind == KIND_CHI:
        # 吃数组里装两种东西：一句话（顺子）和**绞牌**（一对 + 一张同点异
        # 大小写）。绞牌不是顺子，走 classify 才不会把它当垃圾。
        name, xi, tip = classify(arr, origin)
        if name != "?":
            kind = name
        else:
            pts = sorted(pt(x) for x in arr)
            xi = seq_xi(pts, b) if len(set(pts)) == 3 else 0
            tip = "%s字顺 %s" % ("大" if b else "小", cnames(sort_cards(arr)))
    else:
        xi = XIXI.get((kind, b), 0)
        tip = "%s字%s" % ("大" if b else "小", kind)
    return {"kind": kind, "cards": arr, "xi": xi,
            "tip": tip, "origin": origin, "from_array": which}


# ---------------------------------------------------------------- 手牌拆门子
#
# 「7 方门子」是胡牌硬条件（help.txt 第三条 B）：手里的 + 桌上的合起来 7 方。
# 明牌我们已经有数组了，**手牌里的 3 类东西只能自己拆**：
#
#   坎  3 张同值同大小写，起手就在手上，服务端不发事件、不进任何数组
#   顺子 3 张同大小写连号（含 2-7-10 / 1-2-3 两种特殊顺）
#   将  一对牌 —— ★ 它不是牌型，也不产生胡息。只有「已有跑或提」时，
#       这一对才能单独算 1 方门子（help.txt 第 17 条：「叫做将」）
#
# 这就是「明牌自算的胡息 vs origWinNum」对不上的全部原因：我们以前只算了
# 明牌那一半。手牌零息的局能对上（第 5 局 18=18），手牌有坎/顺子的局必然少。

def _seq_key(cards):
    """兼容旧名，逻辑在 `seq_key()`（classify 和手牌拆门子共用一份，别再分叉）。"""
    return seq_key(cards)


def split_hand(hand, foreign=None):
    """把手牌拆成 坎 / 顺子 / 将 / 拆不掉的余牌。

    返回 dict：
        kan    [(牌值, 胡息)]      —— 3 张同值同大小写
        seq    [(3 张牌, 胡息)]
        jiao   [[3 张牌]]          —— 绞牌（一对 + 一张同点异大小写），0 胡
        pair   [[2 张牌]]          —— 候选将
        rest   [牌值]              —— 拆不掉的（可能拆法不唯一）
        peng   [(牌值, 胡息)]      —— ★ 手里的「碰」：3 张同值但其中一张是别人
                                      打的（由 `foreign` 指定），见下

    贪心顺序：先坎（坎不能拆散，规则明写）→ 再顺子 → 再绞牌 → 剩下的成对。
    拆法不唯一是常态（同一手牌可能有多种 7 门子组合），这里只给一种
    自洽解，用于「能不能对上 origWinNum」，不是判定胡牌。

    ★★ `foreign`：这些牌值来自**别人**，凑成的 3 张同值只能算「碰」不能算「坎」。
    help.txt 第 4 条：「碰牌：当别人打或摸的牌自己手中有一对，则可以碰牌」——
    第 3 张来自别人时是碰（大 3 / 小 1 息），自己摸齐的才是坎（大 6 / 小 3）。
    胡牌瞬间的 `mjhand` 里**含刚胡进来的那张**，所以非自摸时 `lastPut` 那一路
    同值牌必须按碰算。2026-10-26 第 4 局实证：
        手牌 9 张 [109×3, 101×3, 7,8,9]，lastPut=109，lastDrawPlayer≠winner
        ⇒ 坎壹 6 + 顺789 0 + **碰玖 3** = 9；origWinNum 15 − 明牌 6 = 9 ✓
    同局按「坎玖 6」算会得 12（总 18），差 3 —— 这就是一直没闭合的那 3 息。
    """
    cards = sort_cards(hand or [])
    foreign = set(int(x) for x in (foreign or ()))
    cnt = {}
    for c in cards:
        cnt[c] = cnt.get(c, 0) + 1

    kan, seq, pair, rest, hand_peng = [], [], [], [], []
    # 同一个牌值如果有 4 张、且只有 1 张来自别人：3 张成碰，剩 1 张留下来参与
    # 后面的顺子/将判定（不能直接吞掉）。

    # ① 坎：同值同大小写 3 张（其中含外来的第 3 张则降级为碰）
    for c in sorted(cnt, key=lambda x: (pt(x), is_big(x))):
        while cnt.get(c, 0) >= 3:
            cnt[c] -= 3
            b = is_big(c)
            if c in foreign:
                hand_peng.append((c, XIXI[(KIND_PENG, b)]))
            else:
                kan.append((c, KAN_XI[b]))

    left = []
    for c, n in cnt.items():
        left.extend([c] * n)
    left = sort_cards(left)

    # ② 顺子：同大小写三连（含 2-7-10 / 1-2-3 特殊顺）
    used = [False] * len(left)
    for i in range(len(left)):
        if used[i]:
            continue
        for j in range(i + 1, len(left)):
            if used[j] or is_big(left[j]) != is_big(left[i]):
                continue
            for k in range(j + 1, len(left)):
                if used[k] or is_big(left[k]) != is_big(left[i]):
                    continue
                key = seq_key([left[i], left[j], left[k]])
                if key:
                    used[i] = used[j] = used[k] = True
                    seq.append(([left[i], left[j], left[k]],
                                seq_xi(key, is_big(left[i]))))
                    break
            if used[i]:
                break

    rest = [left[i] for i in range(len(left)) if not used[i]]

    # ②' 绞牌：3 张点数相同、大小写混（一对 + 一张异大小写）。合法门子，0 胡。
    #    排在顺子之后，否则会把本该成顺的牌拆散。只在正好 3 张时判，4 张留给将。
    rc0 = {}
    for c in rest:
        rc0.setdefault(pt(c), []).append(c)
    rest = []
    jiao = []
    for p in sorted(rc0):
        g = sort_cards(rc0[p])
        if len(g) == 3 and len(set(is_big(x) for x in g)) > 1:
            jiao.append(g)
        else:
            rest.extend(g)

    # ③ 将：剩下的成对
    rc = {}
    for c in rest:
        rc[c] = rc.get(c, 0) + 1
    rest2 = []
    pair = []
    for c in sort_cards(rc):
        n = rc[c]
        while n >= 2:
            pair.append([c, c])
            n -= 2
        rest2.extend([c] * n)

    return {"kan": kan, "seq": seq, "jiao": jiao, "pair": pair, "rest": rest2,
            "peng": hand_peng}


def hand_xi(hand, count_kan: bool | None = None, foreign=None) -> dict:
    """手牌门子的胡息合计 + 门子数。`count_kan=None` 取模块开关 COUNT_KAN。

    `menzi` 是手牌能凑出的方数（坎 + 碰 + 顺 + 绞 + 将），配合明牌组数要正好 7。
    `foreign` 见 `split_hand()`：这些牌值只在胡牌瞬间从别人那里进来，
    凑出的 3 张同值按碰计息。
    """
    if count_kan is None:
        count_kan = COUNT_KAN
    s = split_hand(hand, foreign=foreign)
    kan_xi = sum(x for _c, x in s["kan"]) if count_kan else 0
    sx = sum(x for _g, x in s["seq"])
    peng_xi = sum(x for _c, x in s["peng"])
    menzi = (len(s["kan"]) + len(s["peng"]) + len(s["seq"])
             + len(s["jiao"]) + len(s["pair"]))
    return {"kan_xi": kan_xi, "seq_xi": sx, "peng_xi": peng_xi,
            "total": kan_xi + sx + peng_xi,
            "menzi": menzi, "detail": s}


# ---------------------------------------------------------------- 名堂
#
# 出处：官方线上规则页（皮皮跑胡子的运营方多乐）
#   https://op-static.duole.com/cms/phz-play-rules/explain/                   通用
#   https://op-static.duole.com/cms/phz-play-rules/explain/loudifangpaofa/    娄底放炮罚
#
# ★ 我们打的就是「娄底放炮罚」：`limitScore=15` 对应它「胡息数≥15」，
#   而邵阳剥皮（`res/localhelp/help.txt`）是「至少有10胡息」。
#   两套玩法的**胡息表一模一样**（见 GENERAL_XIXI 对照），差别在名堂算法：
#     邵阳剥皮：自摸/天胡/地胡 **+10 胡**（加法）
#     娄底放炮罚：自摸 **×2**（乘法）
#   四局实测：21 → 42（自摸×2）、18 → 72（红胡×2·自摸×2）⇒ 是**乘算**，不是 +10。

FAN_RED_HU_LO, FAN_RED_HU_HI = 10, 13   # 10 ≤ 红字 < 13 → 胡息翻倍（mjdesc 里叫「十红」）
FAN_HONG13 = {2: 13, 3: 13, 4: 8}       # 红字 ≥ 这个数 → 固定 100 胡息


def fan_win(orig_win, red_n, self_draw, players: int = 2, card_next=None,
            pool=None, desc=None):
    """从**裸胡息**推 `winNum`（名堂加成后的分数），用于第二次对账。

    `orig_win` 裸胡息｜`red_n` 该玩家全部牌（明牌 + 手牌）里的红字张数
    `self_draw` 自摸｜`players` 人数（影响十三红门槛）
    `desc` 服务端 `mjdesc`，**只用来交叉验证**，不参与判定 —— 字符串没穷举过，
    靠它做判定会把「没见过的名堂」当成「没有这个名堂」。

    返回 `(winNum, [(名堂, 倍率/100, 依据)])`。倍率是**乘算**。

    还没实现的三个（数据里没出现过，出现时下面会判不一致，别硬猜）：
      一块匾（只有一门红字且全红字，翻倍）
      海底胡（所胡的牌是墩里最后一张，翻倍）
      天胡 / 地胡（各 100 胡息，需要「第几手」信息，`tData` 里没有）
    """
    orig_win = int(orig_win or 0)
    red_n = int(red_n or 0)
    hits = []

    # ① 固定 100 胡息的名堂 —— 命中就不再看倍率（它们本身就「算 100」）
    h13 = FAN_HONG13.get(int(players or 2), 13)
    if red_n >= h13:
        return 100, [("十三红", 100, "红 %d 张 ≥ %d" % (red_n, h13))]
    if red_n == 0:
        return 100, [("乌胡", 100, "红 0 张")]
    if orig_win == 30:
        return 100, [("三十胡", 100, "胡息正好 30")]

    # ② 翻倍名堂，乘算
    mult = 1
    if FAN_RED_HU_LO <= red_n < FAN_RED_HU_HI:
        mult *= 2
        hits.append(("红胡", 2, "红 %d 张（%d≤红<%d）" % (red_n, FAN_RED_HU_LO, FAN_RED_HU_HI)))
    if red_n == 1:
        mult *= 2
        hits.append(("一点红", 2, "红 1 张"))
    if orig_win == 20:
        mult *= 2
        hits.append(("卡胡", 2, "胡息正好 20"))
    if self_draw:
        mult *= 2
        hits.append(("自摸", 2, "胡的牌是自己摸的"))
    if not hits:
        hits.append(("放炮", 1, "胡的是别人打的牌，不翻倍"))
    return orig_win * mult, hits


# 通用规则页的胡息表（与邵阳剥皮 help.txt 第 44-51 行逐项一致，交叉验证通过）：
#   牌型     大字  小字
#   提        12    9
#   偎/臭偎/坎 6    3
#   跑         9    6
#   碰         3    1
#   壹贰叁/一二三  6  3
#   贰柒拾/二七十  6  3
#   绞、其他一句话 0  0
# ★ 「坎、偎、臭偎」在官方表里是**同一行**（6/3）——「坎」不是我们编出来的一档。

