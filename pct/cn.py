# -*- coding: utf-8 -*-
"""数字 → 汉字。面板上不许出现阿拉伯数字。

为什么单独一层
--------------
牌的**值**本来就有映射（`rules.cname` / 前端 `cardText`），但 AI 生成的那句话
是自由文本，它会写「吃3成句，还差6方」，里面全是阿拉伯数字，读起来很割裂。

所以这里做的是**文本级**替换：任何一段连续数字，按下面的规则转成汉字。

    1~10      → 一二三四五六七八九十
    101~110   → 壹贰叁肆伍陆柒捌玖拾     （大字牌）
    其它      → 十 / 十五 / 二十 / 六十 / 一百零五

★ 为什么 101~110 走大写单字而不是「一百零五」：
  跑胡子里 101 是「大壹」这一张牌，不是数量。写成「一百零五」既长又不对。
  代价是：文本里真出现「105 张」这种数量时也会被当成牌。实际不会——一局只有 60 张。
"""
from __future__ import annotations

import re

XIAO = "一二三四五六七八九十"
DA = "壹贰叁肆伍陆柒捌玖拾"
_DIGITS = "零一二三四五六七八九"

_NUM = re.compile(r"\d+")

# 模型偶尔把 101 逐位念成「壹零壹」、110 念成「壹壹零」。这属于同一个牌的两种写法，
# 收敛回单字，否则面板上会出现「不拆壹零壹门子」这种别扭的话。
_DIGIT_READ_101_109 = re.compile(r"[壹一]零([壹贰叁肆伍陆柒捌玖])")
_DIGIT_READ_110 = re.compile(r"[壹一][壹一]零")


def _collapse_digit_readings(text: str) -> str:
    text = _DIGIT_READ_101_109.sub(lambda m: DA["壹贰叁肆伍陆柒捌玖".index(m.group(1))], text)
    return _DIGIT_READ_110.sub("拾", text)


def num_cn(n: int) -> str:
    """整数 → 中文读法（0 <= n < 10000）。超出范围原样返回。"""
    n = int(n)
    if n < 0 or n >= 10000:
        return str(n)
    if n < 11:
        return _DIGITS[n] if n < 10 else "十"
    if n < 20:
        return "十" + _DIGITS[n % 10]
    if n < 100:
        s = _DIGITS[n // 10] + "十"
        return s + (_DIGITS[n % 10] if n % 10 else "")
    if n < 1000:
        s = _DIGITS[n // 100] + "百"
        r = n % 100
        if r == 0:
            return s
        if r < 10:
            return s + "零" + _DIGITS[r]
        return s + num_cn(r)
    s = _DIGITS[n // 1000] + "千"
    r = n % 1000
    if r == 0:
        return s
    if r < 100:
        return s + "零" + num_cn(r)
    return s + num_cn(r)


def one(v: int) -> str:
    """单个数字按「牌值优先」翻译：101~110 用大写单字，1~10 用小写单字，其余按读法。"""
    if 101 <= v <= 110:
        return DA[v - 101]
    if 1 <= v <= 10:
        return XIAO[v - 1]
    return num_cn(v)


def to_cn_text(text: str) -> str:
    """把一段文本里的所有阿拉伯数字换成汉字。非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    return _collapse_digit_readings(_NUM.sub(lambda m: one(int(m.group())), text))
