# WEEX 虚拟盘实测档案

记录在 **WEEX 虚拟盘**（`WEEX_VIRTUAL_ONLY=true`）上实测得到的接口行为。

这份文档存在的理由：项目的 `CLAUDE.md` 与代码注释里有多条关于虚拟盘的**错误结论**
（最典型的是「虚拟盘无衍生品数据」），而那些结论会持续误导实现。这里只记录**实测过**的
事实，并标注复验方法。**任何结论都要能给出复验步骤，做不到的就别写进来。**

- 实测时间：2026-09-11
- 账号：虚拟盘（`SUSDT` 结算），符号对外用 `BTCSUSDT`，对内统一 `BTC-USDT`
- 基址：`https://api-contract.weex.com`

> 记一条方法论：`curl` 探测端点时，**404 表示路径不存在，400/401/405 表示路径存在**
> （缺参数 / 缺鉴权 / 方法不对）。本项目当初「虚拟盘无衍生品数据」的错误结论，就是因为
> 只看文档没做这个区分。

## 1. 公开行情端点

全部**无需鉴权**。`symbol` 必须是不带横杠的 `BTCUSDT`；写 `BTC-USDT` 一律返回
`{"code":-1142,"msg":"Parameter 'symbol' is invalid."}`。

| 端点 | 状态 | 关键参数 / 返回 |
|---|---|---|
| `GET /capi/v3/market/klines` | 已用 | 单请求上限 **1000 根**。1h ≈ 41 天，4h ≈ 166 天，5m ≈ 3.5 天 |
| `GET /capi/v3/market/ticker/24hr` | 已用 | 见下方字段清单 |
| `GET /capi/v3/market/depth` | **可用** | 只接受 `symbol` 一个参数；传 `limit` 会报 `-1142`。返回 15 档买卖盘 |
| `GET /capi/v3/market/trades` | **可用** | 逐笔成交，每笔带 `isBuyerMaker`（可算主动买/卖量） |
| `GET /capi/v3/market/fundingRate` | **可用** | 8 小时一期，返回数组，带 `fundingTime` 与 `markPrice` |
| `GET /capi/v3/market/openInterest` | **可用** | 当前值 + 毫秒时间戳 |
| `GET /capi/v3/market/exchangeInfo` | 已用 | 精度、最小下单量、杠杆区间 |
| `GET /capi/v3/market/markPrice` | 不存在 | 404。但 `markPrice` / `indexPrice` **在 ticker 里就有** |
| `GET /capi/v3/market/indexPrice` | 不存在 | 404 |
| `GET /capi/v3/market/longShortRatio` | 不存在 | 404 |
| `GET /capi/v3/market/aggTrades` | 不存在 | 404 |

### 1.1 `klines` 的两个坑

- **上限 1000 根，且 `startTime` / `endTime` 被静默忽略** —— 传了时间窗仍返回最近 N 根，
  不会报错、也不会分页。所以历史深度就是 1000 根封顶，拿不到更长。
- 采集器原先每轮只抓 100 根且不累积，导致库里 1h 只有 102 根（4.25 天）。评估策略时
  先确认 `market_candles` 的实际深度，别被「抓了 100 根」误导成「有 100 根历史」。

### 1.2 `ticker/24hr` 的完整字段

实测返回：

```json
{"symbol":"BTCUSDT","priceChange":"-987.5","priceChangePercent":"-0.012630",
 "lastPrice":"77193.9","openPrice":"78181.4","highPrice":"78496.1","lowPrice":"76414.5",
 "volume":"23544.2086","quoteVolume":"1817917050.88401",
 "openTime":1789021800000,"closeTime":1789108200000,
 "markPrice":"77199.2","indexPrice":"77237.45275"}
```

代码目前只读了 `lastPrice` 和 `volume`，**其余 7 个字段全被丢弃**。其中：

- `openPrice` / `highPrice` / `lowPrice` / `priceChangePercent` / `quoteVolume` 可做日内区间位置
- `markPrice` vs `indexPrice` 的基差
- **`closeTime` 是 24h 滚动窗口的边界，不是报价时刻**，实测落后约 750 秒。拿它当
  `captured_at` 会让 `MARKET_DATA_MAX_AGE_SECONDS=90` 的新鲜度门永远失败、一单不下。
  `captured_at` 必须取本地观测时刻。

## 2. 私有端点

只有 4 个：`GET balance`、`GET position/allPosition`、`POST order`、`GET order/history`。

- `GET /capi/v3/sim/order` → **405**（路径存在但只支持 POST），无单笔查单
- `GET /capi/v3/sim/userTrades` → **404**，无成交流水
- **`cancelOrder` / `order/cancel` / `openOrders` / `orders` 全部 404** ——
  **没有撤单、没有改单、没有挂单列表**
- 下单响应只有 `orderId` / `clientOrderId` / `success`，**没有 status**；市价单 T+0 即可
  回查 `order/history` 拿到终态，单次回查足够
- `order/history` 只含**终态**订单（FILLED / CANCELED）。**挂单中的订单（含止损触发单）
  查不到**，持仓记录的 `raw` 里也没有止损字段

401 错误码可区分根因：`-1044` key 无效、`-1049` passphrase 不匹配、`-1047` secret 不匹配。

## 3. 止损触发单的生命周期

这是「交易所侧挂灾难止损 + 软件层按需平仓」这个方案能否成立的前提，**实测过两次**。

