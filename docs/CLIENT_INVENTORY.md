# 客户端可获取信息清单（Dobby / LSPatch 实现规格）

> 目的：把「这个客户端到底能拿到什么」一次性钉死，作为原生钩子（LSPatch + Dobby）
> 的规格书。每一条都标了**在哪个对象上**、**什么时候有值**、**局中能不能拿**。
>
> 更新时间：2026-10-28 · 依据：实机雷电 9（Android 9 / x86_64 / houdini），
> 游戏 `com.happyplay.hnphz`，Cocos2d-JS + SpiderMonkey。

---

## 0. 一句话结论

**能拿到**：自己 20 张手牌、双方全部公开门子、双方全部弃牌、每张牌的先后顺序、
牌墩还剩几张、庄家是谁、当前该谁动、本人可做的操作、对手的在线状态与思考时长、
**每局的结算快照（双方终局手牌）**、**历史局回放（双方全程手牌）**。

**拿不到（局中）**：对手手牌、墩里具体牌值、开局抽走的 20 张。
这三样服务端**局中不下发**，只有结算/回放才给。

**尚未验证**：最底层原始收包（`pomelo.decode`）里有没有业务层吃掉、但业务层
没暴露的字段。这是唯一还没闭合的项，见 §6。

---

## 1. 数据分三层，Dobby 要分别对待

| 层 | 位置 | 内容 | 获取难度 |
|---|---|---|---|
| **L1 内存对象** | 游戏 JS VM 堆：`jsclient.data` | `sData`（牌局全量）、`pinfo`（账号）、`playLog`（战绩） | 需要**在 JS VM 里执行代码**，不能直接读指针 |
| **L2 网络包** | `pomelo.decode/notify/emit/request` | 服务端原始推送 | 同 L1，是 JS 函数，得在 VM 内挂钩 |
| **L3 场景树** | `cc.director.getRunningScene()` | 本人按钮可见性（吃/碰/胡/过） | 同 L1 |

★ **关键**：这三层全都在 JS 引擎里，**不是**普通 native 内存。
Dobby 直接 hook `libcocos2djs.so` 的 C++ 符号很难对上业务对象；
**正确做法是 hook Java 侧的 `Cocos2dxJavascriptJavaBridge.evalString(String)`**，
把 JS 注入进去执行 —— 这和现在 frida 用 `Java.use(...).evalString(js)` 是同一个入口。
**抓取逻辑一行都不用重写，只换注入通道。**

---

## 2. L1：`jsclient.data` 有哪些键（实机枚举）

```
result:Number          pinfo:Object        accessToken:String    gameid:String
gameInfo:Object        vipTable:Number     roleType:Number       time:Number
remoteCoinGameState    playLog:Object      sData:Object
```

- `gameInfo.phz` = 局数配置（`round1/round6/…/round10000` → 底分档），**规则常量**，一次性读走即可
- `pinfo` = 账号信息（uid / nickname / coin / 道具 / 绑定设备），与牌局无关
- `playLog.logs[]` = 战绩列表，每条只有：`tableid / logid / now / timeEnd / roomType /
  gameid / playType / money / owner / players[2]`；`players[i]` 只有
  `uid / winall / nickname / money / wininfo{头像,倍率}`。
  **列表层没有牌**，要牌必须进回放。

### 2.1 `sData` 只有三个键

```
sData = { players, tData, serverNow }
```

### 2.2 `sData.tData`（牌桌全局，80 个键，按用途分类）

| 类 | 字段 | 说明 |
|---|---|---|
| **身份** | `me`（在 `jsclient.data.pinfo`）、`uids[]`、`playerNum`、`initHandNum` | 下标 = 座位号 |
| **状态** | `tState`、`curPlayer`、`zhuang`、`winner`、`playNum`、`tableid` | `tState`：3 出牌 / 4 等人出牌 / 5 等响应 / 8 胡牌确认 / 6 结算 / -1 平局 |
| **牌** | `lastPut`、`lastPutPlayer`、`lastDrawPlayer`、`cardNext`、`choupai`、`leftCards`、`xingCard`、`caiPaiArr` | 见 §4 的可用性说明 |
| **规则** | `huziType`、`limitScore`、`playType`、`canEatHu`、`canEatMustHu`、`isEatHuForce`、`daNiao`、`daNiaoScore`、`addScore*`、`needAddCard`、`gangAllLie`、`showWei`、`canDiHu`、`qiduidaikan` | 决定「能不能胡」「要不要包赔」 |
| **流程** | `owner`、`roundNum`、`roundAll`、`roundtable`、`startTime`、`firstSend`、`sliceCard`、`delEnd`、`tableShareStatus` | 房间生命周期 |
| **无用** | `achInfo`、`shuffleProp`、`cpsround*` | 成就/洗牌道具 |

