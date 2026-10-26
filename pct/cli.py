# -*- coding: utf-8 -*-
"""命令行入口。

    python -m pct replay <文件> [--round N]     还原牌谱（不需要设备）
    python -m pct probe                        自检钩子
    python -m pct watch [--sec 900]            边打边记
    python -m pct dump                         打一份当前牌谱
    python -m pct arm / clear                  装钩 / 清空（clear 会卸钩）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

from . import replay, render
from .bridge import Bridge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data")


def _say(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def cmd_replay(args):
    ev, live = replay.load(args.path)
    if not ev:
        _say("✗ 没读到事件：%s" % args.path)
        return 2
    for which in (args.round if args.round else [None]):
        r = replay.build(ev, live, which)
        if which is not None:
            _say("=" * 72)
            _say("%s  第 %s 局" % (os.path.basename(args.path), which))
            _say("=" * 72)
        _say(render.render(r))
        if args.json:
            _say(json.dumps({k: v for k, v in r.items()
                             if k not in ("melds_live", "melds_event")},
                            ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_probe(args):
    b = Bridge()
    pid = b.boot()
    if not pid:
        _say("✗ 游戏没在跑（%s）" % b.cfg["package"])
        return 2
    js = b.probe()
    if not js:
        _say("✗ 脚本没回话（agent 没装上，或者 eval 通道断了）")
        return 2
    _say("游戏 pid=%s  命令白名单 %s 条" % (pid, js.get("cmdKeys")))
    _say("钩子状态：%s" % json.dumps(js.get("hookState") or {}, ensure_ascii=False))
    if js.get("errs"):
        _say("脚本内部报错：%s" % js["errs"])
    hs = js.get("hookState") or {}
    if not hs.get("iEmit") and not hs.get("pEmit"):
        _say("⚠ emit 没钩上 —— 打一局也收不到包")
    return 0


def cmd_arm(args):
    b = Bridge()
    js = b.arm()
    if not js:
        _say("✗ arm 失败")
        return 2
    _say("装钩 %s 个  %s" % (js.get("hooked"), js.get("where")))
    return 0


def cmd_clear(args):
    b = Bridge()
    js = b.clear()
    _say("已清空。%s" % (js or {}).get("note", ""))
    _say("★ clear 顺带卸钩，接下来必须 arm，否则收不到任何事件。")
    return 0


def cmd_dump(args):
    b = Bridge()
    js = b.dump()
    if not js:
        _say("✗ dump 失败")
        return 2
    _save(js)
    r = replay.build(js.get("events") or [], js.get("live"), args.round)
    _say(render.render(r))
    return 0


def _save(js):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "dump_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False)
    return p


def _rounds(events):
    return sum(1 for e in events if e.get("e") == "roundEnd")


def cmd_watch(args):
    """边打边记：轮询 dump，增量落盘，每局结束把当天局中最后一帧的 live 附上。

    ★ 结算后 sData 会被清空（put/chi 全变 []），所以必须留「局中最后一帧」；
      而且不能只留一份 —— 多局连打时前面几局会被覆盖，守恒就永远对不平。
      这里把 live 以 `_live` 事件追在 roundEnd 后面一起写进事件流。
    """
    b = Bridge()
    pid = b.boot()
    if not pid:
        _say("✗ 游戏没在跑")
        return 2
    _say("游戏 pid=%s" % pid)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "stream_%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    out = io.open(path, "w", encoding="utf-8", newline="\n")
    seen = set()
    total = 0
    last_inround = None
    end_at = None
    t0 = time.time()
    _say("落盘：%s" % path)

    try:
        while True:
            now = time.time()
            if now - t0 > args.sec:
                _say("\n⏱ 到 %ds 上限，收工" % args.sec)
                break
            if end_at and now - end_at > 12:
                _say("\n✓ 结算后再采两轮，收工")
                break

            js = b.dump()
            if not js:
                time.sleep(2)
                continue

            live = js.get("live") or {}
            if live and live.get("tState") != 6:
                last_inround = live

            fresh = []
            for e in js.get("events") or []:
                k = json.dumps(e, sort_keys=True)
                if k in seen:
                    continue
                seen.add(k)
                fresh.append(e)

            if fresh:
                total += len(fresh)
                is_end = False
                for e in sorted(fresh, key=lambda x: x.get("t", 0)):
                    out.write(json.dumps(e, ensure_ascii=False) + "\n")
                    if e.get("e") == "roundEnd":
                        is_end = True
                    _echo(e)
                if is_end and last_inround:
                    out.write(json.dumps({"e": "_live", "d": last_inround},
                                         ensure_ascii=False) + "\n")
                    _say("  ✓ 本局 live 快照已随事件流落盘（playNum=%s）"
                         % last_inround.get("playNum"))
                out.flush()
                if is_end:
                    end_at = time.time()
                    last_inround = None
                if any(e.get("e") == "mjhand" for e in fresh):
                    end_at = None
                    _say("  ↻ 新一局发牌，继续录")
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
            time.sleep(1)
    finally:
        out.close()
        _say("\n共 %d 条事件 → %s" % (total, path))
        _say("复盘：python -m pct replay \"%s\"" % path)
    return 0


def _echo(e):
    from . import rules
    nm, d = e.get("e"), e.get("d") or {}
    t = e.get("t")
    if nm == "MJPut":
        _say("  出牌 uid=%s 【%s】 桌面=%s"
             % (d.get("uid"), rules.cname(d.get("card")),
                rules.cnames(d.get("mjput"), " ")))
    elif nm == "MJGang":
        _say("  MJGang uid=%s 【%s】 gang=%s from=%s"
             % (d.get("uid"), rules.cname(d.get("card")), d.get("gang"), d.get("from")))
    elif nm == "MJPeng":
        _say("  MJPeng uid=%s 【%s】 from=%s"
             % (d.get("uid"), rules.cname(d.get("card")), d.get("from")))
    elif nm == "MJChi":
        _say("  MJChi  uid=%s %s from=%s"
             % (d.get("uid"), rules.cnames(d.get("mjchi"), " "), d.get("from")))
    elif nm == "roundEnd":
        _say("  ★ 结算 winner=%s winNum=%s cards=%s"
             % (d.get("winner"), d.get("winNum"), rules.cnames(d.get("cards"), " ")))
    elif nm == "mjhand":
        h = d.get("mjhand") or []
        _say("  发牌 %d 张 %s" % (len(h), rules.cnames(h, " ")))


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="pct", description="皮皮跑胡子 牌局采集与还原")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("replay", help="还原牌谱（离线，不需要设备）")
    p.add_argument("path")
    p.add_argument("--round", type=int, nargs="*", help="第几局（1 起，-1=倒数第一）")
    p.add_argument("--json", action="store_true", help="额外打一份 JSON")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("probe", help="自检钩子")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("arm", help="装钩")
    p.set_defaults(fn=cmd_arm)

    p = sub.add_parser("clear", help="清空缓冲（会卸钩）")
    p.set_defaults(fn=cmd_clear)

    p = sub.add_parser("dump", help="打一份当前牌谱")
    p.add_argument("--round", type=int)
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("watch", help="边打边记")
    p.add_argument("--sec", type=int, default=900)
    p.set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
