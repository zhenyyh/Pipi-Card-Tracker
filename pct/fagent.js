// =============================================================================
// fagent.js —— Frida 侧 agent（Pipi Card Tracker）
//
// 职责切分（重要）：
//   本文件只做三件事 —— ① 把 JS 送进游戏的 JS 引擎 ② 把结果读回来 ③ 画界面。
//   **规则计算（牌值/门子/胡息/守恒）一行都不写在这里**，全部留在 Python 侧
//   （pct/rules.py 那份是已经对过账的），避免同一套口径维护两遍。
//
// 通道原理
// --------
// libccjs.so 导出了 Java_org_cocos2dx_lib_Cocos2dxJavascriptJavaBridge_evalString
// ⇒ Frida 只要 Java.use('org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge') 就能让
//   游戏执行任意 JS。这是游戏自己的 JNI 入口，比 hook SpiderMonkey（符号没导出）
//   稳得多。
//
// 三个必须遵守的约束
// ------------------
// 1. evalString 返回 bool，拿不到返回值 ⇒ 让游戏 JS 自己写文件，这边 Java IO 读回。
// 2. evalString 必须在 JS 上下文所在线程调用 ⇒ 统一走 Cocos2dxHelper.runOnGLThread。
// 3. 内容相同无法用「返回值变了没」判断 ⇒ 每次带 nonce，按 nonce 认领结果。
// =============================================================================

var PKG = 'com.happyplay.hnphz';
var FILE_DIR = '/data/user/0/' + PKG + '/files/';
var EVAL_OUT = FILE_DIR + 'pct_eval.json';

var log = function (m) { send({ t: 'log', m: String(m) }); };

// ------------------------------------------------------------------ Java 辅助
var J = {};

function boot_java() {
    if (J.ok) return;
    J.String = Java.use('java.lang.String');
    J.File = Java.use('java.io.File');
    J.FIS = Java.use('java.io.FileInputStream');
    J.BAOS = Java.use('java.io.ByteArrayOutputStream');
    J.Bridge = Java.use('org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge');
    J.Helper = Java.use('org.cocos2dx.lib.Cocos2dxHelper');
    J.Runnable = Java.use('java.lang.Runnable');
    J.Context = Java.use('android.content.Context');
    J.ActivityThread = Java.use('android.app.ActivityThread');
    J.ViewManager = Java.use('android.view.ViewManager');
    J.LP = Java.use('android.view.WindowManager$LayoutParams');
    J.ok = true;
}

// 手写 UTF-8 解码：Frida 的 runtime 不一定有 TextDecoder，而 JSON 里可能带中文
function utf8(bytes, len) {
    var s = '', i = 0;
    while (i < len) {
        var c = bytes[i++];
        if (c < 0x80) { s += String.fromCharCode(c); }
        else if (c < 0xE0) {
            var c2 = bytes[i++];
            s += String.fromCharCode(((c & 0x1F) << 6) | (c2 & 0x3F));
        } else if (c < 0xF0) {
            var c2 = bytes[i++], c3 = bytes[i++];
            s += String.fromCharCode(((c & 0x0F) << 12) | ((c2 & 0x3F) << 6) | (c3 & 0x3F));
        } else {
            var a2 = bytes[i++], a3 = bytes[i++], a4 = bytes[i++];
            var cp = (((c & 0x07) << 18) | ((a2 & 0x3F) << 12) |
                      ((a3 & 0x3F) << 6) | (a4 & 0x3F)) - 0x10000;
            s += String.fromCharCode(0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF));
        }
    }
    return s;
}

// 用 Frida 原生 File API 读（进程内同 uid，不走 adb，省一次往返）。
// ★ 这个 runtime 里 `new File(path,'rb')` 会抛 "not a function"，
//   只有静态方法 File.readAllText(path) 可用；readAllBytes 不存在。
function readText(path) {
    try {
        return File.readAllText(path);
    } catch (e) {
        return null;
    }
}

function removeFile(path) {
    boot_java();
    try { J.File.$new(path).delete(); } catch (e) { }
}

// ------------------------------------------------------------- evalString 通道
// 游戏内 JS 的包装：执行 code，把结果连同 nonce 写盘。
// code 必须是**表达式**，它的值会被序列化。
function wrapEval(code, nonce) {
    return '(function(){var __n=' + JSON.stringify(nonce) + ';var __r=null,__e=null;' +
        'try{__r=(function(){return (' + code + ');})();}catch(e){__e=String(e);}' +
        'var __o;try{__o=JSON.stringify({__n:__n,ok:__e?0:1,r:__r,e:__e});}catch(x){__o=JSON.stringify({__n:__n,ok:0,e:"json:"+x});}' +
        'try{jsb.fileUtils.writeStringToFile(__o, jsb.fileUtils.getWritablePath()+"pct_eval.json");}catch(w){}' +
        'return __n+":"+Date.now();})()';
}

var nonceSeq = 0;
var PENDING = null;          // 待执行的包裹代码（GL 线程从这里取）
var RUNNER = null;           // 只注册一次的 Runnable

function ensureRunner() {
    if (RUNNER) return RUNNER;
    boot_java();
    // ★ 每次 eval 都 registerClass 会不断新增类，实测会出现「成功-超时-成功」的
    //   交替失败。类只注册一次，参数走全局变量。
    RUNNER = Java.registerClass({
        name: 'com.pct.EvalRunner',
        implements: [J.Runnable],
        methods: {
            run: function () {
                var code = PENDING;
                PENDING = null;
                if (!code) return;
                try { J.Bridge.evalString(code); } catch (e) { }
            }
        }
    });
    return RUNNER;
}

