# -*- coding: utf-8 -*-
"""牌局落盘：每局结束/退出时把整局的门子与出牌追加到 jsonl。

为什么是 jsonl 而不是一个 json 数组：
- 追加写，进程被 kill 也不会把之前的记录写坏
- 一行一局，直接 grep / 用 pandas 读都方便

文件位置：data/records.jsonl（data/ 已在 .gitignore）
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict


class Recorder:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def append(self, obj: Dict[str, Any]) -> bool:
        """追加一条。空局（没门子也没出牌）不记，省得刷一堆噪音。"""
        try:
            if not obj:
                return False
            if not obj.get("melds") and not obj.get("puts"):
                return False
            rec = dict(obj)
            rec["ts"] = int(time.time())
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return True
        except Exception:
            return False
