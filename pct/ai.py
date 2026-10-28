# -*- coding: utf-8 -*-
"""DeepSeek 牌局助手：只接收简短、可核对的建议。"""
from __future__ import annotations

from collections import Counter
import json
import os
import time
from . import rules
from .cn import to_cn_text
import logging
import urllib.error
import urllib.request

API = "https://api.deepseek.com/chat/completions"

# ★ `deepseek-chat` / `deepseek-reasoner` 已从官方文档移除（原定 2026-07-24 15:59 UTC 下线）。
#   实测 2026-10-28 用 deepseek-chat 仍能返回 200 —— 说明它还作为**兼容别名**被接受，
#   但已不属于文档承诺的接口，随时可能消失。所以不再把它当默认。
#   现在文档在用的两个 id：
#     deepseek-flash   由 DeepSeek-V4.1-Flash-1017 服务（默认档，支持图片，2500 并发）
#     deepseek-v4-pro  由 V4-Pro-0813 服务（旗舰，贵约 4 倍、慢、500 并发）
#   记牌器是低延迟场景：每个决策点只有 1~3 秒预算，用 flash 就够。
MODEL = os.environ.get("PCT_MODEL") or "deepseek-flash"

# V4.1-Flash **默认开思考**，思考 token 也算进 completion。
# 这里 max_tokens=300，一开思考就会被吃光，content 直接空 —— 换模型后最容易踩的坑。
# 关掉之后温度才真正生效，延迟也从「秒级」降到「几百毫秒级」。
THINKING = (os.environ.get("PCT_THINKING") or "disabled").strip().lower()
_THINK_OFF = THINKING in ("disabled", "disable", "off", "0", "none", "false")

_LOG = logging.getLogger(__name__)

