# -*- coding: utf-8 -*-
"""测试模型输出的边界，不调用外部服务。"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pct import ai


PUT = {"mode": "put", "tState": 4, "mySeat": 1, "curPlayer": 1, "me": {"hand": [1, 2, 2, 106, 7, 7, 7]}}
RESPONSE = {"mode": "response", "tState": 5, "lastPut": 106,
            "availableActions": ["peng", "hu", "pass"], "responsePlayer": 1, "mySeat": 1,
            "me": {"hand": [106, 106, 1]}}
NONE = {"action": "none", "reason": "暂无可靠建议"}


def response(action="put", card=106, reason="留对子"):
    """模型**返回**的最小形状（只有必填三项）。"""
    return {"action": action, "card": card, "reason": reason}


def full(action="put", card=106, reason="留对子", blacklist=None, chiCards=None):
    """`validate` 的**输出**形状：blacklist / chiCards 恒定存在，UI 直接读这两栏。

    以前测试直接拿 `response()` 去比，等于假设 validate 原样回传 —— 那是在
    blacklist / chiCards 加进来之前的契约，早过期了。
    """
    return {"action": action, "card": card, "reason": reason,
            "blacklist": blacklist or [], "chiCards": chiCards}


FORCED_HU = full(action="hu", card=106, reason="能胡必胡（放炮强制胡）")


def test_valid_put_and_strict_action_card_state():
    assert ai.validate(PUT, response()) == full()

    # 这些都必须整条作废
    for result in [response(card=3),            # 不在手里
                   response(card=7),            # 手里 3 张同值，不能打
                   response(card=True),         # bool 不是 int
                   response(action="peng"),     # put 模式不许碰
                   response(card="106"),        # 字符串
                   response(reason="这一段理由明显超过二十四个汉字会被长度校验直接拒绝掉"),
                   response(reason="留对子\n再说")]:
        assert ai.validate(PUT, result) == NONE

    # ★ reason 上限是 24 字，不是 16 —— 超过 16 但仍在上限内的理由必须放行
    ok20 = "这是一段超过十六个字而且不够简洁的理由"
    assert 16 < len(ok20) <= 24, len(ok20)
    assert ai._valid_reason(ok20)
    assert ai.validate(PUT, response(reason=ok20))["reason"] == ok20

    # ★ 黑榜不合规只清洗条目，不整条作废（拿不准时给空数组是有意义的信息）
    assert ai.validate(PUT, {**response(), "blacklist": [1, 999, "x"]})["blacklist"] == [1]
    assert ai.validate(PUT, {**response(), "blacklist": [999]})["blacklist"] == []
    assert ai.validate(PUT, {**response(), "blacklist": [1, 1, 2, 2]})["blacklist"] == [1, 2]

    assert ai.validate({**PUT, "tState": 5}, response()) == NONE
    assert ai.validate({**PUT, "curPlayer": 2}, response()) == NONE
    assert ai.validate({**PUT, "mySeat": None}, response()) == NONE
    assert ai.validate({**PUT, "handKnown": False}, response()) == NONE
    assert ai.validate({**PUT, "me": {**PUT["me"], "handKnown": False}}, response()) == NONE
    assert ai.validate({**PUT, "me": {"hand": []}}, response()) == NONE
    assert ai.validate({**PUT, "me": {"hand": [106] * 5}}, response()) == NONE


def test_response_requires_authoritative_actions_and_target():
    # 没有 hu 的窗口：碰 / 过照常
    no_hu = {**RESPONSE, "availableActions": ["peng", "pass"]}
    assert ai.validate(no_hu, response("peng")) == full(action="peng")
    assert ai.validate(no_hu, response("pass", None)) == full(action="pass", card=None)
    assert ai.validate(no_hu, response("eat")) == NONE          # 窗口里没有吃
    assert ai.validate(no_hu, response("peng", 1)) == NONE      # 碰的不是 lastPut
    assert ai.validate(no_hu, response("pass", 106)) == NONE    # 过牌不能带牌
    assert ai.validate({**no_hu, "me": {"hand": [106, 1]}}, response("peng")) == NONE

    # ★ 放炮强制胡：窗口里有 hu，选别的动作一律纠正成 hu。
    #   这条是规则不是策略 —— 漏胡一次等于白送一局。
    assert ai.validate(RESPONSE, response("peng")) == FORCED_HU
    assert ai.validate(RESPONSE, response("pass", None)) == FORCED_HU
    assert ai.validate(RESPONSE, response("hu")) == full(action="hu")

    for changes in [{"availableActions": None}, {"availableActions": ["pass"]},
                    {"lastPut": None}, {"responsePlayer": None}, {"responsePlayer": 2}]:
        assert ai.validate({**no_hu, **changes}, response("peng")) == NONE

    # ★ tState=4 也算合法响应窗口：实测 tState=4 时吃/碰/过按钮同样会弹出，
    #   「按钮可见」才是证据，不能只看 tState==5（见 _response_actions 注释）。
    assert ai.validate({**no_hu, "tState": 4}, response("peng")) == full(action="peng")


def test_parser_handles_trailing_prose_but_rejects_duplicate_keys():
    assert ai._parse('```json\n{"action":"put","card":106,"reason":"留对子"}\n```多余文字') == response()
    assert ai._parse('说明 {仅参考}：{"action":"put","card":106,"reason":"留对子"} 完') == response()
    try:
        ai._parse('{"action":"put","action":"hu"}')
        assert False, "duplicate keys must fail"
    except ValueError:
        pass
    try:
        ai._parse('前文 {说明} {"action":"put","action":"hu","card":106,"reason":"留对子"}')
        assert False, "duplicate keys must fail after prefix text"
    except ValueError:
        pass


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps({"choices": [{"message": {"content": self.content}}]},
                          ensure_ascii=False).encode("utf-8")


def test_request_and_model_failures_are_conservative():
    with patch("pct.ai.urllib.request.urlopen", return_value=FakeResponse(
            '{"action":"put","card":106,"reason":"留对子"}额外内容')):
        assert ai.ask(PUT, "test-key") == full()
    with patch("pct.ai.urllib.request.urlopen", return_value=FakeResponse("无 JSON")):
        assert ai.ask(PUT, "test-key")["reason"] == "AI格式错误"
    with patch("pct.ai.urllib.request.urlopen", return_value=FakeResponse(
            '{"action":"put","action":"hu","card":106,"reason":"留对子"}')):
        assert ai.ask(PUT, "test-key")["reason"] == "AI格式错误"
    malformed = FakeResponse("")
    malformed.read = lambda: b'{"choices": []}'
    with patch("pct.ai.urllib.request.urlopen", return_value=malformed):
        assert ai.ask(PUT, "test-key")["reason"] == "AI格式错误"
    with patch("pct.ai.urllib.request.urlopen", return_value=FakeResponse(
            '{"action":"put","card":999,"reason":"留对子"}')):
        assert ai.ask(PUT, "test-key")["reason"] == "AI建议无效"
    with patch("pct.ai.urllib.request.urlopen", return_value=FakeResponse(
            '{"action":"none","reason":"缺少card"}')):
        assert ai.ask(PUT, "test-key")["reason"] == "AI建议无效"
    with patch("pct.ai.urllib.request.urlopen", return_value=FakeResponse(
            '{"action":"none","card":null,"reason":"无把握"}')):
        assert ai.ask(PUT, "test-key") == NONE
    with patch("pct.ai.urllib.request.urlopen", side_effect=TimeoutError("secret")):
        assert ai.ask(PUT, "test-key")["reason"] == "AI请求失败"
    with patch("pct.ai.urllib.request.urlopen") as network:
        assert ai.ask({**RESPONSE, "availableActions": None}, "test-key") == NONE
        assert ai.ask(PUT, "")["reason"] == "未配置密钥"
        network.assert_not_called()


def test_failures_never_log_key_or_exception_body():
    import logging
    from io import StringIO
    output = StringIO()
    handler = logging.StreamHandler(output)
    ai._LOG.addHandler(handler)
    try:
        with patch("pct.ai.urllib.request.urlopen", side_effect=TimeoutError("secret-key")):
            result = ai.ask(PUT, "secret-key")
        assert result["reason"] == "AI请求失败"
        assert "secret-key" not in output.getvalue()
    finally:
        ai._LOG.removeHandler(handler)




if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  ✓ %s" % name)
            except Exception as exc:
                failures += 1
                print("  ✗ %s: %s" % (name, exc))
    print("\n失败 %d" % failures)
    sys.exit(1 if failures else 0)
