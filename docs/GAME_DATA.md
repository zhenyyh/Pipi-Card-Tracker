# 从游戏能拿到什么数据（完整版）

按「采集 → 事件 → 推导 → 不可得」四层整理。所有字段均来自实际代码，
不是推测。对应实现：`pct/gs_install.js`（采集）、`pct/tracker.py`（推导）。

数据通路：`jsclient.data.sData`（内存快照）+ `pomelo`（网络事件）
→ frida 注入读内存 → 写 `pct_state.json` → tracker 解析。

---

## 第一层：内存快照（直接读到的确定值）

来源：`jsclient.data.sData.tData`，由 `snapshot()` 每 600ms 或事件触发时采样。

### 1.1 牌桌全局

| 字段 | 含义 | 实测说明 |
|---|---|---|
| `me` | 我的 uid | 来自 `jsclient.data.pinfo.uid` |
| `tState` | 牌局状态 | **3=出牌 4=等人出牌 5=等响应（可吃碰胡）8=胡牌确认 6=结算 -1=平局** |
| `tableid` | 牌桌 id | 换桌必变 |
| `playNum` | 第几局 | 局号，用于作废旧 AI 结果 |
| `uids` | 座位 uid 数组 | 2 人局，下标 = 座位号 |
| `curPlayer` | 当前该谁出牌 | 座位号 |
| `zhuang` | 庄家座位号 | **判断「对家是否庄」的唯一依据** |
| `lastPut` | 最后打出的牌 | 牌值编码见下 |
| `lastPutPlayer` | 最后出牌的人 | ⚠️ 不可靠，对手出牌后有时仍显示为我 |
| `lastDrawPlayer` | 最后摸牌的人 | — |
| `cardNext` | 发牌指针（已摸走张数） | 与 `choupai` 一起算剩余 |
| `leftCards` | 剩牌 | ⚠️ **局中恒为空数组，只在结算时给** |
| `xingCard` | 醒牌 | ⚠️ **实测恒为 -1**，不可用 |
| `caiPaiArr` | 财牌数组 | 底牌兜底来源之一 |
| `sliceCard` | 是否切牌 | 布尔 |
| `firstSend` | 是否首发 | 布尔 |
| `winner` | 赢家 | -1 = 平局/未定 |
| `choupai` | 抽走张数 | 固定 20 |
| `initHandNum` | 初始手牌数 | 20（庄 21） |
| `playerNum` | 人数 | 2 |
| `huziType` | 胡牌类型规则 | 进 `roomRules` |
| `limitScore` | 封顶分 | 进 `roomRules` |
| `playType` | 玩法类型 | 进 `roomRules` |
| `visibleActions` | 本人可见操作按钮 | **由场景树扫描得出，见 1.3** |

牌值编码：小字 `1~10`，大字 `101~110`（`101`=大壹 … `110`=大拾）。

### 1.2 每个座位（`sData.players[uid]`）

| 字段 | 原始键 | 含义 |
|---|---|---|
| `put` | `mjput` | 该家打出的牌（有序） |
| `peng` | `mjpeng` | 碰（3 张一组） |
| `peng1` | `mjpeng1` | 偎（暗 3 张） |
| `gang0` | `mjgang0` | 提（明 4 张） |
| `gang1` | `mjgang1` | 跑（4 张） |
| `chi` | `mjchi` | 吃 / 绞（3 张一组，靠 `rules.classify` 区分） |
| `lie` | `mjlie` | 摆（单张亮出） |
| `hand` | `mjhand` | 手牌 —— **只有自己这一份是真的** |
| `handN` | — | 手牌张数 |
| `state` | `mjState` | 玩家状态 |
| `handKnown` | — | 该家 `mjhand` 是否真的是数组（区分「空手牌」与「没这个字段」） |
| `putKnown` | — | 同上，针对 `mjput` |
| `knownMeldFields` | — | 六个门子字段各自是否存在的细粒度标记 |
| `meldsKnown` | — | 六个门子字段是否全部完整 |

> `*Known` 系列是刻意的防御设计：**空数组 ≠ 字段不存在**。拿不到时宁可返回
> `None` 让上层降级，也不把「没读到」当成「没有」。

### 1.3 操作按钮可见性（场景树扫描）

`actionPanel()` 遍历 Cocos 场景树（上限 2500 节点，缓存 1 秒），
找名为 `eat` 的控制节点，检查其中的 `guo / hu / peng / chi0` 按钮
**及其所有祖先是否可见**，返回可见按钮名单。

这是「我现在能不能碰/吃/胡」的**唯一可靠证据**——比 `tState` 和
`lastPutPlayer` 都准（实测 `tState=4` 时按钮也会弹出）。

---

## 第二层：网络事件流

### 2.1 四个钩子入口

