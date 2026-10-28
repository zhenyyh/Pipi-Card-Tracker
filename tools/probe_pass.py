# -*- coding: utf-8 -*-
"""把 `mjpassChi` 的口径坐实 —— 到底是「全局过张」还是「本家过张」。

为什么要单独验
--------------
`MJPut` / `MJPass` 都带 `mjpassChi`。这是全工程唯一一份「过张」证据：
一张牌打出去、没人吃碰胡，它就进这个列表 —— 等于直接告诉你
「这张对某一家没用」，比思考时长硬得多。

但两份实测数据打架：
  round_v3 座位0 的 MJPut(card=9) 带回 [10,1,103,103,4,7,9]（含对手打过的 1/103/4）
  round_v3 座位1 的 MJPut(card=4) 带回 [1,4]（只有自己打过的）
同一局、同一个字段，两种口径。不判清楚就接进推断层，等于拿脏数据喂模型。

这个脚本不猜，只摆证据：对每一个带 `mjpassChi` 的事件，把当时的
「自家累计出牌」和「双方累计出牌」都算出来，和快照做集合比对，看它更像哪个。
裁决靠计数，不靠感觉。

用法
----
    python tools/probe_pass.py                    # 跑全部 fixture
    python tools/probe_pass.py path/to/stream.jsonl
"""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("e"):
                rows.append(o)
    return rows


def probe(path):
    puts = {}       # uid -> 该家累计打出的牌（MJPut.mjput 本身就是累计数组）
    rows = []
    for o in _load(path):
        name, d = o.get("e"), (o.get("d") or {})
        uid = d.get("uid")
        if name == "MJPut" and isinstance(d.get("mjput"), list):
            try:
                puts[int(uid)] = [int(c) for c in d["mjput"] if c]
            except (TypeError, ValueError):
                pass

        pc = d.get("mjpassChi")
        if not isinstance(pc, list):
            continue
        try:
            snap = [int(c) for c in pc if c]
        except (TypeError, ValueError):
            continue

        union = []
        for v in puts.values():
            union.extend(v)
        own = list(puts.get(uid, [])) if uid is not None else None
        rows.append({
            "t": o.get("t"), "e": name, "uid": uid,
            "snap": snap,
            "eq_own": own is not None and sorted(snap) == sorted(own),
            "eq_union": sorted(snap) == sorted(union),
            "own": own, "union": union,
        })
    return rows


def main(argv):
    if len(argv) > 1:
        paths = argv[1:]
    else:
        paths = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "*.jsonl")))
        paths += sorted(glob.glob(os.path.join(ROOT, "data", "stream_*.jsonl")))

    tally = {"own": 0, "union": 0, "other": 0}
    for path in paths:
        rows = probe(path)
        if not rows:
            continue
        print("=" * 72)
        print(os.path.basename(path), "共 %d 个带 mjpassChi 的事件" % len(rows))
        for r in rows:
            owner = {"own": "=自家累计", "union": "=双方累计"}.get(
                "own" if r["eq_own"] else ("union" if r["eq_union"] else ""), "≠都不是")
            tally["own" if r["eq_own"] else ("union" if r["eq_union"] else "other")] += 1
            print("  %-12s uid=%-8s n=%-3d %s" % (r["e"], r["uid"], len(r["snap"]), owner))
            print("      快照 %s" % r["snap"])
            if not (r["eq_own"] or r["eq_union"]):
                print("      自家 %s" % r["own"])
                print("      双方 %s" % r["union"])

    print("=" * 72)
    total = sum(tally.values())
    print("裁决：等于自家累计 %d / 等于双方累计 %d / 都不是 %d（共 %d）"
          % (tally["own"], tally["union"], tally["other"], total))
    if total and tally["own"] > total * 0.8:
        print("→ 按座位口径：mjpassChi = 本家打出且被过掉的牌")
    elif total and tally["union"] > total * 0.8:
        print("→ 按全局口径：mjpassChi = 本局所有被过掉的牌")
    else:
        print("→ 两种都不占多数，口径仍未坐实；补几局带 MJPut 的实战流再跑")
        print("   注意 MJPut 自带 mjput（累计）和 mjpassChi 两个字段，")
        print("   同一条事件里两者做差 = 该家「打出但没被过」的牌。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
