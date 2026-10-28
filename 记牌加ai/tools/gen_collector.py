# -*- coding: utf-8 -*-
"""从 assets/collector.js 生成 CollectorJs.java（把 JS 编译进 dex 常量）。

为什么不用 assets
----------------
LSPatch 以 "legacy module" 方式加载模块，**模块自己的 assets 在目标 App 的
AssetManager 里看不见** —— 实测 `ctx.getAssets().open("collector.js")` 抛
FileNotFoundException，注入静默失败。

把 JS 作为 String 常量编进 dex 最稳，代价是改完 JS 要重跑一次这个脚本
（`tools/build.sh` 第 1 步会自动跑）。
"""
import io, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'app', 'src', 'main', 'assets', 'collector.js')
DST = os.path.join(ROOT, 'app', 'src', 'main', 'java', 'com', 'pct', 'lsp', 'CollectorJs.java')


def main():
    src = io.open(SRC, encoding='utf-8').read()
    lines = src.split('\n')
    out = [
        'package com.pct.lsp;',
        '',
        '/**',
        ' * 采集脚本（由 tools/gen_collector.py 从 assets/collector.js 生成，别手改）。',
        ' *',
        ' * 为什么是常量而不是 assets：LSPatch 以 legacy module 方式加载，模块的 assets',
        ' * 在目标 App 的 AssetManager 里不可见（实测 FileNotFoundException）。',
        ' */',
        'public final class CollectorJs {',
        '    private CollectorJs() {}',
        '',
        '    public static final String JS = String.join("\\n", new String[]{',
    ]
    for ln in lines:
        esc = ln.replace('\\', '\\\\').replace('"', '\\"')
        out.append('        "%s",' % esc)
    out += ['    });', '}', '']
    io.open(DST, 'w', encoding='utf-8', newline='\n').write('\n'.join(out))
    print('  生成 %s（%d 行 JS，%d 字节）' % (os.path.basename(DST), len(lines), os.path.getsize(DST)))


if __name__ == '__main__':
    main()
