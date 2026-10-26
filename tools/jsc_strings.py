#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 cocos2d-x 的 .jsc（SpiderMonkey XDR 字节码）里精确抠出字符串原子。

格式（实测破解，不是猜的）
--------------------------
文件头 4 字节是 XDR 魔数 `2C C0 73 B9`。每个字符串原子前面有一个小端 uint32：

    uint32 v      # v == 2 * 字符数 + 1   ← 最低位是「字符串」标志，不是长度
    char   s[n]   # n = (v - 1) / 2，UTF-8
                  # ★ 没有 NUL 收尾！后面紧接字节码

实测校验（phz.jsc，逐条对过）：
    v=0x15(21) → n=10 → "getKingNum"   （后面紧跟 0x1D，不是 0）
    v=0x0b(11) → n= 5 → "isRed"
    v=0x19(25) → n=12 → "kingHuMatrix"
    v=0x1f(31) → n=15 → "allChiFlagArray"

踩过的坑：一开始以为字符串是 NUL 结尾、按 NUL 切，结果属性名会和后面的
字节码粘成 `playType#` 这种垃圾；后来又以为 `2n+1` 后面必有 NUL，只捞到
106 条（漏了 90%）。正确规则就是「奇数 == 2n+1 + n 字节 + 无 NUL」。

用途：还原游戏自己的字段口径 —— 比如客户端到底读不读 `paoNum`、
提/跑/偎/坎在引擎里叫什么、财牌用哪个键。比在运行时猜字段可靠得多。

用法
----
    python tools/jsc_strings.py Play.jsc                        # 全部原子
    python tools/jsc_strings.py Play.jsc --grep gang -C 4       # 命中项带邻居
    python tools/jsc_strings.py --dir ../src --grep caiPai --tree
    python tools/jsc_strings.py --dir ../src --field-map        # 字段使用矩阵
"""
from __future__ import annotations

import argparse
import io
import os
import re
import struct
import sys

MAGIC = b"\x2c\xc0\x73\xb9"
MAXLEN = 4096


def extract(data: bytes) -> list[tuple[int, str]]:
    """返回 [(偏移, 字符串)]，按文件顺序。"""
    out: list[tuple[int, str]] = []
    n = len(data)
    i = 0
    while i + 5 <= n:
        (v,) = struct.unpack_from("<I", data, i)
        if (v & 1) and 3 <= v <= MAXLEN * 2 + 1:
            ln = (v - 1) // 2
            raw = data[i + 4:i + 4 + ln]
            if len(raw) == ln:
                try:
                    txt = raw.decode("utf-8")
                except UnicodeDecodeError:
                    txt = None
                # 要求可打印且含字母/汉字：纯符号和乱码都是字节码噪声
                if txt and txt.isprintable() and any(c.isalpha() for c in txt):
                    out.append((i, txt))
                    i += 4 + ln
                    continue
        i += 1
    return out


def is_jsc(path: str) -> bool:
    try:
        with io.open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


def walk_jsc(root: str) -> list[str]:
    out = []
    for r, _d, fs in os.walk(root):
        for fn in fs:
            p = os.path.join(r, fn)
            if fn.endswith(".jsc") and is_jsc(p):
                out.append(p)
    return sorted(out)


def field_map(root: str, names: list[str]) -> None:
    """客户端到底读不读某个字段 —— 直接查字节，不解析格式。"""
    files = walk_jsc(root)
    blobs = {}
    for p in files:
        try:
            blobs[p] = io.open(p, "rb").read()
        except OSError:
            pass
    print("%-20s %s" % ("字段", "客户端使用情况"))
    print("-" * 84)
    for nm in names:
        k = nm.encode()
        hits = [(p, b.count(k)) for p, b in blobs.items() if k in b]
        hits.sort(key=lambda x: -x[1])
        if not hits:
            print("%-20s ✗ 0 个文件 —— 客户端从不读，服务端字段" % nm)
            continue
        top = ", ".join("%s×%d" % (os.path.basename(p), c) for p, c in hits[:3])
        print("%-20s %2d 个文件   %s" % (nm, len(hits), top))


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="精确提取 .jsc 里的字符串原子")
    ap.add_argument("path", nargs="?", help="单个 .jsc")
    ap.add_argument("--dir", help="目录（递归找 .jsc）")
    ap.add_argument("--grep", help="只保留匹配这个正则的原子")
    ap.add_argument("-C", "--context", type=int, default=0, help="命中项前后各带几条原子")
    ap.add_argument("--tree", action="store_true", help="按文件分组")
    ap.add_argument("--uniq", action="store_true", help="去重保序")
    ap.add_argument("--ident", action="store_true", help="只要像标识符的")
    ap.add_argument("--stats", action="store_true", help="只打统计")
    ap.add_argument("--dump", help="原子表写到这个文件")
    ap.add_argument("--field-map", action="store_true",
                    help="配合 --dir，输出「客户端读不读这些字段」矩阵")
    args = ap.parse_args(argv)

    if args.field_map:
        if not args.dir:
            print("--field-map 需要 --dir", file=sys.stderr)
            return 1
        field_map(args.dir, [
            "gang", "mjgang", "tui", "wei", "kan", "peng", "chi", "lie",
            "paoNum", "tiNum", "fireNum", "huNum", "selfDrawNum", "mjdesc",
            "caiPaiArr", "caiPaiCount", "caiPaiZuan", "caipaiType", "kingHuChoice",
            "isAllCardMine", "winone", "winall", "winNum", "origWinNum", "multiple",
            "limitScore", "choupai", "huziType", "playType", "cardNext", "cardNums",
            "huType", "paoType", "mjhand", "mjput", "mjchi", "mjpeng", "mjlie",
        ])
        return 0

    targets = walk_jsc(args.dir) if args.dir else ([args.path] if args.path else [])
    if not targets:
        ap.print_help()
        return 1

    pat = re.compile(args.grep, re.I) if args.grep else None
    ident = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
    hits = 0
    dump = io.open(args.dump, "w", encoding="utf-8", newline="\n") if args.dump else None

    for p in targets:
        with io.open(p, "rb") as f:
            data = f.read()
        atoms = extract(data)
        if args.ident:
            atoms = [(o, s) for o, s in atoms if ident.match(s)]
        if args.uniq:
            seen, tmp = set(), []
            for o, s in atoms:
                if s not in seen:
                    seen.add(s)
                    tmp.append((o, s))
            atoms = tmp
        rel = os.path.relpath(p, args.dir or os.path.dirname(p))
        if args.stats:
            print("%-56s %5d 原子  %9d 字节" % (rel, len(atoms), len(data)))
            continue
        if dump:
            dump.write("\n# ===== %s (%d) =====\n" % (rel, len(atoms)))
            for _o, s in atoms:
                dump.write(s + "\n")
        idx = [k for k, (_o, s) in enumerate(atoms) if (pat.search(s) if pat else True)]
        if not idx:
            continue
        if args.tree:
            print("\n=== %s  (%d 原子, 命中 %d) ===" % (rel, len(atoms), len(idx)))
        for k in idx:
            hits += 1
            if args.context:
                lo, hi = max(0, k - args.context), min(len(atoms), k + args.context + 1)
                print("  @%06x  %s" % (atoms[k][0], "  |  ".join(s for _o, s in atoms[lo:hi])))
            else:
                print("  %s" % atoms[k][1])
    if dump:
        dump.close()
    if not args.stats:
        print("\n命中合计 %d 条" % hits)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
