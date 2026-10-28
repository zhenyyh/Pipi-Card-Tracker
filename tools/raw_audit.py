# -*- coding: utf-8 -*-
"""原始收包审计：进到游戏自己的 JS VM 里，挂钩 `pomelo` 与 `netCallBack`。

为什么不能直接用 frida 脚本
---------------------------
`jsclient` / `pomelo` 活在**游戏自己的 JS 引擎**里，普通 frida 脚本跑在另一个
运行时，看不到它们（实测 `cannot read property ... of undefined`）。
所以必须走 `evalString` 注入到游戏 VM —— 和 `gs_install.js` 同一条路。

钩子只做一件事：**记录 route + 顶层键名**，不改返回值、不改流程（纯 pass-through）。
目的不是再攒牌谱，是核对「业务层有没有把字段吃掉」。

产出
----
设备上 `pct_raw.json` → 拉到本地 `data/raw_audit.jsonl`，每条：
    {"t":毫秒, "via":"decode|notify|emit|request|cb:xxx", "route":"MJPut", "keys":[...]}

用法
----
    python tools/raw_audit.py --sec 600
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pct.frida import FridaBridge, FridaError  # noqa: E402

# 在游戏 VM 里执行：装钩子 + 起定时器落盘（幂等）
INSTALL = r"""
(function () {
  var S = jsclient, G = S;
  // 幂等：已经装过就只重建定时器（上次可能就是定时器没写成功）
  if (G.__pctAudit) {
    if (G.__pctAuditTimer) { try { clearInterval(G.__pctAuditTimer); } catch (e) {} }
    G.__pctAuditTimer = setInterval(function () {
      try {
        jsb.fileUtils.writeStringToFile(JSON.stringify(G.__pctAudit),
            jsb.fileUtils.getWritablePath() + 'pct_raw.json');
      } catch (e) {}
    }, 2000);
    return JSON.stringify({ok: 1, already: 1, n: G.__pctAudit.length});
  }
  G.__pctAudit = [];
  var MAX = 3000;

  function keysOf(v) {
    try {
      if (!v || typeof v !== 'object') return null;
      var a = [], n = 0;
      for (var k in v) {
        if (Object.prototype.hasOwnProperty.call(v, k)) {
          a.push(k); if (++n >= 40) break;
        }
      }
      return a;
    } catch (e) { return ['<err>']; }
  }
  function rec(via, route, obj) {
    try {
      if (G.__pctAudit.length >= MAX) return;
      G.__pctAudit.push({ t: Date.now(), via: via,
        route: (typeof route === 'string') ? route.slice(0, 48) : null,
        keys: keysOf(obj) });
    } catch (e) {}
  }
  function wrap(host, fn, via) {
    try {
      if (!host || typeof host[fn] !== 'function') return false;
      var ori = host[fn];
      host[fn] = function () {
        var a = arguments, route = null, obj = null;
        for (var i = 0; i < a.length && i < 3; i++) {
          if (typeof a[i] === 'string' && !route) route = a[i];
          else if (a[i] && typeof a[i] === 'object' && !obj) obj = a[i];
        }
        var ret = ori.apply(this, a);
        rec(via, route, obj || ret);
        return ret;
      };
      return true;
    } catch (e) { return false; }
  }

  var hooked = [];
  if (typeof pomelo !== 'undefined') {
    ['decode', 'notify', 'emit', 'request'].forEach(function (f) {
      if (wrap(pomelo, f, f)) hooked.push('pomelo.' + f);
    });
  }
  if (S.netCallBack) {
    var n = 0;
    for (var k in S.netCallBack) {
      if (!Object.prototype.hasOwnProperty.call(S.netCallBack, k)) continue;
      if (typeof S.netCallBack[k] === 'function' && wrap(S.netCallBack, k, 'cb:' + k)) n++;
    }
    hooked.push('netCallBack×' + n);
  }

  G.__pctAuditTimer = setInterval(function () {
    try {
      // ★ 必须绝对路径：只给文件名会静默失败（被 catch 吞掉）。
      jsb.fileUtils.writeStringToFile(JSON.stringify(G.__pctAudit),
          jsb.fileUtils.getWritablePath() + 'pct_raw.json');
    } catch (e) {}
  }, 2000);

  return JSON.stringify({ ok: 1, hooked: hooked });
})()
"""

CLEAR = r"""
(function () {
  if (!jsclient.__pctAudit) return JSON.stringify({ ok: 0 });
  var n = jsclient.__pctAudit.length;
  jsclient.__pctAudit = [];
  return JSON.stringify({ ok: 1, cleared: n });
})()
"""


def main(argv):
    sec = 600.0
    out_path = os.path.join(ROOT, "data", "raw_audit.jsonl")
    if "--sec" in argv:
        sec = float(argv[argv.index("--sec") + 1])
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]

    b = FridaBridge()
    try:
        b.attach()
    except FridaError as e:
        print("✗ 附着失败：%s" % e)
        return 2
    try:
        raw = b.eval(INSTALL, timeout=4.0)
        print("安装结果：", raw)
    finally:
        b.detach()
    print("审计中 %d 秒（只记 route + 键名，不落牌值）。去回放或打一局，我这边自动收。" % sec)

    seen_routes, written = {}, 0
    t0 = time.time()
    while time.time() - t0 < sec:
        time.sleep(15)
        b = FridaBridge()
        try:
            b.attach()
            txt = b.script.exports_sync.readtext("/data/user/0/%s/files/pct_raw.json" % b.pkg)
        except Exception:
            txt = None
        finally:
            try:
                b.detach()
            except Exception:
                pass
        if not txt:
            continue
        try:
            rows = json.loads(txt)
        except ValueError:
            continue
        new = rows[written:] if len(rows) > written else []
        if new:
            with open(out_path, "a", encoding="utf-8") as f:
                for r in new:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            written = len(rows)
            for r in new:
                k = r.get("route") or str(r.get("via"))
                seen_routes[k] = seen_routes.get(k, 0) + 1
            print("  +%d 条，累计 %d，route %d 种" % (len(new), written, len(seen_routes)))

    if seen_routes:
        print("\n出现过的 route：")
        for k, v in sorted(seen_routes.items(), key=lambda kv: -kv[1])[:30]:
            print("   %-28s %d" % (k, v))
    print("\n共 %d 条 → %s" % (written, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
