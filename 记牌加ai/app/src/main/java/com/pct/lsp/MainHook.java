package com.pct.lsp;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.callbacks.XC_LoadPackage.LoadPackageParam;

/**
 * LSPatch / Xposed 入口。
 *
 * 只干一件事：在目标进程起来时把 Injector 装上。
 * 进程重启（切房间/闪退/被回收）时 Xposed 会重新走这里，所以「自愈」是白送的——
 * 这正好补上 frida 那版最脆的一环（采集器活不过进程重启，实测丢过 16 分钟数据）。
 */
public class MainHook implements IXposedHookLoadPackage {

    /** 目标包名，与原版 APK 一致。 */
    public static final String PKG = "com.happyplay.hnphz";

    private static final String TAG = "PCT/";

    @Override
    public void handleLoadPackage(LoadPackageParam lp) {
        if (!PKG.equals(lp.packageName)) return;
        log("进程已加载，装 Injector（loader=" + lp.classLoader + "）");
        Injector.install(lp.classLoader);
    }

    static void log(String s) {
        XposedBridge.log(TAG + s);
    }
}
