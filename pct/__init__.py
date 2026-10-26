# -*- coding: utf-8 -*-
"""Pipi Card Tracker —— 皮皮跑胡子牌局数据采集与还原。"""
from __future__ import annotations

import sys

__version__ = "1.0.0"


def _force_utf8_stdio():
    """把 stdout/stderr 钉到 UTF-8。

    Windows 的控制台默认是 cp936（中文系统）或 cp1252（英文系统），
    而我们满屏都是中文牌名和 ✓ / ✗ —— 在那种编码下 print 会直接抛
    `UnicodeEncodeError: 'charmap' codec can't encode character '\\u2713'`。
    本地用 Git Bash 跑看不出来（它给的是 UTF-8），但 GitHub Actions 的
    windows-latest 就是 cp1252，CI 第一次跑就炸在这儿（2026-10-26）。

    `reconfigure` 只在流支持时可用（被重定向到管道时仍然有效），
    所以两边都 try 一下，失败就算了，不因为有奇怪的重定向就整体崩掉。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()
