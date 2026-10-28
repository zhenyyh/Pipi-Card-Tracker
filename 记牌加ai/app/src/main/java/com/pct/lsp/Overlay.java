package com.pct.lsp;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;

/**
 * 悬浮窗 —— **逐行照搬 pct/fagent.js**（常量、尺寸、布局、交互全部对齐）。
 *
 * 抄的依据（fagent.js 行号）：
 *   186/716  UI_W = dp(320)，TITLE_H = dp(44)          ← 宽度必须写死
 *   189      CARD_DIR = /data/local/tmp/pct_cards/      ← 牌是图片不是字
 *   188      CARD_DP = 28
 *   207-215  CLR 常量表
 *   336-357  paiView / slotView / miniPaiView
 *   415-468  rowView：4 格右对齐 + → + after（空位补左边）
 *   530-628  标题栏 / AI 行 / divider / body / footLine
 *   640-670  触摸：标题栏拖动；UP 时 rightGap∈[dp(24),dp(50)) 开设置，否则折叠
 *   686-697  拖动节流：1px 内不重排
 */
public final class Overlay {

    private static final String TAG = "PCT/ui ";
    private static final String STATE = "pct_state.json";
    private static final String AI = "pct_ai.json";

    private static final String CARD_DIR = "/data/local/tmp/pct_cards/";
    private static final int CARD_DP = 28;
    private static final int MINI_DP = 24;

    // CLR（fagent.js 207-215）
    private static final int CLR_CARD = 0xE6EFEFEF;   // 牌底
    private static final int CLR_BACK = 0xFF2F4C7E;
    private static final int CLR_LABEL = 0xFF9AA79C;
    private static final int CLR_DIM = 0xFF64746A;

    private static int UI_W, TITLE_H, ALPHA = 0xC0, SCREEN_W = 1080;

    private static View sRoot;
    private static WindowManager sWm;
    private static WindowManager.LayoutParams sLp;
    private static LinearLayout sBody;
    private static TextView sHead, sZhuang, sChev, sAiText, sFoot, sBottomTxt, sFirstTxt;
    private static ImageView sMiniBottom, sMiniFirst;
    private static View sSettings, sAiLine;
    private static Handler sHandler;
    private static Context sCtx;
    private static boolean sCollapsed = false, sInSettings = false;
    private static long sLastActivity = 0;
    private static String sLastSig = "";

    private Overlay() {}