| 入口 | 说明 |
|---|---|
| `pomelo.emit` | 客户端事件派发 |
| `pomelo.notify` | 服务端主动推送（**底牌走这里**，只钩 emit 会全漏） |
| `pomelo.request` | 请求应答 |
| `pomelo.decode` | **最底层收包**，能看到「解出来但没往上派发」的字段 |
| `jsclient.netCallBack.*` | 游戏直接回调、绕过 pomelo 的路径（来源标 `s:'cb'`） |

### 2.2 监听的事件白名单（19 个）

```
MJPut  MJPeng  MJChi  MJGang  MJHu  MJPass  MJLao  MJWai
roundEnd  MJBeginHu  MJZhongJiang
mjhand  MJShow  MJShowPL  MJBegin  MJXing
MJBuxiaCard  MJYu  MJDi
```

非白名单事件**只记第一次出现**（用于发现新字段），不会刷爆队列。

### 2.3 事件记录结构

```json
{"s": "emit|notify|request|cb", "e": "事件名", "t": 时间戳ms, "q": 自增序号, "d": {字段}}
```

- `d` 内保留标量（字符串截断 80 字）和数组（最多 30 元素）
- 队列上限 **240 条**，超出丢弃最旧的
- `q` 是全局自增序号，用于去重（同一事件多路径到达时）

---

## 第三层：状态机推导（加工后的结论）

`pct/tracker.py` 把上面两层喂成可直接用的结论。

### 3.1 记牌器展示用的

| 数据 | 规则 |
|---|---|
| **底牌** `bottom` | 庄家第 21 张。**只在「对家是庄」时记**。优先级：开局第一条 `newCard` 的 `lastPut`（最准）→ 快照 `hand[20]` → `xingCard` → `caiPaiArr` |
| **首出** `first_put` | 对家为庄时，庄家打出的第一张 |
| **门子** `melds` | 每方含 `kind`（吃/碰/偎/提/跑/绞/摆）+ `cards` + `owner`（me/opp）+ `after`（该门子成型后打出的第一张）+ 时间戳 |
| **弃牌** `puts` | 双方打出的牌序列 |
| **回合流水** `timeline` | `{n: 序号, who: me/opp, act: put/peng_or_wei/chi_or_jiao/gang, card: 牌值}` |

门子**不合并**：手里 3 张又来同值要单独占一行（保留「中间那次成型」）。
最多显示 8 行（`MAX_ROWS`）。

### 3.2 给 AI 的派生量

| 字段 | 含义 |
|---|---|
| `deckLeft` | `max(0, 80 − choupai − cardNext)`，牌墩还剩几张 |
| `mySeat` | 我的座位号 |
| `responsePlayer` | 按钮可见即视为「该我响应」，否则 `None` |
| `availableActions` | 从可见按钮映射为 `peng / hu / pass / eat` |
| `roomRules` | `huziType / limitScore / playType` |

### 3.3 `analysis`（替模型算好，避免它自己数错）

| 字段 | 含义 |
|---|---|
| `meldCount` | 已成型门子数 |
| `needMelds` | `max(0, 7 − meldCount)`，还差几方到胡 |
| `pairs` | 对子（恰好 2 张的牌值列表） |
| `triples` | 坎（≥3 张的牌值列表） |
| `possibleRuns` | 手里已能凑成的一句话（一二三 / 二七十，大小字各一套） |
| `singleCount` | 孤张数量 |
| `seen` | 已露面的牌各几张，**上限夹到 4**（数据冲突保护） |

`seen` 的统计口径 = 我的手牌 + 双方门子 + 双方弃牌。
对手暗牌、墩里剩牌、抽走的 20 张都不在内，所以它是「**已确认已现**」，
不是「全部去向」。

---

## 第四层：拿不到的（已穷举验证）

| 拿不到的东西 | 原因 |
|---|---|
| **对手手牌** | 服务端**根本不下发**。全局 22 条 `MJPut.mjhand` 事件里 uid 全是自己（防作弊设计）。曾穷举 20 个数据角度确认。 |
| **墩里具体牌值** | `leftCards` 局中恒为空数组，只在结算给一次。 |
| **抽走的 20 张** | `choupai=20` 只给了张数，没给具体牌。 |
| **对手暗门子里的牌** | 偎（`mjpeng1`）、提可能含暗牌；所以只向 AI 发公开的碰/跑/吃/摆。 |
| **对手的思考/犹豫** | 没有可读信号，代码里明确禁止从出牌历史反推其手牌。 |
| **`xingCard`（醒牌）** | 实测恒 -1，字段在但没值。 |

> 「反推对手手牌」这条路走过：因为 `choupai=20` 抽走使总数基数不成立，
> 算不回去。已放弃。

---

## 一句话总结

**能确定看到**：自己 20 张手牌、双方所有公开门子、双方所有弃牌、
每张牌的先后顺序、牌墩还剩几张、庄家是谁、当前该谁动、我现在能做什么操作。

**确定看不到**：对手手里的牌、墩里躺着的牌、抽走的 20 张。
这三样是服务端刻意不给的，任何「推测」都只会污染判断。

**唯一能确定的对手信息 = 他打过什么 + 他亮出过什么门子。**
