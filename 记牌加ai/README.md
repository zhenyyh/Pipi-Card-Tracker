# 记牌加AI · LSPatch + Dobby 版

跑胡子（`com.happyplay.hnphz`）的**数据桥**。把 `pct/` 那套已经验证过的采集逻辑，
从 frida 注入换成 LSPatch 模块注入，**产出一模一样的 `pct_state.json`**。

## 这个目录做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 在游戏进程里注入常驻 JS 采集器 | 不做解析（那是 `pct/tracker.py` 的活） |
| 落盘 `pct_state.json`（字段与现版完全一致） | 不做 AI（那是 `pct/ai.py` 的活） |
| 跟着进程生命周期自愈 | 不做悬浮窗（那是 `pct/fagent.js` + `pct/overlay.py` 的活） |

**关键取舍：只换注入通道，不动下游。** 所以 `pct/` 里的解析、守恒对账、AI 建议、
悬浮窗全部可以原样跑，一行不用改。

## 架构

```
LSPatch 模块（本目录）
  └─ MainHook（Xposed 入口，只在 com.happyplay.hnphz 里生效）
       └─ Injector
            ├─ hook android.app.Application.attach(Context)  ← 拿 Context，进程启动必触发
            ├─ 从 assets 读 collector.js
            └─ Cocos2dxHelper.runOnGLThread → Cocos2dxJavascriptJavaBridge.evalString(js)
                 └─ JS 在游戏自己的 SpiderMonkey 里跑
                      ├─ 挂钩 pomelo emit/notify/request + netCallBack
                      ├─ 每 600ms / 每次事件 拍一份 sData 快照
                      └─ jsb.fileUtils.writeStringToFile → pct_state.json
```

落盘路径：`/data/user/0/com.happyplay.hnphz/files/pct_state.json`
（root 下 `adb shell su -c 'cat ...'` 可直接读；`pct/overlay.py` 走 frida 读同一份）

## 为什么不用 Dobby 抓数据

`jsclient.data.sData`、`pomelo` 全在**游戏 JS 堆**里，不是 native 内存。
从 C++ 侧只能拿到 `JSContext*` / `JS::Value`，要自己解引擎私有布局。

实测本包：

```
org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge   ← 类存在
  evalString                                    ← 方法存在
org.cocos2dx.lib.Cocos2dxHelper.runOnGLThread   ← 存在
org.cocos2dx.javascript.AppActivity             ← 入口
lib/arm64-v8a/libccjs.so                        ← JS 引擎（25MB）
```

牌局数据没有一份落在 native 内存，**Dobby hook 拿不到任何牌**。
Dobby 只留了备用模板（`app/src/main/jni/`），用于 evalString 被加固/改名的情况，
启用方式见 [docs/DENS.md](docs/DENS.md)。

## 构建

```bash
cd 记牌加ai
./gradlew :app:assembleRelease
# 产物：app/build/outputs/apk/release/app-release.apk（这就是"模块"）
```

需要 Android SDK 34 + JDK 17。`compileOnly 'de.robv.android.xposed:api:82'`
——Xposed API 由 LSPatch 提供，**不要打进包里**。

## 打包进原版 APK

```bash
# 本地模式：模块直接嵌入，不需要装 LSPatch 管理器
java -jar lspatch.jar 素材/phz.20241010.apk \
     -m 记牌加ai/app/build/outputs/apk/release/app-release.apk \
     -o phz-patched.apk

# 用 apksigner 重签（LSPatch 也会签，但自己签能控制证书）
apksigner sign --ks my.jks phz-patched.apk
```

装上去之后启动游戏，看日志：

```bash
adb logcat -s PCT/
# 期望看到：
#   PCT/ 进程已加载，装 Injector
#   PCT/ evalString(t+1500) → true，脚本 12345 字符
```

再看文件有没有生成：

```bash
adb shell su -c 'ls -l /data/user/0/com.happyplay.hnphz/files/pct_state.json'
```

## 三条硬约束（frida 那版踩出来的，这里同样成立）

1. **`evalString` 只返回 `boolean`**，拿不到脚本返回值 → JS 必须自己落盘。
2. **落盘会被内容去重**：状态不变就不写，文件 mtime 会「冻住」。
   **「文件没变」不等于「游戏没动」** —— 实测冻过 8 分钟。别拿 mtime 当判据。
3. **`evalString` 阻塞 GL 线程**：250ms 一次会把游戏打到 ANR。
   正确做法是**注入一次**，之后由游戏自己按事件 + 定时器落盘，外侧只读文件。

## 第四条（frida 版最脆的一环，这版天然解决）

采集器活在游戏进程里，**进程一重启（切房间/闪退/被回收）钩子就全没了**。
frida 那版为此丢过 16 分钟数据（`pct_state.json` 冻在最后一帧）。

LSPatch 方案里，进程重启 → Xposed 会重新走 `MainHook` → 自动重新注入。
**自愈是白送的**，这也是换掉 frida 最主要的收益之一。

## 目录

```
记牌加ai/
  app/src/main/
    AndroidManifest.xml          xposedmodule / xposedscope 声明
    assets/
      xposed_init                入口类名（Xposed 靠它找 MainHook）
      collector.js               采集脚本（= pct/gs_install.js，已验证过）
    java/com/pct/lsp/
      MainHook.java              Xposed 入口
      Injector.java              evalString 注入 + GL 线程 + 重试
      NativeHook.java            Dobby 备用路径（默认关）
    jni/                         Dobby 模板（默认不参与构建）
  docs/DENS.md                   Dobby 什么时候才需要
```

## 下一步（本版没做）

- **AI 进模块**：现在 AI 在 `pct/ai.py`（Python + DeepSeek API）。
  要内置的话，在模块里加个前台 Service 读同一个 json、调 API、再用 Xposed
  把结果显示到游戏里（或做一个独立悬浮窗）。**建议先别动**——Python 侧已经
  验证过，改语言等于把验证过的逻辑重写一遍。
- **悬浮窗**：现版是 `fagent.js` 在游戏里画 Native View，靠 frida 调。
  模块版可以用同样的 Xposed `addView` 方式，但要重写一遍布局。
- **`needOneCard` 语义**：还没坐实（见 `pct/docs/CLIENT_INVENTORY.md`）。
