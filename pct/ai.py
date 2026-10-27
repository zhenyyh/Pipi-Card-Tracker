# -*- coding: utf-8 -*-
"""DeepSeek 牌局助手：只接收简短、可核对的建议。"""
from __future__ import annotations

from collections import Counter
import json
import os
import time
from . import rules
import logging
import urllib.request

API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
_LOG = logging.getLogger(__name__)

# 不提供与当前数据无法核实的规则，也不将推测写成事实。
SYSTEM_PROMPT = """你是二人跑胡子助手。只根据输入中已确认的牌局状态给一条建议。
牌值 1~10 是小字，101~110 是大字；手中三张或四张同值牌不拆。
对手手牌未知，不能断言对手能吃、碰、胡，不能从犹豫或历史出牌推断其手牌。
只依据已确认的手牌和门子；参考对手已公开的吃、碰和弃牌，但不能猜测对手的暗牌。
输入缺失或过时的信息不要推测。

## analysis 字段（已替你算好，直接采信，不要自己重数）
- meldCount 已成型门子数 / needMelds 还差几方到胡（胡需 7 方）
- pairs 对子（可碰/偎）/ triples 坎（可提/跑）
- possibleRuns 手里已能凑成的一句话（如 [101,102,103]）
- singleCount 孤张数量（越大越该先打孤张）
- seen 已经露面的牌各几张（含你的手牌/双方门子/双方弃牌；对手暗牌与墩里未知）——
  用它算「还剩几张可等」

## 实战策略（高手经验，按条件执行）
### 出牌（mode=put）
1. **宁小莫大**：开局优先打孤张小字（1~10），大字留着（大字门子息更高：碰3/偎6/跑9/提12）
2. **诱跑例外**：手里有 3 张同值小牌（如 3 张小八）时，优先打对应的大牌（大捌）——
   别人跟打大捌的「臭」或打出小八，你能跑起来
3. **别人碰过 2 次的牌别捏手里**：说明对手在做这门，捏着既不成门子又容易点炮
4. **中张孤牌早打**：早打不喂牌，晚打防点炮
5. **听牌后（needMelds ≤ 1）退熟张**：宁可少 3 息也不退生张——放炮罚下点炮代价远大于 3 息
6. **没须时转防守**：needMelds 高、possibleRuns 为空、孤张极多时，放弃主攻，
   专打对手已出过/已断张的牌，绝不往对手门子方向送字

### 操作（mode=response）
7. **能提（12息）/跑（9息）优先做**，收益远大于摸一张牌
8. **碰**：拆掉的是「可替换对子」就碰；只有拆散已成型门子时才过
9. **吃**：能凑成 一二三/二七十/绞 就吃（尤其 needMelds ≤ 3 时）
10. **节奏**：needMelds ≥ 5 优先多摸牌凑门子；needMelds ≤ 3 优先成门子+涨息
11. **听牌优先级**：多头听（听 3 张以上）> 隐蔽听（听对手想不到的绝张）> 快听（放弃大牌速听小胡）
12. **听存活率高的**：用 analysis.seen 判断——已现 3 张的牌基本死了（别等），已现 2 张的谨慎，
    已现 0~1 张的才值得等
13. **危险期**：墩上剩 10 张以内不再出生张（seen 为 0 的牌），优先打已现 2~3 张的
14. **拆对保安全**：必须拆对子时优先拆小黑对（1、5 这类边缘牌），别拆能发展成坎的
15. **碰完打闲张**：碰完对子后优先打孤张闲牌，让手牌紧凑、更快听牌
16. **判断对手听牌**：对手出牌后明显犹豫、或开始盯场上已现牌 → 大概率听牌，别打生张；
    对手连续弃同一门的牌 → 他在做那门，别往那个方向送
17. **红黑均衡**：手牌红（2/7/10）黑比例别太偏，单色成型难

## 三个作用
1. 出牌推荐（mode=put）：从 me.hand 选一张可打的牌，action=put
2. 避免点炮：输出 blacklist——现在手里**绝对不能打**的牌（打出去对手就能吃/碰/胡的方向，或拆掉自己必成的门子）。拿不准就空数组
3. 特殊操作指导（mode=response，lastPut 是待响应的牌）——**必须逐个评估 availableActions 里的每个动作，不许只算一个就下结论**：
   - action=peng：碰。算清楚：手里是否真有 2 张 lastPut；碰后成 1 方门子（大字3息/小字1息）；
     这两张是否本来已能和其他牌组成门子（尤其是「绞」，大小同点交错）——碰了会不会把它拆散；
     碰不摸牌，等于牺牲一次摸牌机会
   - action=pass：过。过则轮到自己摸牌，多一张凑牌机会
   - action=hu：胡
   - action=eat：吃，**必须给 chiCards** = [lastPut, X, Y] 的具体组合（X/Y 来自手牌；组合只能是 一二三 / 二七十 / 大小同点绞）
   - 只能从 availableActions 里选；拿不准就 pass 或 none

## 输出（只输出一个 JSON 对象，不要 Markdown、解释、其他文字）
{"action":"put|peng|eat|hu|pass|none","card":牌值或null,"reason":"一句大白话≤16字","blacklist":[牌值...],"chiCards":[牌值...]或null}

- put：card=要打的牌（必须在 me.hand 里，且不是 3/4 张同值）
- eat：card=lastPut，chiCards=[lastPut, 手牌X, 手牌Y]
- peng/hu：card=lastPut
- pass/none：card=null，chiCards=null
- blacklist：最多 3 张手牌（现在不能打的），拿不准就空数组
- reason：大白话一句，≤24 字，不换行。**mode=response 时必须体现比较结论**
  （例：「碰只+1息还拆绞，过能摸牌」）"""