function evalOnGL(code, timeoutMs) {
    boot_java();
    return new Promise(function (resolve) {
        var nonce = 'n' + Date.now() + '_' + (++nonceSeq);
        var wrapped = wrapEval(code, nonce);
        var settled = false;

        function finish(v) {
            if (settled) return;
            settled = true;
            resolve(v);
        }

        removeFile(EVAL_OUT);

        var R;
        try {
            var Cls = ensureRunner();
            R = Cls.$new();
        } catch (e) {
            finish({ ok: 0, e: 'runner:' + e });
            return;
        }
        PENDING = wrapped;
        try { J.Helper.runOnGLThread(R); }
        catch (e) { finish({ ok: 0, e: 'post:' + e }); return; }

        var waited = 0, step = 50;
        var timer = setInterval(function () {
            waited += step;
            var txt = readText(EVAL_OUT);
            if (txt) {
                var obj = null;
                try { obj = JSON.parse(txt); } catch (e) { }
                if (obj && obj.__n === nonce) {
                    clearInterval(timer);
                    if (obj.ok) finish({ ok: 1, r: obj.r });
                    else finish({ ok: 0, e: obj.e });
                    return;
                }
            }
            if (waited >= timeoutMs) {
                clearInterval(timer);
                finish({ ok: 0, e: 'timeout ' + timeoutMs + 'ms' });
            }
        }, step);
    });
}

// ---------------------------------------------------------------------- UI
// ★ 绝对不要用 WebView 承载这个界面。
//   它在游戏进程里会拉起 com.android.webview:sandboxed_process0 渲染子进程，
//   那个进程一旦 SIGSEGV，Android 判定「崩溃未被 webview 处理」，直接把宿主
//   （游戏）一起杀掉。实测 logcat：
//     chromium: Render process (22986) crash detected (code 11)
//     [FATAL] Render process's crash wasn't handled by all associated webviews,
//             triggering application crash
//     libc: Fatal signal 11 in tid 22694 (happyplay.hnphz)
//   原生 View 没有这个子进程，崩也只崩自己。
//
// 牌面用 素材/Card_00X.png（透明背景的字），不再画白底方块 —— 白底会把
// 桌面的牌挡住。偎/提的盖牌用 Card_0000.png（牌背）。
//   001..010 = 小写一~十     011..020 = 大写壹~拾

var DENS = 1.5;                    // density 240 → 1.5x
var UI_W = 470;                    // 主窗宽度（像素）：一行要放 4 张门子+箭头+1 张出牌
var TITLE_H = 66;                  // 44dp 标题栏
var CARD_DP = 28;                  // 统一牌面和占位尺寸（4×28dp≈336px + 箭头 + 出牌 ≈ 470px 内）
var CARD_DIR = '/data/local/tmp/pct_cards/';
var UI_TAG = 'pct_overlay';
var MAX_ROWS = 8;                  // 卡片最多显示几行门子

