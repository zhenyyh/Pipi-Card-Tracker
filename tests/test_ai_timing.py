# -*- coding: utf-8 -*-
"""离线验证牌局快照变化及后台请求交付，不连接设备或网络。"""
import copy
import queue
import sys
import types
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch

# 这些测试不需要 Frida；该可选依赖仅用于实际连接设备。
sys.modules.setdefault("frida", types.ModuleType("frida"))
from pct.overlay import AIRequestGate, ai_worker, decision_fingerprint, result_is_current, thinking_text


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.state = {"tableid": "room", "playNum": 1, "tState": 3,
                      "curPlayer": 0, "cardNext": 41, "lastPut": 2,
                      "me": 123, "seats": [{"seat": 0, "uid": 123,
                        "hand": [1, 2, 3], "handKnown": True}],
                      "ev": [{"q": 8}]}

    def test_important_changes_invalidate_fingerprint(self):
        original = decision_fingerprint(self.state)
        self.assertTrue(original)
        # 真正影响「该打哪张」的字段：变了就必须重算
        for key, value in [("tableid", "other"), ("playNum", 2),
                           ("tState", 5), ("curPlayer", 1),
                           ("lastPut", 3)]:
            changed = copy.deepcopy(self.state)
            changed[key] = value
            self.assertNotEqual(original, decision_fingerprint(changed), key)
        changed = copy.deepcopy(self.state)
        changed["seats"][0]["hand"] = [1, 2]
        self.assertNotEqual(original, decision_fingerprint(changed))

    def test_volatile_fields_do_not_invalidate_fingerprint(self):
        """cardNext/ev/playType 高频抖动，不能用来作废结果。

        历史 bug：fingerprint 含 lastEvent（事件 seq）和 cardNext，导致每次新事件
        fingerprint 就变 → 结果永远「过期」→ 无限请求-丢弃循环（实测 3 分钟 100+ 次）。
        这些变化已被 hand 覆盖：自己摸牌 hand 必变；对手摸牌本就不该重算我的出牌。
        """
        original = decision_fingerprint(self.state)
        for key, value in [("cardNext", 42), ("playType", 123), ("lastPutPlayer", 1)]:
            changed = copy.deepcopy(self.state)
            changed[key] = value
            self.assertEqual(original, decision_fingerprint(changed), key)
        changed = copy.deepcopy(self.state)
        changed["ev"].append({"q": 9})
        self.assertEqual(original, decision_fingerprint(changed))
        self.assertEqual("", decision_fingerprint(None))
        self.assertEqual("", decision_fingerprint({"tableid": "room", "err": "bad"}))

    def test_only_current_result_is_accepted(self):
        fingerprint = decision_fingerprint(self.state)
        self.assertTrue(result_is_current(2, fingerprint, 2, fingerprint, self.state))
        self.assertFalse(result_is_current(3, fingerprint, 2, fingerprint, self.state))
        self.assertFalse(result_is_current(2, fingerprint, 2, fingerprint, None))
        changed = copy.deepcopy(self.state)
        changed["tState"] = 5
        self.assertFalse(result_is_current(2, fingerprint, 2, fingerprint, changed))
        self.assertFalse(result_is_current(2, "", 2, fingerprint, self.state))

    def test_thinking_animation_cycles_only_one_to_three_dots(self):
        self.assertEqual("AI思考中.", thinking_text(10, 10))
        self.assertEqual("AI思考中..", thinking_text(10, 10.5))
        self.assertEqual("AI思考中...", thinking_text(10, 11))
        self.assertEqual("AI思考中.", thinking_text(10, 11.5))

    def test_gate_limits_concurrency_and_retries(self):
        gate = AIRequestGate()
        self.assertTrue(gate.can_start("a", 10.0))
        gate.start("a", 10.0)
        self.assertFalse(gate.can_start("b", 15.0))
        gate.finish(True, 15.0)
        self.assertFalse(gate.can_start("a", 16.0))
        self.assertTrue(gate.can_start("a", 17.0))
        gate.start("a", 17.0)
        gate.finish(True, 20.0)
        self.assertFalse(gate.can_start("a", 30.0))
        self.assertTrue(gate.can_start("b", 30.0))
        gate.start("b", 30.0)
        gate.invalidate()
        self.assertFalse(gate.can_start("c", 40.0))
        gate.finish(False, 40.0)
        self.assertTrue(gate.can_start("c", 41.0))

    def test_worker_delivers_result_and_failure_without_ui(self):
        results = queue.Queue()
        with patch("pct.overlay.ai_ask", return_value={"action": "put", "card": 2, "reason": "留对子"}):
            ai_worker({}, "test", 3, "fingerprint", results)
        self.assertEqual((3, "fingerprint", {"action": "put", "card": 2, "reason": "留对子"}), results.get_nowait())
        with patch("pct.overlay.ai_ask", side_effect=ValueError("secret")):
            ai_worker({}, "test", 4, "fingerprint", results)
        self.assertEqual((4, "fingerprint", {"action": "none", "reason": "AI请求失败"}), results.get_nowait())


if __name__ == "__main__":
    unittest.main()
