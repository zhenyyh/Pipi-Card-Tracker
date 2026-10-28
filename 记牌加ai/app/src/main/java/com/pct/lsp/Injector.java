package com.pct.lsp;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;

/**
 * 把 {@link CollectorJs#JS} 注入游戏自己的 JS VM，并起一个悬浮窗。
 *
 * 最后一个坑（决定成败）
 * ----------------------
 * `evalString` **必须在游戏的 GL 线程上调用**。实测：
 *   - 游戏自己调 evalString 时顺势注入（已在引擎线程）→ 成功，文件正常落盘
 *   - 从后台 Java 线程反射调用 → 返回 true，但脚本没真跑（文件不生成）
 * `evalString` 恒返回 boolean，脚本抛异常也返回 true，所以「返回值」不能当成功判据。
 *
 * 因此这里两条路都做：
 *   1) **搭便车**：游戏自己调 evalString 时，就地注入（此时一定在正确线程）
 *   2) **看门狗**：每 3 秒把注入**post 到 GL 线程**再调；判据是「pct_state.json 出现没有」
 *      —— 出现了才算装上，否则继续重试。
 *
 * 其余三条约束（frida 那版踩出来的）依然成立：
 *   - evalString 只返回 boolean，拿不到脚本返回值 → JS 自己落盘
 *   - 落盘有内容去重，状态不变就不写 → 文件 mtime 会冻住
 *   - evalString 阻塞 GL 线程，不能高频调 → 只注入一次，之后靠脚本自己的定时器
 */
public final class Injector {

    private static final String TAG = "PCT/";
    private static final String BRIDGE = "org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge";
    private static final String HELPER = "org.cocos2dx.lib.Cocos2dxHelper";

    private static volatile ClassLoader sLoader;
    private static volatile Context sCtx;
    private static volatile boolean sDone;       // 落盘确认成功
    private static volatile boolean sBusy;       // 正在注入（防递归）
    private static volatile boolean sNoEngine;   // 这个进程没有 JS 引擎

    private Injector() {}

    public static void install(final ClassLoader loader) {
        sLoader = loader;
        installOverlay(loader);
        try {
            XposedHelpers.findAndHookMethod(BRIDGE, loader, "evalString", String.class,
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            // 自己递归进来（sBusy）或已完成（sDone）都跳过
                            if (sDone || sBusy) return;
                            // ★ 此刻我们就在引擎线程上：直接注入，这是成功率最高的一条路
                            injectNow("piggyback");
                        }
                    });
            log("已挂 evalString（游戏自己调用时就地注入）");
        } catch (Throwable t) {
            log("挂 evalString 失败: " + t);
        }
        startWatchdog();
    }

    // ------------------------------------------------------------------ 悬浮窗
    private static void installOverlay(final ClassLoader loader) {
        try {
            XposedHelpers.findAndHookMethod("android.app.Application", loader, "attach",
                    Context.class, new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            if (param.args == null || param.args.length == 0) return;
                            final Context ctx = (Context) param.args[0];
                            sCtx = ctx;
                            new Handler(Looper.getMainLooper()).postDelayed(new Runnable() {
                                @Override public void run() { Overlay.show(ctx); }
                            }, 1500);
                        }
                    });
            log("已挂 Application.attach（仅用于悬浮窗）");
        } catch (Throwable t) {
            log("挂 attach 失败（悬浮窗不可用）: " + t);
        }
    }

    // ------------------------------------------------------------------ 注入
    /** 就地注入（调用方需保证在 GL 线程上）。 */
    private static void injectNow(String why) {
        if (sDone || sBusy) return;
        sBusy = true;
        try {
            Class<?> c = XposedHelpers.findClass(BRIDGE, sLoader);
            Object ret = XposedHelpers.callStaticMethod(c, "evalString", CollectorJs.JS);
            if (stateWritten()) {
                sDone = true;
                log("注入成功(" + why + ")，pct_state.json 已生成");
            } else {
                log("注入(" + why + ") 返回 " + ret + " 但没落盘，稍后重试");
            }
        } catch (Throwable t) {
            log("注入失败(" + why + "): " + t);
        } finally {
            sBusy = false;
        }
    }

    /**
     * 把注入 post 到 GL 线程再调 evalString。
     * `Cocos2dxHelper.runOnGLThread` 在 Activity 还没起来时会 NPE —— 那就返回 false，
     * 由看门狗下一轮再试。
     */
    private static boolean injectOnGl(final String why) {
        final boolean[] done = new boolean[1];
        Runnable task = new Runnable() {
            @Override public void run() {
                injectNow(why);
                done[0] = true;
            }
        };
        try {
            Class<?> helper = XposedHelpers.findClass(HELPER, sLoader);
            XposedHelpers.callStaticMethod(helper, "runOnGLThread", task);
        } catch (Throwable t) {
            return false;
        }
        for (int i = 0; i < 20 && !done[0]; i++) {
            try { Thread.sleep(50); } catch (InterruptedException e) { return false; }
        }
        return done[0];
    }

    private static void startWatchdog() {
        new Thread(new Runnable() {
            @Override public void run() {
                int miss = 0;
                for (int i = 0; i < 40 && !sDone && !sNoEngine; i++) {
                    try { Thread.sleep(i == 0 ? 4000 : 3000); } catch (InterruptedException e) { return; }
                    if (sDone || sNoEngine) return;
                    if (sBusy) continue;
                    if (injectOnGl("watchdog#" + i) && sDone) return;
                    if (!injectOnGl("fallback#" + i)) {
                        if (++miss >= 10) {
                            sNoEngine = true;
                            log("放弃：这个进程没有可用的 JS 引擎/线程");
                        }
                    } else {
                        miss = 0;
                    }
                }
            }
        }, "pct-watchdog").start();
    }

    /** collector 有没有真跑起来 —— 只认它落的文件。 */
    private static boolean stateWritten() {
        try {
            Context c = sCtx;
            return c != null && new java.io.File(c.getFilesDir(), "pct_state.json").exists();
        } catch (Throwable t) {
            return false;
        }
    }

    private static void log(String s) {
        XposedBridge.log(TAG + s);
    }
}
