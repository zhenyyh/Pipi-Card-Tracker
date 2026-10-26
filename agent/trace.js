// =============================================================================
// trace.js —— 牌局事件流采集（Pipi Card Tracker 版）
//
// 只钓 pomelo 的 emit / notify / request 三个口子。为什么不全量挂钩：
// 早先版本对 jsclient.phz（98 个函数）+ jsclient（576 个函数）全挂，
// 8 秒就产生 800+ 事件把环形缓冲冲爆，一局打完只剩最后 8 秒。
// 罪魁是 getKingNum / isRed / canHuHand 这些**每帧 UI 查询**，不是牌局事件。
//
// 三条硬约束（都是踩出来的，别改）
// -------------------------------
// 1. `evalString` 只返回 bool ⇒ 结果必须自己 writeStringToFile 落盘。
// 2. agent 靠「返回值是否变化」判断脚本跑完 ⇒ return 必须每次不同，
//    末尾带 Date.now()。内容一样会被当成没执行，5 秒超时还不拷输出。
// 3. dump 时如果钩子还活着就**不要**重新 arm：arm 内部是先 unhook 再装，
//    那几十微秒里来消息就漏事件。
//
// 模式
// ----
//   arm    装钩
//   dump   装钩（如果没装）+ 导出缓冲 + 附 live 快照
//   probe  只自检，不导出
//   clear  清空缓冲（★ 会卸钩！别随手用）
//   off    卸载
// =============================================================================
(function () {
  var MODE = 'arm';

  // DEPTH 3 而不是 2：事件的 tData 嵌套两层，depth 2 会把 tData.uids 压成
  // "<a2>"，座位映射就没了。
  // KEYCAP 原来是 40，实测 tData 有 90 个键，尾巴（caiPaiArr / playType /
  // limitScore / huziType / choupai）全被切成 {"…":1}，导致"财牌没有任何体现"。
  var MAXEV = 3000, MAXOUT = 400000, MAXSTR = 90, MAXARR = 40, DEPTH = 3, KEYCAP = 120;
  // 少数事件要更深：roundEnd 的 players[uid].mjhand 是赢家终局手牌，
  // depth 3 会压成 "<a8>"，总牌守恒就永远对不平。
  var DEEP = { roundEnd: 5, mjhand: 4, MJGang: 4, MJChi: 4, MJPeng: 4 };

  var errs = [], E = function (w, e) { if (errs.length < 15) errs.push(w + ': ' + e); };
  var G = this;
  var isArr = function (v) { return Object.prototype.toString.call(v) === '[object Array]'; };
  var L = function (t) { try { console.log('[PCT] ' + t); } catch (e) {} };

  // ---- 事件名白名单：只有在 jsclient.netCallBack 里登记过的才记 ----
  var KEYS = {};
  function loadKeys() {
    KEYS = {};
    try {
      var cb = jsclient.netCallBack;
      for (var k in cb) {
        if (k.indexOf('__pct') === 0) continue;
        if (!Object.prototype.hasOwnProperty.call(cb, k)) continue;
        KEYS[k] = 1;
      }
    } catch (e) { E('loadKeys', e); }
  }
  function allow(name) {
    if (typeof name !== 'string') return false;
    if (KEYS[name]) return true;
    return name.indexOf('.') > 0;      // 原始路由包（形如 a.b.c）一律放行
  }

  // ---- 紧凑序列化：外层永远是合法 JSON ----
  function brief(v, d) {
    if (v === null) return null;
    if (v === undefined) return '<undef>';
    var t = typeof v;
    if (t === 'number' || t === 'boolean') return v;
    if (t === 'string') return v.length > MAXSTR ? v.substring(0, MAXSTR) + '…' : v;
    if (t === 'function') return '<fn>';
    if (d <= 0) return isArr(v) ? '<a' + v.length + '>' : '<o>';
    try {
      if (isArr(v)) {
        var a = [];
        for (var i = 0; i < v.length && i < MAXARR; i++) a.push(brief(v[i], d - 1));
        if (v.length > MAXARR) a.push('…' + v.length);
        return a;
      }
      var o = {}, n = 0;
      for (var k in v) {
        if (k.indexOf('__pct') === 0) continue;
        if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
        if (++n > KEYCAP) { o['…'] = (n - 1); break; }
        o[k] = brief(v[k], d - 1);
      }
      return o;
    } catch (e) { return '<err>'; }
  }

  function push(rec) {
    try {
      var b = jsclient.__pct;
      if (!b) b = jsclient.__pct = [];
      if (b.length >= MAXEV) b.shift();
      rec.t = Date.now();
      b.push(rec);
    } catch (e) { }
  }

  // ---------------------------------------------------------------------------
  // live 快照：把「当前桌面」抄一份。
  //
  // 为什么必须有：`MJPut` 这条收包事件并不是每手都发，光靠事件出牌只抓到一半。
  // 而 `sData.players[uid]` 是**当前真实状态**，明牌/手牌/弃牌堆都在里面。
  //
  // ★★ 明牌在客户端里是四个独立数组，一定要分开读：
  //      mjpeng  碰（接别人打的）     mjpeng1  偎（自己摸的第 3 张）
  //      mjgang0 提（4 张全在手）     mjgang1  跑（3 张 + 别人打的第 4 张）
  //    老版本只读 mjpeng、还把 mjgang0/1 拼成一个数组，结果「偎」整组消失、
  //    「跑」被当成「碰」，总牌守恒永远差 3 张。分开读就自带来源信息，
  //    不用再去猜 MJGang.gang（1/2/4/6）是什么编码。
  // ---------------------------------------------------------------------------
  function cpArr(v, n) {
    var a = [];
    if (!isArr(v)) return a;
    var lim = n || 60;
    for (var i = 0; i < v.length && i < lim; i++) a.push(v[i]);
    return a;
  }

  function liveSnap() {
    var r = {};
    try {
      var sd = jsclient.data && jsclient.data.sData;
      if (!sd) { r.none = 1; return r; }
      var t = sd.tData || {}, uids = t.uids || [];
      r.gameid = sd.gameid;
      r.tableid = t.tableid;
      r.tState = t.tState;              // 6=结算（sData 会被清空）3=局中 8=胡牌确认中
      r.playNum = t.playNum;
      r.winner = t.winner;
      r.curPlayer = t.curPlayer;
      r.zhuang = t.zhuang;
      r.cardNext = t.cardNext;          // 牌墙指针
      r.lastPut = t.lastPut;
      r.lastPutPlayer = t.lastPutPlayer;
      r.limitScore = t.limitScore;      // 起胡线
      r.huziType = t.huziType;
      r.playType = t.playType;          // 玩法位
      r.playerNum = t.playerNum;
      r.initHandNum = t.initHandNum;
      r.choupai = t.choupai;            // 整副 80 张里抽走不用的（2 人局 20）
      r.caiPaiArr = t.caiPaiArr;        // {uid: 财牌点数}，0 = 本局无财牌
      r.caiPaiZuanNumArr = t.caiPaiZuanNumArr;
      r.uids = cpArr(uids, 8);

      var ps = sd.players || {}, seats = [];
      for (var i = 0; i < uids.length && i < 8; i++) {
        var u = uids[i], p = ps[String(u)] || ps[u] || {};
        seats.push({
          seat: i,
          uid: u,
          put: cpArr(p.mjput, 60),        // 弃牌堆快照（被吃碰跑会消失）
          peng: cpArr(p.mjpeng, 20),      // 碰
          peng1: cpArr(p.mjpeng1, 20),    // 偎  ★ 老版本漏了这个
          gang0: cpArr(p.mjgang0, 20),    // 提  ★ 老版本和 gang1 拼在一起了
          gang1: cpArr(p.mjgang1, 20),    // 跑
          chi: cpArr(p.mjchi, 30),        // 吃（3 张一组平铺）
          lie: cpArr(p.mjlie, 20),
          hand: cpArr(p.mjhand, 30),
          handN: isArr(p.mjhand) ? p.mjhand.length : 0,
          winall: p.winall,
          caiPaiCount: p.caiPaiCount,     // 财牌用掉几张
        });
      }
      r.seats = seats;
    } catch (e) { r.err = String(e); }
    return r;
  }

  // ---- 包装：必须转发调用方的 this ----
  // 钩子挂在 prototype 上时 this 是实例；写死成 host(prototype) 会让 pomelo
  // 内部读到 undefined 的属性直接抛错。
  function wrap(host, name, kind) {
    var f = host[name];
    if (typeof f !== 'function') return 0;
    if (f.__pctW) return 0;
    var w = function () {
      try {
        var nm = arguments[0];
        if (allow(nm)) {
          var nmS = String(nm);
          push({ s: kind, e: nmS, d: brief(arguments[1], DEEP[nmS] || DEPTH) });
        }
      } catch (e) { }
      return f.apply(this, arguments);
    };
    w.__pctW = 1;
    w.__pctOrig = f;
    try { host[name] = w; } catch (e) { E('wrap ' + name, e); return 0; }
    return 1;
  }

  function unhook(host) {
    var n = 0;
    try {
      // ★ 不能用 for...in：class 方法 / defineProperty 定义的都不可枚举，
      //   一个都还原不到（离线测出来过）。
      var keys = Object.getOwnPropertyNames(host);
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i], f = host[k];
        if (typeof f === 'function' && f.__pctW && f.__pctOrig) {
          try { host[k] = f.__pctOrig; n++; } catch (e) { }
        }
      }
    } catch (e) { }
    return n;
  }

  function getPomelo() {
    try { if (typeof pomelo !== 'undefined') return pomelo; } catch (e) { }
    try { return G.pomelo; } catch (e) { }
    return null;
  }

  function arm() {
    var out = { hooked: 0, where: [], unhooked: 0 };
    function doHost(host, label) {
      if (!host) return;
      // 先卸再装：arm 会跳过已包的函数，逻辑改了不重装就永远是旧的。
      var u = unhook(host);
      if (u) out.unhooked += u;
      var c = 0;
      c += wrap(host, 'emit', 'in');      // 收包落地点
      c += wrap(host, 'notify', 'out');   // 客户端发出
      c += wrap(host, 'request', 'req');  // 带回调的请求
      if (c) { out.hooked += c; out.where.push(label + ':' + c); }
    }
    try {
      var pm = getPomelo();
      if (pm) {
        doHost(Object.getPrototypeOf(pm), 'pomelo.proto');
        doHost(pm, 'pomelo');
      }
      var alts = ['GameNet', 'NetManager', 'net', 'socket'];
      for (var i = 0; i < alts.length; i++) {
        try {
          var o = G[alts[i]];
          if (o && typeof o === 'object' && !isArr(o)) doHost(o, alts[i]);
        } catch (e) { }
      }
    } catch (e) { E('arm', e); }
    return out;
  }

  // ---------------------------------------------------------------- 主流程
  var out = { ts: Date.now(), mode: MODE, errs: errs };
  loadKeys();
  out.cmdKeys = Object.keys(KEYS).length;

  try {
    var pm = getPomelo();
    if (MODE === 'clear') {
      jsclient.__pct = [];
      out.cleared = true;
      // ★ clear 会顺带卸钩（早期就是这样，改不掉也别踩）：调用方必须重新 arm。
      var n0 = 0;
      if (pm) { n0 += unhook(Object.getPrototypeOf(pm)); n0 += unhook(pm); }
      out.unhooked = n0;
      out.note = 'clear 已卸钩，需要重新 arm';
    } else if (MODE === 'off') {
      var n = 0;
      if (pm) { n += unhook(Object.getPrototypeOf(pm)); n += unhook(pm); }
      out.unhooked = n;
      out.left = (jsclient.__pct || []).length;
    } else {
      var alive = !!(pm && pm.emit && pm.emit.__pctW && pm.notify && pm.notify.__pctW);
      if (MODE === 'dump' && alive) {
        out.skippedArm = 1;
      } else if (MODE !== 'probe') {
        var a = arm();
        out.hooked = a.hooked;
        out.where = a.where;
        out.unhooked = a.unhooked;
      }
    }

    // 钩子存活自检：换房/换 pomelo 实例后钩子可能丢，一眼看出
    try {
      var pr = pm ? Object.getPrototypeOf(pm) : null;
      out.hookState = {
        pEmit: pr && pr.emit && pr.emit.__pctW ? 1 : 0,
        pNotify: pr && pr.notify && pr.notify.__pctW ? 1 : 0,
        iEmit: pm && pm.emit && pm.emit.__pctW ? 1 : 0,
        iNotify: pm && pm.notify && pm.notify.__pctW ? 1 : 0,
        iRequest: pm && pm.request && pm.request.__pctW ? 1 : 0,
        sameInst: pm === G.__pctInst,
      };
      G.__pctInst = pm;
    } catch (e) { E('hookState', e); }

    if (MODE === 'dump') {
      try { out.live = liveSnap(); } catch (e) { E('live', e); }

      var ev = jsclient.__pct || [];
      var keep = ev, dropped = 0;
      if (JSON.stringify(ev).length > MAXOUT) {
        // 从尾部倒推能装多少条，只砍头部，外层永远合法
        var i = ev.length - 1, acc = 2;
        while (i >= 0 && acc < MAXOUT) { acc += JSON.stringify(ev[i]).length + 1; i--; }
        keep = ev.slice(i + 1);
        dropped = i + 1;
      }
      out.events = keep;
      out.count = ev.length;
      out.kept = keep.length;
      out.dropped = dropped;
    }
  } catch (e) { E('main', e); }

  var json = '';
  try { json = JSON.stringify(out); } catch (e) { json = '{"fatal":"stringify"}'; }
  var w = 0;
  try {
    // ★★ 文件名不能改：原生 agent（agent-arm64/jni/agent.cpp）里写死了
    //    kProbeSrc = "/data/user/0/com.happyplay.hnphz/files/phz_probe_out.json"，
    //    它只拷这一个文件到 phz_eval_out。写别的名字 ⇒ 拷不到 ⇒ 看起来像「脚本没跑」。
    //    要改就得重编 ARM agent 并冷启动，不值得。
    jsb.fileUtils.writeStringToFile(json, jsb.fileUtils.getWritablePath() + 'phz_probe_out.json');
    w = json.length;
  } catch (e) { E('write', e); }

  L('end mode=' + MODE + ' hooked=' + (out.hooked || 0) + ' bytes=' + w);
  // ★ 返回值必须每次不同，见文件头第 2 条约束。
  return 'pct:' + MODE + ':' + (out.hooked || 0) + ':' + (out.count || 0) +
         ':' + w + ':' + Date.now();
})()
