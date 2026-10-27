# -*- coding: utf-8 -*-
"""Frida 桥：注入游戏进程，借它自己的 evalString 执行 JS，并把结果读回来。

为什么不用 phz-hook-emu 那条原生 agent 通道
------------------------------------------
那条通道要编译 Zygisk provider + ARM 翻译层，是为**长期隐蔽**准备的。本模块是
第一轮的快速通道：只要 frida-server 在跑，attach 上去就能拿到 `jsclient`。

通道（已验证）
--------------
    Frida attach
      → Java.use('org.cocos2dx.lib.Cocos2dxJavascriptJavaBridge').evalString(js)
      → 游戏 JS 把结果写 /data/user/0/<pkg>/files/pct_eval.json
      → 本模块用 Java IO 把文件读回（不经过 adb，省一次往返）

★ evalString 只返回 bool，也没有同步等待。所以每次调用带上 nonce，
  按 nonce 认领结果 —— 不能用「内容变了没」，内容相同就会一直等下去。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import threading
import sys
from typing import Any, Optional

import frida

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_ADB = "D:/Soft-green/scrcpy-win64-v2.7/adb.exe"
DEFAULT_SERIAL = "emulator-5554"
DEFAULT_PKG = "com.happyplay.hnphz"
DEFAULT_SERVER = "/data/local/tmp/fs16"      # frida-server 16.x（17 在 houdini 下会崩）


class FridaError(RuntimeError):
    pass


class FridaBridge:
    def __init__(self, pkg: str = DEFAULT_PKG, adb: str = DEFAULT_ADB,
                 serial: str = DEFAULT_SERIAL, server: str = DEFAULT_SERVER):
        self.pkg = pkg
        self.adb_path = adb
        self.serial = serial
        self.server_path = server
        self.session = None
        self.script = None
        self._logs: list[str] = []
        self._ready = False

    # ---------------------------------------------------------------- device
    def _device(self):
        try:
            return frida.get_usb_device(timeout=8)
        except Exception:
            dm = frida.get_device_manager()
            return dm.add_remote_device(self.serial)

    def adb(self, args, timeout: int = 40, root: bool = False):
        cmd = [self.adb_path, "-s", self.serial] + args
        if root:
            cmd = [self.adb_path, "-s", self.serial, "shell", "su", "-c",
                   " ".join(args[1:])] if args[0] == "shell" else cmd
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")

    # ----------------------------------------------------------------- server
    def server_alive(self) -> bool:
        try:
            dev = self._device()
            dev.enumerate_processes()
            return True
        except Exception:
            return False

    def start_server(self) -> bool:
        """起 frida-server。★ killall 用精确名字，别用 pkill -f —— 那个会把自己杀掉。"""
        self.adb(["shell", "killall", "fs", "fs16"], root=True)
        time.sleep(0.4)
        # setsid 让 server 脱离 adb shell 的会话，否则 adb 一断就被回收
        cmd = ("chmod 755 %s; setsid %s -l 127.0.0.1:27042 "
               ">/data/local/tmp/fs16.log 2>&1 </dev/null &" % (self.server_path,
                                                               self.server_path))
        subprocess.run([self.adb_path, "-s", self.serial, "shell", "su", "-c", cmd],
                       capture_output=True, timeout=30)
        for _ in range(20):
            time.sleep(0.3)
            if self.server_alive():
                return True
        return False

    def ensure_server(self) -> None:
        if not self.server_alive():
            if not self.start_server():
                raise FridaError("frida-server 起不来，看 /data/local/tmp/fs16.log")

    # ------------------------------------------------------------------- pid
    def find_pid(self, dev=None) -> Optional[int]:
        dev = dev or self._device()
        for a in dev.enumerate_applications():
            if a.identifier == self.pkg and a.pid:
                return a.pid
        # 游戏进程名是中文（皮皮跑胡子），按包名查 process 查不到，只能走应用表
        for p in dev.enumerate_processes():
            if p.name == self.pkg:
                return p.pid
        return None

    def game_running(self) -> bool:
        # 设备掉线/adb 重启时 enumerate_applications 可能**挂起不返回**（实测卡死守护
        # 11 分钟），或者抛错。这里用线程 + 3 秒超时兜住，两种情况都按「没在跑」处理。
        result = [False]

        def _probe():
            try:
                result[0] = self.find_pid() is not None
            except Exception:
                result[0] = False

        th = threading.Thread(target=_probe, daemon=True)
        th.start()
        th.join(3.0)
        return result[0]

    # ---------------------------------------------------------------- attach
    def attach(self, wait: int = 0) -> None:
        self.ensure_server()
        dev = self._device()
        pid = self.find_pid(dev)
        if pid is None:
            if wait:
                time.sleep(wait)
                pid = self.find_pid(dev)
            if pid is None:
                raise FridaError("游戏没在跑（%s）" % self.pkg)

        try:
            self.session = dev.attach(pid)
        except Exception as e:
            # 设备重连/模拟器重启后 frida-server 会消失，但 frida 自己那层
            # "server 还活着"的判断可能还是 True。这里兜一次：重启 server 再试。
            msg = str(e)
            if "frida-server" in msg or "closed" in msg or "not responding" in msg.lower():
                print("frida-server 不可用（%s），重启后重试…" % msg[:60], file=sys.stderr)
                self.start_server()
                time.sleep(1.0)
                dev = self._device()
                pid = self.find_pid(dev) or pid
                self.session = dev.attach(pid)
            else:
                raise
        self.session.on("detached", lambda *a: self._on_detached())

        js_path = os.path.join(HERE, "fagent.js")
        with open(js_path, encoding="utf-8") as f:
            src = f.read()
        self.script = self.session.create_script(src)
        self.script.on("message", self._on_message)
        self.script.load()
        for _ in range(50):
            if self._ready:
                break
            time.sleep(0.05)
        if not self._ready:
            raise FridaError("agent 没回 ready，注入可能失败")
        self.pid = pid
        # 预热：Runnable 类首次调用要过一遍类加载，不预热的话紧接着的第一次
        # 真实调用容易吃一次超时（实测交替失败）。
        for _ in range(2):
            try:
                self.script.exports_sync.evaljs("1", 2000)
                break
            except Exception:
                time.sleep(0.2)

    def _on_detached(self, *a):
        self._ready = False
        self.session = None
        self.script = None

    def _on_message(self, msg, data):
        if msg.get("type") == "send":
            p = msg.get("payload") or {}
            if p.get("t") == "ready":
                self._ready = True
            elif p.get("t") == "log":
                m = str(p.get("m"))
                self._logs.append(m)
                # JS 端 log 实时打到 stderr（aiWin 这类构建错误第一时间能看到）
                try:
                    import sys as _s
                    print("[JS] " + m, file=_s.stderr)
                except Exception:
                    pass
        elif msg.get("type") == "error":
            e = "ERR " + str(msg.get("description"))[:300]
            self._logs.append(e)
            try:
                import sys as _s
                print("[JS-ERR] " + e, file=_s.stderr)
            except Exception:
                pass

    @property
    def logs(self) -> list[str]:
        return self._logs

    def alive(self) -> bool:
        return bool(self.script and self._ready)

    # ------------------------------------------------------------------ eval
    def eval(self, code: str, timeout: float = 1.5, retries: int = 1) -> Any:
        """让游戏执行 JS 表达式，返回它的值（需可 JSON 序列化）。

        正常一次 50ms。偶发超时是 GL 线程在忙（切场景/播动画），重试一次即可。
        """
        last = None
        for i in range(retries + 1):
            if not self.alive():
                self.attach()
            try:
                res = self.script.exports_sync.evaljs(code, int(timeout * 1000))
            except Exception as e:
                last = e
                time.sleep(0.05)
                continue
            if res and res.get("ok"):
                return res.get("r")
            last = FridaError("eval 失败: %s" % (res or {}).get("e"))
            time.sleep(0.05)
        raise last if isinstance(last, Exception) else FridaError("eval 失败")

    def eval_json(self, code: str, timeout: float = 3.0) -> Any:
        """同 eval，但要求 code 返回字符串，这里再解一次 JSON。"""
        v = self.eval(code, timeout)
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v

    def detach(self) -> None:
        try:
            if self.session:
                self.session.detach()
        except Exception:
            pass
        self.session = None
        self.script = None
        self._ready = False

    # -------------------------------------------------------- 常驻采集器
    @property
    def state_path(self) -> str:
        return "/data/user/0/%s/files/pct_state.json" % self.pkg

    def install(self) -> dict:
        """注入常驻采集器（幂等）。返回 {ok, hook, timer, bytes}。

        只调一次：之后游戏自己按事件/按周期落盘，这里只读文件 —— 不再往
        GL 线程灌 evalString（那是把游戏打到 ANR 的直接原因）。
        """
        with open(os.path.join(HERE, "gs_install.js"), encoding="utf-8") as f:
            src = f.read()
        raw = self.eval(src, timeout=4.0)
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": 0, "raw": raw}

    def read_state(self) -> Optional[dict]:
        """读采集器落盘的状态。不碰 Java、不碰主线程，纯文件读。"""
        if not self.alive():
            return None
        try:
            txt = self.script.exports_sync.readtext(self.state_path)
        except Exception:
            return None
        if not txt:
            return None
        try:
            return json.loads(txt)
        except Exception:
            return None

    # ------------------------------------------------------------------- ui
    def push_ui(self, path: str = None) -> bool:
        """把界面 HTML 推到设备（/data/local/tmp，644 让游戏进程读得到）。"""
        src = path or os.path.join(HERE, "ui", "overlay.html")
        env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
        r = subprocess.run([self.adb_path, "-s", self.serial, "push", src,
                            "/data/local/tmp/pct_ui.html"],
                           capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0:
            return False
        subprocess.run([self.adb_path, "-s", self.serial, "shell",
                        "su -c 'chmod 644 /data/local/tmp/pct_ui.html'"],
                       capture_output=True, text=True, timeout=30)
        return True

    def ui_show(self) -> dict:
        if not self.alive():
            self.attach()
        self.push_ui()
        return self.script.exports_sync.uishow()

    def ui_render(self, state: dict) -> dict:
        if not self.alive():
            self.attach()
        return self.script.exports_sync.uirender(state)

    def ui_reload(self) -> dict:
        if not self.alive():
            self.attach()
        self.push_ui()
        return self.script.exports_sync.uireload()

    def ui_ai(self, result: dict) -> dict:
        if not self.alive():
            self.attach()
        return self.script.exports_sync.uiai(result)

    def ui_top(self) -> dict:
        """把悬浮窗重新加一次，提到 WindowManager 最上层（防被游戏弹窗盖住）。"""
        if not self.alive():
            return {"ok": 0, "e": "detached"}
        try:
            return self.script.exports_sync.uitop()
        except Exception as e:
            return {"ok": 0, "e": str(e)[:60]}

    def ui_close(self) -> dict:
        if not self.alive():
            return {"ok": 0, "e": "detached"}
        return self.script.exports_sync.uiclose()

    def ui(self, op: str, payload: Any = None) -> Any:
        if op == "show":
            return self.ui_show()
        if op == "render":
            return self.ui_render(payload or {})
        if op == "reload":
            return self.ui_reload()
        if op == "close":
            return self.ui_close()
        raise FridaError("未知 ui 操作 %s" % op)


if __name__ == "__main__":
    b = FridaBridge()
    b.attach()
    print("pid", b.pid)
    print(b.eval("1+1"))
    print(b.eval("jsclient ? 'has jsclient' : 'no'"))
    b.detach()
