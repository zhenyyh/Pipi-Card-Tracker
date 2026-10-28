# -*- coding: utf-8 -*-
"""实机只读探针：把「对面状态到底能不能感知」这件事一次问清楚。

探的是什么
----------
之前离线数据定不下一个前提：`tState=5`（等响应）出现时，我方按钮是空的，
是不是就等于「被问的是对面」？离线流里没有按钮可见性（那是场景树里的东西），
只能实机看。

所以这个探针不猜，它同时抓三样，让它们自己对上：
  1. `tData.tState` / `curPlayer` / `lastPut` / `lastPutPlayer`
  2. 场景树里本人 eat 面板的可见按钮（guo / hu / peng / chi0）
  3. 两家 `sData.players[uid]` 的字段快照（只看有哪些键、手牌长度，
     **不读对手手牌值** —— 那是服务端不下发的东西，读也读不到）

判据（需要连续采到才成立）：
    tState=5 且 我方按钮为空 → 被问的是对面
    再叠 canEatMustHu=true：他若被问过又没胡 → 他一定没有胡

用法
----
    python tools/live_probe.py            # 采 1 次，打报告
    python tools/live_probe.py --watch 30 # 每 0.8s 采一次，共 30 次
    python tools/live_probe.py --json out.json

★ 只读：不写任何游戏状态，不调用任何请求接口。每次只跑一个 eval，
  避免把 GL 线程灌爆（历史上就是这么把游戏打到 ANR 的）。
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pct.frida import FridaBridge, FridaError  # noqa: E402

# 一次 eval 拿全：四个部分都包在同一个 IIFE 里，减少 GL 线程往返
JS = r"""
(function () {
  var out = {ok: 1, ts: Date.now()};
  var D = (typeof jsclient !== 'undefined' && jsclient.data) ? jsclient.data : null;
  if (!D) { out.ok = 0; out.err = 'no jsclient.data'; return JSON.stringify(out); }

  var sd = D.sData || null;
  out.hasSData = !!sd;
  out.me = (D.pinfo && D.pinfo.uid) || null;
  if (!sd) return JSON.stringify(out);

  var t = sd.tData || {};
  out.sDataKeys = Object.keys(sd).slice(0, 30);
  out.tState = t.tState;
  out.curPlayer = t.curPlayer;
  out.lastPut = t.lastPut;
  out.lastPutPlayer = t.lastPutPlayer;
  out.lastDrawPlayer = t.lastDrawPlayer;
  out.cardNext = t.cardNext;
  out.choupai = t.choupai;
  out.zhuang = t.zhuang;
  out.uids = t.uids;
  out.canEatHu = t.canEatHu;
  out.canEatMustHu = t.canEatMustHu;
  out.playNum = t.playNum;
  out.tableid = t.tableid;

  // ---- 两家座位：只报「字段在不在 + 手牌长度」，不报对手手牌值 ----
  var ps = sd.players || {};
  var seats = [];
  for (var k in ps) {
    if (!Object.prototype.hasOwnProperty.call(ps, k)) continue;
    var p = ps[k] || {};
    var keys = [];
    for (var kk in p) if (Object.prototype.hasOwnProperty.call(p, kk)) keys.push(kk);
    var isMe = String(k) === String(out.me);
    seats.push({
      uid: k,
      isMe: isMe,
      keys: keys,
      handN: (p.mjhand && p.mjhand.length) || p.handN || 0,
      handIsArray: Object.prototype.toString.call(p.mjhand) === '[object Array]',
      // 对手手牌内容一律不下发，这里只留长度做守恒校验
      mjState: p.mjState,
      putN: (p.mjput && p.mjput.length) || 0
    });
  }
  out.seats = seats;

  // ---- 本人操作栏可见性（场景树，和 gs_install.js 同一套口径）----
  out.buttons = null;
  try {
    var scene = (typeof cc !== 'undefined' && cc.director) ? cc.director.getRunningScene() : null;
    if (scene) {
      var queue = [scene], seen = 0, panel = null;
      while (queue.length && seen++ < 2500 && !panel) {
        var n = queue.shift();
        if (n.getName && n.getName() === 'eat') { panel = n; break; }
        var cs = n.getChildren ? n.getChildren() : [];
        for (var i = 0; i < cs.length; i++) queue.push(cs[i]);
      }
      if (panel) {
        var names = {guo: 'guo', hu: 'hu', peng: 'peng', chi0: 'chi0'};
        var vis = [];
        var q2 = [panel], s2 = 0;
        while (q2.length && s2++ < 400) {
          var m = q2.shift();
          var nm = m.getName ? m.getName() : '';
          if (names[nm]) {
            var okv = true, cur = m;
            while (cur) {
              if (cur.isVisible && !cur.isVisible()) { okv = false; break; }
              cur = cur.getParent ? cur.getParent() : null;
            }
            if (okv) vis.push(nm);
          }
          var c2 = m.getChildren ? m.getChildren() : [];
          for (var j = 0; j < c2.length; j++) q2.push(c2[j]);
        }
        out.buttons = vis;
        out.panelFound = true;
      } else {
        out.buttons = [];
        out.panelFound = false;
      }
    }
  } catch (e) {
    out.buttons = null;
    out.btnErr = String(e).slice(0, 120);
  }

  return JSON.stringify(out);
})()
"""


def one(b: FridaBridge) -> dict:
    raw = b.eval(JS, timeout=3.0)
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {"ok": 0, "err": "bad return"}


def fmt(r: dict) -> str:
    if not r.get("ok"):
        return "✗ %s" % r.get("err")
    btns = r.get("buttons")
    btn_s = "无面板" if r.get("panelFound") is False else (
        "空" if btns == [] else (",".join(btns) if btns else "未知"))
    return ("tState=%-3s cur=%-3s lastPut=%-4s lastPutPlayer=%-3s 墩剩=%-3s "
            "我的按钮=%-12s | 对手手牌长度=%s"
            % (r.get("tState"), r.get("curPlayer"), r.get("lastPut"),
               r.get("lastPutPlayer"),
               (80 - (r.get("choupai") or 20) - (r.get("cardNext") or 0)) if r.get("cardNext") is not None else "?",
               btn_s,
               next((s["handN"] for s in r.get("seats") or [] if not s["isMe"]), "?")))


def main(argv):
    watch = 1
    interval = 0.8
    out_path = None
    if "--watch" in argv:
        watch = int(argv[argv.index("--watch") + 1])
    if "--json" in argv:
        out_path = argv[argv.index("--json") + 1]

    b = FridaBridge()
    try:
        b.attach()
    except FridaError as e:
        print("✗ 附着失败：%s" % e)
        return 2
    print("已附着 pid=%s" % getattr(b, "pid", "?"))

    rows = []
    try:
        for i in range(watch):
            try:
                r = one(b)
            except Exception as e:
                print("  [%02d] eval 失败：%s" % (i, str(e)[:80]))
                time.sleep(interval)
                continue
            rows.append(r)
            print("  [%02d] %s" % (i, fmt(r)))
            if i + 1 < watch:
                time.sleep(interval)
    finally:
        b.detach()

    # ---- 判据汇总：tState=5 时我方按钮是否为空 ----
    five = [r for r in rows if r.get("ok") and r.get("tState") == 5]
    if five:
        empty = [r for r in five if r.get("buttons") == []]
        print("\ntState=5 样本 %d 个，其中我方按钮为空 %d 个" % (len(five), len(empty)))
        if empty:
            print("→ 支持「tState=5 且我方空 ⇒ 被问的是对面」。仍需连续多局确认，")
            print("  单帧不足以排除「按钮还没渲染出来」的竞态。")
        else:
            print("→ tState=5 时我方按钮都有内容，说明 5 就是「问我」，不能反推对面。")
    else:
        print("\n本次没采到 tState=5。要验证判据，请在「对面出牌后、你等他反应」那一刻重跑。")

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("已写 %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