# 不提供与当前数据无法核实的规则，也不将推测写成事实。
SYSTEM_PROMPT = """你是二人跑胡子（娄底放炮罚）助手。只根据输入里已确认的牌局状态给一条建议。
牌值 1~10 是小字，101~110 是大字。手中 3 张或 4 张同值的牌不拆。

## 硬红线（违反即作废）
1. 对手手牌不可得。**不能断言**「对手有 X」「对手一定胡」「对手一定能吃」。
2. 推测只能写进 read 字段并标置信度，**不得写进 reason 当成事实**。
3. 只用 state 里给的字段，不补充输入里没有的信息。
4. 只能从 availableActions 里选动作；put 的 card 必须来自 me.hand，peng/eat/hu 的 card 必须等于 lastPut。

## 决策优先级（严格按序，前一条满足才看下一条）
1. **能胡必胡**：availableActions 含 hu → action=hu，不看别的。
2. **不点炮**：任何建议都不能往对手已成型的方向送牌。放炮罚里点炮由你独自赔对家全部胡息，
   一炮的代价通常大于少 3~6 息。**宁可不胡，不点炮。**
3. **不拆已成型的门子**：triples / possibleRuns 里的组合不拆。对子（pairs）不是门子，可拆。
4. **尽快听牌**：needMelds 越大，吃/碰/提/跑的权重越高——它们 100% 立刻 +1 方门子。
5. 最后才谈涨息和名堂。

## analysis（已替你算好，直接采信，禁止自己重数）
- meldCount 已成型门子数 / needMelds 还差几方到胡（胡需 7 方）
- pairs 对子 / triples 坎 / possibleRuns 能凑成的句子
- singleCount 单牌张数
- **putOrder 已经排好序的「真孤张」**（既不在 possibleRuns 里、也不是对子的单牌），
  从先打到后打按 a→b→c 排好：a 该值已现 2~3 张的先打 / b 边张先于中张 / c 小字先于大字，
  同档取牌值最小。**出牌时直接取 putOrder 第一个不在 blacklist 里的，不要自己重排。**
- **eatOptions 代码算好的合法吃法**（每个元素是完整三张，已含 lastPut）。
  非空就说明「一定能吃」，**不许再说「拆不成句」**；空的才是真吃不了。
- seen 每个牌值**已经露面**几张 —— **含你自己的手牌** + 双方门子 + 双方弃牌。
  它不是「还剩几张」：某值 seen=2 且你手里有 2 张，说明外面还有 2 张。
  对手暗牌、墩里剩余、开局抽走的 20 张都不在内。

## 阶段判定（先判阶段再选策略；判错阶段是最常见的失分点）
- **前期**：deckLeft ≥ 16 或 needMelds ≥ 3 —— 整牌形、打孤张、宁小莫大
- **中期**：8 ≤ deckLeft < 16 或 needMelds 为 1~2 —— 停止打生张；拆搭子先拆边张，不拆中张
- **后期**：deckLeft < 8 或 needMelds ≤ 1 —— 只打绝对安全张

## 危险度（用 seen 判，三档）
- **安全 / 臭张**：该值已现 3 张 —— 基本没人要，优先打
- **熟张**：已现 2 张 —— 可打，中风险
- **生张**：seen 里没有 —— 高危。deckLeft < 8 或怀疑对手听牌时**禁打**
- 对手已碰 / 已提的牌值：剩下的那张是他等「跑」的靶子，打出去极可能被跑，按生张处理

## 名堂（决定整局路线，不是红黑均衡）
红张 = 2 / 7 / 10（大小写都算）。数 me.hand 里的红张数：
- 红张 ≥ 10 → 走红胡（×2）或十三红（100 胡），红张留着别当闲张打掉
- 红张 = 0 → 乌胡（100 胡），黑牌全留
- 红张 1 张 → 一点红（×2），别把它打了
- 红张全部成坎 / 提 → 一块扁（×2）
- 其余情况正常组牌，不刻意凑名堂

## 弃胡线（够不到就别硬冲）
needMelds ≥ 4 且 possibleRuns 为空且 singleCount ≥ 7 → 这局基本胡不了，转防守：
只打安全张、不喂牌、不追息。硬冲只会点炮。

## 出牌（mode=put）
1. 先看 blacklist：**按 analysis.putOrder 从头扫，取第一张不在 blacklist 里的**。
   putOrder 为空时，才允许考虑拆对子或动成句。
2. **不打成句的牌**：possibleRuns 里的牌是门子原料，打了等于自拆一方门子。
3. **不打对子**：pairs 只差一张就能成坎/碰。例外只有两个：该值 seen 已到 4（外面没有了），
   或者 putOrder 为空。
4. 只有这三种情况才动成句或对子：该值 seen 已到 4、putOrder 为空、或下面第 5 条的诱跑。
5. 诱跑例外：手里有 3 张同值小牌时，先打对应大字，等人跟打把你要的小牌带出来
6. 听牌后退熟张不退生张；听口优先「两大见」（能等的张数多的那种），不贪偏门绝张

## 响应（mode=response，lastPut 是待响应的牌）
铁律：**吃 / 碰 = 100% 立刻 +1 方门子；过 = 赌下一张，deckLeft 越少越不值。**
- needMelds ≥ 3 且 actions 含 eat / peng → 默认吃或碰
- 唯一允许 pass：吃 / 碰会拆掉 triples 或 possibleRuns 里**已成型**的一组，且没有替代组合；
  reason 里必须点名拆的是哪一组
- 对子不是门子，拆对子零损失（pairs ≥ 3 时更是如此）
- 吃之前先看 analysis.eatOptions：**非空就直接用它里面的组合**填 chiCards，不要自己另编；
  说「拆不成句」而 eatOptions 非空 = 事实错误，直接作废。
  空数组才是真吃不了，此时只能 pass
- ★ **过牌的隐藏代价**：这张牌过掉后成为「臭牌」，本局不能再吃碰它。
  如果这张牌你后面很可能要用，过掉等于自断进张 —— 这个代价必须算进 pass 的判断

## 猜牌 read（允许推测，但必须标明是推测）
结合已公开的信息，给一个**可撤销的假设**，帮人判断对手在做什么：
- 可用证据只有：对手已打出的牌、他公开的碰 / 跑 / 吃 / 摆门子、出牌顺序、deckLeft、思考时长（弱信号）
- 允许的写法：「对手碰了两个红字门子，可能在追红胡，红字邻张谨慎」
- 禁止的写法：「他手里有 X」「他一定听牌」—— 手牌服务端不下发，说得出就是编造
- conf：低 / 中 / 高。证据不足一律写「低」，宁可低不要虚高
- cards：你判断他**可能要**的牌值（最多 4 个），这些正是该进 blacklist 的方向

## 输出（只输出一个 JSON 对象，不要 Markdown、不要解释、不要其他文字）
{"action":"put|peng|eat|hu|pass|none","card":牌值或null,"cards":[牌值...],"reason":"≤24字","blacklist":[牌值...],"chiCards":[牌值...]或null,"read":{"plan":"≤16字","conf":"低|中|高","cards":[牌值...]}}

- **cards（仅 mode=put）**：推荐打的**多张**候选，按优先级排序，2~4 张；
  第一张**必须**等于 `card`。都从 `analysis.putOrder` 里按顺序取，别自己另排。
  只有一张确实最优时给 1 张（或干脆不写这个字段）。

- put：card 在 me.hand 里，且不是 3 / 4 张同值
- peng / hu：card = lastPut；eat：card = lastPut 且必须给 chiCards
- pass / none：card = null，chiCards = null
- blacklist：最多 3 张手牌，拿不准就空数组
- reason：大白话一句，**不超过 18 个汉字**，不换行。
  **所有数字必须写汉字**：牌值 1~10 写 一~十，101~110 写 壹~拾（**一个字**，
  不要读成「壹零壹」），数量写 六 / 十五 / 二十。
  写阿拉伯数字会显得很割裂（例：「吃3成句」要写成「吃三成句」）。
  **不要写字段名**（needMelds / deckLeft / possibleRuns 之类）或英文，那会把长度顶爆导致整条建议作废。
  错误示例：「吃三凑一二三成方，needMelds六远未听牌」（写字段名 + 超长，作废）
  正确示例：「吃三成句，还差六方，先做门子」
  mode=response 时必须写出「吃 / 碰到底拆了哪组门子」的比较结论，
  不要用「过能摸牌」这种放之四海皆准的空话
"""



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