★ **`canEatMustHu = true`**（实测本房间）：能胡必须胡，没有「过」这个选项。
这条直接影响 AI 合法性，必须读。

### 2.3 `sData.players[uid]`：两家字段集**不对称**

| 两边都有（16） | 只有本人那份（13） |
|---|---|
| `info` `onLine` `delRoom` `isNew` `winall` `winningStreak` | `mjhand`（手牌，局中只有自己这份是真的） |
| `mjpeng` `mjpeng1` `mjgang0` `mjgang1` `mjchi` `mjlie` `mjput` | `mjState` `waitLiuType` |
| `mjputHand` `mjpengFrom` `mjptop` `mjwtop` | `mjpassChi` `mjpassPeng`（过张/过碰，只有自己有） |
| `pengScore` `piaoType` `piaoSet` `sishou` **`needOneCard`** | `cannotPut` `eatFlag` `originEatFlag` `kingHuFlag` |

字段含义（门子类）：

| 键 | 含义 | 数组口径 |
|---|---|---|
| `mjput` | 该家弃牌（有序） | 每元素 1 张 |
| `mjpeng` | 碰 | **每元素 = 一组 3 张** |
| `mjpeng1` | 偎（暗三张） | 每元素 = 一组 3 张 |
| `mjgang0` | 提 | 每元素 = 一组 4 张 |
| `mjgang1` | 跑 | 每元素 = 一组 4 张 |
| `mjchi` | 吃 / 绞 | **每 3 个元素 = 一组** |
| `mjlie` | 摆（比牌） | 每元素 1 张 |

★ 这张表的「数组口径」是最容易算错的地方：把 `mjpeng1:[109,107]` 当 2 张算会少 4 张，
整局守恒就对不上（看起来像采集漏牌）。

---

## 3. L2：网络包（`pomelo`）

```
pomelo = { init, decode, encode, disconnect, request, notify, _callbacks }
```

| 入口 | 什么时候触发 | 价值 |
|---|---|---|
| `decode` | **最底层解包**，解出来但没往上派发的字段也能看到 | 最全，但拿到的是原始结构 |
| `notify` | 服务端主动推送（**底牌走这里**，只钩 emit 会全漏） | 高 |
| `emit` | 客户端事件派发（`MJPut/MJPeng/MJChi/MJGang/roundEnd/...`） | 高 |
| `request` | 请求应答 | 中 |

另有绕开 pomelo 的直连回调 `jsclient.netCallBack.*`（`initSceneData` / `reInitSceneData` /
`addPlayer` / `removePlayer` / `updateInfo` / `MJChat` / `loadWxHead` …），实测约 30 个。
**发牌/开局数据有一部分走这条，不走 pomelo。**（本机 `netCallBack` 的函数不可枚举，
Dobby 侧建议按已知名字逐个 hook。）

事件负载结构：`{s:来源, e:事件名, t:时间戳, q:序号, d:{字段}}`。

---

## 4. 关键字段的「什么时候有值」

| 字段 | 局中 | 结算(tState=6) | 回放 | 备注 |
|---|---|---|---|---|
| **`players[我].mjhand`** | ✅ 真值 | ✅ | ✅ | 自己的手牌 |
| **`players[对手].mjhand`** | ❌ **空数组** | ✅ **填满** | ✅ **填满** | ★ 唯一能拿对手手牌的两种时机 |
| `leftCards`（墩里剩的牌） | ❌ `[]` | ✅ **全量**（实测 15 张 = 80−choupai−cardNext） | ✅ | 局中只知道**张数** |
| 抽走的 `choupai` 20 张 | ❌ | ❌ | ❌ | 只给张数，不给具体牌 |
| `xingCard`（醒牌） | ❌ 恒 −1 | — | — | 字段在，没值 |
| `caiPaiArr`（财牌） | ⚠️ `{uid: 0}` 字典，非数组 | 同 | 同 | 直接 `for` 会遍历出 uid |
| `visibleActions` | ✅（需扫场景树） | ❌ 隐藏 | ❌ | 本人能不能吃/碰/胡的唯一可靠证据 |
| 对手思考时长 | ✅（`newCard`→`MJPut` 时间差） | — | — | 只有时长，没有内容 |

