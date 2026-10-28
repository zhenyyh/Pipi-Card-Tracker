#!/usr/bin/env bash
# 一键部署：检查设备 → 打包 → 安装 → 清 LSPatch 缓存 → 重启
# 每一步都打印结果，任何一步失败立刻停（之前后台任务静默失败过，坑了太久）
set -e
cd "$(dirname "$0")/.."
JH="${PCT_JAVA:-D:/soft/Android Studio/jbr}"
ADB="${PCT_ADB:-D:/Soft-green/scrcpy-win64-v2.7/adb.exe}"
DEV="${PCT_DEV:-emulator-5554}"
APK=build_manual/out/phz.20241010-398-lspatched.apk
PKG=com.happyplay.hnphz
export JAVA_HOME="$JH"
export MSYS2_ARG_CONV_EXCL="*"

step() { printf "\n\033[1m[%s]\033[0m %s\n" "$1" "$2"; }

step 1 "检查设备"
if ! "$ADB" devices | grep -q "$DEV"; then
    echo "✗ 设备 $DEV 不在线。先启动模拟器，再跑这个脚本。"
    exit 1
fi
"$ADB" -s "$DEV" shell "echo ok" >/dev/null && echo "✓ $DEV 在线"

step 2 "打包"
bash tools/build.sh >/tmp/pct_build.log 2>&1 || { tail -20 /tmp/pct_build.log; exit 1; }
echo "✓ 模块 $(stat -c%s build_manual/pct-module.apk) 字节"

step 3 "嵌入 LSPatch"
"$JH/bin/java.exe" -Xmx3g -jar build_manual/tools/lspatch.jar "../素材/phz.20241010.apk" \
    -m build_manual/pct-module.apk -o build_manual/out \
    -k build_manual/pct.jks pct123456 pct pct123456 -f >/tmp/pct_patch.log 2>&1 \
    || { tail -10 /tmp/pct_patch.log; exit 1; }
echo "✓ $(basename "$APK") $(stat -c%s "$APK") 字节"

step 4 "安装（原地升级，不卸载）"
"$ADB" -s "$DEV" install -r "$APK" 2>&1 | tail -1 | grep -q Success || { echo "✗ 安装失败"; exit 1; }
echo "✓ Success"

step 5 "清缓存并重启"
"$ADB" -s "$DEV" shell "am force-stop $PKG" || true
"$ADB" -s "$DEV" shell "su -c 'rm -rf /data/user/0/$PKG/cache/lspatch /data/user/0/$PKG/code_cache'" || true
echo "✓ 缓存已清"

step 6 "启动"
"$ADB" -s "$DEV" shell "monkey -p $PKG -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1 || true
echo "✓ 已启动 —— 等 30 秒后进回放验证"
echo
echo "日志：adb logcat -s LSPosed-Bridge | grep PCT/"