// ---- 牌值 → 显示字 --------------------------------------------------------
// 1..10 小写，101..110 大写。
// ★ 大写「六」用简体的「陆」：字体 STXinwei 里没有「陸」这个码位。
var NUM_S = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
var NUM_B = ['', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖', '拾'];
// 红张：二七十，大小写都红
var RED = { 2: 1, 7: 1, 10: 1, 102: 1, 107: 1, 110: 1 };

function cardText(v) {
    if (!v || v < 1) return '';
    return v > 100 ? (NUM_B[v - 100] || '?') : (NUM_S[v] || '?');
}

// ---- 颜色 ----------------------------------------------------------------
var CLR = {
    card: 0x9E0E1611,     // 卡片底：深墨绿（透明度可调）
    edge: 0x26FFFFFF,
    label: 0xFF9AA79C,
    dim: 0xFF64746A,
    red: 0xFFBD0000,
    black: 0xFF191919,
    back: 0xFF2F4C7E      // 盖牌底色
};

// 圆角矩形统一入口。颜色一律 |0 —— JS 的 0xFFxxxxxx 超过 int32，
// Frida 会当成 double，setColor(int) 匹配不到重载直接抛错。
function rrect(color, radiusDp, strokeColor, strokeW) {
    var g = Java.use('android.graphics.drawable.GradientDrawable').$new();
    g.setColor(color | 0);
    g.setCornerRadius(dp(radiusDp));
    if (strokeColor) {
        try { g.setStroke(strokeW || 1, strokeColor | 0); } catch (e) { }
    }
    return g;
}

var ALPHA_MIN = 40, ALPHA_MAX = 255, ALPHA_DEF = 150;

var UI = {
    root: null, body: null, settings: null, lp: null, wm: null, ctx: null,
    ready: false, collapsed: false, inSettings: false,
    miniBottom: null, miniFirst: null, chev: null,
    alpha: ALPHA_DEF, gearX: 0, tf: null, lastX: 36, lastY: 250, lastRows: 0,
    zhuangLabel: null, footLine: null, aiText: null
};
var drag = { moved: false, sx: 0, sy: 0, ox: 0, oy: 0 };
var uiClassSeq = 0;
var uiClassSession = String(Date.now()) + String(Math.floor(Math.random() * 100000));
var BMP = {};
var _paiAlpha = 0;                 // 牌面透明度缓存（跟卡片分开调）

function dp(v) { return Math.round(v * DENS); }

// LinearLayout.setOrientation(int) 在 Frida 里直接传 JS number 有时匹配不到重载，
// 显式指定 'int' 最稳。
function setOrient(v, dir) {
    v.setOrientation.overload('int').call(v, dir ? 1 : 0);
}


function cardBg(alpha) {
    var g = Java.use('android.graphics.drawable.GradientDrawable').$new();
    g.setColor(((alpha << 24) | 0x0E1611) | 0);
    g.setCornerRadius(dp(18));
    g.setStroke(1, 0x28FFFFFF | 0);
    return g;
}

function applyAlpha(a) {
    UI.alpha = a;
    try {
        UI.root.setBackground(cardBg(a));
        var sp = UI.ctx.getSharedPreferences('pct', 0);
        sp.edit().putInt('alpha', a).apply();
    } catch (e) { }
}

// 牌 = 用字体直接画的字（透明背景，不挡桌面）

// 文字描边：卡片是半透明深墨绿，纯黑字在上面几乎看不见。
// TextView 没有 stroke 接口，用文字阴影近似描边 —— radius 小、颜色浅就够提对比。
function strokeText(tv, v) {
    try {
        if (RED[v]) {
            // 红字：白边，避免红压在绿底上发闷
            tv.setShadowLayer(1.8, 0, 0, 0xF0FFFFFF | 0);
        } else {
            // 黑字：白边（这是用户反馈的核心问题）
            tv.setShadowLayer(1.8, 0, 0, 0xE8FFFFFF | 0);
        }
    } catch (e) { }
}


// 牌背：素材图 → 圆角 + 半透明 + 细边框。
// 直接拿 BitmapDrawable 铺满会是一整块方角红图，和深绿卡片糊在一起分不出边界。
// 这里用 ShapeDrawable(RoundRectShape) + BitmapShader：先缩放到牌位尺寸，
// 再套圆角形状，paint 设 alpha 做半透明，最后 LayerDrawable 叠一层描边。
var BMP_BACK = null;
function loadBackDrawable() {
    if (BMP_BACK) return BMP_BACK;
    try {
        var Bitmap = Java.use('android.graphics.Bitmap');
        var src = Java.use('android.graphics.BitmapFactory').decodeFile(CARD_DIR + 'Card_0000.png');
        if (!src) return null;
        var w = dp(CARD_DP), h = dp(CARD_DP);
        var bmp = Bitmap.createScaledBitmap(src, w, h, true);

        var shader = Java.use('android.graphics.BitmapShader').$new(bmp, 0, 0);   // CLAMP
        var radii = Java.array('float', [dp(4), dp(4), dp(4), dp(4),
                                         dp(4), dp(4), dp(4), dp(4)]);
        var shape = Java.use('android.graphics.drawable.shapes.RoundRectShape')
                        .$new(radii, null, null);
        var sd = Java.use('android.graphics.drawable.ShapeDrawable').$new(shape);
        var paint = sd.getPaint();
        paint.setShader(shader);
        paint.setAlpha(0xC0);            // 75% 不透明：能看见下面的桌面，又有牌感
        paint.setAntiAlias(true);

        var border = rrect(0x00000000, 4, 0x66FFFFFF, 1);
        var arr = Java.array('android.graphics.drawable.Drawable', [sd, border]);
        BMP_BACK = Java.use('android.graphics.drawable.LayerDrawable').$new(arr);
    } catch (e) { log('back ' + e); }
    return BMP_BACK;
}

// 牌值 → 素材文件。001..010 小写，011..020 大写，0000 是牌背。
// 用图片而不是字体：游戏里的牌面是美术字，字体怎么调都对不上。
function cardFile(v) {
    if (!v) return CARD_DIR + 'Card_0000.png';
    var idx = v > 100 ? (v - 100 + 10) : v;
    return CARD_DIR + 'Card_' + ('00' + idx).slice(-3) + '.png';
}

var BMP = {};
function loadBmp(v) {
    var key = v || 0;
    if (BMP[key] !== undefined) return BMP[key];
    var b = null;
    try { b = Java.use('android.graphics.BitmapFactory').decodeFile(cardFile(v)); } catch (e) { }
    BMP[key] = b;
    return b;
}

function paiView(v) {
    var iv = Java.use('android.widget.ImageView').$new(UI.ctx);
    iv.setScaleType(Java.use('android.widget.ImageView$ScaleType').FIT_CENTER.value);
    if (!v) {
        // 盖牌（偎的两张暗牌）
        var bd = loadBackDrawable();
        if (bd) iv.setBackground(bd);
        else iv.setBackground(rrect(CLR.back, 4, 0xFFFFFFFF, 1));
    } else {
        iv.setBackground(rrect(0xE6EFEFEF, 4, 0x33000000, 1));   // 牌底 #EFEFEF
        var b = loadBmp(v);
        if (b) iv.setImageBitmap(b);
        // 图案别顶死格子，留一圈安全边
        iv.setPadding(dp(2), dp(2), dp(2), dp(2));
    }
    var lp = Java.use('android.widget.LinearLayout$LayoutParams').$new(dp(CARD_DP), dp(CARD_DP));
    lp.setMargins(0, 0, dp(2), 0);
    iv.setLayoutParams(lp);
    return iv;
}

function slotView() {
    var tv = Java.use('android.widget.TextView').$new(UI.ctx);
    tv.setBackground(rrect(0x14FFFFFF, 4, 0x22FFFFFF, 1));
    var lp = Java.use('android.widget.LinearLayout$LayoutParams').$new(dp(CARD_DP), dp(CARD_DP));
    lp.setMargins(0, 0, dp(2), 0);
    tv.setLayoutParams(lp);
    return tv;
}

function labelView(text, color, sizeSp, bold) {
    var tv = Java.use('android.widget.TextView').$new(UI.ctx);
    tv.setText(J.String.$new(text));
    tv.setTextColor(color | 0);
    tv.setTextSize(sizeSp);
    tv.setIncludeFontPadding(false);
    if (bold) { try { tv.setTypeface(null, 1); } catch (e) { } }
    return tv;
}

function miniPaiView(v) {
    var iv = Java.use('android.widget.ImageView').$new(UI.ctx);
    iv.setScaleType(Java.use('android.widget.ImageView$ScaleType').FIT_CENTER.value);
    if (v) {
        iv.setBackground(rrect(0xE6EFEFEF, 4, 0x33000000, 1));
        var b = loadBmp(v);
        if (b) iv.setImageBitmap(b);
        iv.setPadding(dp(2), dp(2), dp(2), dp(2));
    } else {
        // 空槽：不写「—」，只留一个淡框
        iv.setBackground(rrect(0x3DFFFFFF, 4, 0x22FFFFFF, 1));
    }
    var lp = Java.use('android.widget.LinearLayout$LayoutParams').$new(dp(24), dp(24));
    lp.setMargins(dp(1), 0, dp(1), 0);
    iv.setLayoutParams(lp);
    return iv;
}

function updateMini(iv, v) {
    if (!iv) return;
    try {
        if (!v) {
            iv.setImageDrawable(null);
            iv.setBackground(rrect(0x3DFFFFFF, 4, 0x22FFFFFF, 1));
            return;
        }
        iv.setBackground(rrect(0xE6EFEFEF, 4, 0x33000000, 1));
        var b = loadBmp(v);
        if (b) iv.setImageBitmap(b);
    } catch (e) { }
}

function rowView(row) {
    var LL = Java.use('android.widget.LinearLayout');
    var LLP = Java.use('android.widget.LinearLayout$LayoutParams');
    var rowL = LL.$new(UI.ctx);
    setOrient(rowL, 0);
    rowL.setGravity(17);            // CENTER_VERTICAL | CENTER_HORIZONTAL
    var rowLP = LLP.$new(-1, dp(CARD_DP));
    rowLP.setMargins(0, dp(2), 0, dp(2));
    rowL.setLayoutParams(rowLP);

    var g1 = LL.$new(UI.ctx);
    setOrient(g1, 0);
    g1.setGravity(16);
    var cards = row.cards || [];
    var kind = row.kind;

    // 门子按「4 格右对齐」排：提/跑本身就是 4 张，吃/碰/偎是 3 张，
    // 摆火 1 张。不足的格子补在**左边** —— 这样最右边那张始终贴着箭头，
    // 每一行的箭头位置都一样，看起来才是齐的。
    var cells = [];
    if (kind === '偎') {
        cells.push(cards[0]);
        cells.push(0);            // 偎是 1 亮 2 盖
        cells.push(0);
    } else if (kind === '提' || kind === '跑') {
        for (var i = 0; i < 4; i++) cells.push(cards[i] || cards[0]);
    } else {
        for (var j = 0; j < cards.length; j++) cells.push(cards[j]);
    }
    while (cells.length < 4) cells.unshift(null);   // ★ 空位补在左边
    for (var k = 0; k < cells.length; k++) {
        g1.addView(cells[k] === null ? slotView() : paiView(cells[k]));
    }
    rowL.addView(g1);

    var arrow = labelView('→', 0x50FFFFFF, 15.0, false);
    var alp = LLP.$new(dp(14), -2);
    alp.setMargins(dp(2), 0, dp(2), 0);
    arrow.setLayoutParams(alp);
    arrow.setGravity(17);
    rowL.addView(arrow);

    var g2 = LL.$new(UI.ctx);
    setOrient(g2, 0);
    g2.setGravity(16);
    g2.addView(row.after ? paiView(row.after) : slotView());
    rowL.addView(g2);

    // 不要末尾的 spacer（它会把内容顶到左边）；整行居中，内容在窗口中间
    return rowL;
}

// ------------------------------------------------------------------ 设置面板
function buildSettings() {
    var LL = Java.use('android.widget.LinearLayout');
    var LLP = Java.use('android.widget.LinearLayout$LayoutParams');
    var View = Java.use('android.view.View');

    var p = LL.$new(UI.ctx);
    setOrient(p, 1);
    p.setPadding(dp(14), dp(6), dp(14), dp(12));
    p.setVisibility(8);                                  // 默认隐藏

    var head = LL.$new(UI.ctx);
    setOrient(head, 0);
    head.setGravity(16);
    head.addView(labelView('背景透明度', 0xFF9AA79C, 12.0, false));
    var sp = View.$new(UI.ctx);
    sp.setLayoutParams(LLP.$new(0, 1, 1.0));
    head.addView(sp);
    var val = labelView(String(UI.alpha), 0xFFE8E8E8, 12.0, true);
    head.addView(val);
    p.addView(head, LLP.$new(-1, -2));

    var sb = Java.use('android.widget.SeekBar').$new(UI.ctx);
    sb.setMax(ALPHA_MAX - ALPHA_MIN);
    sb.setProgress(UI.alpha - ALPHA_MIN);
    var slp = LLP.$new(-1, dp(34));
    slp.setMargins(0, dp(2), 0, dp(2));
    sb.setLayoutParams(slp);

    var listener = Java.registerClass({
        name: 'com.pct.Seek' + uiClassSession + (++uiClassSeq),
        implements: [Java.use('android.widget.SeekBar$OnSeekBarChangeListener')],
        methods: {
            onProgressChanged: {
                returnType: 'void',
                argumentTypes: ['android.widget.SeekBar', 'int', 'boolean'],
                implementation: function (bar, prog, fromUser) {
                    var a = ALPHA_MIN + prog;
                    applyAlpha(a);
                    val.setText(J.String.$new(String(a)));
                }
            },
            onStartTrackingTouch: { returnType: 'void', argumentTypes: ['android.widget.SeekBar'], implementation: function () { } },
            onStopTrackingTouch: { returnType: 'void', argumentTypes: ['android.widget.SeekBar'], implementation: function () { } }
        }
    });
    sb.setOnSeekBarChangeListener(listener.$new());
    p.addView(sb);

    p.addView(labelView('点标题栏收起设置', 0xFF5A6A60, 11.0, false));
    return p;
}

function uiBuild(cb) {
    boot_java();
    var View = Java.use('android.view.View');
    var MotionEvent = Java.use('android.view.MotionEvent');
    var LL = Java.use('android.widget.LinearLayout');
    var LLP = Java.use('android.widget.LinearLayout$LayoutParams');

    Java.scheduleOnMainThread(function () {
        try {
            var ctx = J.ActivityThread.currentApplication().getApplicationContext();
            UI.ctx = ctx;

            try {
                var sp = ctx.getSharedPreferences('pct', 0);
                UI.alpha = sp.getInt('alpha', ALPHA_DEF);
            } catch (e) { }

            uiInitScreen();
            // 游戏顶部“局数”和右侧控制键之间的空位。
            UI.lastX = Math.max(0, SCREEN.w - UI_W - dp(70));
            UI.lastY = 0;
            try {
                UI.tf = Java.use('android.graphics.Typeface').createFromFile(CARD_DIR + 'pct.ttf');
            } catch (e) { log('typeface ' + e); }

            var root = LL.$new(ctx);
            setOrient(root, 1);
            root.setBackground(cardBg(UI.alpha));
            try { root.setTag(J.String.$new(UI_TAG)); } catch (e) { }

            // ---- 标题栏
            var head = LL.$new(ctx);
            setOrient(head, 0);
            head.setGravity(16);
            head.setPadding(dp(6), 0, dp(6), 0);
            head.setLayoutParams(LLP.$new(-1, dp(44)));

            head.addView(labelView('底牌', 0xFF8A9A90, 10.0, false));
            UI.miniBottom = miniPaiView(0);
            head.addView(UI.miniBottom);

            var sep = labelView('|', 0x30FFFFFF, 12.0, false);
            var slp = LLP.$new(-2, -2);
            slp.setMargins(dp(2), 0, dp(2), 0);
            sep.setLayoutParams(slp);
            head.addView(sep);

            head.addView(labelView('首出', 0xFF8A9A90, 10.0, false));
            UI.miniFirst = miniPaiView(0);
            head.addView(UI.miniFirst);

            // 庄家标记：只有对家为庄，底牌/首出才是「对方的」，才记录
            UI.zhuangLabel = labelView('', 0xFF6B7A70, 9.0, false);
            var zlp = Java.use('android.widget.LinearLayout$LayoutParams').$new(-2, -2);
            zlp.setMargins(dp(1), 0, 0, 0);
            UI.zhuangLabel.setLayoutParams(zlp);
            head.addView(UI.zhuangLabel);

            var spHead = View.$new(ctx);
            spHead.setLayoutParams(LLP.$new(0, 1, 1.0));
            head.addView(spHead);

            // 齿轮：打开设置（靠 x 坐标判定）
            var gear = labelView('⚙', 0x99FFFFFF, 15.0, false);
            var glp = LLP.$new(dp(26), -1);
            gear.setLayoutParams(glp);
            gear.setGravity(17);
            head.addView(gear);

            UI.chev = labelView('▲', 0x88FFFFFF, 11.0, false);
            var clp = LLP.$new(dp(18), -1);
            UI.chev.setLayoutParams(clp);
            UI.chev.setGravity(17);
            head.addView(UI.chev);

            root.addView(head);

            // AI 与主面板共用一个窗口：醒目的徽标和一行短建议，收起时仍可见。
            var aiLine = LL.$new(ctx);
            setOrient(aiLine, 0);
            aiLine.setGravity(16);
            aiLine.setPadding(dp(8), 0, dp(8), 0);
            var aiBadge = labelView('✦ AI', 0xFF172F29, 10.0, true);
            aiBadge.setGravity(17);
            aiBadge.setBackground(rrect(0xFFFFD56B, 5, 0x66FFFFFF, 1));
            aiLine.addView(aiBadge, LLP.$new(dp(34), dp(19)));
            UI.aiText = labelView('等待建议', 0xFFE5EDE7, 11.0, true);
            UI.aiText.setPadding(dp(5), 0, 0, 0);
            UI.aiText.setSingleLine(false);
            try { UI.aiText.setMaxLines(3); } catch (e) { }
            try { UI.aiText.setEllipsize(Java.use('android.text.TextUtils$TruncateAt').END.value); } catch (e) { }
            aiLine.addView(UI.aiText, LLP.$new(0, -2, 1.0));
            root.addView(aiLine, LLP.$new(-1, dp(55)));   // 2-3 行建议

            var div = View.$new(ctx);
            div.setBackgroundColor(0x1AFFFFFF | 0);
            root.addView(div, LLP.$new(-1, 1));

            var body = LL.$new(ctx);
            setOrient(body, 1);
            body.setPadding(dp(11), dp(3), dp(11), dp(7));
            root.addView(body, LLP.$new(-1, -2));
            UI.body = body;

            // 底部信息行：对方已出的牌 + 墩里剩余张数（都是确定值）
            UI.footLine = labelView('', 0xFF7C8C82, 11.0, false);
            UI.footLine.setPadding(dp(12), dp(2), dp(12), dp(7));
            root.addView(UI.footLine, LLP.$new(-1, -2));

            UI.settings = buildSettings();
            root.addView(UI.settings, LLP.$new(-1, -2));

            var lp = J.LP.$new(UI_W, -2, 2038, 40, -3);
            lp.gravity.value = 51;
            lp.x.value = Math.min(UI.lastX, Math.max(0, SCREEN.w - UI_W));
            lp.y.value = UI.lastY;

            var wm = ctx.getSystemService(J.Context.WINDOW_SERVICE.value);
            J.ViewManager.addView.overload('android.view.View', 'android.view.ViewGroup$LayoutParams')
                .call(wm, root, lp);

            UI.root = root; UI.lp = lp; UI.wm = wm;

            // ---- 触摸：标题栏内区分拖动 / 折叠 / 设置
            // ★ Java.registerClass 注册的类在 VM 里永久存在、无法释放。
            //   之前每次 uiBuild 都注册一个新类（名字递增），重建几次就堆满 →
            //   点击直接卡死/崩溃。这里只在第一次注册，之后复用同一个实例。
            if (!UI.touchListener) {
            var touchCls = Java.registerClass({
                name: 'com.pct.Touch' + uiClassSession,
                implements: [Java.use('android.view.View$OnTouchListener')],
                methods: {
                    onTouch: {
                        returnType: 'boolean',
                        argumentTypes: ['android.view.View', 'android.view.MotionEvent'],
                        implementation: function (v, ev) {
                            var act = ev.getActionMasked();
                            if (act === 0) {
                                drag.sx = ev.getRawX(); drag.sy = ev.getRawY();
                                drag.ox = UI.lp.x.value; drag.oy = UI.lp.y.value;
                                drag.moved = false;
                                return (ev.getRawY() - drag.oy) <= TITLE_H + 6;
                            }
                            if (act === 2) {
                                var dx = ev.getRawX() - drag.sx, dy = ev.getRawY() - drag.sy;
                                if (!drag.moved && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) drag.moved = true;
                                if (drag.moved) {
                                    var nx = Math.round(drag.ox + dx);
                                    var ny = Math.round(drag.oy + dy);
                                    // 1px 内的抖动没必要触发重排
                                    if (Math.abs(nx - UI.lastX) >= 1 || Math.abs(ny - UI.lastY) >= 1) {
                                        uiMove(nx, ny);
                                    }
                                }
                                return true;
                            }
                            if (act === 1 || act === 3) {
                                if (!drag.moved) {
                                    // 齿轮区：右内边距 6dp + 折叠区 18dp，再往左 26dp
                                    var rightGap = (drag.ox + UI_W) - ev.getRawX();
                                    if (rightGap >= dp(24) && rightGap < dp(50)) uiToggleSettings();
                                    else uiToggle();
                                }
                                drag.moved = false;
                                return true;
                            }
                            return false;
                        }
                    }
                }
            });
            UI.touchListener = touchCls.$new();
            }
            root.setOnTouchListener(UI.touchListener);

            UI.ready = true;
            var aiStatus = aiRender({ action: 'none', reason: '等待建议' });
            if (!aiStatus.ok) log('ai panel: ' + aiStatus.e);
            log('ui built alpha=' + UI.alpha);
            cb && cb({ ok: 1, ai: aiStatus });
        } catch (e) {
            log('uiBuild failed: ' + e);
            cb && cb({ ok: 0, e: String(e) });
        }
    });
}

// ★ 拖动必须节流。onTouch 的 MOVE 一次滑动能来上百个事件，每个都
//   updateViewLayout 会让 WindowManager 不停重排，把游戏主线程拖死（实测直接崩）。
//   这里做三件事：屏幕尺寸只取一次、坐标没变就返回、变化小于 1px 不重排。
var SCREEN = { w: 1080, h: 1920, ok: false };

function uiInitScreen() {
    try {
        // getDefaultDisplay().getSize() 在 Application Context 上会报
        // "not a function"（这个 Frida 环境里）。直接读资源里的 DisplayMetrics 最稳。
        var dm = UI.ctx.getResources().getDisplayMetrics();
        SCREEN.w = dm.widthPixels.value;
        SCREEN.h = dm.heightPixels.value;
        DENS = dm.density.value || DENS;
        // ★ 这里才是真正生效的宽度（创建窗口前调用，会覆盖文件顶部的常量）。
        //   220dp 装不下「4 张门子 + 箭头 + 1 张出牌」（约 195dp），会挤成一团。
        UI_W = dp(320);
        TITLE_H = dp(44);
        SCREEN.ok = true;
    } catch (e) { log('screen ' + e); }
}

function uiMove(x, y) {
    // ★ 右边界要按整窗宽度算：之前允许拖到「屏宽-90」，470px 宽的窗只露一角 → 被裁掉
    var maxX = Math.max(0, SCREEN.w - UI_W);
    if (x > maxX) x = maxX;
    if (x < -UI_W + 90) x = -UI_W + 90;
    if (y < 0) y = 0;
    if (y > SCREEN.h - 60) y = SCREEN.h - 60;
    if (x === UI.lastX && y === UI.lastY) return;
    UI.lastX = x;
    UI.lastY = y;
    try {
        UI.lp.x.value = x;
        UI.lp.y.value = y;
        J.ViewManager.updateViewLayout
            .overload('android.view.View', 'android.view.ViewGroup$LayoutParams')
            .call(UI.wm, UI.root, UI.lp);
    } catch (e) { log('move ' + e); }
}

function uiToggle() {
    try {
        UI.collapsed = !UI.collapsed;
        UI.body.setVisibility(UI.collapsed || UI.inSettings ? 8 : 0);
        UI.footLine.setVisibility(UI.collapsed || UI.inSettings ? 8 : 0);
        UI.chev.setText(J.String.$new(UI.collapsed ? '▼' : '▲'));
        UI.lp.height.value = uiHeight(UI.lastRows || 0);
        J.ViewManager.updateViewLayout
            .overload('android.view.View', 'android.view.ViewGroup$LayoutParams')
            .call(UI.wm, UI.root, UI.lp);
    } catch (e) { log('toggle ' + e); }
}

function uiToggleSettings() {
    try {
        UI.inSettings = !UI.inSettings;
        UI.settings.setVisibility(UI.inSettings ? 0 : 8);
        UI.body.setVisibility(UI.inSettings ? 8 : (UI.collapsed ? 8 : 0));
        UI.footLine.setVisibility(UI.inSettings || UI.collapsed ? 8 : 0);
        UI.lp.height.value = uiHeight(UI.lastRows || 0);
        J.ViewManager.updateViewLayout
            .overload('android.view.View', 'android.view.ViewGroup$LayoutParams')
            .call(UI.wm, UI.root, UI.lp);
    } catch (e) { log('settings ' + e); }
}

// 标题、AI 行始终显示；展开时再为牌行和底部信息预留空间。
function uiHeight(nRows) {
    var base = dp(44) + dp(25) + 1;
    if (UI.inSettings) return base + dp(118);
    if (UI.collapsed) return base;
    var h = base + dp(10) + dp(22);
    if (!nRows) return h + dp(52);
    return h + nRows * (dp(CARD_DP) + dp(4));
}

function uiRender(state) {
    if (!UI.ready) return { ok: 0, e: 'no ui' };
    try {
        updateMini(UI.miniBottom, state.bottom);
        updateMini(UI.miniFirst, state.first);
        if (UI.footLine) {
            var parts = [];
            var puts = state.oppPuts || [];
            if (puts.length) {
                var names = [];
                for (var pi = 0; pi < puts.length && pi < 12; pi++) names.push(cardText(puts[pi]));
                parts.push('对方出 ' + names.join(' '));
            }
            UI.footLine.setText(J.String.$new(parts.join('   |   ')));
        }

        if (UI.zhuangLabel) {
            if (state.oppZhuang) {
                UI.zhuangLabel.setText(J.String.$new('对家为庄'));
                UI.zhuangLabel.setTextColor(0xFFD8A84E | 0);      // 金
            } else {
                UI.zhuangLabel.setText(J.String.$new('对家非庄'));
                UI.zhuangLabel.setTextColor(0xFF6B7A70 | 0);      // 灰
            }
        }

        if (!UI.inSettings) {
            var body = UI.body;
            body.removeAllViews();
            var rows = state.rows || [];
            // 门子多的时候（一局长局能到十几个）卡片会超出屏幕，
            // 只显示最近的 8 行 —— 前面的早就打完配过了，看最近的才有用
            if (rows.length > MAX_ROWS) rows = rows.slice(-MAX_ROWS);
            UI.lastRows = rows.length;
            if (!rows.length) {
                var msg = state.ended ? '本局已结束'
                    : (state.inGame ? '对方还没有成门子' : '等待开局…');
                var tv = labelView(msg, 0xFF6B7A70, 12.0, false);
                tv.setPadding(0, dp(14), 0, dp(16));
                tv.setGravity(17);
                body.addView(tv, Java.use('android.widget.LinearLayout$LayoutParams').$new(-1, -2));
            } else {
                for (var i = 0; i < rows.length; i++) body.addView(rowView(rows[i]));
            }
            UI.lp.height.value = uiHeight(rows.length);
            J.ViewManager.updateViewLayout
                .overload('android.view.View', 'android.view.ViewGroup$LayoutParams')
                .call(UI.wm, UI.root, UI.lp);
        }
        return { ok: 1, rows: (state.rows || []).length };
    } catch (e) {
        return { ok: 0, e: String(e) };
    }
}

function uiCleanup(cb) {
    boot_java();
    var killed = 0;
    try {
        var ctx = J.ActivityThread.currentApplication().getApplicationContext();
        var wm = ctx.getSystemService(J.Context.WINDOW_SERVICE.value);
        // ★ 不要用 WindowManagerGlobal.getInstance()：在这个脚本上下文里它会报
        //   "not a function"，异常被 catch 吞掉 ⇒ 清理静默失败 ⇒ 卡片一层层叠加。
        //   改成 Java.choose 按 tag 找（cocos 走 OpenGL，进程里的 LinearLayout
        //   只可能是我们建的，tag 精确区分）。
        Java.choose('android.widget.LinearLayout', {
            onMatch: function (v) {
                try {
                    var t = null;
                    try { t = String(v.getTag()); } catch (e) { }
                    if (t === UI_TAG || t === 'pct_ai') {
                        J.ViewManager.removeView.overload('android.view.View').call(wm, v);
                        killed++;
                    }
                } catch (e) { }
            },
            onComplete: function () {
                log('cleanup killed=' + killed);
                if (cb) cb();
            }
        });
    } catch (e) { log('cleanup ' + e); if (cb) cb(); }
}

// ---------------- 主面板中的 AI 建议
// result = {"action":"put","card":106,"reason":"留对子"}
function aiRender(result) {
    try {
        if (!UI.ready || !UI.aiText) throw new Error('记牌器尚未就绪');
        var text;
        if (!result || !result.action || result.action === 'none') {
            text = (result && result.reason) ? String(result.reason).slice(0, 24) : '等待建议';
        } else {
            var ACT = { put: '出', eat: '吃', peng: '碰', hu: '胡', pass: '过' };
            text = (ACT[result.action] || result.action) + (result.card ? cardText(result.card) : '');
            // 吃的具体组合（如 [柒贰拾]）
            if (result.chiCards && result.chiCards.length) {
                var cs = [];
                for (var ci = 0; ci < result.chiCards.length && ci < 3; ci++) cs.push(cardText(result.chiCards[ci]));
                text += '[' + cs.join('') + ']';
            }
            if (result.reason) text += ' · ' + String(result.reason).slice(0, 16);
            // 黑榜：避免点炮——现在不能打的牌
            if (result.blacklist && result.blacklist.length) {
                var bl = [];
                for (var bi = 0; bi < result.blacklist.length && bi < 3; bi++) bl.push(cardText(result.blacklist[bi]));
                text += ' ⚠' + bl.join('');
            }
        }
        UI.aiText.setText(J.String.$new(text));
        UI.aiText.setTextColor((!result || !result.action || result.action === 'none' ? 0xFFB9C8BE : 0xFFFFE199) | 0);
        return { ok: 1, shown: 1 };
    } catch (e) {
        log('aiRender failed: ' + e + (e.stack ? ' ' + e.stack : ''));
        return { ok: 0, e: String(e) };
    }
}

// ---------------------------------------------------------------------- rpc
// ★ 所有 Java.* 调用都必须落在 Java.perform 里：rpc 跑在 frida 自己的线程上，
//   不在 VM 上下文里直接 Java.use 会抛 class-factory 错。
rpc.exports = {
    ping: function () {
        return { ts: Date.now(), pkg: PKG };
    },
    // 让游戏执行一段 JS 表达式，返回其结果
    evaljs: function (code, timeoutMs) {
        return new Promise(function (resolve) {
            Java.perform(function () {
                evalOnGL(code, timeoutMs || 3000).then(resolve, function (e) {
                    resolve({ ok: 0, e: String(e) });
                });
            });
        });
    },

    // ---- 悬浮窗
    uishow: function () {
        return new Promise(function (resolve) {
            Java.perform(function () {
                if (UI.ready) { resolve({ ok: 1, reused: 1 }); return; }
                Java.scheduleOnMainThread(function () {
                    try { uiCleanup(function () { uiBuild(function (r) { resolve(r); }); }); }
                    catch (e) { log('uishow ' + e); resolve({ ok: 0, e: String(e) }); }
                });
            });
        });
    },
    uirender: function (state) {
        return new Promise(function (resolve) {
            Java.perform(function () {
                Java.scheduleOnMainThread(function () {
                    resolve(uiRender(state));
                });
            });
        });
    },
    uitoggle: function () {
        return new Promise(function (resolve) {
            Java.perform(function () {
                Java.scheduleOnMainThread(function () {
                    uiToggle(); resolve({ ok: 1, collapsed: UI.collapsed });
                });
            });
        });
    },
    uiai: function (result) {
        return new Promise(function (resolve) {
            Java.perform(function () {
                Java.scheduleOnMainThread(function () {
                    try { resolve(aiRender(result)); }
                    catch (e) { log('uiai ' + e); resolve({ ok: 0, e: String(e) }); }
                });
            });
        });
    },
    // 把窗口重新加一次（WindowManager 按 add 顺序排层，remove+add 可提到最上）
    uitop: function () {
        return new Promise(function (resolve) {
            Java.perform(function () {
                Java.scheduleOnMainThread(function () {
                    try {
                        if (!UI.ready || !UI.root || !UI.lp) { resolve({ ok: 0 }); return; }
                        J.ViewManager.removeView.overload('android.view.View').call(UI.wm, UI.root);
                        J.ViewManager.addView
                            .overload('android.view.View', 'android.view.ViewGroup$LayoutParams')
                            .call(UI.wm, UI.root, UI.lp);
                        resolve({ ok: 1 });
                    } catch (e) { resolve({ ok: 0, e: String(e) }); }
                });
            });
        });
    },
    uiclose: function () {
        return new Promise(function (resolve) {
            Java.perform(function () {
                Java.scheduleOnMainThread(function () {
                    var errors = [];
                    try {
                        if (UI.root) J.ViewManager.removeView.overload('android.view.View').call(UI.wm, UI.root);
                    } catch (e) { errors.push('记牌器: ' + e); }
                    UI.aiText = null; UI.root = null; UI.ready = false;
                    UI.collapsed = false; UI.inSettings = false; UI.lastRows = 0;
                    if (errors.length) { log('uiclose ' + errors.join('; ')); resolve({ ok: 0, e: errors.join('; ') }); }
                    else resolve({ ok: 1 });
                });
            });
        });
    },

    // 调试用：把悬浮窗的 View 树打出来（类名/尺寸/文本），排查「字没显示」
    debugtree: function () {
        var out = [];
        Java.perform(function () {
            function rec(v, d) {
                try {
                    var cls = String(v.getClass().getSimpleName());
                    var txt = '';
                    try { txt = String(v.getText()); } catch (e) { }
                    out.push(new Array(d + 1).join('| ') + cls +
                             ' ' + v.getWidth() + 'x' + v.getHeight() + ' [' + txt + ']');
                    var n = 0;
                    try { n = v.getChildCount(); } catch (e) { }
                    for (var i = 0; i < n; i++) rec(v.getChildAt(i), d + 1);
                } catch (e) { out.push('ERR ' + e); }
            }
            try { rec(UI.root, 0); } catch (e) { out.push('rootERR ' + e); }
        });
        return out.join('\n');
    },
    // 调试用：直接读设备上的文件（Frida 原生 File，不经过 Java）
    readtext: function (path) {
        return readText(path);
    },
    // 调试用：原样返回 Java 异常栈，定位注入层问题
    probe: function () {
        var out = { ok: 0 };
        Java.perform(function () {
            try {
                boot_java();
                out.ok = 1;
                out.bridge = !!J.Bridge;
                out.helper = !!J.Helper;
                out.dirExists = J.File.$new(FILE_DIR).exists();
            } catch (e) { out.e = String(e); }
        });
        return out;
    }
};

Java.perform(function () {
    boot_java();
    send({ t: 'ready' });
});