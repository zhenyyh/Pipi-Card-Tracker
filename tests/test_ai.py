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
    return {"action": action, "card": card, "reason": reason}


def test_valid_put_and_strict_action_card_state():
    assert ai.validate(PUT, response()) == response()
    for result in [response(card=3), response(card=7), response(card=True),
                   response(action="peng"), response(card="106"),
                   response(reason="这是一段超过十六个字而且不够简洁的理由"),
                   response(reason="留对子\n再说"),
                   {**response(), "blacklist": [1]}]:
        assert ai.validate(PUT, result) == NONE
    assert ai.validate({**PUT, "tState": 5}, response()) == NONE
    assert ai.validate({**PUT, "curPlayer": 2}, response()) == NONE
    assert ai.validate({**PUT, "mySeat": None}, response()) == NONE
    assert ai.validate({**PUT, "handKnown": False}, response()) == NONE
    assert ai.validate({**PUT, "me": {**PUT["me"], "handKnown": False}}, response()) == NONE
    assert ai.validate({**PUT, "me": {"hand": []}}, response()) == NONE
    assert ai.validate({**PUT, "me": {"hand": [106] * 5}}, response()) == NONE


def test_response_requires_authoritative_actions_and_target():
    assert ai.validate(RESPONSE, response("peng")) == response("peng")
    assert ai.validate(RESPONSE, response("pass", None)) == response("pass", None)
    assert ai.validate(RESPONSE, response("hu")) == response("hu")
    assert ai.validate(RESPONSE, response("eat")) == NONE
    assert ai.validate(RESPONSE, response("peng", 1)) == NONE
    assert ai.validate({**RESPONSE, "me": {"hand": [106, 1]}}, response("peng")) == NONE
    assert ai.validate(RESPONSE, response("pass", 106)) == NONE
    for changes in [{"availableActions": None}, {"availableActions": ["pass"]},
                    {"lastPut": None}, {"responsePlayer": None}, {"responsePlayer": 2}, {"tState": 4}]:
        assert ai.validate({**RESPONSE, **changes}, response("peng")) == NONE


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
        assert ai.ask(PUT, "test-key") == response()
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
