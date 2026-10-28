#!/usr/bin/env bash
# 离线手工构建：javac → d8 → aapt2 → zipalign → apksigner
# 不需要 Gradle / AGP（本机没装，且网络拉 AGP 容易卡在版本兼容）
set -e
cd "$(dirname "$0")/.."

# .bat 工具（d8/apksigner）读 %JAVA_HOME%，必须是 Windows 风格路径
JH="${PCT_JAVA:-D:/soft/Android Studio/jbr}"
export JAVA_HOME="$JH"
SDK="${ANDROID_SDK:-C:/Users/WCS/AppData/Local/Android/Sdk}"
BT="$SDK/build-tools/36.0.0"
AJ="$SDK/platforms/android-36/android.jar"
OUT=build_manual

JAVAC="$JH/bin/javac.exe"; JAR="$JH/bin/jar.exe"

echo "[1/8] 生成 CollectorJs.java"
python tools/gen_collector.py

echo "[2/8] javac"
rm -rf "$OUT/classes"; mkdir -p "$OUT/classes"
"$JAVAC" -encoding UTF-8 --release 11 -cp "$AJ" -d "$OUT/classes" \
    $(find "$OUT/stubs" app/src/main/java -name "*.java")

echo "[3/8] 剔除编译期桩（运行时由 LSPatch 提供）"
rm -rf "$OUT/classes/de"

echo "[4/8] jar"
"$JAR" cf "$OUT/classes.jar" -C "$OUT/classes" .

echo "[5/8] d8"
rm -rf "$OUT/dex"; mkdir -p "$OUT/dex"
"$BT/d8.bat" --min-api 21 --lib "$AJ" --output "$OUT/dex" "$OUT/classes.jar"

echo "[6/8] aapt2"
"$BT/aapt2.exe" compile --dir app/src/main/res -o "$OUT/res.zip"
"$BT/aapt2.exe" link -o "$OUT/module-unsigned.apk" -I "$AJ" \
    --manifest app/src/main/AndroidManifest.xml -A app/src/main/assets "$OUT/res.zip" \
    --min-sdk-version 21 --target-sdk-version 34 --version-code 1 --version-name 1.0

echo "[7/8] 装 dex + align"
python - <<'PY'
import zipfile
src='build_manual/module-unsigned.apk'; dst='build_manual/module-with-dex.apk'
zi=zipfile.ZipFile(src); zo=zipfile.ZipFile(dst,'w',zipfile.ZIP_DEFLATED)
for it in zi.infolist(): zo.writestr(it, zi.read(it.filename))
zo.write('build_manual/dex/classes.dex','classes.dex'); zo.close(); zi.close()
PY
"$BT/zipalign.exe" -f -p 4 "$OUT/module-with-dex.apk" "$OUT/module-aligned.apk"

echo "[8/8] 签名"
"$BT/apksigner.bat" sign --ks "$OUT/pct.jks" --ks-pass pass:pct123456 \
    --key-pass pass:pct123456 --out "$OUT/pct-module.apk" "$OUT/module-aligned.apk"
"$BT/apksigner.bat" verify "$OUT/pct-module.apk"
echo "OK → $OUT/pct-module.apk ($(stat -c%s "$OUT/pct-module.apk") 字节)"