### 实验 A：触发单是否真的生效

持 **SHORT** 仓位，`slTriggerPrice` 挂在市价**上方** 0.01%（做空止损天然在上方）。

```
last=77274.8  止损=77282.5（上方 +7.7）
开空: FILLED
[  0s] price=77279.7  仓位仍在，越过价差 -2.8
[ 10s] price=77290.5  >>> 仓位已被平掉
```

**结论：`slTriggerPrice` 真的生效** —— 价格越过触发价后 10 秒内仓位被自动平掉。

### 实验 B：平仓后触发单是否残留

开 SHORT 带止损，**立刻手动平仓**，然后等价格越过原触发价。

```
last=77300.1  止损=77307.8（上方 +7.7）
开空: FILLED
立刻平仓: FILLED
[  0s] price=77299.9  positions=[]
[ 60s] price=77323.9  已越过触发价
[120s] price=77345.0
[180s] price=77355.3  越过触发价 +47.5，positions=[]，无任何新订单记录
```

**结论：平仓时交易所把关联的触发单一起撤销了。** 没有反手开仓，也没有残留的隐形挂单。

### 实验 C：触发价方向由交易所校验

给 **LONG** 挂高于市价的 `slTriggerPrice` → **500**，且响应里没有可读的错误信息
（与精度校验同一种失败形态）。见 §4。

### 对实现的三条约束

1. **可以在入场单上直接带 `slTriggerPrice` 作为灾难止损** —— 它真生效，且平仓后不留残渣。
2. **挂上去就改不了、撤不掉**（无 cancel 端点）。所以「移动止损 / 保本」不能靠改交易所的
   触发单实现，只能是**软件层本地记账 + 按需下 reduceOnly 市价单**。
3. **触发单是否还活着，无法通过任何端点观测** —— 只能靠行为验证（让价格触碰）。排查时
   别浪费时间找 open-orders 接口，不存在。

## 4. 精度与校验失败形态

**违反校验一律回 500，没有可读错误信息。** 已知会触发的情形：

| 情形 | 结果 |
|---|---|
| 数量未按 `quantityPrecision` 舍入（BTC-USDT 为 4） | 500 |
| 触发价未按 `pricePrecision` 舍入（BTC-USDT 为 1） | 500 |
| LONG 的 `slTriggerPrice` 高于市价 | 500 |

所以 `slTriggerPrice` / `tpTriggerPrice` **也要做精度舍入**，别只舍入 quantity。

## 5. 成本与账户语义

- **taker 手续费 0.08%/边**，往返约 **0.16%**。实测 `openFee=0.06181600` /
  `openValue=77.27000`。评估策略时这是必须计入的成本，忽略会显著高估收益。
- **balance 不含未实现盈亏**：钱包余额只随手续费与已实现盈亏变动。所以
  `equity = balance + unrealized_pnl`，已实现盈亏 = 相邻快照的 Δbalance 累加。
- **固定 20 倍杠杆，无 UpdateLeverage 接口，调不了**。`max_leverage` 必须与之一致，
  否则一开仓就再也无法加仓。
- 保证金模式实测 `marginType: CROSSED`。

## 6. 数据可信度：⚠️ 疑似合成

虚拟盘的盘口与衍生品数据**很可能是合成出来的**，用于策略信号时必须打折：

- `depth` 价差只有 0.1 USDT ≈ **0.00013%** —— 真实市场不可能这么紧
- `openInterest` 数值偏大，且 `markPrice` 与 `lastPrice` 的基差偏宽（0.85% 量级）
- `trades` 成交流水看起来过于规整

**结论：这些字段只能当辅助确认项，不单独构成入场理由。** 第一版实现只记录不使用，
等前向数据证明它们与收益真的相关再决定要不要纳入打分。

## 7. 复验方法

上面的结论都可复现。复验时需要：

1. `WEEX_VIRTUAL_ONLY=true` 且账号是虚拟盘 —— 动手前先断言，别在真实账户上试
2. 用 `quantityPrecision` 允许的**最小量级**（BTC-USDT 可下 0.0001，约 8 USDT 名义）：
   20x 下保证金不到 0.4 USDT，单次实验成本以分计
3. 用 `TradingCycleService._exchange_for_user(user_id)` 造客户端，避免手工处理凭据
4. 每一步之后同时检查 `get_positions()` 与 `_order_history()`；**末尾无条件清仓**，
   并确认 `available == balance`（`frozen` 为 0）
5. 价格是不受控的。想验证「触发后会发生什么」时，注意 **做多止损只能挂在下方、
   做空止损只能挂在上方**（§3 实验 C），选对方向才能等到触发

2026-09-11 这一轮实测的全部成本：**0.85 USDT**（纯手续费）。

## 8. 与现有文档的出入

以下说法在别处出现过，**已被实测证伪**，读到时应以本文档为准：

| 出处 | 原说法 | 实测 |
|---|---|---|
| `CLAUDE.md` | 「虚拟盘无衍生品数据」 | 资金费率、持仓量、盘口、成交流水**都能拿到**（§1） |
| `CLAUDE.md` | 「只有 4 个端点」 | 指的是**私有**端点；公开行情端点有 6 个可用（§1） |
| `app/exchange/weex.py` `get_market_snapshot` | 只读 lastPrice / volume | ticker 还有 7 个字段可用（§1.2） |
