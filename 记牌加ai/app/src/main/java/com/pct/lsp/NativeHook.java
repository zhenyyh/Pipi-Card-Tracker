package com.pct.lsp;

import de.robv.android.xposed.XposedBridge;

/**
 * Dobby 备用路径（**默认不启用**）。
 *
 * 什么时候才需要它
 * ----------------
 * 只有当 Java 侧的 `Cocos2dxJavascriptJavaBridge.evalString` 走不通时才用：
 *   - 该类被加固/改名（实测本包未被混淆，类名和方法名都在）；
 *   - 或者要做「读 native 层状态」这种 Java 拿不到的事。
 *
 * 本包实测结论（2026-10-28，见 pct/docs/CLIENT_INVENTORY.md）：
 *   - 类名 `org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge` 存在，方法 `evalString` 存在
 *   - JS 引擎在 `lib/arm64-v8a/libccjs.so`（不是常见的 libcocos2djs.so）
 *   - 业务数据（sData / pomelo）全在 JS 堆里，native 侧看不到牌
 *
 * 所以：**数据链路根本不需要 Dobby**。这里留一个空实现，
 * 是为了需要 hook native（例如反调试绕过、或 evalString 失效时的
 * `ScriptingCore::evalString`）时有地方落笔。
 *
 * 启用步骤见 docs/DENS.md：
 *   1. 编译 libpctdobby.so（jni/ 下有模板）
 *   2. 把 .so 放进 app/src/main/jniLibs/arm64-v8a/
 *   3. 在 MainHook 里打开 NativeHook.tryInstall()
 */
public final class NativeHook {

    private static final String TAG = "PCT/native ";
    private static final String LIB = "pctdobby";

    private NativeHook() {}

    /** 加载并安装 native 钩子。失败只记日志，不影响 Java 主链路。 */
    public static void tryInstall() {
        try {
            System.loadLibrary(LIB);
            int n = nativeInstall();
            XposedBridge.log(TAG + "native 钩子安装数=" + n);
        } catch (Throwable t) {
            XposedBridge.log(TAG + "跳过（未打包 .so 或有符号不匹配）: " + t);
        }
    }

    private static native int nativeInstall();
}