_TRACE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "ai_trace.jsonl")


def _trace(obj: dict) -> None:
    """完整落盘一次请求/响应（不截断），用于复盘 AI 判断依据。"""
    try:
        os.makedirs(os.path.dirname(_TRACE_PATH), exist_ok=True)
        rec = dict(obj)
        rec["ts"] = int(time.time())
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _none() -> dict:
    return {"action": "none", "reason": "暂无可靠建议"}


def _failure(reason: str) -> dict:
    """返回可供界面显示和调用方判断的简短故障状态。"""
    return {"action": "none", "reason": reason}


def _card(value) -> bool:
    return type(value) is int and (1 <= value <= 10 or 101 <= value <= 110)


def _hand(state):
    if state.get("handKnown") is False:
        return None
    me = state.get("me")
    if isinstance(me, dict) and me.get("handKnown") is False:
        return None
    hand = me.get("hand") if isinstance(me, dict) else None
    if not isinstance(hand, list) or not hand or not all(_card(c) for c in hand):
        return None
    if any(n > 4 for n in Counter(hand).values()):
        return None
    return hand


def _response_actions(state):
    """只信明确提供的合法动作及响应牌；缺少信息时不进行推断。"""
    actions = state.get("availableActions")
    # 不要求 tState==5：实测 tState=4 时吃/碰/过按钮也会弹出（按钮可见才是证据）
    if (state.get("responsePlayer") is None
            or state.get("mySeat") is None
            or state.get("responsePlayer") != state.get("mySeat")
            or not _card(state.get("lastPut"))
            or not isinstance(actions, list) or not actions
            or not all(type(a) is str for a in actions)):
        return set()
    # 吃（chi0）需要指定具体组合（chiCards），有具体组合才显示「吃」
    return set(actions) & {"peng", "hu", "pass", "eat"}


def _eligible(state) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("mode") == "put":
        hand = _hand(state)
        return (state.get("tState") in (3, 4)
                and state.get("mySeat") is not None
                and state.get("curPlayer") is not None
                and state.get("mySeat") == state.get("curPlayer")
                and hand is not None
                and any(n < 3 for n in Counter(hand).values()))
    if state.get("mode") == "response":
        return _hand(state) is not None and bool(_response_actions(state))
    return False


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey("duplicate JSON key")
        out[key] = value
    return out


def _parse(text):
    """兼容对象外的多余文字；对象本身和字段仍按严格 JSON 校验。"""
    if not isinstance(text, str):
        raise ValueError("content is not text")
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    start = 0
    while True:
        start = text.find("{", start)
        if start < 0:
            raise ValueError("JSON object missing or malformed")
        try:
            result, _ = decoder.raw_decode(text[start:])
            return result
        except _DuplicateKey:
            # 不得跳到重复键对象内的子对象，以免绕过严格校验。
            raise
        except json.JSONDecodeError:
            # 普通前言可能含有非 JSON 的大括号；继续找完整对象。
            start += 1