    public static void show(final Context ctx) {
        if (sRoot != null) return;
        sCtx = ctx;
        // ★ 别人的机器没有 `appops set` —— 必须自己申请悬浮窗权限，
        //   否则 addView 抛异常、面板永远不出现（用户实测「装上没有记牌器」）。
        if (Build.VERSION.SDK_INT >= 23 && !android.provider.Settings.canDrawOverlays(ctx)) {
            try {
                android.content.Intent i = new android.content.Intent(
                        android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        android.net.Uri.parse("package:" + ctx.getPackageName()));
                i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                ctx.startActivity(i);
                android.widget.Toast.makeText(ctx,
                        "请允许「显示在其他应用上层」，然后重开游戏",
                        android.widget.Toast.LENGTH_LONG).show();
            } catch (Throwable t) {
                log("申请悬浮窗权限失败: " + t);
            }
            return;                       // 没权限就别硬 addView，会崩
        }
        try {
            float dens = ctx.getResources().getDisplayMetrics().density;
            SCREEN_W = ctx.getResources().getDisplayMetrics().widthPixels;
            UI_W = (int) (320 * dens);          // fagent.js 716
            TITLE_H = (int) (44 * dens);        // fagent.js 717

            sWm = (WindowManager) ctx.getSystemService(Context.WINDOW_SERVICE);

            LinearLayout root = new LinearLayout(ctx);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setBackground(cardBg(ALPHA));

            // ── 标题栏（44dp）
            LinearLayout head = new LinearLayout(ctx);
            head.setOrientation(LinearLayout.HORIZONTAL);
            head.setGravity(Gravity.CENTER_VERTICAL);
            head.setPadding(dp(ctx, 6), 0, dp(ctx, 6), 0);
            // sHead：标题位（平时空着，回放时显示「回放」）。
            // 之前只声明没创建 → 每次 render 调 sHead.getText() 都 NPE，
            // 整个 render 崩溃，面板停在旧画面（用户看到的就是「一直没更新」）。
            sHead = labelView(ctx, "", 0xFF7C8C82, 9f, false);
            head.addView(sHead);
            head.addView(labelView(ctx, "底牌", 0xFF8A9A90, 10f, false));
            sMiniBottom = miniPaiView(ctx);
            head.addView(sMiniBottom);
            TextView sep = labelView(ctx, "|", 0x30FFFFFF, 12f, false);
            LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(-2, -2);
            slp.setMargins(dp(ctx, 2), 0, dp(ctx, 2), 0);
            sep.setLayoutParams(slp);
            head.addView(sep);
            head.addView(labelView(ctx, "首出", 0xFF8A9A90, 10f, false));
            sMiniFirst = miniPaiView(ctx);
            head.addView(sMiniFirst);
            sZhuang = labelView(ctx, "", 0xFF6B7A70, 9f, false);
            LinearLayout.LayoutParams zlp = new LinearLayout.LayoutParams(-2, -2);
            zlp.setMargins(dp(ctx, 1), 0, 0, 0);
            sZhuang.setLayoutParams(zlp);
            head.addView(sZhuang);
            View spacer = new View(ctx);
            head.addView(spacer, new LinearLayout.LayoutParams(0, 1, 1f));

            TextView gear = labelView(ctx, "⚙", 0x99FFFFFF, 15f, false);
            gear.setLayoutParams(new LinearLayout.LayoutParams(dp(ctx, 26), -1));
            gear.setGravity(Gravity.CENTER);
            head.addView(gear);

            sChev = labelView(ctx, "▲", 0x88FFFFFF, 11f, false);
            sChev.setLayoutParams(new LinearLayout.LayoutParams(dp(ctx, 18), -1));
            sChev.setGravity(Gravity.CENTER);
            head.addView(sChev);
            root.addView(head, new LinearLayout.LayoutParams(-1, TITLE_H));

            // ── AI 行（55dp）
            LinearLayout aiLine = new LinearLayout(ctx);
            aiLine.setOrientation(LinearLayout.HORIZONTAL);
            aiLine.setGravity(Gravity.CENTER_VERTICAL);
            aiLine.setPadding(dp(ctx, 8), 0, dp(ctx, 8), 0);
            TextView badge = labelView(ctx, "✦ AI", 0xFF172F29, 10f, true);
            badge.setGravity(Gravity.CENTER);
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(0xFFFFD56B); bg.setCornerRadius(5f); bg.setStroke(1, 0x66FFFFFF);
            badge.setBackground(bg);
            aiLine.addView(badge, new LinearLayout.LayoutParams(dp(ctx, 34), dp(ctx, 19)));
            sAiText = labelView(ctx, "等待建议", 0xFFE5EDE7, 11f, true);
            sAiText.setPadding(dp(ctx, 5), 0, 0, 0);
            sAiText.setSingleLine(false);
            sAiText.setMaxLines(3);
            sAiText.setEllipsize(TextUtils.TruncateAt.END);
            aiLine.addView(sAiText, new LinearLayout.LayoutParams(0, -2, 1f));
            sAiLine = aiLine;
            root.addView(aiLine, new LinearLayout.LayoutParams(-1, dp(ctx, 55)));

            View div = new View(ctx);
            div.setBackgroundColor(0x1AFFFFFF);
            root.addView(div, new LinearLayout.LayoutParams(-1, 1));

            sBody = new LinearLayout(ctx);
            sBody.setOrientation(LinearLayout.VERTICAL);
            sBody.setPadding(dp(ctx, 11), dp(ctx, 3), dp(ctx, 11), dp(ctx, 7));
            root.addView(sBody, new LinearLayout.LayoutParams(-1, -2));

            sFoot = labelView(ctx, "", 0xFF7C8C82, 11f, false);
            sFoot.setPadding(dp(ctx, 12), dp(ctx, 2), dp(ctx, 12), dp(ctx, 7));
            root.addView(sFoot, new LinearLayout.LayoutParams(-1, -2));

            sSettings = buildSettings(ctx);
            root.addView(sSettings, new LinearLayout.LayoutParams(-1, -2));

            sLp = new WindowManager.LayoutParams(
                    UI_W, -2,
                    Build.VERSION.SDK_INT >= 26 ? 2038 : WindowManager.LayoutParams.TYPE_PHONE,
                    40, android.graphics.PixelFormat.TRANSLUCENT);
            sLp.gravity = Gravity.TOP | Gravity.LEFT;
            sLp.x = Math.max(0, SCREEN_W - UI_W - dp(ctx, 70));
            sLp.y = 0;

            root.setOnTouchListener(touch);
            sWm.addView(root, sLp);
            sRoot = root;

            sHandler = new Handler(Looper.getMainLooper());
            sHandler.post(refresh);
            log("悬浮窗已添加 type=" + sLp.type + " x=" + sLp.x + " y=" + sLp.y
                    + " w=" + sLp.width + " 屏幕=" + SCREEN_W);
            try { android.widget.Toast.makeText(ctx, "记牌器已启动", android.widget.Toast.LENGTH_SHORT).show(); } catch (Throwable ignored) {}
        } catch (Throwable t) {
            logErr("悬浮窗失败", t);
        }
    }

