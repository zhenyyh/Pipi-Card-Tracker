# -*- coding: utf-8 -*-
"""盯对面那一份公开状态的变化 —— 回答「能不能感知对面」这个问题。

思路
----
实机枚举发现对手对象里有一批**我方客户端也收到**的字段（不是暗牌）：

    onLine        掉线时牌局会停摆          ← 实测踩到过，别误判成卡死
    needOneCard   待考证：疑似「还差一张」
    sishou / piaoSet / piaoType
    mjptop / mjwtop
    mjputHand     每家自己打出的牌（按 uid 分，不受事件漏发影响）
    mjpengFrom    碰的来源座位

这些字段有没有用，取决于它们**什么时候变**。所以这个脚本不猜，只记变化：
每 700ms 读一次常驻采集器落的 `pct_state.json`（纯文件读，不碰 GL 线程，
不会像反复 evalString 那样把游戏打到 ANR），字段一变就打一行。

跑一局之后回看输出就能定：
  ① `needOneCard` 在他听牌前后有没有翻 → 有，就是「对面听牌」的公开信号
  ② `onLine` 掉线会不会让 tState 卡住 → 卡住就是掉线，不是我们在等
  ③ `mjptop`/`mjwtop` 装的是什么 → 一直空就不是牌值

用法
----
    python -m pct.frida_dummy            # 不需要
    python tools/watch_opp.py            # 默认盯 300 秒
    python tools/watch_opp.py --sec 900 --out data/opp_watch.jsonl

前置：采集器已装（`python -m pct arm`），否则 `pct_state.json` 不存在。
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pct.frida import FridaBridge, FridaError  # noqa: E402

# adb 直读：daemon 已经占着 frida 会话时，再 attach 一次容易互相踢。
# 状态文件在 /data/user/0/<pkg>/files/pct_state.json，root 下 cat 出来即可，
# 完全不碰 frida、也不碰游戏主线程。
_ADB = "D:/Soft-green/scrcpy-win64-v2.7/adb.exe"
_SERIAL = "emulator-5554"
_PKG = "com.happyplay.hnphz"
_STATE = "/data/user/0/%s/files/pct_state.json" % _PKG


def read_state_adb():
    """用 adb 直接读采集器落盘的状态。读不到返回 None。"""
    import subprocess
    env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")   # 否则 /data/... 会被 Git Bash 改写
    try:
        r = subprocess.run([_ADB, "-s", _SERIAL, "shell", "su -c 'cat %s'" % _STATE],
                           capture_output=True, timeout=15, env=env)
    except Exception:
        return None
    txt = (r.stdout or b"").decode("utf-8", "replace").strip()
    if not txt.startswith("{"):
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None

WATCH_KEYS = ("state", "onLine", "needOneCard", "sishou", "piaoSet", "piaoType",
              "handN", "peng", "chi", "gang0", "gang1", "passChi", "passPeng",
              "cannotPut", "putHand", "pengFrom")


def sig_of(st):
    """把快照压成可比较的指纹：谁、哪些字段、变了什么。"""
    out = {}
    for seat in st.get("seats") or []:
        d = {}
        for k in WATCH_KEYS:
            if k not in seat:
                continue
            v = seat[k]
            if isinstance(v, list):
                d[k] = "len%d" % len(v) if k in ("peng", "chi", "gang0", "gang1",
                                                 "passChi", "passPeng", "cannotPut",
                                                 "putHand") else tuple(v[:6])
            else:
                d[k] = v
        out["seat%s" % seat.get("seat")] = d
    return out


def diff(prev, cur):
    """只报变了的键，避免每秒刷一屏。"""
    out = []
    for who in sorted(cur):
        a, b = prev.get(who, {}), cur[who]
        for k in sorted(b):
            if a.get(k) != b.get(k):
                out.append("%s.%s %s → %s" % (who, k, a.get(k, "∅"), b[k]))
    return out


def main(argv):
    sec = 300.0
    out_path = os.path.join(ROOT, "data", "opp_watch.jsonl")
    if "--sec" in argv:
        sec = float(argv[argv.index("--sec") + 1])
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]

    use_adb = "--via" in argv and argv[argv.index("--via") + 1] == "adb"
    b = None
    if use_adb:
        st = read_state_adb()
        if not st:
            print("✗ adb 读不到状态：确认设备在线、游戏在跑、采集器已装")
            return 2
        print("已用 adb 直读状态（不占 frida 会话，可与 overlay 同时跑）")
    else:
        b = FridaBridge()
        try:
            b.attach()
        except FridaError as e:
            print("✗ 附着失败：%s" % e)
            return 2
        st = b.read_state()
        if not st:
            print("✗ 读不到 %s" % b.state_path)
            print("  先装常驻采集器：python -m pct arm")
            b.detach()
            return 2
    print("采集器在线（%d 字节状态）" % len(json.dumps(st)))
    print("开局状态：")
    for who, d in sig_of(st).items():
        print("  %s %s" % (who, json.dumps(d, ensure_ascii=False)))
    print("-" * 68)

    prev = sig_of(st)
    t0 = time.time()
    n = 0
    fh = open(out_path, "a", encoding="utf-8")
    try:
        while time.time() - t0 < sec:
            time.sleep(0.7)
            try:
                st = read_state_adb() if use_adb else b.read_state()
            except Exception:
                st = None
            if not st:
                continue
            cur = sig_of(st)
            ch = diff(prev, cur)
            if ch:
                n += 1
                line = ("%s  %s" % (time.strftime("%H:%M:%S"), " | ".join(ch)))
                print(line)
                fh.write(json.dumps({"t": int(time.time() * 1000), "chg": ch,
                                     "tState": st.get("tState"),
                                     "playNum": st.get("playNum")},
                                    ensure_ascii=False) + "\n")
                fh.flush()
                prev = cur
    finally:
        fh.close()
        if b is not None:
            b.detach()

    print("-" * 68)
    print("共 %d 次变化，已追加到 %s" % (n, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
