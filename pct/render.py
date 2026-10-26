# -*- coding: utf-8 -*-
"""把还原结果排成人能读的牌谱。"""
from __future__ import annotations

from . import rules


def render(r: dict) -> str:
    L = []
    w = L.append
    w("")
    for st, u in enumerate(r["uids"]):
        w("  座位%d  uid=%s" % (st, u))
    if r.get("limit"):
        w("  起胡线 %s 胡   playType=%s   huziType=%s" % (r["limit"], r.get("play_type"),
                                                    r.get("huzi_type")))
        if r.get("limit") == 15:
            # 官方规则页：邵阳剥皮「至少有10胡息」，娄底放炮罚「胡息数≥15」
            w("            ↑ 15 胡 == 娄底放炮罚的起胡线（邵阳剥皮是 10）")

    cai = r.get("cai") or {}
    if cai:
        hot = [(u, v) for u, v in cai.items() if v]
        if not hot:
            w("  财牌：无（caiPaiArr 全 0，playType=%s 没开财牌玩法）" % r.get("play_type"))
        else:
            for u, v in hot:
                st = r["seat_of"].get(int(u))
                cnt = (r.get("cai_cnt") or {}).get(st) or {}
                w("  财牌：座位%s 财牌=「%s」用掉 %d 张  钻=%s"
                  % (st, rules.cname(v), sum(cnt.values()),
                     (r.get("cai_zuan") or {}).get(u, "?")))

    if r.get("deal"):
        w("")
        w("发牌 %d 张：%s" % (len(r["deal"]), rules.cnames(r["deal"], " ")))
        w("  红张 %d 张（二七十/贰柒拾，整副 24 张红）" % rules.red_count(r["deal"]))

    w("")
    w("── 明牌 ──")
    if not r["melds"]:
        w("  （没有明牌）")
    for st in range(r["nseat"]):
        grp = [m for m in r["melds"] if m["seat"] == st]
        if not grp:
            continue
        w("  座位%d  合计 %d 胡" % (st, sum(m["xi"] for m in grp)))
        for m in grp:
            w("    %s %-12s %2d胡  %-22s [%s]"
              % (m["kind"], rules.cnames(rules.sort_cards(m["cards"]), " "),
                 m["xi"], m["tip"] or "-", m["from_array"]))

    w("")
    w("── 弃牌堆 ──")
    for st in range(r["nseat"]):
        p = (r.get("put") or {}).get(st) or []
        w("  座位%d (%2d张) %s" % (st, len(p), rules.cnames(p, " ")))

    w("")
    w("── 终局手牌 ──")
    for st in range(r["nseat"]):
        h = (r.get("final_hand") or {}).get(st) or []
        if h:
            w("  座位%d (%2d张) %-30s 红张%d"
              % (st, len(h), rules.cnames(h, " "), rules.red_count(h)))
            hx = (r.get("xi_hand") or {}).get(st) or {}
            d = hx.get("detail") or {}
            bits = []
            if d.get("kan"):
                bits.append("坎 " + "、".join("%s(%d胡)" % (rules.cname(c), x)
                                             for c, x in d["kan"]))
            if d.get("seq"):
                bits.append("顺 " + "、".join("%s(%d胡)" % (rules.cnames(g), x)
                                             for g, x in d["seq"]))
            if d.get("jiao"):
                bits.append("绞 " + "、".join(rules.cnames(g) for g in d["jiao"]) + "（0胡）")
            if d.get("peng"):
                # 3 张同值但第 3 张是别人打的 ⇒ 碰而不是坎（胡牌瞬间那张还在手上）
                bits.append("碰 " + "、".join("%s(%d胡，第3张来自别人)"
                                             % (rules.cname(c), x) for c, x in d["peng"]))
            if d.get("pair"):
                bits.append("将 %s（%d 对，只有已有跑/提时才算 1 方门子，"
                            "本身不产生胡息）"
                            % ("、".join(rules.cnames(p) for p in d["pair"]),
                               len(d["pair"])))
            if d.get("rest"):
                bits.append("拆不掉 " + rules.cnames(d["rest"], " "))
            for b in bits:
                w("           %s" % b)
            w("           手牌门子自算 %d 胡（坎 %d + 顺 %d + 碰 %d），%d 方门子%s"
              % (hx.get("total", 0), hx.get("kan_xi", 0), hx.get("seq_xi", 0),
                 hx.get("peng_xi", 0), hx.get("menzi", 0),
                 "" if rules.COUNT_KAN else "  ※ 坎按 0 计（本玩法实测计息）"))
        else:
            w("  座位%d  没拿到（这次 dump 的 roundEnd 里 mjhand 被截断了）" % st)

    a = r["audit"]
    w("")
    w("── 总牌守恒 ──")
    for row in a["rows"]:
        w("  座位%d  手%2d + 弃%2d + 明%2d = %2d"
          % (row["seat"], row["hand"], row["put"], row["meld"],
             row["hand"] + row["put"] + row["meld"]))
    if a["rest"] is None:
        mark = ""
    elif not a["hand_known"]:
        mark = ""
    else:
        mark = "✓" if a["ok"] else "✗"
    w("  合计 %d 张在场 + 墩剩 %s = %s / 本局 %s 张  %s"
      % (a["total"], a["rest"],
         a["total"] + (a["rest"] or 0) if a["rest"] is not None else "?",
         a["pool"], mark))
    if a["rest"] is None:
        w("  ※ 没有 cardNext，算不出墩剩")
    if not a["hand_known"]:
        w("  ※ 终局手牌缺失，守恒对不了 —— 这一行只能看弃牌和明牌")

    po = r.get("put_order") or []
    if po:
        w("")
        w("── 出牌时序（%d 张）──" % len(po))
        seq = ["%d:%s" % (st, rules.cname(c)) for _t, st, c in po]
        for i in range(0, len(seq), 14):
            w("  " + " ".join(seq[i:i + 14]))

    e = r.get("end")
    w("")
    if not e:
        ls = r.get("live_state") or {}
        if ls:
            w("── 结算：roundEnd 不在缓冲里（live tState=%s winner=%s playNum=%s）"
              "—— 想要完整结算就开着 watch 打一局 ──"
              % (ls.get("tState"), ls.get("winner"), ls.get("playNum")))
        else:
            w("── 结算：还没打完 ──")
    elif e["winner"] == -1:
        w("── 结算：荒庄（无人胡）tState=%s ──" % e["tState"])
    else:
        w("── 结算：座位%s 胡牌  winNum=%s origWinNum=%s ×%s ──"
          % (e["winner"], e["winNum"], e["origWinNum"], e["multiple"]))
        w("  胡牌来源：%s%s"
          % ("自摸（lastDrawPlayer 就是赢家）" if e.get("self_draw")
             else "接炮（lastPut=%s 是别人打的）" % e.get("last_put"),
             "" if e.get("self_draw") or not r.get("foreign")
             else "；这一门按碰计息，不是坎"))
        if e["cards"]:
            w("  胡牌牌型：%s" % rules.cnames(e["cards"], " "))
        for st in sorted(e["players"]):
            p = e["players"][st]
            xm = (r.get("xi_meld") or {}).get(st, 0)
            hx = (r.get("xi_hand") or {}).get(st) or {}
            xh = hx.get("total", 0)
            w("  座位%d  本局 %+s 分  累计 %s" % (st, p["winone"], p["winall"]))
            if hx:
                got = xm + xh
                # origWinNum 是**胡牌那一家**的原始胡息，别拿去对输家的
                tgt = e.get("origWinNum") if st == e.get("winner") else None
                tail = ""
                if isinstance(tgt, int):
                    tail = ("  ✓ 与 origWinNum 一致" if got == tgt
                            else "  差 %+d（坎计息=%s；拆法可能不唯一）"
                                 % (tgt - got, rules.COUNT_KAN))
                w("          胡息自算：明牌 %d + 手牌 %d = %d%s" % (xm, xh, got, tail))
            else:
                w("          胡息自算：明牌 %d（手牌缺失，无法对账）" % xm)
            if p["mjdesc"]:
                w("          胡型：%s" % " · ".join(p["mjdesc"]))
            w("          原始：huNum=%s paoNum=%s tiNum=%s fireNum=%s selfDrawNum=%s"
              % (p["huNum"], p["paoNum"], p["tiNum"], p["fireNum"], p["selfDrawNum"]))
        f = r.get("fan")
        if f:
            wn = f["seat"]
            w("  名堂对账：红 %d 张 → 预测 %s 分，实际 winNum=%s  %s"
              % (r["red_n"].get(wn, 0), f["pred"], f["actual"], "✓" if f["ok"] else "✗"))
            w("            %s" % " · ".join("%s(×%s：%s)" % (k, m, why)
                                            for k, m, why in f["hits"]))

    if r.get("warn"):
        w("")
        for x in r["warn"]:
            w("⚠ %s" % x)
    return "\n".join(L)