### 4.1 对手手牌的三条可得性

1. **局中**：`players[对手].mjhand` 是**空数组**（不是没这个键）。
   服务端刻意不下发，**这是防作弊设计，不是采集问题**。
2. **结算瞬间**（`tState=6`）：两家 `mjhand` 都填满 —— 游戏在结算面板上要亮牌。
3. **回放**：`战绩 → 某局 → 回放`，进去后 `sData` 被灌成真实牌桌，
   **全程两家手牌都是真值**。这是唯一能拿到「逐步状态 + 对手手牌」配对的通路。

★ 边界：回放是**已结束的局**，只能用于事后校准，不能用于活局。

---

## 5. Dobby / LSPatch 实现建议

### 5.1 注入通道（推荐）

```
LSPatch 模块（Xposed API）
   └─ hook java: com.cocos2dx.lib.Cocos2dxJavascriptJavaBridge.evalString(String)
        └─ 在合适的时机（Activity onResume 后 / 定时器）把自己的 JS 灌进去
             └─ JS 在游戏 VM 里读 jsclient.data.sData、写文件
                  └─ 模块（或 adb）读文件 → 业务侧解析
```

**为什么不建议直接 hook native**：

- 业务对象在 SpiderMonkey 堆里，Dobby hook `libcocos2djs.so` 的 C++ 函数只能拿到
  引擎内部结构（`JSContext*` / `JS::Value`），要自己解 sm 的私有布局，工作量大且随版本变。
- `evalString` 是**官方预留的 JS 入口**，签名稳定（`(Ljava/lang/String;)Z`），
  只要拿到字符串就能在 VM 里执行任意 JS —— 相当于「游戏自己给你的脚本通道」。

### 5.2 三个必须遵守的约束（都是踩出来的）

1. **`evalString` 只返回 `bool`**，拿不到返回值。JS 必须自己
   `jsb.fileUtils.writeStringToFile()` 落盘。
2. **落盘文件会被内容去重**：状态不变就不写 → 文件 mtime 冻住。
   **「文件没变」不等于「游戏没动」**，判断逻辑别靠 mtime。
3. **不要高频 evalString**：它跑在 GL 线程（主线程）上，250ms 一次会把游戏打到 ANR。
   正确做法是**注入一次常驻脚本**，之后由游戏自己按事件 + 定时器落盘，外侧只读文件。

### 5.3 建议的产出结构

一个 `pct_state.json` 就够，字段按 §2.2 / §2.3 全量落：
`tData` 选读 + `seats[]`（含 `mjhand` / `mjput` / 六个门子数组 / `handN` / `onLine` /
`needOneCard` / `cannotPut` / `mjpassChi`）+ 事件队列（白名单见 `pct/gs_install.js`）。

---

## 6. 尚未验证的一项（唯一）

**`pomelo.decode` 的原始载荷里，有没有业务层没暴露的字段。**

- 已经做的：业务层全量枚举（§2）；事件白名单 19 个；非白名单事件只记第一次出现。
- 还没做的：把 `decode` 收到的**每个包**的 route + 顶层键名都记下来，
  和业务层字段做差集。
- 工具已就绪：`python tools/raw_audit.py --sec 600`
  （在游戏 VM 里挂 `pomelo.decode/notify/emit/request`，只记 route + 键名，不落牌值）。
- **实测结果（2026-10-28 11:17，回放期间）**：钩子确认生效，但只录到 **12 条出向 `request`**
  （`pkcon.handler.ProxyAccess` ×9、`pkplayer.handler.pushRabbitMqData` ×3），
  **入向 `decode`/`notify`/`emit` 一条都没有**。

  结论：**回放是本地播放，不走网络**。35~40 帧的状态变化全部来自内存里的记录，
  pomelo 全程没参与。

- 所以这一项**只能靠一局真牌验**（需要金币）。在它之前，能确认的边界是：
  > **业务层能拿的都拿完了；网络层有没有多出来的字段，未知。**

- 另外实测到一条运维事实：**采集器活不过进程重启**。游戏切房间/闪退/被回收后
  （实测 pid 3933 → 32334 → 13137），钩子全丢，`pct_state.json` 冻在最后一帧
  （冻了 16 分钟才发现）。LSPatch 模块同样要跟着 Activity 生命周期自愈。