def _valid_reason(reason) -> bool:
    return (type(reason) is str and bool(reason.strip()) and len(reason) <= 24
            and reason == reason.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in reason))


_REQUIRED_KEYS = {"action", "card", "reason"}


def _clean_blacklist(value, hand):
    """黑榜：最多 3 张、必须在手里、合法牌值；不合规矩就丢弃（不整条判无效）。"""
    if not isinstance(value, list):
        return []
    out = []
    for v in value[:3]:
        if _card(v) and v in hand and v not in out:
            out.append(v)
    return out


def _clean_chi(value, state):
    """吃的具体组合：必须含 lastPut、其余在手牌、张数 3、类型仅限已知三种。"""
    if not isinstance(value, list) or len(value) != 3:
        return None
    hand = _hand(state)
    last = state.get("lastPut")
    if hand is None or not _card(last):
        return None
    if last not in value:
        return None
    rest = [c for c in value if c != last]
    if len(rest) != 2 or not all(_card(c) for c in rest):
        return None
    if rest[0] not in hand or rest[1] not in hand:
        return None
    if rest[0] == rest[1] and hand.count(rest[0]) < 2:
        return None
    # 组合必须是跑胡子的合法句：一句话（一二三/二七十）或绞（大小同点交错）
    try:
        kind = rules.classify([last, rest[0], rest[1]], "other")[0]
    except Exception:
        return None
    if kind not in ("吃", "绞"):
        return None
    return [last, rest[0], rest[1]]


def validate(state: dict, result) -> dict:
    """校验模型返回。无法核实的动作一律不显示为具体建议。"""
    if not _eligible(state) or not isinstance(result, dict):
        return _none()
    if not _REQUIRED_KEYS.issubset(result) or not set(result) <= (
            _REQUIRED_KEYS | {"blacklist", "chiCards"}):
        return _none()
    action, card, reason = result["action"], result["card"], result["reason"]
    if type(action) is not str or not _valid_reason(reason):
        return _none()
    if action == "none" and card is None:
        return _none()
    hand = _hand(state)
    out = {"action": action, "card": card, "reason": reason,
           "blacklist": _clean_blacklist(result.get("blacklist"), hand),
           "chiCards": None}
    if state["mode"] == "put":
        if (action != "put" or not _card(card) or card not in hand
                or hand.count(card) >= 3):
            return _none()
    else:
        if action not in _response_actions(state):
            return _none()
        if action == "pass":
            if card is not None:
                return _none()
        elif action == "eat":
            chi = _clean_chi(result.get("chiCards"), state)
            if chi is None or card != state["lastPut"]:
                return _none()
            out["chiCards"] = chi
        elif (not _card(card) or card != state["lastPut"]
              or (action == "peng" and hand.count(card) < 2)):
            return _none()
    return out


def ask(state: dict, api_key: str, timeout: float = 10.0) -> dict:
    """请求模型并验证结果；网络、格式和校验失败均不编造建议。"""
    if not _eligible(state):
        _LOG.info("AI 请求跳过：缺少可验证的决策信息")
        return _none()
    if not isinstance(api_key, str) or not api_key.strip():
        _LOG.warning("AI 请求跳过：未配置 API 密钥")
        return _failure("未配置密钥")
    _trace({"kind": "request", "mode": state.get("mode"), "state": state})
    try:
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 300,
        }
        req = urllib.request.Request(
            API,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:
        # 不记录异常内容、响应正文或密钥，避免泄露请求凭据。
        _LOG.warning("AI 请求失败（%s）", type(exc).__name__)
        _trace({"kind": "response", "error": type(exc).__name__})
        return _failure("AI请求失败")
    try:
        data = json.loads(raw.decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        parsed = _parse(content)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        _LOG.warning("AI 返回格式错误")
        _trace({"kind": "response", "error": "format"})
        return _failure("AI格式错误")
    result = validate(state, parsed)
    valid_none = (isinstance(parsed, dict) and set(parsed) == {"action", "card", "reason"}
                  and parsed.get("action") == "none" and parsed.get("card") is None
                  and _valid_reason(parsed.get("reason")))
    if result["action"] == "none" and not valid_none:
        _LOG.warning("AI 建议未通过合法性校验")
        _trace({"kind": "response", "raw": content, "parsed": parsed,
                "validated": result, "rejected": True})
        return _failure("AI建议无效")
    _trace({"kind": "response", "raw": content, "parsed": parsed, "validated": result})
    return result