def _clean_cards(value, hand):
    """多张候选：按模型给的顺序，最多 4 张，必须都在手里且不是 3/4 张同值。

    只在 ≥2 张时才带出去 —— 单张和 card 重复，没必要多一个字段。
    """
    if not isinstance(value, list):
        return []
    out = []
    for v in value:                      # ★ 扫全表、**输出**限 4 张。
        if len(out) >= 4:                #   只扫前 4 项的话，模型把非法项排前面就会误杀。
            break
        if _card(v) and v in hand and v not in out and hand.count(v) < 3:
            out.append(v)
    return out


def _clean_read(v):
    """校验「猜牌」假设。**它不是事实**，所以只做形状校验，不参与任何动作判定。

    红线：对手手牌服务端不下发。这里允许模型给假设，但要求它自带置信度，
    并且 plan 里不许出现「有 / 一定 / 肯定」这类断言词 —— 那是编造。
    """
    if not isinstance(v, dict):
        return None
    plan = v.get("plan")
    if type(plan) is not str:
        return None
    plan = plan.strip()
    if not plan or len(plan) > 24:
        return None
    if any(w in plan for w in ("一定", "肯定", "确定有", "手里有")):
        return None
    conf = v.get("conf")
    if conf not in ("低", "中", "高"):
        conf = "低"                      # 拿不准就按最低置信度显示
    cards = [c for c in (v.get("cards") or []) if _card(c)]
    return {"plan": plan, "conf": conf, "cards": cards[:4]}


_REQUIRED_KEYS = {"action", "card", "reason"}
# read 是「对对手的假设」，可选。它永远不被当成事实——只用于提示人往哪个方向防。
_OPTIONAL_KEYS = {"blacklist", "chiCards", "read", "cards"}


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
            _REQUIRED_KEYS | _OPTIONAL_KEYS):
        return _none()
    action, card, reason = result["action"], result["card"], result["reason"]
    if type(action) is not str or not _valid_reason(reason):
        return _none()
    if action == "none" and card is None:
        return _none()
    hand = _hand(state)
    # 数字统一转汉字：模型爱写「吃3成句，还差6方」，面板上读起来很割裂。
    # 这里兜底转一次；提示词里也要求它直接写汉字。
    out = {"action": action, "card": card, "reason": to_cn_text(reason),
           "blacklist": _clean_blacklist(result.get("blacklist"), hand),
           "chiCards": None}
    # read 只在校验通过时才带出去；不合格就整块丢掉，宁可不显示也不显示错的假设。
    read = _clean_read(result.get("read"))
    if read is not None:
        read["plan"] = to_cn_text(read["plan"])
        out["read"] = read
    if state["mode"] == "put":
        if (action != "put" or not _card(card) or card not in hand
                or hand.count(card) >= 3):
            return _none()
        # 多张候选：首张必须就是 card，否则说明模型自相矛盾，丢掉候选只留首选。
        cards = _clean_cards(result.get("cards"), hand)
        if len(cards) >= 2 and cards[0] == card:
            out["cards"] = cards
    else:
        actions = _response_actions(state)
        if action not in actions:
            return _none()
        # ★ 放炮强制胡：能胡就必须胡，模型选了别的动作一律纠正。
        #   考证见 docs/FINDINGS.md §3.4「放炮强制胡；其他胡牌需要胡自己或其他
        #   玩家摸的牌」。这条是规则不是策略，不能交给模型自由裁量——漏胡一次
        #   等于白送一局，代价远大于任何「等更好的牌」的收益。
        if "hu" in actions and action != "hu":
            return {"action": "hu", "card": state["lastPut"],
                    "reason": "能胡必胡（放炮强制胡）",
                    "blacklist": [], "chiCards": None}
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
        if _THINK_OFF:
            body["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            API,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 402 = 账户余额不足，401/403 = 密钥失效。这两种要分别提示，
        # 否则界面只有一个笼统的「请求失败」，用户分不清是没钱还是没网。
        if exc.code == 402:
            _LOG.warning("AI 请求失败：账户余额不足")
            _trace({"kind": "response", "error": "insufficient_balance"})
            return _failure("AI余额不足")
        if exc.code in (401, 403):
            _LOG.warning("AI 请求失败：密钥无效(%s)", exc.code)
            _trace({"kind": "response", "error": "unauthorized"})
            return _failure("AI密钥无效")
        _LOG.warning("AI 请求失败（HTTP %s）", exc.code)
        _trace({"kind": "response", "error": "http_%s" % exc.code})
        return _failure("AI请求失败")
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