    // ------------------------------------------------------------------ 触摸
    // fagent.js 640-670：标题栏拖动；UP 时 rightGap∈[dp(24),dp(50)) 开设置，否则折叠。
    // 拖动要节流（686-697）：1px 内不重排，否则拖一次上百个 MOVE 会把主线程拖死。
    private static float dSX, dSY, dOX, dOY;
    private static boolean dMoved = false;

    private static final View.OnTouchListener touch = new View.OnTouchListener() {
        @Override public boolean onTouch(View v, MotionEvent ev) {
            int act = ev.getActionMasked();
            if (act == MotionEvent.ACTION_DOWN) {
                dSX = ev.getRawX(); dSY = ev.getRawY();
                dOX = sLp.x; dOY = sLp.y;
                dMoved = false;
                return (ev.getRawY() - dOY) <= TITLE_H + 6;
            }
            if (act == MotionEvent.ACTION_MOVE) {
                float dx = ev.getRawX() - dSX, dy = ev.getRawY() - dSY;
                if (!dMoved && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) dMoved = true;
                if (dMoved) {
                    int nx = Math.round(dOX + dx), ny = Math.round(dOY + dy);
                    if (Math.abs(nx - sLp.x) >= 1 || Math.abs(ny - sLp.y) >= 1) {
                        sLp.x = nx; sLp.y = ny;
                        try { sWm.updateViewLayout(sRoot, sLp); } catch (Throwable ignored) {}
                    }
                }
                return true;
            }
            if (act == MotionEvent.ACTION_UP || act == MotionEvent.ACTION_CANCEL) {
                if (!dMoved) {
                    int rightGap = (int) ((dOX + UI_W) - ev.getRawX());
                    if (rightGap >= dp(sCtx, 24) && rightGap < dp(sCtx, 50)) toggleSettings();
                    else toggleCollapse();
                }
                dMoved = false;
                return true;
            }
            return false;
        }
    };

    private static void toggleCollapse() {
        sCollapsed = !sCollapsed;
        // ★ 折叠要把 AI 建议一起收进去（用户要求）
        sBody.setVisibility(sCollapsed ? View.GONE : View.VISIBLE);
        sFoot.setVisibility(sCollapsed ? View.GONE : View.VISIBLE);
        if (sAiLine != null) sAiLine.setVisibility(sCollapsed ? View.GONE : View.VISIBLE);
        sChev.setText(sCollapsed ? "▼" : "▲");
    }

    private static void toggleSettings() {
        sInSettings = !sInSettings;
        sSettings.setVisibility(sInSettings ? View.VISIBLE : View.GONE);
    }

