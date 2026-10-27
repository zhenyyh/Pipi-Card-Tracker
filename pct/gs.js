// =============================================================================
// gs.js —— 跑在游戏 JS 引擎里的采集脚本（由 Frida 通过 evalString 送入）
//
// 返回一个 JSON 字符串：{ ev: [本轮新增事件], live: {...快照}, hook: 0|1|-1 }
//
// 为什么要挂事件钩子而不是只轮询快照
// ----------------------------------
// 记牌器的核心是「对方的门子 → 这门子之后打出的那张牌」。这个配对**依赖先后
// 顺序**，而 sData 里的 mjput 是弃牌堆快照：被吃走的牌会消失，光看快照既拿不到
// 顺序，也分不清「新打的一张」和「上一张还在」。所以按 pomelo 收包顺序记事件。
//
// 钩子只在 pomelo 原型上包 emit 一个函数，白名单过滤 —— 早先对 jsclient 全量挂钩
// 8 秒就 800+ 事件把缓冲冲爆，罪魁是每帧 UI 查询，不是牌局事件。
//
// 三条约束（和 trace.js 同源）
//   1. evalString 只返回 bool ⇒ 结果自己序列化成字符串交回去
//   2. 本脚本必须是**表达式**（外层会包成 return (...)）
//   3. 只用 ES5：引擎是 cocos 内置的 SpiderMonkey，别用 let/箭头函数
// =============================================================================
(function () {
    var G = this;
    var Q = '__pct_q';       // 事件环形队列
    var H = '__pct_hook';    // 钩子已装标记（挂在 jsclient 上，跨 eval 保留）

    // 只记牌局事件。「门子 → 之后打出」需要 MJPeng/MJChi/MJGang + MJPut；
    // 其余几个用于判断回合边界与胡牌，避免把结算后的事件算进下一局。
    var WATCH = {
        MJPut: 1, MJPeng: 1, MJChi: 1, MJGang: 1,
        MJHu: 1, MJPass: 1, MJLao: 1, MJWai: 1,
        roundEnd: 1, MJBeginHu: 1, MJZhongJiang: 1
    };

    function getPomelo() {
        try { if (typeof pomelo !== 'undefined') return pomelo; } catch (e) { }
        try { return G.pomelo; } catch (e) { }
        return null;
    }

    // 幂等装钩：每次调用都检查原型上是不是已经被我们包过。
    // 换房/换连接后 pomelo 可能换实例，靠这个自愈，不用外部重新 arm。
    function ensureHook() {
        var pm = getPomelo();
        if (!pm) return -1;
        var host = null;
        try { host = Object.getPrototypeOf(pm); } catch (e) { }
        if (!host || typeof host.emit !== 'function') return -2;
        if (host.emit.__pct) { try { jsclient[H] = 1; } catch (e) { } return 0; }

        var orig = host.emit;
        var wrapped = function () {
            try {
                var nm = arguments[0];
                if (typeof nm === 'string' && WATCH[nm]) {
                    var d = arguments[1] || {};
                    // ★ 和 trace.js 落盘格式保持一致：{s,e,d,t}，字段都在 d 里。
                    //   两套格式不统一的话，离线 fixture 和实时数据就得写两套解析。
                    var rec = { s: 'in', e: nm, t: Date.now(), d: {} };
                    // 只留标量字段：对象/数组在这里没用，反而撑爆队列
                    for (var k in d) {
                        try {
                            var v = d[k];
                            var ty = typeof v;
                            if (ty === 'number' || ty === 'string' || ty === 'boolean') {
                                if (String(v).length <= 80) rec.d[k] = v;
                            }
                        } catch (e2) { }
                    }
                    var q = jsclient[Q];
                    if (!q) q = jsclient[Q] = [];
                    q.push(rec);
                    if (q.length > 400) q.splice(0, q.length - 400);
                }
            } catch (e3) { }
            return orig.apply(this, arguments);   // ★ 必须转发 this
        };
        wrapped.__pct = 1;
        try { host.emit = wrapped; } catch (e4) { return -3; }
        try { jsclient[H] = 1; } catch (e5) { }
        return 1;
    }

    function arr(v, n) {
        var a = [];
        if (!v) return a;
        var lim = n || 40;
        for (var i = 0; i < v.length && i < lim; i++) a.push(v[i]);
        return a;
    }

    function snapshot() {
        var o = {};
        try {
            var sd = jsclient.data && jsclient.data.sData;
            if (!sd) { o.none = 1; return o; }
            var t = sd.tData || {};
            var me = 0;
            try { me = jsclient.data.pinfo.uid; } catch (e) { }

            o.me = me;
            o.tState = t.tState;
            o.tableid = t.tableid;
            o.playNum = t.playNum;
            o.uids = arr(t.uids, 8);
            o.curPlayer = t.curPlayer;
            o.lastPut = t.lastPut;
            o.lastPutPlayer = t.lastPutPlayer;
            o.lastDrawPlayer = t.lastDrawPlayer;
            o.cardNext = t.cardNext;
            o.leftCards = arr(t.leftCards, 60);
            o.xingCard = t.xingCard;
            o.firstSend = t.firstSend;
            o.winner = t.winner;
            o.choupai = t.choupai;
            o.initHandNum = t.initHandNum;
            o.playerNum = t.playerNum;
            o.huziType = t.huziType;
            o.limitScore = t.limitScore;

            var ps = sd.players || {}, seats = [];
            for (var i = 0; i < o.uids.length; i++) {
                var u = o.uids[i];
                var p = ps[String(u)] || ps[u] || {};
                seats.push({
                    uid: u,
                    seat: i,
                    put: arr(p.mjput, 60),
                    peng: arr(p.mjpeng, 20),
                    peng1: arr(p.mjpeng1, 20),
                    gang0: arr(p.mjgang0, 20),
                    gang1: arr(p.mjgang1, 20),
                    chi: arr(p.mjchi, 40),
                    lie: arr(p.mjlie, 20),
                    handN: (p.mjhand ? p.mjhand.length : 0),
                    hand: arr(p.mjhand, 40),
                    desc: arr(p.mjdesc, 20),
                    state: p.mjState
                });
            }
            o.seats = seats;
        } catch (e) { o.err = String(e); }
        return o;
    }

    var out = { hook: ensureHook(), live: snapshot(), ev: [] };
    try {
        var q = jsclient[Q];
        if (q && q.length) {
            out.ev = q.splice(0, q.length);
        }
    } catch (e) { out.qerr = String(e); }

    return JSON.stringify(out);
})()
