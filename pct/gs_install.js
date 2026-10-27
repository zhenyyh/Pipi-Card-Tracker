// =============================================================================
// gs_install.js —— 在游戏 JS 引擎里装一个常驻采集器（只注入一次）
//
// 为什么不再反复 evalString
// ------------------------
// evalString 是**同步**跑在 GL 线程（= 主线程）上的。之前每 250ms 灌一次脚本，
// 每次都要重新编译执行 + 落盘读盘，还带超时重试，任务在 GL 线程队列里堆积，
// 直接把游戏打到 ANR（实测出现过「皮皮跑胡子没有响应」）。
//
// 现在改成：
//   注入一次 → 装钩子 + 装定时器 → 之后由游戏自己按事件/按周期落盘
//   Python 侧只读文件（Frida 原生 File.readAllText，不碰 Java、不碰主线程）
//
// 对主线程的负担从「每 250ms 编译执行一段 4KB 脚本」降到「每次状态变化
// stringify 一个 1KB 对象 + 写一次小文件」。
//
// 幂等：重复注入只会替换，不会叠加。
// =============================================================================
(function () {
    var G = this;
    var S = jsclient;
    var Q = '__pct_q';
    var H = '__pct_hook';
    var T = '__pct_timer';
    var LAST = '__pct_last';
    var OUT = 'pct_state.json';

    var WATCH = {
        MJPut: 1, MJPeng: 1, MJChi: 1, MJGang: 1,
        MJHu: 1, MJPass: 1, MJLao: 1, MJWai: 1,
        roundEnd: 1, MJBeginHu: 1, MJZhongJiang: 1,
        // 开局亮底牌可能是这几个之一，都收集起来看
        mjhand: 1, MJShow: 1, MJShowPL: 1, MJBegin: 1, MJXing: 1,
        MJBuxiaCard: 1, MJYu: 1, MJDi: 1
    };

    function arr(v, n) {
        var a = [];
        if (!v) return a;
        var lim = n || 40;
        for (var i = 0; i < v.length && i < lim; i++) a.push(v[i]);
        return a;
    }

    // 本人操作栏的可见按钮是游戏 UI 的直接证据；找不到或不唯一时返回未知。
    // 缓存节点避免每次事件都遍历场景。所有祖先可见才算真正显示。
    var controlScene = null, controlPanel = null, lastPanelSearch = 0;
    function actionPanel() {
        try {
            var scene = cc.director.getRunningScene();
            if (!scene) return null;
            if (scene !== controlScene || (controlPanel && !controlPanel.getParent())) {
                controlScene = scene; controlPanel = null; lastPanelSearch = 0;
            }
            var now = Date.now();
            if (!controlPanel && now - lastPanelSearch >= 1000) {
                lastPanelSearch = now;
                var queue = [scene], matches = [], seen = 0;
                while (queue.length && seen++ < 2500) {
                    var n = queue.shift(), cs = n.getChildren ? n.getChildren() : [];
                    if (n.getName && n.getName() === 'eat' && n.getChildByName &&
                        n.getChildByName('guo') && n.getChildByName('hu') &&
                        n.getChildByName('peng') && n.getChildByName('chi0')) matches.push(n);
                    for (var i = 0; i < cs.length; i++) queue.push(cs[i]);
                }
                if (matches.length === 1) controlPanel = matches[0];
            }
            if (!controlPanel) return null;
            function shown(node) {
                for (var x = node; x; x = x.getParent ? x.getParent() : null) {
                    if (x.isVisible && !x.isVisible()) return false;
                    if (x === scene) return true;
                }
                return false;
            }
            var available = [], children = controlPanel.getChildren();
            for (var j = 0; j < children.length; j++) {
                var b = children[j], name = b.getName ? b.getName() : '';
                var kind = name === 'guo' ? 'pass' : name === 'hu' ? 'hu' :
                           name === 'peng' ? 'peng' : /^chi\d+$/.test(name) ? 'eat' : null;
                if (kind && shown(b) && (!b.isEnabled || b.isEnabled()) &&
                    (!b.isTouchEnabled || b.isTouchEnabled()) && available.indexOf(kind) < 0)
                    available.push(kind);
            }
            return available.sort();
        } catch (e) { return null; }
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
            // ★ 庄家座位号：判断「对家是否为庄」全靠它，漏了会导致底牌/首出永远不记
            o.zhuang = t.zhuang;
            o.lastPut = t.lastPut;
            o.lastPutPlayer = t.lastPutPlayer;
            o.lastDrawPlayer = t.lastDrawPlayer;
            o.cardNext = t.cardNext;
            o.leftCards = arr(t.leftCards, 60);
            o.xingCard = t.xingCard;
            // 底牌候选：醒牌(xingCard) 经常是 -1，财牌数组(caiPaiArr) 是另一套来源
            o.caiPaiArr = arr(t.caiPaiArr, 8);
            o.sliceCard = t.sliceCard;
            o.firstSend = t.firstSend;
            o.winner = t.winner;
            o.choupai = t.choupai;
            o.initHandNum = t.initHandNum;
            o.playerNum = t.playerNum;
            o.huziType = t.huziType;
            o.limitScore = t.limitScore;
            o.playType = t.playType;
            // tState=5 可属于任何一方；仅本人操作栏可见时才视为本人有选择。
            // ★ 按钮可见性随时采集（tState=4 时吃/碰/过按钮也会弹出，不能只看 tState=5）
            o.visibleActions = actionPanel();

            var ps = sd.players || {}, seats = [];
            for (var i = 0; i < o.uids.length; i++) {
                var u = o.uids[i];
                var p = ps[String(u)] || ps[u] || {};
                seats.push({
                    uid: u,
                    seat: i,
                    put: arr(p.mjput, 60),
                    putKnown: Object.prototype.toString.call(p.mjput) === '[object Array]',
                    peng: arr(p.mjpeng, 20),
                    peng1: arr(p.mjpeng1, 20),
                    gang0: arr(p.mjgang0, 20),
                    gang1: arr(p.mjgang1, 20),
                    chi: arr(p.mjchi, 40),
                    lie: arr(p.mjlie, 20),
                    handN: (p.mjhand ? p.mjhand.length : 0),
                    hand: arr(p.mjhand, 40),
                    // 空手牌与客户端没有这个字段是两回事。
                    handKnown: Object.prototype.toString.call(p.mjhand) === '[object Array]',
                    knownMeldFields: {
                        peng: Object.prototype.toString.call(p.mjpeng) === '[object Array]',
                        peng1: Object.prototype.toString.call(p.mjpeng1) === '[object Array]',
                        gang0: Object.prototype.toString.call(p.mjgang0) === '[object Array]',
                        gang1: Object.prototype.toString.call(p.mjgang1) === '[object Array]',
                        chi: Object.prototype.toString.call(p.mjchi) === '[object Array]',
                        lie: Object.prototype.toString.call(p.mjlie) === '[object Array]'
                    },
                    meldsKnown: ['mjpeng', 'mjpeng1', 'mjgang0', 'mjgang1', 'mjchi', 'mjlie'].every(function (k) {
                        return Object.prototype.toString.call(p[k]) === '[object Array]';
                    }),
                    state: p.mjState
                });
            }
            o.seats = seats;
        } catch (e) { o.err = String(e); }
        return o;
    }

    // ---- 装钩子（幂等，pomelo 换实例也能自愈）
    // ★ pomelo 有三个入口：emit（客户端派发）、notify（服务端推送）、request（请求应答）。
    //   之前只钩了 emit，底牌这种「服务端单独推一次」的数据大概率走 notify，全漏了。
    //   三个口子都包，并且对**没见过的事件名**也记一条（只记第一次），
    //   这样新出现的字段一眼就能看到，不会把队列刷爆。
    var METHODS = ['emit', 'notify', 'request'];

    function pushEvent(src, nm, d) {
        jsclient.__pct_seq = (jsclient.__pct_seq || 0) + 1;
        var rec = { s: src, e: nm, t: Date.now(), q: jsclient.__pct_seq, d: {} };
        if (d && typeof d === 'object') {
            for (var k in d) {
                try {
                    var v = d[k], ty = typeof v;
                    if (ty === 'number' || ty === 'string' || ty === 'boolean') {
                        if (String(v).length <= 80) rec.d[k] = v;
                    } else if (Object.prototype.toString.call(v) === '[object Array]') {
                        if (v.length <= 30) rec.d[k] = v.slice(0, 30);
                    }
                } catch (e1) { }
            }
        }
        var q = jsclient[Q];
        if (!q) { q = jsclient[Q] = []; }
        q.push(rec);
        if (q.length > 240) q.splice(0, q.length - 240);
        flush();
    }

    function recEvent(src, events, d) {
        var names = [];
        if (typeof events === 'string') names = [events];
        else if (Object.prototype.toString.call(events) === '[object Array]') names = events;
        var seen = jsclient.__pct_seen || (jsclient.__pct_seen = {});
        for (var i = 0; i < names.length; i++) {
            var nm = names[i];
            if (typeof nm !== 'string') continue;
            if (WATCH[nm]) { pushEvent(src, nm, d); continue; }
            if (seen[nm]) continue;          // 非白名单只记第一次出现
            seen[nm] = 1;
            pushEvent(src, nm, d);
        }
    }

    function ensureHook() {
        var pm = null;
        try { if (typeof pomelo !== 'undefined') pm = pomelo; } catch (e) { }
        if (!pm) { try { pm = G.pomelo; } catch (e2) { } }
        if (!pm) return -1;
        var host = null;
        try { host = Object.getPrototypeOf(pm); } catch (e) { }
        if (!host) return -2;

        var installed = 0;
        for (var mi = 0; mi < METHODS.length; mi++) {
            (function (mname) {
                var orig = null;
                try { orig = host[mname]; } catch (e) { }
                if (typeof orig !== 'function' || orig.__pct) return;
                var w = function () {
                    try { recEvent(mname, arguments[0], arguments[1]); } catch (e) { }
                    return orig.apply(this, arguments);
                };
                w.__pct = 1;
                try { host[mname] = w; installed++; } catch (e) { }
            })(METHODS[mi]);
        }
        return installed;
    }


    // ★ 有些回调不走 pomelo.emit（游戏直接调用 jsclient.netCallBack.xxx），
    //   只钩 emit 会漏。这里把 netCallBack 里的函数也包一层，两条路都收。
    var HOOKED = '__pct_cb';
    function hookCallbacks() {
        var n = 0;
        try {
            var nc = jsclient.netCallBack;
            if (!nc) return 0;
            for (var k in nc) {
                if (!/^(MJ|mj|newCard|PickCard|addCard|lastCard|showEnd)/.test(k)) continue;
                var item = nc[k];
                if (!item || item.length < 2) continue;
                var fn = item[1];
                if (typeof fn !== 'function' || fn[HOOKED]) continue;
                (function (name, arr, orig) {
                    var wrapped = function () {
                        try { pushRec(name, arguments[0]); } catch (e) { }
                        return orig.apply(this, arguments);
                    };
                    wrapped[HOOKED] = 1;
                    arr[1] = wrapped;
                    n++;
                })(k, item, fn);
            }
        } catch (e) { }
        return n;
    }

    function pushRec(nm, d) {
        if (!d || typeof d !== 'object') return;
        jsclient.__pct_seq = (jsclient.__pct_seq || 0) + 1;
        var rec = { s: 'cb', e: nm, t: Date.now(), q: jsclient.__pct_seq, d: {} };
        for (var k in d) {
            try {
                var v = d[k], ty = typeof v;
                if (ty === 'number' || ty === 'string' || ty === 'boolean') {
                    if (String(v).length <= 80) rec.d[k] = v;
                } else if (Object.prototype.toString.call(v) === '[object Array]') {
                    if (v.length <= 30) rec.d[k] = v.slice(0, 30);
                }
            } catch (e) { }
        }
        var q = jsclient[Q];
        if (!q) { q = jsclient[Q] = []; }
        q.push(rec);
        if (q.length > 240) q.splice(0, q.length - 240);
    }


    // ★ pomelo.decode 是**最底层**的收包入口（emit/notify 都在它之后）。
    //   钩这里能看到服务端下发的原貌，包括那些「解出来但没往上派发」的字段。
    function hookDecode() {
        try {
            if (typeof pomelo === 'undefined' || typeof pomelo.decode !== 'function') return 0;
            if (pomelo.decode.__pct) return 0;
            var orig = pomelo.decode;
            var w = function (data) {
                var r = orig.apply(this, arguments);
                try {
                    var q = jsclient.__pct_dec || (jsclient.__pct_dec = []);
                    var rec = { t: Date.now(), k: '', s: 0 };
                    try {
                        if (r && typeof r === 'object') {
                            rec.k = Object.keys(r).slice(0, 20).join(',');
                            rec.s = JSON.stringify(r).length;
                            // 只留体积较大的（小包是心跳/聊天）
                            if (rec.s > 400) rec.body = JSON.stringify(r).slice(0, 4000);
                        } else { rec.v = String(r).slice(0, 80); }
                    } catch (e) { }
                    q.push(rec);
                    if (q.length > 30) q.shift();
                } catch (e) { }
                return r;
            };
            w.__pct = 1;
            pomelo.decode = w;
            return 1;
        } catch (e) { return 0; }
    }

    // ---- 落盘：内容变了才写，避免无谓 IO
    var writing = false;
    function flush() {
        if (writing) return;
        writing = true;
        try {
            var o = snapshot();
            var q = jsclient[Q];
            o.ev = (q && q.length) ? q.slice(-240) : [];
            var txt = JSON.stringify(o);
            if (txt !== jsclient[LAST]) {
                jsclient[LAST] = txt;
                jsb.fileUtils.writeStringToFile(txt, jsb.fileUtils.getWritablePath() + OUT);
            }
        } catch (e) {
        } finally { writing = false; }
    }

    var hk = ensureHook();
    var ncb = hookCallbacks();
    var ndc = hookDecode();

    // ---- 定时器兜底：没有事件时（读秒、结算、重连）也要保证文件是新的
    if (typeof setInterval === 'function') {
        try { if (jsclient[T]) clearInterval(jsclient[T]); } catch (e) { }
        try {
            jsclient[T] = setInterval(function () {
                try { ensureHook(); hookCallbacks(); hookDecode(); flush(); } catch (e) { }
            }, 600);
        } catch (e) { jsclient[T] = 0; }
    }

    flush();

    return JSON.stringify({
        ok: 1,
        hook: hk,
        callbacks: ncb,
        decode: ndc,
        timer: jsclient[T] ? 1 : 0,
        bytes: (jsclient[LAST] || '').length
    });
})()
