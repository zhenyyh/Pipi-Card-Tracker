# -*- coding: utf-8 -*-
"""设备桥：把一段 JS 送进游戏进程执行，把结果取回来。

链路（文件驱动 eval）
---------------------
    phz_eval.js  ——写脚本——→  游戏 files 目录
    触发文件      ——agent 轮询到——→ evalString(脚本)
    脚本          ——自己 writeStringToFile——→  phz_probe_out.json
    agent         ——拷出来——→  phz_eval_out
    本模块        ——读——→  dict

★ 三个必须记住的坑（都是踩出来的）
----------------------------------
1. `evalString` **只返回 bool**，拿不到返回值。所以脚本必须自己落盘。
2. agent 判定「脚本跑完」的依据是 `phzjs::lastResult()` **返回值是否变化** ——
   所以脚本的 `return` 必须每次都不同（末尾带 `Date.now()`），否则内容一样
   会被当成还没执行，5 秒超时且不拷输出。
3. 不要在 shell 里自己拼 `su -mm -c '...'` 再交给 adb：adb 会吃掉外层引号，
   到设备上变成 `su -mm -c rm -f ...`，su 只认第一个词，rm 静默失败。

原生 agent（`ldmon`）是可执行文件，负责注入和 evalString 派发。这一层不重写，
通过 `agent_cmd` 配置调用 —— 它是独立工程（Zygisk provider + ARM 翻译层）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULTS = {
    "adb": "D:/Soft-green/LDPlayer9/adb.exe",
    "serial": "127.0.0.1:5555",
    "package": "com.happyplay.hnphz",
    # 原生 agent 的驱动脚本（外部工程 phz-hook-emu）
    "agent_cmd": [sys.executable, "D:/Python学习/phz-hook-emu/ldmon.py"],
    "workdir": os.path.join(ROOT, ".pct"),
}


class Bridge:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        self.script = os.path.join(ROOT, "agent", "trace.js")
        os.makedirs(self.cfg["workdir"], exist_ok=True)

    # ---------------------------------------------------------------- adb
    def adb(self, args, timeout: int = 40):
        return subprocess.run([self.cfg["adb"], "-s", self.cfg["serial"]] + args,
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")

    def boot(self) -> Optional[str]:
        subprocess.run([self.cfg["adb"], "start-server"], capture_output=True)
        try:
            subprocess.run([self.cfg["adb"], "connect", self.cfg["serial"]],
                           capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            pass
        r = self.adb(["shell", "pidof %s" % self.cfg["package"]])
        pid = (r.stdout or "").strip().split()
        return pid[0] if pid else None

    # ---------------------------------------------------------------- eval
    def run(self, mode: str = "dump", limit: int = 300000,
            timeout: int = 60) -> Optional[dict]:
        """执行 agent/trace.js 的某个 mode，返回脚本落盘的 JSON。"""
        cmd = list(self.cfg["agent_cmd"]) + [
            "eval", self.script, "--mode", mode, "--limit", str(limit), "--raw",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=ROOT)
        txt = r.stdout or ""
        # agent 会先打一堆提示，真正的 JSON 从 {"ts" 开始
        i = txt.find('{"ts"')
        if i < 0:
            i = txt.find('{\n  "ts"')
        if i < 0:
            return None
        try:
            obj, _ = json.JSONDecoder().raw_decode(txt[i:])
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def probe(self) -> Optional[dict]:
        """轻量自检：钩子还在不在。"""
        return self.run("probe", limit=2000)

    def arm(self) -> Optional[dict]:
        return self.run("arm")

    def clear(self) -> Optional[dict]:
        """★ 注意：clear 会**顺带卸钩**。别在开新一轮前随手 clear。"""
        return self.run("clear")

    def dump(self) -> Optional[dict]:
        return self.run("dump")