    /** 设置面板（fagent buildSettings 简化版：透明度 + 说明）。 */
    private static View buildSettings(Context ctx) {
        LinearLayout p = new LinearLayout(ctx);
        p.setOrientation(LinearLayout.VERTICAL);
        p.setPadding(dp(ctx, 14), dp(ctx, 6), dp(ctx, 14), dp(ctx, 12));
        p.setVisibility(View.GONE);
        p.addView(labelView(ctx, "背景透明度", CLR_LABEL, 12f, false));
        SeekBar sb = new SeekBar(ctx);
        sb.setMax(255 - 60);
        sb.setProgress(ALPHA - 60);
        sb.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar seekBar, int v, boolean u) {
                ALPHA = 60 + v;
                try { sRoot.setBackground(cardBg(ALPHA)); } catch (Throwable ignored) {}
            }
            @Override public void onStartTrackingTouch(SeekBar seekBar) {}
            @Override public void onStopTrackingTouch(SeekBar seekBar) {}
        });
        p.addView(sb);
        p.addView(labelView(ctx, "点标题栏收起 · 齿轮打开设置 · 标题栏可拖动", CLR_DIM, 10f, false));
        return p;
    }

    // ------------------------------------------------------------------ 渲染
    private static Runnable refresh = new Runnable() {
        @Override public void run() {
            try { render(); } catch (Throwable t) { log("render: " + t); }
            if (sHandler != null) sHandler.postDelayed(this, 500);
        }
    };

    private static void render() throws Exception {
        if (sCtx == null) return;
        String raw = read(sCtx, STATE);
        if (raw == null) { sFoot.setText("等采集器"); return; }
        JSONObject o = new JSONObject(raw);
        sBody.removeAllViews();

        // ★ 清空流程：只有「局中态」才记牌。退出房间/结算后 tState 会离开这几个值，
        //   此时必须整块清零 —— 否则上一局的底牌/首出/门子会一直挂在那儿（用户实测）。
        //   3 出牌 / 4 等出牌 / 5 等响应 / 6 结算 / 8 胡牌确认
        int tStateNow = o.optInt("tState", -999);
        boolean inGame = tStateNow == 3 || tStateNow == 4 || tStateNow == 5
                || tStateNow == 6 || tStateNow == 8;
        // ★ 退出房间的信号游戏不直接给（sData 不置空），只能用「停摆」推断：
        //   结算态(6)连续 60 秒且 lastPut/cardNext 都没动过 → 人已经走了 → 立即清空。
        //   （正常局里结算后几秒内就会发新牌；回放暂停不受影响，因为回放分支不触发这条。）
        String sig = tStateNow + "|" + o.optInt("lastPut") + "|" + o.optInt("cardNext")
                + "|" + o.optString("tableid");
        long now = System.currentTimeMillis();
        if (!sig.equals(sLastSig)) { sLastSig = sig; sLastActivity = now; }
        // ★ 回放也要清：退回选择列表后状态停摆，跟退出房间是一样的。
        //   （代价：回放暂停超过 60 秒面板会清空，重进刷新即可。）
        // ★ 回放退出到列表时 tState 停在 4（不是 6），且 sData 不置空 —— 没有退出信号，
        //   只能靠停摆推断。回放态 15 秒即清（对局结算态仍给 60 秒，避免误清）。
        long replayGap = isReplay(o) ? 15000 : 60000;
        boolean stale = ((tStateNow == 6 && !isReplay(o)) || isReplay(o))
                && sLastActivity > 0 && (now - sLastActivity) > replayGap;
        if (o.optInt("none", 0) == 1 || o.isNull("tState") || !inGame || stale) {
            sFoot.setText("不在牌桌");
            sZhuang.setText("");
            sAiText.setText("等待建议");
            updateMini(sMiniBottom, 0);
            updateMini(sMiniFirst, 0);
            updateMini(sMiniBottom, 0);
            updateMini(sMiniFirst, 0);
            return;
        }

        int deckLeft = Math.max(0, 80 - o.optInt("choupai", 20) - o.optInt("cardNext", 0));
        int zhuang = o.optInt("zhuang", -1);
        // ★ me 有两种写法：uid 或**座位号**（实测快照里有 "me": 1）。
        //   拿座位号比 uid 会永远不相等 → 两家都成了「对面」→ 自己的门子/手牌被算进去。
        //   这里就地解析成 uid，后面的 me.equals(...) 才能正常工作。
        String me = resolveMeUid(o);
        // ★ 硬保护：认不出「我」就**什么都不记**。
        //   否则 me 为空时所有座位都判成「对面」，第一个座位就是自己 → 自己的牌全被算进去。
        if (me == null || me.length() == 0) {
            sFoot.setText("me 未识别 · 暂不记牌");
            sZhuang.setText("");
            sAiText.setText(isReplay(o) ? "回放中 · AI 不生效" : "等待建议");
            return;
        }

        // 庄家标记：只有对家为庄，底牌/首出才是对方的（fagent 543-552）
        JSONArray seats = o.optJSONArray("seats");
        String oppZhuang = "";
        int mySeat = -1, zhuSeat = -1;
        if (seats != null) {
            for (int i = 0; i < seats.length(); i++) {
                JSONObject s = seats.optJSONObject(i);
                if (s == null) continue;
                if (me.equals(s.optString("uid", ""))) mySeat = i;
            }
            zhuSeat = (zhuang >= 0 && zhuang < seats.length()) ? zhuang : -1;
            // ★ 对家为庄 → 金色「对家为庄」；否则灰色「对家非庄」（fagent 792-799，别写反）
            oppZhuang = (zhuSeat >= 0 && zhuSeat != mySeat) ? "对家为庄" : "对家非庄";
        }
        sZhuang.setText(oppZhuang);
        sZhuang.setTextColor("对家为庄".equals(oppZhuang) ? 0xFFD8A84E : 0xFF6B7A70);

        // ★ 本局起点：ev 跨局累积，底牌/首出/门子都必须只认 roundEnd 之后的本局事件。
        JSONArray evScan = o.optJSONArray("ev");
        int evStart = 0;
        if (evScan != null) {
            for (int i = evScan.length() - 1; i >= 0; i--) {
                JSONObject e = evScan.optJSONObject(i);
                if (e != null && "roundEnd".equals(e.optString("e", ""))) { evStart = i + 1; break; }
            }
        }
        // 首出 = 对手本局第一张打出的牌（tracker.py:550）。只在**对家为庄**时有意义。
        // ★ 不能用 put[0]：被吃/碰掉的牌会从 put 里消失，取到的就不是首出了。
        if (zhuSeat >= 0 && zhuSeat != mySeat) {
            String ou = "";
            for (int i = 0; i < seats.length(); i++) {
                JSONObject s2 = seats.optJSONObject(i);
                if (s2 != null && !me.equals(s2.optString("uid", ""))) {
                    ou = s2.optString("uid", "");
                    break;
                }
            }
            updateMini(sMiniFirst, firstOppPut(evScan, evStart, ou));
        } else {
            updateMini(sMiniFirst, 0);
        }
            // 底牌：只有**对家为庄**才记（tracker.py 247-255 的口径）——
            // 那时底牌才是「对方的」信息。取值：xingCard → caiPaiArr 第一个。
            if (zhuSeat >= 0 && zhuSeat != mySeat) {
                // 最准：开局第一条 newCard 的 lastPut（tracker.py 297）
                int bottom = firstNewCardLastPut(evScan, evStart);
                if (bottom <= 0) {
                    JSONArray cai = o.optJSONArray("caiPaiArr");
                    if (cai != null && cai.length() > 0) bottom = cai.optInt(0);
                    if (bottom <= 0) {
                        JSONObject cd = o.optJSONObject("caiPaiArr");
                        if (cd != null) {
                            java.util.Iterator<String> it = cd.keys();
                            while (it.hasNext()) {
                                int v = cd.optInt(it.next());
                                if (v > 0) { bottom = v; break; }
                            }
                        }
                    }
                }
                updateMini(sMiniBottom, bottom > 0 ? bottom : 0);
            } else {
                updateMini(sMiniBottom, 0);
            }

        // 门子行：4 格右对齐 + → + after
        int[] evPtr = new int[]{0};
        // ★ 记牌器只记**对面**：自己的门子/手牌一律不算（回放里两家的都是满的）。
        //   body 只有门子行 —— fagent 的 rowView 本来就没有手牌行。
        JSONObject opp = null;
        if (seats != null) {
            for (int i = 0; i < seats.length(); i++) {
                JSONObject s = seats.optJSONObject(i);
                if (s == null) continue;
                if (!me.equals(s.optString("uid", ""))) { opp = s; break; }
            }
        }
        if (opp != null) {
            // 事件流跨局累积，只认本局：从最后一个 roundEnd 之后开始扫
            JSONArray evAll = o.optJSONArray("ev");
            int start = 0;
            if (evAll != null) {
                for (int i = evAll.length() - 1; i >= 0; i--) {
                    JSONObject e = evAll.optJSONObject(i);
                    if (e != null && "roundEnd".equals(e.optString("e", ""))) { start = i + 1; break; }
                }
            }
            // ★ 门子行按**事件时间顺序**构建（和 tracker.py 同一条路）。
            //   之前按「碰/偎/提/吃」类别遍历快照数组，顺序跟事件轴对不上，
            //   after 配对就全乱了。
            String oppUid = opp.optString("uid", "");
            for (int i = start; evAll != null && i < evAll.length(); i++) {
                JSONObject e = evAll.optJSONObject(i);
                if (e == null) continue;
                if (!uidIs(e, oppUid)) continue;
                String n = e.optString("e", "");
                JSONObject d = e.optJSONObject("d");
                if (d == null) continue;
                int[] cells = null;
                String kind = null;
                if ("MJPeng".equals(n) || "MJPeng1".equals(n)) {
                    int c = d.optInt("card");
                    cells = new int[]{c, c, c};
                    kind = "MJPeng1".equals(n) ? "偎" : "碰";
                } else if ("MJGang".equals(n) || "MJGang1".equals(n)) {
                    int c = d.optInt("card");
                    cells = new int[]{c, c, c, c};
                    kind = "MJGang".equals(n) ? "提" : "跑";
                } else if ("MJChi".equals(n)) {
                    JSONArray mc = d.optJSONArray("mjchi");
                    if (mc != null && mc.length() >= 3) {
                        int[] chiCards = new int[]{mc.optInt(0), mc.optInt(1), mc.optInt(2)};
                        java.util.Arrays.sort(chiCards);     // ★ 肆伍陆，不是伍陆肆
                        cells = chiCards;
                        kind = "吃";
                    }
                } else if ("MJLie".equals(n)) {
                    cells = new int[]{d.optInt("card")};
                    kind = "摆";
                }
                if (cells == null) continue;
                sBody.addView(meldRow(sCtx, kind, cells, nextAfter(evAll, i + 1, oppUid)));
            }
        }

        // 底部信息行：对方已出 + 墩剩
        StringBuilder foot = new StringBuilder();
        if (seats != null) {
            for (int i = 0; i < seats.length(); i++) {
                JSONObject s = seats.optJSONObject(i);
                if (s == null || me.equals(s.optString("uid", ""))) continue;
                JSONArray put = s.optJSONArray("put");
                if (put != null && put.length() > 0) {
                    foot.append("对方出 ");
                    for (int k = 0; k < put.length() && k < 12; k++) {
                        foot.append(cardName(put.optInt(k)));
                        if (k < Math.min(put.length(), 12) - 1) foot.append(' ');
                    }
                }
            }
        }
        if (foot.length() > 0) foot.append("   |   ");
        foot.append("墩剩").append(cn(deckLeft));
        // 门子多时卡片会超屏，只留最近 8 行（fagent MAX_ROWS 语义）
        if (sBody.getChildCount() > 8) {
            sBody.removeViews(0, sBody.getChildCount() - 8);
        }
        sFoot.setText(foot.toString());

        // ★ 回放和对局的逻辑必须分开（用户要求）：
        //   回放时 AI 不生效，只做记牌；对局时才读 pct_ai.json。
        boolean replay = isReplay(o);
        if (replay) {
            sHead.setText("回放");
            sAiText.setText("回放中 · AI 不生效");
        } else {
            sAiText.setText(aiText());
        }
    }

    /**
     * after = 门子成型后，**对手**打出的下一张（tracker.py 555）。
     * from 是门子事件的下标，从这里往后找第一个对手 MJPut。
     */
    private static int nextAfter(JSONArray ev, int from, String oppUid) {
        if (ev == null) return 0;
        for (int i = from; i < ev.length(); i++) {
            JSONObject e = ev.optJSONObject(i);
            if (e == null) continue;
            if (!"MJPut".equals(e.optString("e", ""))) continue;
            if (!uidIs(e, oppUid)) continue;
            JSONObject d = e.optJSONObject("d");
            return d == null ? 0 : d.optInt("card");
        }
        return 0;
    }

    /** me 可能是 uid，也可能是座位号 —— 统一解析成 uid。认不出返回 ""。 */
    private static String resolveMeUid(JSONObject o) {
        JSONArray seats = o.optJSONArray("seats");
        String raw = o.optString("me", "");
        if (seats == null) return raw;
        for (int i = 0; i < seats.length(); i++) {
            JSONObject s = seats.optJSONObject(i);
            if (s != null && raw.equals(s.optString("uid", ""))) return raw;
        }
        try {
            int idx = Integer.parseInt(raw.trim());
            if (idx >= 0 && idx < seats.length()) {
                JSONObject s = seats.optJSONObject(idx);
                if (s != null) return s.optString("uid", "");
            }
        } catch (NumberFormatException ignored) {}
        return "";
    }

    /**
     * 回放识别：**两家 handKnown 都为 True** —— 只有回放才这样。
     * 正常对局里对手 mjhand 服务端不下发（handKnown=false）。
     * 回放时的规则：
     *   - 只记**对面**那一家的门子/出牌（自己的一律不算）
     *   - AI 不生效（不读 pct_ai.json）
     */
    private static boolean isReplay(JSONObject o) {
        JSONArray seats = o.optJSONArray("seats");
        if (seats == null || seats.length() < 2) return false;
        for (int i = 0; i < seats.length(); i++) {
            JSONObject s = seats.optJSONObject(i);
            if (s == null || !s.optBoolean("handKnown", false)) return false;
        }
        return true;
    }

    /** 对手本局第一张打出的牌（首出）。只认 MJPut 且 uid 匹配。 */
    private static int firstOppPut(JSONArray ev, int from, String oppUid) {
        if (ev == null) return 0;
        for (int i = from; i < ev.length(); i++) {
            JSONObject e = ev.optJSONObject(i);
            if (e == null || !"MJPut".equals(e.optString("e", ""))) continue;
            if (!uidIs(e, oppUid)) continue;
            JSONObject d = e.optJSONObject("d");
            int c = d == null ? 0 : d.optInt("card");
            if (c > 0) return c;
        }
        return 0;
    }

    /** 开局第一条 newCard 的 lastPut = 底牌（tracker.py 297 说这是最准的）。 */
    private static int firstNewCardLastPut(JSONArray ev, int from) {
        if (ev == null) return 0;
        for (int i = from; i < ev.length(); i++) {
            JSONObject e = ev.optJSONObject(i);
            if (e == null || !"newCard".equals(e.optString("e", ""))) continue;
            JSONObject d = e.optJSONObject("d");
            if (d == null) continue;
            int c = d.optInt("lastPut");
            if (c > 0) return c;
            c = d.optInt("card");
            if (c > 0) return c;
        }
        return 0;
    }

    private static boolean uidIs(JSONObject e, String uid) {
        JSONObject d = e == null ? null : e.optJSONObject("d");
        return d != null && uid.equals(String.valueOf(d.optInt("uid")));
    }

    /** fagent rowView：4 格右对齐 + → + after。空位补左边。 */
    private static View meldRow(Context ctx, String kind, int[] cards, int after) {
        LinearLayout rowL = new LinearLayout(ctx);
        rowL.setOrientation(LinearLayout.HORIZONTAL);
        rowL.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams rowLP = new LinearLayout.LayoutParams(-1, dp(ctx, CARD_DP));
        rowLP.setMargins(0, dp(ctx, 2), 0, dp(ctx, 2));
        rowL.setLayoutParams(rowLP);

        LinearLayout g1 = new LinearLayout(ctx);
        g1.setOrientation(LinearLayout.HORIZONTAL);
        g1.setGravity(Gravity.CENTER_VERTICAL);
        Integer[] cells = new Integer[4];
        for (int i = 0; i < cards.length && i < 4; i++) cells[i] = cards[i];
        // 空位补左边（fagent.js 456）
        Integer[] fixed = new Integer[4];
        int pad = 4 - cards.length;
        for (int i = 0; i < pad; i++) fixed[i] = null;
        for (int i = 0; i < cards.length && i < 4; i++) fixed[pad + i] = cards[i];
        for (int k = 0; k < 4; k++) g1.addView(fixed[k] == null ? slotView(ctx) : paiView(ctx, fixed[k]));
        rowL.addView(g1);

        TextView arrow = labelView(ctx, "→", 0x50FFFFFF, 15f, false);
        LinearLayout.LayoutParams alp = new LinearLayout.LayoutParams(dp(ctx, 14), -2);
        alp.setMargins(dp(ctx, 2), 0, dp(ctx, 2), 0);
        arrow.setLayoutParams(alp);
        arrow.setGravity(Gravity.CENTER);
        rowL.addView(arrow);

        LinearLayout g2 = new LinearLayout(ctx);
        g2.setOrientation(LinearLayout.HORIZONTAL);
        g2.addView(paiView(ctx, after));
        rowL.addView(g2);
        return rowL;
    }

    // ------------------------------------------------------------------ 牌组件
    private static View paiView(Context ctx, int v) {
        ImageView iv = new ImageView(ctx);
        iv.setScaleType(ImageView.ScaleType.FIT_CENTER);
        if (v == 0) {
            iv.setBackground(rrect(CLR_BACK, 4, 0xFFFFFFFF, 1));   // 盖牌
        } else {
            iv.setBackground(rrect(CLR_CARD, 4, 0x33000000, 1));   // 牌底 #EFEFEF
            Bitmap b = loadBmp(v);
            if (b != null) iv.setImageBitmap(b);
            iv.setPadding(dp(ctx, 2), dp(ctx, 2), dp(ctx, 2), dp(ctx, 2));
        }
        iv.setLayoutParams(new LinearLayout.LayoutParams(dp(ctx, CARD_DP), dp(ctx, CARD_DP)));
        return iv;
    }

    private static View slotView(Context ctx) {
        TextView tv = labelView(ctx, "", CLR_LABEL, 10f, false);
        tv.setBackground(rrect(0x14FFFFFF, 4, 0x22FFFFFF, 1));
        tv.setLayoutParams(new LinearLayout.LayoutParams(dp(ctx, CARD_DP), dp(ctx, CARD_DP)));
        return tv;
    }

    private static ImageView miniPaiView(Context ctx) {
        ImageView iv = new ImageView(ctx);
        iv.setScaleType(ImageView.ScaleType.FIT_CENTER);
        iv.setBackground(rrect(0x3DFFFFFF, 4, 0x22FFFFFF, 1));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(ctx, MINI_DP), dp(ctx, MINI_DP));
        lp.setMargins(dp(ctx, 1), 0, dp(ctx, 1), 0);
        iv.setLayoutParams(lp);
        return iv;
    }

    private static void updateMini(ImageView iv, int v) {
        if (iv == null) return;
        if (v == 0) {
            iv.setImageDrawable(null);
            iv.setBackground(rrect(0x3DFFFFFF, 4, 0x22FFFFFF, 1));
            return;
        }
        iv.setBackground(rrect(CLR_CARD, 4, 0x33000000, 1));
        Bitmap b = loadBmp(v);
        if (b != null) iv.setImageBitmap(b);
    }

    private static final java.util.HashMap<Integer, Bitmap> sCardCache = new java.util.HashMap<>();

    /**
     * 牌面：先读 /data/local/tmp/pct_cards/（开发机有 root 时用），
     * 读不到就用**打进 dex 的 base64 素材** —— 真机没 root，/data/local/tmp 不可读，
     * 只靠外部文件的话牌面全是空白（用户实测）。
     */
    private static Bitmap loadBmp(int v) {
        try {
            int idx = v > 100 ? (v - 100 + 10) : v;      // fagent.js 322-324
            Bitmap cached = sCardCache.get(idx);
            if (cached != null) return cached;
            String s = "00" + idx;
            String path = CARD_DIR + "Card_" + s.substring(s.length() - 3) + ".png";
            Bitmap b = BitmapFactory.decodeFile(path);
            if (b == null) b = fromDex(idx);
            if (b == null) return null;
            int px = dp(sCtx, CARD_DP);
            b = Bitmap.createScaledBitmap(b, px, px, true);
            sCardCache.put(idx, b);
            return b;
        } catch (Throwable t) {
            return null;
        }
    }

    /** 内置牌面（Cards.B64）解码 + 缓存。 */
    private static Bitmap fromDex(int idx) {
        try {
            if (idx < 0 || idx >= Cards.B64.length) return null;
            String b64 = Cards.B64[idx];
            if (b64 == null || b64.length() == 0) return null;
            byte[] raw = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
            return BitmapFactory.decodeByteArray(raw, 0, raw.length);
        } catch (Throwable t) {
            return null;
        }
    }

    // ------------------------------------------------------------------ 工具
    private static GradientDrawable cardBg(int alpha) {
        GradientDrawable g = new GradientDrawable();
        g.setColor((alpha << 24) | 0x0E1611);
        g.setCornerRadius(18f);
        g.setStroke(1, 0x28FFFFFF);
        return g;
    }

    private static GradientDrawable rrect(int color, float radius, int stroke, int sw) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(radius * 2);
        if (stroke != 0) g.setStroke(sw, stroke);
        return g;
    }

    private static TextView labelView(Context ctx, String text, int color, float sp, boolean bold) {
        TextView tv = new TextView(ctx);
        tv.setText(text);
        tv.setTextColor(color);
        tv.setTextSize(sp);
        tv.setIncludeFontPadding(false);
        if (bold) tv.setTypeface(Typeface.DEFAULT_BOLD);
        return tv;
    }

    private static String read(Context ctx, String name) {
        File f = new File(ctx.getFilesDir(), name);
        if (!f.exists()) return null;
        FileInputStream in = null;
        try {
            in = new FileInputStream(f);
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            return new String(bo.toByteArray(), "UTF-8");
        } catch (Throwable t) {
            return null;
        } finally {
            try { if (in != null) in.close(); } catch (Throwable ignored) {}
        }
    }

    private static String aiText() {
        String raw = read(sCtx, AI);
        if (raw == null) return "等待建议";
        try {
            return new JSONObject(raw).optString("text", "等待建议");
        } catch (Throwable t) {
            return "等待建议";
        }
    }

    private static String cardName(int v) {
        if (v == 0) return "";
        String x = "一二三四五六七八九十", d = "壹贰叁肆伍陆柒捌玖拾";
        if (v >= 101 && v <= 110) return String.valueOf(d.charAt(v - 101));
        if (v >= 1 && v <= 10) return String.valueOf(x.charAt(v - 1));
        return "";
    }

    private static String cn(int n) {
        String x = "零一二三四五六七八九";
        if (n < 0) return "?";
        if (n < 10) return String.valueOf(x.charAt(n));
        if (n == 10) return "十";
        if (n < 20) return "十" + x.charAt(n - 10);
        return String.valueOf(x.charAt(n / 10)) + "十" + (n % 10 == 0 ? "" : String.valueOf(x.charAt(n % 10)));
    }

    private static int dp(Context ctx, int v) {
        return (int) (v * ctx.getResources().getDisplayMetrics().density);
    }

    private static void log(String s) {
        de.robv.android.xposed.XposedBridge.log(TAG + s);
        try {
            java.io.File dir = new java.io.File("/sdcard/pct");
            if (!dir.exists()) dir.mkdirs();
            java.io.FileWriter w = new java.io.FileWriter(
                    new java.io.File(dir, "init.log"), true);
            w.write(new java.text.SimpleDateFormat("HH:mm:ss").format(new java.util.Date())
                    + " [ui] " + s + "\n");
            w.close();
        } catch (Throwable ignored) {}
    }

    /** 把异常连堆栈一起写日志 —— 真机上这是唯一的线索。 */
    private static void logErr(String stage, Throwable t) {
        StringBuilder sb = new StringBuilder(stage).append(": ").append(t);
        for (StackTraceElement e : t.getStackTrace()) {
            sb.append("\n    at ").append(e);
        }
        log(sb.toString());
    }
}
