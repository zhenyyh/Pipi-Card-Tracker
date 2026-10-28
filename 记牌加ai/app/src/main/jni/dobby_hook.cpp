// Dobby 备用钩子模板（默认不参与构建，除非你按 docs/DENS.md 放好 libdobby.a）
//
// 用途：当 Java 侧 evalString 走不通时，直接 hook libccjs.so 里的
//       ScriptingCore::evalString，从 native 侧把 JS 塞进去。
//
// 注意：C++ 符号在 release 包里通常被 strip，Dobby 需要用**偏移量**而不是符号名。
//      偏移量要用 IDA 对着 lib/arm64-v8a/libccjs.so 找，且**随版本变化**。
#include <jni.h>
#include <android/log.h>
#include <dlfcn.h>
#include <cstring>
#include "dobby.h"

#define TAG "PCT/native"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)

// 形如：bool ScriptingCore::evalString(const char*)
typedef bool (*eval_string_t)(void* self, const char* script);

static eval_string_t g_orig = nullptr;

static bool hook_eval(void* self, const char* script) {
    LOGI("evalString 被调用，长度=%zu", script ? strlen(script) : 0);
    return g_orig ? g_orig(self, script) : false;
}

extern "C" JNIEXPORT jint JNICALL
Java_com_pct_lsp_NativeHook_nativeInstall(JNIEnv*, jclass) {
    void* handle = dlopen("libccjs.so", RTLD_NOW);
    if (!handle) {
        LOGI("dlopen libccjs.so 失败: %s", dlerror());
        return 0;
    }
    // TODO: 换成你用 IDA 找到的偏移
    // void* target = (void*)((uintptr_t)dlsym(handle, "_ZN13ScriptingCore10evalStringEPKc"));
    void* target = nullptr;
    if (!target) {
        LOGI("没配偏移，跳过（见 docs/DENS.md）");
        return 0;
    }
    if (DobbyHook(target, (void*) hook_eval, (void**) &g_orig) == 0) {
        LOGI("hook 成功");
        return 1;
    }
    return 0;
}
