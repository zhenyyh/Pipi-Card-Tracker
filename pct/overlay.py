# -*- coding: utf-8 -*-
"""记牌器主循环：注入 → 挂悬浮窗 → 装常驻采集器 → 读文件渲染。

用法
----
    python -m pct.overlay                 # 守护模式：等游戏，游戏一起来就自动挂上
    python -m pct.overlay --interval 0.15 # 更快的刷新
    python -m pct.overlay --once          # 只跑一轮，打印状态（调试用）

★ 两条链路必须分清（第一版踩过坑）
----------------------------------
注入（重，会阻塞主线程）：
    Frida → evalString(gs_install.js) → 游戏里装钩子 + 定时器
    **只做一次**。第一版每 250ms 灌一次 evalString，把游戏打到 ANR 了。

读取（轻，不碰主线程）：
    Frida → File.readAllText(pct_state.json) → 解析
    游戏自己按事件（出牌瞬间）+ 定时器（600ms 兜底）落盘。

自动恢复
--------
游戏没开就等，开了自动挂；进程被杀了（换房/崩溃）也会重新注入。
所以这个进程开着，游戏什么时候起，记牌器什么时候自己出来。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import threading
import queue

from .frida import FridaBridge, FridaError, DEFAULT_ADB, DEFAULT_PKG, DEFAULT_SERIAL
from .tracker import Tracker
from .ai import ask as ai_ask, _eligible as ai_eligible
import os as _os

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

# 优先使用环境变量；本机配置可保留原有密钥，不写进源码。
def load_ai_key() -> str:
    key = _os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(_os.path.join(ROOT, ".pct", "deepseek_key"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


AI_KEY = load_ai_key()
AI_COOLDOWN = 1.0
AI_RETRY_DELAY = 2.0
AI_MAX_ATTEMPTS = 2


def decision_fingerprint(st: dict) -> str:
    """判断结果是否仍对应当前决策点。

    ★ 只用「决策相关」的稳定字段。之前把 lastEvent（事件 seq）和未排序的手牌
    塞进来，导致每次新事件/手牌顺序抖动 fingerprint 就变 → 结果永远过期 →
    无限请求-丢弃循环（实测 3 分钟 100+ 次请求）。手牌一律排序后比较。
    """
    if (not isinstance(st, dict) or st.get("none") or st.get("err")
            or st.get("tableid") is None or st.get("playNum") is None):
        return ""

    def _cards(v):
        return sorted(v) if isinstance(v, list) else v

    me_s = None
    for seat in st.get("seats") or []:
        try:
            if int(seat.get("uid")) == st.get("me"):
                me_s = seat
                break
        except (TypeError, ValueError):
            pass
    value = {
        "tableid": st.get("tableid"),
        "playNum": st.get("playNum"),
        "tState": st.get("tState"),
        "curPlayer": st.get("curPlayer"),
        "lastPut": st.get("lastPut"),
        "hand": _cards(me_s.get("hand")) if me_s else None,
        "put": _cards(me_s.get("put")) if me_s else None,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def result_is_current(request_id: int, fingerprint: str, result_id: int,
                      expected: str, fresh: dict) -> bool:
    """仅接受最新请求，且请求前、轮询中和展示前状态完全一致。"""
    return (result_id == request_id and bool(fingerprint) and expected == fingerprint
            and decision_fingerprint(fresh) == expected)


def thinking_text(start: float, now: float) -> str:
    """只在本次请求等待期间循环显示一到三个点。"""
    dots = int(max(0.0, now - start) / 0.5) % 3 + 1
    return "AI思考中" + "." * dots


class AIRequestGate:
    """一次只允许一个请求；同一状态的临时失败最多重试一次。"""
    def __init__(self):
        self.active = False
        self.fingerprint = None
        self.attempts = 0
        self.retryable = False
        self.next_time = 0.0
        self.last_start = 0.0

    def can_start(self, fingerprint: str, now: float) -> bool:
        if self.active or not fingerprint or now - self.last_start < AI_COOLDOWN:
            return False
        if fingerprint != self.fingerprint:
            return True
        return self.retryable and self.attempts < AI_MAX_ATTEMPTS and now >= self.next_time

    def start(self, fingerprint: str, now: float) -> None:
        if fingerprint != self.fingerprint:
            self.fingerprint = fingerprint
            self.attempts = 0
        self.attempts += 1
        self.active = True
        self.retryable = False
        self.last_start = now

    def finish(self, retryable: bool, now: float) -> None:
        self.active = False
        self.retryable = retryable
        self.next_time = now + AI_RETRY_DELAY

    def invalidate(self) -> None:
        # 请求可能仍在线程中运行，不能释放并发限制。
        self.fingerprint = None
        self.retryable = False


def show_ai(bridge: "FridaBridge", result: dict) -> bool:
    try:
        response = bridge.ui_ai(result)
        if response.get("ok"):
            return True
        print("AI 面板更新失败: %s" % response, file=sys.stderr)
    except Exception as e:
        print("AI 面板更新失败: %s" % e, file=sys.stderr)
    return False


def ai_worker(state: dict, key: str, request_id: int, fingerprint: str,
              results: "queue.Queue") -> None:
    """网络请求在线程中执行，只有主循环可以更新界面。"""
    try:
        result = ai_ask(state, key)
    except Exception as e:
        print("[AI] 请求异常: %s" % type(e).__name__, file=sys.stderr)
        result = {"action": "none", "reason": "AI请求失败"}
    results.put((request_id, fingerprint, result))


def mount(bridge: FridaBridge) -> bool:
    """注入 + 装采集器 + 建悬浮窗。成功返回 True。"""
    bridge.attach()
    info = bridge.install()
    if not info.get("ok"):
        print("采集器安装失败: %s" % info, file=sys.stderr)
        return False
    r = bridge.ui_show()
    if not r.get("ok"):
        print("悬浮窗创建失败: %s" % r, file=sys.stderr)
        return False
    ai_ui = bridge.ui_ai({"action": "none", "reason": "待命"})
    if not ai_ui.get("ok"):
        print("AI 悬浮窗创建失败: %s" % ai_ui, file=sys.stderr)
    print("[%s] 已挂载 pid=%s 采集器=%s" % (time.strftime("%H:%M:%S"), bridge.pid, info))
    return True


def run(args) -> int:
    bridge = FridaBridge(pkg=args.pkg, adb=args.adb, serial=args.serial)
    tracker = Tracker()
    last_state = None
    last_seq = 0
    last_key = None
    snap_key = None
    results = queue.Queue()
    request_id = 0
    last_tstate = None
    _turn_since = None
    gate = AIRequestGate()
    displayed_fingerprint = None
    pending_render = None
    next_render_time = 0.0
    last_notice = None
    state_unavailable = False

    if args.once:
        try:
            bridge.attach()
        except FridaError as e:
            print("注入失败: %s" % e, file=sys.stderr)
            return 2
        info = bridge.install()
        print("采集器: %s" % info)
        st = bridge.read_state()
        if st is None:
            print("读不到 pct_state.json", file=sys.stderr)
            return 5
        evs = [e for e in (st.get("ev") or []) if (e.get("q") or 0) > 0]
        tracker.feed(st, evs)
        print(json.dumps(tracker.ui_state(), ensure_ascii=False, indent=1))
        print("门子:", tracker.debug())
        print("出牌:", tracker.puts, "首出:", tracker.first_put)
        bridge.detach()
        return 0

    # ---- 守护模式
    print("守护中：等游戏启动（Ctrl-C 退出）")
    told_waiting = False
    idle = 0.5
    force_tick = 0      # 定期强制重推，防止界面被别的脚本改过后不刷新
    try:
        while True:
            # 会话没了（游戏没起 / 被杀）→ 等着
            if not bridge.alive():
                if not bridge.game_running():
                    if not told_waiting:
                        print("游戏未运行，等待…")
                        told_waiting = True
                    time.sleep(1.5)
                    continue
                try:
                    if not mount(bridge):
                        time.sleep(3)
                        continue
                    told_waiting = False
                    tracker = Tracker()
                    last_state = None
                    last_seq = 0
                    request_id += 1
                    gate.invalidate()
                    pending_render = None
                    displayed_fingerprint = None
                    last_notice = None
                    state_unavailable = False
                except Exception as e:
                    print("挂载异常: %s" % e, file=sys.stderr)
                    try:
                        bridge.detach()
                    except Exception:
                        pass
                    time.sleep(3)
                    continue

            st = bridge.read_state()
            if st is None:
                if not state_unavailable:
                    request_id += 1
                    gate.invalidate()
                    pending_render = None
                    displayed_fingerprint = None
                    last_notice = None
                    state_unavailable = True
                    show_ai(bridge, {"action": "none", "reason": "牌局状态不可用"})
                idle += 0.1
                if idle > 8:
                    # 文件读不到：可能采集器没装上（游戏重启过）→ 重挂
                    print("读不到状态，重新挂载…", file=sys.stderr)
                    try:
                        bridge.detach()
                    except Exception:
                        pass
                    idle = 0.5
                    continue
                time.sleep(args.interval)
                continue
            idle = 0.5
            state_unavailable = False

            # 按自增 q 去重：落盘文件是覆盖写的，队列保留最近 240 条
            evs = []
            for e in st.get("ev") or []:
                q = e.get("q") or 0
                if q > last_seq:
                    evs.append(e)
                    if q > last_seq:
                        last_seq = q

            # ★ 新局瞬间全量抓拍：底牌的字段可能只在开局那一刻有值，
            #   亮完就被清掉。检测到换局就立刻连续 dump 几次原始 sData。
            # ★ 抓拍时机：局中(tState 3/4) 且刚发完牌(cardNext ≈ 41) 的那几帧。
            #   之前用「换局」触发，抓到的是结算态 —— 底牌早收回了。
            key = (st.get("tableid"), st.get("playNum"))
            cn = st.get("cardNext") or 0
            fresh = (st.get("tState") in (3, 4) and 38 <= cn <= 44)
            if fresh and key != snap_key:
                try:
                    # ★ 快速连拍手牌：如果排序是客户端做的，第一帧就该是发牌顺序，
                    #   那第 21 张就是底牌；如果第一帧已经排好序，说明服务端下发即有序。
                    HANDS_JS = ("(function(){try{var sd=jsclient.data.sData,"
                                "me=jsclient.data.pinfo.uid,"
                                "p=sd.players[String(me)]||{},t=sd.tData;"
                                "return JSON.stringify({cn:t.cardNext,z:t.zhuang,"
                                "st:t.tState,n:(p.mjhand||[]).length,"
                                "h:(p.mjhand||[]).slice(0,24)});}catch(e){return 'null';}})()")
                    frames = []
                    for _i in range(18):
                        raw = bridge.eval(HANDS_JS, timeout=1.2)
                        frames.append(raw or "null")
                        time.sleep(0.05)
                    with open(_os.path.join(ROOT, "data", "frames.json"), "w",
                              encoding="utf-8") as _f:
                        json.dump(frames, _f, ensure_ascii=False, indent=1)
                    print("[%s] 开局连拍 %d 帧 playNum=%s" % (
                        time.strftime("%H:%M:%S"), len(frames), st.get("playNum")))
                except Exception as _e:
                    print("连拍失败: %s" % _e, file=sys.stderr)
            if fresh:
                snap_key = key
            last_key = key

            tracker.feed(st, evs)

            # 主线程串行显示结果；只允许一个请求在途。
            fingerprint = decision_fingerprint(st)
            while True:
                try:
                    rid, expected, result = results.get_nowait()
                except queue.Empty:
                    break
                fresh = bridge.read_state()
                current = result_is_current(request_id, fingerprint, rid, expected, fresh)
                retryable = result.get("reason") == "AI请求失败" if current else expected == fingerprint
                gate.finish(retryable, time.monotonic())
                if not current:
                    print("[AI] 已丢弃过期或无法确认的结果", file=sys.stderr)
                    continue
                print("[AI结果] %s" % json.dumps(result, ensure_ascii=False))
                pending_render = (fingerprint, result)
                next_render_time = 0.0

            if pending_render and pending_render[0] != fingerprint:
                pending_render = None
            if pending_render and time.monotonic() >= next_render_time:
                if result_is_current(request_id, fingerprint, request_id, pending_render[0], bridge.read_state()):
                    if show_ai(bridge, pending_render[1]):
                        displayed_fingerprint = fingerprint
                        last_notice = None
                        pending_render = None
                    else:
                        next_render_time = time.monotonic() + 1.0
                else:
                    pending_render = None
                    gate.finish(True, time.monotonic())

            my_seat_no = next((seat.get("seat") for seat in st.get("seats") or []
                               if seat.get("uid") == st.get("me") and st.get("me") is not None), None)
            # ★ 轮到我出牌：curPlayer=我，且处于出牌态。
            #   实测轮到我出牌时是 tState=3（不是 4）——只认 4 会导致
            #   「我庄先出」「对手打完轮到我」全部不触发。
            #   lastPutPlayer 不能作为判据：对手过牌后轮到我时它正是我。
            #   跳牌的瞬间误判由 200ms 防抖兜住（settled）。
            is_my_put = (my_seat_no is not None and st.get("tState") in (3, 4)
                         and st.get("curPlayer") == my_seat_no)
            # ★ 响应（吃/碰/胡/过）：tState=5 且本人操作栏有可见按钮 ——
            #   按钮可见是游戏 UI 的直接证据（1ms 自动跳牌时按钮不可见，不会误触发）
            visible = tracker._visible_actions
            # ★ 响应（吃/碰/胡/过）：只看操作栏按钮是否可见。
            #   lastPutPlayer 不可靠（实测对手打牌后它仍是我的座位号），
            #   用它排除会导致「明明能碰却不给提醒」。按钮可见本身就是
            #   游戏 UI 给的直接证据：我打完牌时按钮不会亮。
            is_my_response = (my_seat_no is not None
                              and isinstance(visible, list) and bool(visible))
            # ★ 防抖：curPlayer 我+按钮可见 这个状态要稳定 200ms 才请求。
            #   1ms 自动跳牌会瞬间把 curPlayer 跳到我、又跳走，不防抖就会无限请求-丢弃循环。
            if is_my_put or is_my_response:
                if _turn_since is None:
                    _turn_since = time.monotonic()
                settled = (time.monotonic() - _turn_since) >= 0.2
            else:
                _turn_since = None
                settled = False

            state = None
            if is_my_put:
                state = tracker.ai_state()
                state["mode"] = "put"
            elif is_my_response:
                state = tracker.ai_state()
                state["mode"] = "response"
            notice = None
            if not fingerprint:
                notice = "牌局状态不可用"
            elif not AI_KEY:
                notice = "请配置密钥"
            elif not is_my_put and not is_my_response:
                notice = ("响应信息不足" if st.get("tState") == 5 else "待命")
            elif not ai_eligible(state):
                # 出牌模式缺的是手牌；响应模式缺的是「可响应动作」
                notice = ("手牌加载中" if (state or {}).get("mode") == "put"
                          else "响应信息不足")
            if displayed_fingerprint is not None and displayed_fingerprint != fingerprint:
                displayed_fingerprint = None
                last_notice = None
            # ★ notice（待命/加载中）不覆盖正在显示的建议结果：
            #   之前 displayed_fingerprint 非空时 notice 一来就冲掉结果 → 结果→待命→结果 反复跳
            if notice != last_notice:
                print("[AI显示] %s → %s | my_put=%s resp=%s elig=%s handKnown=%s" % (
                    last_notice, notice, is_my_put, is_my_response,
                    (ai_eligible(state) if state else None),
                    ((state or {}).get("me", {}) or {}).get("handKnown")), file=sys.stderr)
            if (notice is not None and notice != last_notice
                    and displayed_fingerprint is None and pending_render is None):
                if show_ai(bridge, {"action": "none", "reason": notice}):
                    last_notice = notice
            elif notice is None and displayed_fingerprint is None and pending_render is None:
                wait_text = (thinking_text(gate.last_start, time.monotonic())
                             if gate.active and gate.fingerprint == fingerprint else "等待AI")
                if wait_text != last_notice and show_ai(bridge, {"action": "none", "reason": wait_text}):
                    last_notice = wait_text

            now = time.monotonic()
            if (notice is None and pending_render is None and settled
                    and gate.can_start(fingerprint, now)):
                gate.start(fingerprint, now)
                request_id += 1
                wait_text = thinking_text(now, now)
                if show_ai(bridge, {"action": "none", "reason": wait_text}):
                    displayed_fingerprint = None
                    last_notice = wait_text
                threading.Thread(target=ai_worker,
                                 args=(state, AI_KEY, request_id, fingerprint, results),
                                 daemon=True).start()
                print("[AI请求] %s tState=%s cur=%s lastPutBy=%s mySeat=%s" % (
                    state.get("mode"), st.get("tState"), st.get("curPlayer"),
                    st.get("lastPutPlayer"), my_seat_no), file=sys.stderr)

            # 结算界面/开局弹窗最容易盖住记牌器 —— 在这两个切换点把窗口提到最上层
            ts_now = st.get("tState")
            if ts_now != last_tstate:
                if ts_now in (6, -1) or last_tstate in (6, -1):
                    try:
                        bridge.ui_top()
                    except Exception:
                        pass
                last_tstate = ts_now

            ui = tracker.ui_state()

            # 状态没变也隔几秒强推一次：开发时手动渲染过假数据、或界面被
            # 外部改过，只靠 last_state 比较会让卡片永远停在旧内容上。
            force = force_tick >= 20
            if ui != last_state or force:
                try:
                    bridge.ui_render(ui)
                except Exception as e:
                    print("渲染失败: %s" % e, file=sys.stderr)
                last_state = ui
                force_tick = 0
                if args.verbose and (force or ui != last_state):
                    print("[%s] %s" % (time.strftime("%H:%M:%S"), tracker.debug()))
            force_tick += 1

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        try:
            bridge.ui_close()
        except Exception:
            pass
        bridge.detach()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pct.overlay", description="皮皮跑胡子记牌器")
    ap.add_argument("--pkg", default=DEFAULT_PKG)
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--serial", default=DEFAULT_SERIAL)
    ap.add_argument("--interval", type=float, default=0.15, help="读文件间隔（秒）")
    ap.add_argument("--once", action="store_true", help="只跑一轮并打印结果")
    ap.add_argument("-v", "--verbose", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
