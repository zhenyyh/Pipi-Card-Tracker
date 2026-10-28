# Dobby 路径：什么时候用、怎么启用

## 先说结论

**数据链路不需要 Dobby。**

实测（2026-10-28，原版 APK `phz.20241010.apk`）：

| 项 | 结果 |
|---|---|
| `org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge` | 存在（dex 字符串命中） |
| 方法 `evalString` | 存在 |
| `org.cocos2dx.lib.Cocos2dxHelper.runOnGLThread` | 存在 |
| 入口 Activity | `org.cocos2dx.javascript.AppActivity` |
| JS 引擎 | `lib/arm64-v8a/libccjs.so`（不是常见的 libcocos2djs.so） |
| 业务数据位置 | 全在 JS 堆：`jsclient.data.sData`、`pomelo` |

牌局数据（手牌/门子/弃牌/牌墩）**没有一份在 native 内存里**，
所以从 C++ 侧 hook 拿不到任何牌。Java 侧 `evalString` 一行调用就能进 VM。

## 那 Dobby 还有用吗

有三种情况才需要：

1. **`evalString` 被加固/改名** —— 本包没有，但换版本可能变。届时改用
   `ScriptingCore::evalString` 的 native 偏移。
2. **要 hook 反调试/检测** —— 与本项目目标无关。
3. **要拿引擎内部状态**（例如 `leftCards` 之外的牌堆顺序）——
   理论上只有引擎知道，但实测服务端就没下发，hook 也变不出来。

## 启用步骤

1. 编 Dobby（静态库）：
   ```bash
   git clone https://github.com/jmpews/Dobby
   # 用 NDK 编 arm64-v8a，产出 libdobby.a
   ```
2. 放到 `app/src/main/jni/dobby/arm64-v8a/libdobby.a`
3. 用 IDA 打开 `libccjs.so`，找 `ScriptingCore::evalString` 的偏移，
   填进 `dobby_hook.cpp` 的 `target`
4. `app/build.gradle` 里打开 externalNativeBuild（见下），
   并在 `MainHook` 里调用 `NativeHook.tryInstall()`
5. 把 `.so` 打进 `jniLibs/`

```gradle
android {
    externalNativeBuild { cmake { path "src/main/jni/CMakeLists.txt" } }
    defaultConfig { ndk { abiFilters 'arm64-v8a' } }
}
```

## 风险

- 偏移量**随版本变化**，每次游戏更新都要重找
- `libccjs.so` 有 25MB，符号表可能被 strip；那就只能靠特征码定位
- 相比 Java 方案（30 行、签名稳定），这条路维护成本高一个量级
