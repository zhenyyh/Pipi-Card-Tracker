# -*- coding: utf-8 -*-
"""回放采集器（连续版）：一局接一局地录「公开信息 + 双方真实手牌」。

为什么走回放
------------
局中打牌时，对手 `mjhand` 服务端不下发；**只有回放会把两家手牌填满**
（实测 `tState=4`、两家 `handKnown=True`、各 20 张）。所以回放是唯一能拿到
「对手真实手牌」的通路，而且不用金币。

★ 边界：回放的是**已经结束的局**。它只能用来校准「局中怎么猜」，
  不能用来在活局里看别人牌 —— 后者技术上也不成立（活局 sData 里没有对手手牌）。

这一版和上一版的区别
--------------------
上一版只录一局就收工。这一版**连续等**：你回放完一局，它记完、写盘，
然后继续等下一局；局之间靠 `(tableid, playNum)` 切段。

判定规则（都是踩过的坑）
------------------------
- **进入**：`tState ∈ {3,4,5,8}` 且两家手牌都 >3 张 —— 只有回放满足。
- **结束**：`tState == 6`（结算）连续 3 次，或离开回放态超过 8 秒。
  ★ 不能用「多久没变化」判结束：采集器用内容去重，结算后 `pct_state.json`
    会**冻住不动**（实测冻了 8 分钟），「文件没变」不等于「回放没播」。
- **切段**：`(tableid, playNum)` 变了就是新的一局。

用法
----
    python tools/harvest_replay.py --sec 1800
    python tools/harvest_replay.py --sec 1800 --out data/replay_frames_all.jsonl

每帧一行，多一个 `seg` 字段标第几局。帧是**逐条 flush** 的，进程被杀也不丢已录部分。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ADB = "D:/Soft-green/scrcpy-win64-v2.7/adb.exe"
SERIAL = "emulator-5554"
PKG = "com.happyplay.hnphz"
STATE = "/data/user/0/%s/files/pct_state.json" % PKG

MELD_UNIT = {"peng": 3, "peng1": 3, "gang0": 4, "gang1": 4, "lie": 1}
IN_GAME = (3, 4, 5, 8)


def read_state():
    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    try:
        r = subprocess.run([ADB, "-s", SERIAL, "shell", "su -c 'cat %s'" % STATE],
                           capture_output=True, timeout=12, env=env)
    except Exception:
        return None
    txt = (r.stdout or b"").decode("utf-8", "replace").strip()
    if not txt.startswith("{"):
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def snapshot(st):
    seats = []
    for s in st.get("seats") or []:
        melds = 0
        for k, unit in MELD_UNIT.items():
            melds += len(s.get(k) or []) * unit
        melds += (len(s.get("chi") or []) // 3) * 3
        seats.append({
            "seat": s.get("seat"),
            "uid": s.get("uid"),
            "hand": list(s.get("hand") or []),        # ★ 回放里对手这份是真的
            "handKnown": bool(s.get("handKnown")),
            "puts": list(s.get("put") or []),
            "melds": melds,
            "onLine": s.get("onLine"),
            "needOneCard": s.get("needOneCard"),
            "cannotPut": list(s.get("cannotPut") or []),
            "passChi": list(s.get("passChi") or []),
        })
    return {
        "tableid": st.get("tableid"),
        "playNum": st.get("playNum"),
        "zhuang": st.get("zhuang"),
        "cardNext": st.get("cardNext"),
        "choupai": st.get("choupai"),
        "deckLeft": (80 - (st.get("choupai") or 20) - (st.get("cardNext") or 0)),
        "tState": st.get("tState"),
        "curPlayer": st.get("curPlayer"),
        "lastPut": st.get("lastPut"),
        "lastPutPlayer": st.get("lastPutPlayer"),
        "lastDrawPlayer": st.get("lastDrawPlayer"),
        "seats": seats,
    }


def game_pid():
    """当前游戏进程号。变了就说明进程重启过，采集器已经跟着没了。"""
    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    try:
        r = subprocess.run([ADB, "-s", SERIAL, "shell", "pidof %s" % PKG],
                           capture_output=True, timeout=10, env=env)
    except Exception:
        return None
    txt = (r.stdout or b"").decode("utf-8", "replace").strip()
    return txt.split()[0] if txt else None


def reinstall():
    """把常驻采集器重新注入一个新起的游戏进程。

    ★ 为什么录制器必须自己会这一步：采集器活在游戏进程里，进程一重启
      （切房间/闪退/被系统回收）钩子就全没了，`pct_state.json` 会**冻在最后一帧**。
      实测冻了 16 分钟才发现 —— 那段时间的回放全丢了。
    """
    try:
        from pct.frida import FridaBridge
        b = FridaBridge()
        b.attach()
        try:
            r = b.install()
            return r
        finally:
            b.detach()
    except Exception as e:
        return {"err": str(e)[:80]}


def sig(f):
    """一帧的指纹。★ 必须含 lastPut / passChi：有几步只有这两项在变，
    漏了的话一局 20 步只剩 11 帧。"""
    return (
        f.get("cardNext"), f.get("lastPut"), f.get("tState"),
        tuple(sorted(f["seats"][0]["hand"]) if f["seats"] else ()),
        tuple(sorted(f["seats"][1]["hand"]) if len(f["seats"]) > 1 else ()),
        tuple(len(s["puts"]) for s in f["seats"]),
        tuple(s["melds"] for s in f["seats"]),
        tuple(len(s["passChi"]) for s in f["seats"]),
    )


def in_replay(f):
    """只在「回放中」才成立：局中态 + 两家手牌都有内容。
    活局永远是 False —— 对手那份是空的。"""
    if f.get("tState") not in IN_GAME:
        return False
    if len(f.get("seats") or []) < 2:
        return False
    return all(s["handKnown"] and len(s["hand"]) > 3 for s in f["seats"])


def main(argv):
    sec = 1800.0
    out_path = os.path.join(ROOT, "data", "replay_frames_all.jsonl")
    if "--sec" in argv:
        sec = float(argv[argv.index("--sec") + 1])
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]

    fh = open(out_path, "a", encoding="utf-8")
    print("连续录制中，最长 %.0f 分钟 → %s" % (sec / 60.0, out_path))
    print("在游戏里一局一局点「回放」即可，每局结束会自动收尾并等下一局。\n")

    t0 = time.time()
    last_pid = game_pid()
    next_pid_check = 0.0
    print("游戏 pid=%s" % last_pid)
    run_tag = time.strftime("%H%M%S")   # ★ seg 只在单次运行内唯一，跨运行会撞车
    seg = None          # {"no","key","seen","frames"}
    seg_no = 0
    total = 0
    settle_hits = 0
    away = 0

    def close(reason):
        nonlocal seg, total
        if seg is None:
            return
        print("  ── 第%d局收尾（%s）：%d 帧，tableid=%s playNum=%s"
              % (seg["no"], reason, seg["frames"], seg["key"][0], seg["key"][1]))
        total += seg["frames"]
        seg = None

    try:
        while time.time() - t0 < sec:
            # ---- 进程自愈：pid 变了就重装采集器 ----
            if time.time() >= next_pid_check:
                next_pid_check = time.time() + 10
                pid = game_pid()
                if pid and pid != last_pid:
                    print("⚠ 游戏进程变了 %s → %s，重装采集器…" % (last_pid, pid))
                    print("   结果：%s" % reinstall())
                    last_pid = pid
                    close("进程重启")
                    settle_hits = 0
                    away = 0

            st = read_state()
            if not st:
                time.sleep(0.25)
                continue
            f = snapshot(st)

            if in_replay(f):
                settle_hits = 0
                away = 0
                key = (f.get("tableid"), f.get("playNum"))
                if seg is None:
                    seg_no += 1
                    seg = {"no": seg_no, "key": key, "seen": set(), "frames": 0}
                    print("▶ 第%d局开始：tableid=%s playNum=%s tState=%s"
                          % (seg_no, key[0], key[1], f.get("tState")))
                elif seg["key"] != key:
                    close("换局")
                    seg_no += 1
                    seg = {"no": seg_no, "key": key, "seen": set(), "frames": 0}
                    print("▶ 第%d局开始：tableid=%s playNum=%s"
                          % (seg_no, key[0], key[1]))

                k = sig(f)
                if k not in seg["seen"]:
                    seg["seen"].add(k)
                    rec = dict(f)
                    rec["seg"] = "%s-%d" % (run_tag, seg["no"])
                    rec["frame"] = seg["frames"]
                    rec["wall"] = round(time.time() - t0, 2)
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()          # ★ 逐条落盘：进程被杀也不丢已录部分
                    seg["frames"] += 1
                    hands = " | ".join("座%s手%d" % (s["seat"], len(s["hand"]))
                                       for s in f["seats"])
                    print("  [%02d] cardNext=%-3s %s lastPut=%s"
                          % (rec["frame"], f.get("cardNext"), hands, f.get("lastPut")))
            else:
                if seg is not None:
                    if st.get("tState") == 6:
                        settle_hits += 1
                        if settle_hits >= 3:        # 结算稳住约 1 秒就收尾
                            close("结算")
                            settle_hits = 0
                    else:
                        away += 1
                        if away >= 32:              # 离开回放态 8 秒
                            close("离开回放")
                            settle_hits = 0
                            away = 0
                else:
                    time.sleep(0.25)
                    continue
            time.sleep(0.25)
    except KeyboardInterrupt:
        close("手动中断")
    finally:
        close("结束")
        fh.close()

    print("\n合计 %d 帧，%d 局 → %s" % (total, seg_no, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
