# Pipi Card Tracker

[![test](https://github.com/zhenyyh/Pipi-Card-Tracker/actions/workflows/test.yml/badge.svg)](https://github.com/zhenyyh/Pipi-Card-Tracker/actions/workflows/test.yml)

皮皮跑胡子（`com.happyplay.hnphz`）的牌局采集与还原。**不猜字段**——
每一个口径都能在客户端自己的字节码或官方规则文本里找到出处，
出处和推导过程都在 [`docs/FINDINGS.md`](docs/FINDINGS.md)。

上一代工程（`phz-hook-emu`）能跑，但有一堆「大概是这么回事」的地方，
踩了三个大坑才勉强对上：总牌守恒差 3 张、明牌胡息对不上 `origWinNum`、
「将」归不了类。这个工程把这些点全部回到证据上重做了一遍。

**依赖**：Python 3.9+。离线还原只用标准库，不需要设备或游戏；
设备悬浮窗还需要与设备端 frida-server 版本匹配的 Python `frida` 包。

```bash
python -m pct replay tests/fixtures/round_v3.jsonl   # 立刻看一局
python tests/test_rules.py                           # 回归 23 项
```

---

## 它做什么

```
设备（雷电9 + Zygisk ARM agent）
   │  agent/trace.js 读 jsclient.data.sData + 挂钩 pomelo 收包
   ↓
事件流 jsonl（MJPut / MJPeng / MJGang / MJChi / roundEnd / _live …）
   │  pct/replay.py 两套来源对账
   ↓
牌谱：发牌 / 明牌（带来源）/ 弃牌堆 / 终局手牌拆门子 / 总牌守恒 / 胡息对账
```

**两套来源对账**是这个工程和上一代最大的区别：

| 来源 | 拿到什么 | 弱点 |
|---|---|---|
| `sData` live 快照 | `mjpeng` 碰 / `mjpeng1` 偎 / `mjgang0` 提 / `mjgang1` 跑 / `mjchi` 吃 / `mjlie` 摆火 —— **牌型是数组名给的，不用推** | 结算后会被清空 |
| 事件流 | `MJGang{card, gang, from}` 等收包 | `gang` 是牌型编码不是张数；胡牌那一下会重发，凭空多 3 张 |

两边算出来必须一样。不一样就**报出来**，不静默取信一边。

---

## 三条硬约束（违反任何一条都会「看起来像采集失败」）

1. **`evalString` 只返回 `bool`** —— 拿不到返回值。JS 必须自己
   `jsb.fileUtils.writeStringToFile()` 落盘。
2. **agent 用「`lastResult()` 返回值有没有变」判断脚本跑完了** ——
   内容完全相同会被当成「没执行」，5 秒超时且不拷输出。
   ⇒ **每个采集脚本的 `return` 必须带 `Date.now()`。**
3. **输出文件名写死为 `phz_probe_out.json`** —— 原生 agent
   （`phz-hook-emu/agent-arm64/jni/agent.cpp` 的 `kProbeSrc`）只拷这一个。
   写别的名字等于白跑。

外加一条操作纪律：**`clear` 会顺带卸钩**。开新一轮之前别随手 clear，
否则整局收不到事件（这个坑踩过，表现就是「又打了一局但 0 事件」）。

---

## 跑起来

```bash
# 离线还原（不需要设备、不需要游戏）
python -m pct replay tests/fixtures/round_v3.jsonl
python -m pct replay 某份流.jsonl --round 1 2 -1     # 指定第几局，-1 = 倒数第一

# 上设备
python -m pct probe      # 自检：钩子在不在（pEmit / iEmit 必须是 1）
python -m pct arm        # 装钩
python -m pct watch      # 边打边记，落盘 data/stream_*.jsonl
python -m pct dump       # 打一份当前牌谱（顺手存 data/dump_*.json）

# 回归
python tests/test_rules.py
```

设备参数在 `pct/bridge.py` 的 `DEFAULTS` 里：adb 路径、`127.0.0.1:5555`、
包名、以及原生 agent 的驱动脚本（外部工程 `phz-hook-emu/ldmon.py`，
Zygisk provider + houdini 翻译层，不在这里重写）。

### AI 出牌建议

守护程序优先从环境变量 `DEEPSEEK_API_KEY` 读取 DeepSeek 密钥，也可读取
本机的 `.pct/deepseek_key` 文件。该文件不纳入 Git，可以继续使用已有密钥。
首次使用悬浮窗时安装与设备端匹配的 Frida（本设备为 16.7.19），
然后在启动守护程序的 PowerShell 窗口中设置：

```powershell
python -m pip install frida==16.7.19
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API 密钥"
python -m pct.overlay
```

未配置密钥时记牌器仍可使用，AI 面板会提示配置。AI 只提供简短的出牌建议；
由于目前无法可靠确认响应者及可用操作，吃、碰、胡等响应阶段暂不提供建议。
牌局变化时旧建议会作废。密钥保留在本机配置文件或环境变量中。

### 还原出来长什么样

```
── 明牌 ──
  座位1  合计 18 胡
    吃 壹 贰 叁          6胡  大字顺 壹贰叁（特殊顺子 6胡）  [chi]
    吃 叁 肆 伍          0胡  小字顺 叁肆伍                  [chi]
    跑 十 十 十 十       6胡  小字跑                        [gang1]
    偎 拾 拾 拾          6胡  大字偎                        [peng1]

── 终局手牌 ──
  座位1 ( 8张) 二 玖 八 二 八 捌 柒 捌          红张3
           顺 柒捌玖(0胡)
           绞 八八捌（0胡）
           将 二二（1 对，只有已有跑/提时才算 1 方门子，本身不产生胡息）
           手牌门子自算 0 胡（坎 0 + 顺 0 + 碰 0），3 方门子

── 总牌守恒 ──
  座位0  手11 + 弃 4 + 明 9 = 24
  座位1  手 8 + 弃 3 + 明13 = 24
  合计 48 张在场 + 墩剩 12 = 60 / 本局 60 张  ✓

── 结算：座位1 胡牌  winNum=72 origWinNum=18 ×1 ──
  座位1  本局 72 分  累计 72
          胡息自算：明牌 18 + 手牌 0 = 18  ✓ 与 origWinNum 一致
          胡型：十红 · 自摸
```

---

## 目录

```
pct/
  rules.py     牌值/牌型/胡息，以及「手牌拆门子」（坎 / 顺 / 绞 / 将 / 碰）
  replay.py    事件流 → 牌谱，两套来源对账，总牌守恒
  render.py    排版给人看
  bridge.py    设备桥（文件驱动 evalString）
  cli.py       命令行
agent/
  trace.js     跑在游戏里的采集脚本（live 读六个数组 + 钩 pomelo）
tools/
  jsc_strings.py   从 .jsc（SpiderMonkey XDR）里精确抠字符串原子
docs/
  FINDINGS.md      口径考证：出处 + 推导 + 踩过的坑
  rules/           游戏自带的官方规则文本（邵阳剥皮 / 二人 / 长沙）
tests/
  test_rules.py    20 项回归
  fixtures/        真实对局（含一局荒庄、一局有坎、一局连打 4 把）
```

---

## 口径速查

```
牌值   组别×100 + 点数      1..10 小写 / 101..110 大写     六 显示为「陸」
红张   二七十，大小写都红    [2,7,10,102,107,110]            24 张 / 80
牌池   80 - choupai          本房间 choupai=20 → 池 60（二人精简抽掉 20 张）
发牌   initHandNum × playerNum = 20 × 2 = 40
墩     池 - 发牌 = 20        墩剩 = 池 - cardNext
守恒   手牌 + 弃牌 + 明牌 + 墩剩 = 池
       ★ 弃牌堆要剪掉「lastPut 出现在非 lastPutPlayer 堆里」的那张（已易主）
明牌   mjpeng 碰 / mjpeng1 偎 / mjgang0 提 / mjgang1 跑
       mjchi 吃 + 绞 / mjlie 摆火（官方叫「比牌」）
胡息   提 12/9  跑 9/6  偎 6/3  碰 3/1  坎 6/3
       特殊顺（壹贰叁 / 贰柒拾）大 6 / 小 3   普通顺 0   绞 0   将 0
       ★ 手上 3 张同值：自己摸齐 = 坎(6/3)，第 3 张来自别人 = 碰(3/1)
         判据 lastPut ∈ mjhand 且 lastDrawPlayer != winner（放炮）
对账   ① 明牌息 + 手牌门子息 == origWinNum
       ② origWinNum × 名堂倍率 == winNum        ← 两个都要过
门子   明牌组数 + 手牌组数 == 7 才能胡（特殊 3 提 5 坎例外）
名堂   固定 100：红≥13 十三红 / 红=0 乌胡 / 胡息=30 三十胡
       翻倍（乘算）：10≤红<13 红胡 / 红=1 一点红 / 胡息=20 卡胡 / 自摸
       放炮不翻倍
玩法   huziType=2 == 娄底放炮罚（起胡 15 胡）；邵阳剥皮是 10 胡
```

**玩法归属**：`limitScore=15` 对上娄底放炮罚的「胡息数≥15」，所以 `huziType=2`
就是**娄底放炮罚**。两套玩法的胡息表逐项一致，差别全在名堂算法 ——
邵阳剥皮是「自摸 **+10 胡**」（加法），娄底放炮罚是「自摸 **×2**」（乘法）。
四局实测 `21→42`、`18→72(红胡×2·自摸×2)`，只能是乘法。

**双锚点对账**：`origWinNum` 是裸胡息，`winNum` 是名堂加成后的分数。
两个都要对上才算读懂一局 —— 光对胡息只证明「门子算对了」，名堂对得上才证明整局读通了。
名堂判据完全用红字张数 + 是否自摸自己算，**不看 `mjdesc` 字符串**
（那是展示用的，字符串没穷举过）。

`MJChi/MJPeng/MJGang` 的 `from` 是**座位号**。`from == 自己` 表示「自己摸的牌
自己吃 / 偎」，那张牌**不消耗任何人的弃牌堆** —— 别拿它去剪弃牌。

自摸判据用 **`lastDrawPlayer == winner`**，别用 `mjdesc` 里有没有「自摸」——
那是名堂列表，放炮的局是空的。

**规则出处**（两套，交叉验证过）：
- 包内 `assets/res/localhelp/*.txt`（`docs/rules/`）
- 线上官方页 <https://op-static.duole.com/cms/phz-play-rules/explain> 与
  [`.../loudifangpaofa/`](https://op-static.duole.com/cms/phz-play-rules/explain/loudifangpaofa/)

---

## 已知未闭合

| 项 | 怎么办 |
|---|---|
| `round_kan` 守恒 56+5=61 | 它是**旧房间**（`tableid=812760`）的残留，live 与事件张数本身对不齐。试过「自摸局 cardNext 减 1」去凑，被 `round_v3` 打掉（它也是自摸但 cardNext 是准的）。**别再改公式去凑** |
| 无 live 快照的局张数不可信 | 明牌只能靠 `MJGang/MJPeng/MJChi` 事件拼，胡牌那一下会重发，张数偏大（实测明牌 20 张 / 17 张）。只有带 `_live` 的局才对张数下结论 |
| `huziType=2` 是哪个玩法 | ★ 已定案：**娄底放炮罚**。判据是 `limitScore=15`（官方线上页「胡息数≥15」），邵阳剥皮是 10 |
| 一块匾 / 海底胡 / 天胡 / 地胡 | 线上规则里有、`fan_win()` 还没实现（缺「红字门子数」和「第几手」信息）。遇到会判不一致，不会静默算错 |
| 「7 方门子（特殊 3 提 5 坎例外）」 | 四局最多 1 提，没触发过。攒到 3 提/5 坎的局再验 |
| `paoNum` 真实口径 | 只在 `endAll.jsc` 出现，是结算面板展示计数，不是牌型计数。不要拿它对账 |
| 财牌开启后的字段 | 本房间 `playType=111000110` 未开。`caiPaiArr` 是纯服务端字段（客户端 0 命中） |
| 手牌拆法不唯一 | `split_hand` 是贪心（坎→顺→绞→将），够对账用；要判定胡牌得穷举 |
