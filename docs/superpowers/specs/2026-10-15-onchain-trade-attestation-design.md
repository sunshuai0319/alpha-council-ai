# 手动交易与盈亏的链上存证设计

- 日期：2026-10-15
- 状态：**采集层（§12）已实现并合并进 main**；链上部分（§5–§7）待实现
- 技术栈：WEEX 私有 WebSocket + IPFS（Pinata）+ Base + The Graph + 双层 Merkle Tree

## 1. 背景与目标

系统在 `notify` 模式下通过 Lark 推送交易信号，用户在 WEEX 虚拟盘上**手动**决定是否开仓、开多少。
这些手动交易原先完全没有留痕 —— worker 只在有系统执行时才拉 `order/history`
（`app/services/cycle.py:410`），通知模式下这条路径永不触发。

本设计把「信号 → 用户手动执行 → 实际盈亏」这条链路做成**可被第三方独立验证**的链上存证。

可行性建立在 2026-10-15 的实测之上（见 §15）：**虚拟盘私有 WebSocket 可用，单笔盈亏与
手续费都是交易所回报值**。这个发现推翻了早期版本基于 REST 的整套推导模型。

**当前进度**：数据源那一半已经落地并合并进 `main`（`app/streams/`，见 §12）—— 用户手动
交易的成交、委托与账户资金变动已经实时入库。**链上那一半（Merkle 批次、IPFS、合约、
subgraph）尚未开始**，本 spec 剩下部分描述的就是它。

### 1.1 目标拆成两个命题

| | 命题 | 可达性 |
|---|---|---|
| **P1** | 用户参考系统推送后，确实实现了盈利 | 可证明 |
| **P2** | 系统大部分给的参考信息是正确的 | 可证明，但需额外机制（见 §4.3） |

**P2 单靠「记录跟随后的盈亏」证明不了**，存在三个漏洞：

1. **选择性披露** —— 只把跟随了且盈利的交易上链
2. **覆盖率未知** —— 第三方看不到「一共推了多少次、用户跟了几次」
3. **生存者偏差** —— 用户只挑看起来会赢的信号跟

堵住这三个漏洞的基石是：**信号承诺必须发生在推送时刻，此时结果未知，运营方没有机会事后挑选。**

### 1.2 非目标

- 不做链上交易执行、不做资产托管、不做收益承诺
- 不发行 token
- 不把链上组件接入交易路径（旁路服务，见 §9）

## 2. 数据可得性（2026-10-15 实测确认）

### 2.1 结论先行

**虚拟盘私有 WebSocket 可用，且推送的字段比 REST 完整得多。** 单笔盈亏是交易所回报值，
不是推导值。这一条推翻了本设计早期版本的整个推导模型。

### 2.2 私有 WebSocket 实测

端点 `wss://ws-contract.weex.com/v3/ws/private`，虚拟盘凭证被接受。四个私有频道全部可用：

| 频道 | 推送内容 | v1 是否订阅 |
|---|---|---|
| `fill` | 成交明细：`realizePnl` / `fillFee` / `fillSize` / `fillValue` / `direction` | ✅ |
| `orders` | 委托生命周期：`triggerPrice` / `orderSource` / `positionTpsl` / `status` / `takerFeeRate` | ✅ |
| `account` | 账户资金：`amount` / `cumDepositAmount` / `cumTransferInAmount` / `liquidating` | ✅ |
| `positions` | 持仓变动：`cumOpenFee` / `cumCloseFee` / `cumFundingFee` / `cumLiquidateFee` | ❌ |

**`positions` 不订阅的理由**：它的 `symbol` 是**内部合约 ID**（实测 `20000067`）而不是交易对，
订阅它就得额外维护一份 ID → 交易对映射；而它给的累计费用在 `account` 频道里有聚合版本，
够用。（早期版本的第二个理由是「`orders` / `fill` 会给 symbol，可以从那里反查」——
**这条后来被实测推翻了**，见下。）

签名与 REST **不同**：WS 是 `base64(hmac_sha256(secret, timestamp + requestPath))`，
`requestPath = /v3/ws/private`，不含 method / query / body。复用 `WeexClient.signature`
会鉴权失败。探测实现见 `backend/scripts/probe_private_ws.py`。

### 2.2.1 ⚠️ 推送里没有 `symbol`

**WEEX 文档在 `Order-Channel` 与 `Fill-Channel` 里都列了 `symbol` 字段
（「交易对（大写，如 BTCUSDT）」），但虚拟盘不推。**

实测的行只有 `coin: "SUSDT"` 与 `orderId` / `id` —— `fill` 与 `orders` **两个频道都没有**。
这个发现否定了「从 orders 反查 symbol」的设计，`exchange_fills.symbol` 只能靠
REST `order/history` 按 orderId 回填（见 §12.2）。

**对链上设计的直接影响**：品种这个字段在成交落库的**那一刻是未知的**，要等一轮对账
（默认 5 分钟）才补上。生成叶子时必须处理 `symbol IS NULL` 的情况 ——
要么等到对账补上再封批，要么在叶子 payload 里记 `symbol: null` 并保留 orderId 供复算。
**采用前者**：批次对 30 分钟延迟本来就有容忍度，没必要把不确定性写进链上。

### 2.3 实测证据（两次完整往返）

**手动市价平仓**（12:51:46 开 → 12:52:14 平）：

```
[fill] SHORT/BUY size=100 value=8.02100 fee=0.00641680 realizePnl=-0.00300000 dir=TAKER
```
`8.01800 − 8.02100 = −0.00300` 与 `realizePnl` 逐位吻合。

**条件单触发平仓**（12:56:11 开 → 12:58:06 止盈触发）：

```
[orders] STOP_MARKET        UNTRIGGERED  source=STOP_LOSS    triggerPrice=0.08004  positionTpsl=True
[orders] TAKE_PROFIT_MARKET UNTRIGGERED  source=STOP_PROFIT  triggerPrice=0.07990  positionTpsl=True
[orders] TAKE_PROFIT_MARKET PENDING → FILLED   cumRealizePnl=0.01000000
[fill]   SHORT/BUY size=100 value=7.99000 fee=0.00639200 realizePnl=0.01000000 dir=TAKER
[orders] STOP_MARKET CANCELED  source=STOP_LOSS      ← 另一侧条件单自动撤销
```
`8.00000 − 7.99000 = 0.01000000` 与 `realizePnl` 逐位吻合。

### 2.4 两条必须记住的语义

1. **`realizePnl` 不含手续费**。上例平仓 `realizePnl=0.010`，`fillFee=0.006392`，
   净额是 `0.010 − (0.006400 + 0.006392) = −0.002792`。**直接把 realizePnl 当净盈亏上链是错的。**
   两次实验都是「方向做对了但被 0.16% 往返费用吃掉」。
2. **`orderSource` 枚举不统一**：手动开仓是 `WEB`，手动平仓是 `UNKNOWN_ORDER_SOURCE`，
   条件单是 `STOP_LOSS` / `STOP_PROFIT`。判断手动/系统必须同时看 `orderSource` 与
   `clientOrderId` 前缀（系统单是 `alpha-<28位hex>`）。

### 2.5 REST `order/history` 的实测局限（作为兜底通道仍要知道）

- 字段只有 18 个，**没有任何盈亏/费用字段**
- **`page` 参数被忽略**，`limit=100` 是硬上限且无法翻页回溯
- **条件单完全不可见**（挂单中查不到，触发成交后也不留记录）
- `stopPrice` 恒为 0

所以 REST 只适合做「WS 断线期间的兜底」，且必须检测观测断档（见 §12.2）。

### 2.6 已知盲区（采集层实现后仍然存在）

| 事件 | 状态 |
|---|---|
| WS 断线期间发生的成交 | ⚠️ REST 只能补 `symbol`，**盈亏补不回来**（REST 没有 realizePnl）。已实现 `observation_gap` 检测，见 §12.2 |
| 推送里没有 `symbol`（两个频道都没有） | ⚠️ 靠 REST 回填，刚成交那一刻为 NULL，见 §2.2.1 |
| 爆仓 | ❓ 未验证；`account.liquidating` 字段存在，推测有推送 |



## 3. 关键决策（已确认）

| 决策点 | 选择 |
|---|---|
| 首要目的 | 用户战绩自证（用户能向第三方证明交易与盈亏未被篡改） |
| 归属模型 | 先锚定、可补签（运营方先上链，用户可事后钱包签名背书） |
| 批次粒度 | 弹性批次：满 64 条 或 距上次封批 ≥ 30 分钟 |
| IPFS 隐私 | 默认加密（用户钱包公钥），可选择性公开解密密钥 |
| The Graph 定位 | 公开可验证索引（任何人有地址就能查，不依赖运营方服务器） |
| 目标链 | Base |
| 信号承诺 | 进第一版（P2 的前提） |
| 未跟随信号 | 上链，链上全量 + 展示克制 |

## 4. 上链数据模型

### 4.1 五类叶子

所有叶子追加式，永不修改。

**叶子哈希的定义**（这一条决定了加密与哈希的解耦）：

```
leaf_hash = keccak256(canonical_json(payload_plaintext))
```

哈希基于**明文 payload 的规范化 JSON**（键排序、Decimal 转字符串、无空白），
**与加密无关**。加密只影响 IPFS 上的存储形式。好处：用户可以随时更换加密密钥、
或从「运营方托管密钥」升级为「自己钱包加密」而**不改变叶子哈希**，链上锚定不变。

**① 信号承诺**（Lark 推送时刻入队，结果未知时就上链）

```
signal_id, symbol, action, entry_price, stop_loss, take_profit,
valid_until, decided_at, model_version, reasoning_hash
```

`entry_price` 是信号建议的入场价，`valid_until` 是信号有效期 —— 两者是 ③ 模拟结算
与 §4.3 归因窗口判定的输入。

**② 实际结果**（用户平仓后入队）

全部字段来自 WS 推送，**除标注外都是交易所回报值，不是推导值**：

```
signal_hash              # 引用①，建立因果；用户自发交易时为 null
followed                 # true / false
attribution              # "matched" | "ambiguous" | "none"

symbol, side, qty
open_avg, close_avg      # 由成交额 / 数量得到（表里没有均价列）
open_at, close_at        # exchange_fills.filled_at，毫秒级精确
open_fill_id, close_fill_id   # 交易所成交 ID，可回查
order_source             # "WEB" | "UNKNOWN_ORDER_SOURCE" | "STOP_LOSS" | "STOP_PROFIT" | ...
client_order_id          # 系统单为 alpha-<hex>，手动单为 UUID
exit_reason              # "manual" | "take_profit" | "stop_loss" | "liquidation" | "unknown"
trigger_price            # 条件单触发价；手动平仓时为 null

fee                      # 双边 fillFee 之和（交易所回报）
realize_pnl              # 平仓腿的 realizePnl（交易所回报，不含手续费）
pnl_net                  # realize_pnl − fee
fee_rate_taker, fee_rate_maker   # orders.takerFeeRate / makerFeeRate
pnl_source               # "exchange-reported"
```

**`pnl_source` 恒为 `exchange-reported`** —— 实测确认虚拟盘 WS 直接给
`realizePnl` 与 `fillFee`，无需任何推导。这是本设计与早期版本最大的区别。

**两条实现时必须记住的**（都来自实测，见 §15.5）：

1. **开仓腿的 `realizePnl` 恒为 0**，盈亏全在平仓腿。所以 `realize_pnl` 取的是
   **平仓腿**的值，而 `fee` 是**两腿之和** —— 单条 fill 的 `realizePnl − fee`
   只是半程。
2. **`symbol` 在成交落库时可能是 NULL**（推送里根本没有，见 §2.2.1），
   生成叶子前必须先跑过对账。字段级来源见 §16.1。

**③ 信号结算**（信号有效期结束后入队）

```
signal_hash
simulated_outcome        # "win" | "loss" | "timeout"
simulation_rule          # "first-touch-v1"
resolved_at
price_path_cid           # 用到的 klines 区间引用
```

模拟规则：用信号自身的 `entry / stop_loss / take_profit` 与 `valid_until` 窗口内的
**WEEX 公开 klines**，判定先触 TP 还是先触 SL。规则固定、版本化、无自由裁量空间，
第三方可用公开行情端点独立复算。

**④ 修正**（发现算错时追加，见 §8）

```
supersedes_leaf_hash
reason_code              # "source-change" | "attribution-fix" | "missing-window"
corrected_payload_cid
```

**⑤ 净值曲线**（每个采样窗口入队，见 §12.3）

```
captured_at, equity, balance, unrealized_pnl
cum_deposit, cum_withdraw, cum_transfer_in    # 来自 account 频道，用于剔除资金进出
cum_fill_fee, cum_funding_fee                 # 费用累计，用于解释净值变化
account_version                               # WS 的 v 字段
```

这是 P1 的主力证据：**不管用户用哪种方式平仓，净值变化都会反映在里面**，
且 `cum_deposit` / `cum_withdraw` 让第三方能排除「靠充值做出来的净值增长」。

### 4.2 叶子必须携带证据的理由

如果 payload 里只有 `pnl: +12.3`，链上只证明「这个数字没被改过」，**不证明它是真的**，
与「证明系统有效」直接冲突。最小证据集：

| 字段 | 不可省的理由 |
|---|---|
| symbol / side / qty | 没有它算不出盈亏 |
| open_avg / close_avg | 同上 |
| open_at / close_at | 同上，且证明「信号在前、结果在后」 |
| fee | 往返 0.16%，实测两次都是「方向对了但被费用吃掉」 |
| signal_hash | 因果链 |
| open_fill_id / close_fill_id | 让第三方能回到交易所侧核对成交 |

**开仓与平仓不单独成叶子**，合并为一笔闭合交易 —— 未平仓的仓位没有盈亏可证，
不必上链。代价是开仓到平仓期间链上无记录，可接受。

### 4.3 归因规则

| 情形 | 判定 |
|---|---|
| 信号窗口内该 symbol 出现**同方向**开仓成交 | `followed: true`，记录实际 qty 与建议仓位的偏差 |
| 窗口内无任何成交 | `followed: false` |
| 既有跟随信号、又有自发交易 | `attribution: "ambiguous"`，不强行归因 |

只上非 HOLD 的信号（与 Lark 推送口径一致）。HOLD 不入链，否则一天 288 次 × 2 symbols 全是噪音。

**额外可用信号**：WS 的 `orders` 频道在用户**挂条件单时就推送 `triggerPrice`**，
所以能拿到用户自己设的止盈止损价位，据此判断他是否按系统建议设置。
这个字段进 ② 叶子（`trigger_price`），是归因的辅助证据。

### 4.4 加密方案与未连钱包的降级

**加密**：payload 用随机 AES-256-GCM 对称密钥加密；该对称密钥用**用户钱包公钥**做
ECIES（secp256k1）封装后，随密文一起存 IPFS。用户用自己的钱包私钥解出对称密钥，
再解 payload。解密密钥可选择性公开，用于对外展示战绩。

**用户尚未连接钱包时**（真实场景，不能假设用户一定连过）：

| 阶段 | 处置 |
|---|---|
| 叶子生成时 | 照常入链锚定哈希 —— **哈希基于明文，不依赖任何密钥**，所以不受影响 |
| payload 存储 | 暂用**运营方托管密钥**加密，manifest 标 `encryption: "custodial-v1"` |
| 用户连钱包后 | 重新用钱包公钥加密、重新 pin，manifest 改标 `encryption: "user-ecies-v1"` |

因为叶子哈希与加密无关（§4.1），这个升级**不需要链上做任何改动**。

诚实说明：`custodial-v1` 阶段运营方持有解密密钥，此时「默认加密」的隐私保证是弱的
（运营方本就有明文）。它的价值在于**用户事后可以单方面升级为自持密钥**，
且链上哈希不变。

## 5. 架构：双层 Merkle Tree

```
用户交易叶子 ──┐
用户交易叶子 ──┼──► userRoot（每用户一棵子树）
用户交易叶子 ──┘
                    │
userRoot(用户A) ────┼──► globalRoot ──► 链上 BatchCommitted(globalRoot, cid, ...)
userRoot(用户B) ────┘
                    同时每用户 UserSubtree(batchId, user, userRoot, leafCount)
```

选双层而非「每用户一棵树」的理由：多一笔全局根的 gas（Base 上可忽略），换来一个
**全局时间锚** —— 可以对外声明「截至 T，本平台共 N 笔交易，承诺是 root」。
这是防止运营方**静默丢弃交易**的唯一手段，也是「自证」最大的漏洞所在。

## 6. 合约与批次提交

### 6.1 合约接口（`AttestationRegistry`）

```solidity
event BatchCommitted(
    uint256 indexed batchId, bytes32 globalRoot, string manifestCid,
    uint32 leafCount, uint64 windowStart, uint64 windowEnd, uint16 schemaVersion
);
event UserSubtree(uint256 indexed batchId, address indexed user, bytes32 userRoot, uint32 leafCount);
event Endorsement(uint256 indexed batchId, address indexed user, bytes32 userRoot, bytes signature);
```

**取舍**：

- **纯事件、无状态**（不存 root、不存映射），The Graph 负责索引。唯一状态 `lastBatchId` 用于拒绝重放
- **`onlyOwner` + 不可升级 + 无暂停**。诚实说明：这保留了「运营方提交」的信任假设。
  完全无权限会让任何人往同一事件流灌假批次、污染 subgraph。合约代码不可改，审计一次即永久有效
- 批量提交用户子树：一笔交易里循环 `emit UserSubtree`，省 base fee

### 6.2 封批

**全局批次内待上链叶子总数** ≥ 64 条 **或** 距上次封批 ≥ 30 分钟 → 封批。
（阈值针对整批，不是单个用户 —— 单用户一天可能只有几笔，按用户计阈值永远触发不了。）

用**独立进程** `app/web3/committer.py`（asyncio 循环，每 60 秒检查），不复用 worker 周期：

1. worker 周期实测漂移 ~9 分钟，会让「30 分钟」变成 27–36 分钟
2. `docs/web3-roadmap.md` 已定原则：链上组件必须是交易路径之外的旁路服务

### 6.3 顺序与失败处理

**顺序不可颠倒**：先 pin IPFS 拿 CID → 再算 root（root 覆盖 CID）→ 再上链。

| 故障 | 处置 |
|---|---|
| Pinata pin 失败 | 重试，**不封批** |
| Base RPC 挂 / gas 不足 | 批次留在队列，下轮重试；`batchId` 幂等，重复提交被 `lastBatchId` 挡回 |
| 提交器进程崩 | 队列已落库，重启继续；交易路径完全不受影响 |
| 提交钱包 ETH 余额低 | Lark 告警，不静默 |

**nonce**：单钱包串行提交，不做并发 —— web3.py 自动 nonce 在并发下会撞车。

## 7. The Graph、manifest 与验证路径

### 7.1 原则：subgraph 只是索引，不是真相

链上才是真相来源。subgraph 被篡改或下线**不损害可信度**。所以前端**必须有直读合约
事件的降级路径**（Base RPC + `eth_getLogs`），不能让可信度依赖单一索引器。

### 7.2 Subgraph schema

```graphql
type Batch @entity {
  id: ID!
  globalRoot: Bytes!
  manifestCid: String!
  leafCount: Int!
  windowStart: BigInt!
  windowEnd: BigInt!
  txHash: Bytes!
  subtrees: [Subtree!]! @derivedFrom(field: "batch")
}

type Subtree @entity {
  id: ID!
  batch: Batch!
  user: Bytes!
  userRoot: Bytes!
  leafCount: Int!
  endorsements: [Endorsement!]! @derivedFrom(field: "subtree")
}
```

查询即：「给一个钱包地址，返回它所有批次与 `userRoot`」。

### 7.3 manifest：公开，明细加密

叶子哈希不在链上，需要一份**公开的** manifest 作为中间层：

```json
{
  "batchId": 42,
  "globalRoot": "0x...",
  "schemaVersion": 1,
  "users": [
    { "user": "0x...", "userRoot": "0x...",
      "leaves": ["0xh1", "0xh2"],
      "payloadCid": "bafy...",
      "encryption": "user-ecies-v1" }
  ]
}
```

`leaves` 公开带来一个很强的属性：

> 任何人都能取 manifest → **自己重建两级树** → 验证 `globalRoot` 与链上一致 →
> 为**任意叶子**生成包含证明。

**包含证明不需要运营方提供，运营方 API 完全不参与验证路径。**

代价：叶子数量与分布公开（谁在哪个批次、有几笔）。可接受 —— 地址已在链上公开，
需要匿名可用新钱包。

### 7.4 验证路径（完全去信任）

```
1. subgraph（或 eth_getLogs）→ batchId / globalRoot / manifestCid
2. IPFS 取 manifest → 重建两级树 → 验证 globalRoot
3. 为叶子生成包含证明 → 验证 叶子 ∈ userRoot ∈ globalRoot
4. 取 payloadCid 密文 → 用户提供解密密钥 → 解密
5. 校验 keccak(payload) == 叶子哈希
6. 用 payload 的 open_avg / close_avg / qty / fee 复算 PnL
7. 用 WEEX 公开 klines 校验成交价落在当时 [low, high] 区间
```

第 7 步让第三方能独立验证**成交价是否合理**，而不只是「记录没被改」。
WEEX 公开行情端点无需鉴权（`weex-virtual-api.md` §1）。

### 7.5 用户补签

1. 用户从 subgraph 看到自己的批次
2. 前端构造 EIP-712：

```
Endorsement { uint256 batchId, address user, bytes32 userRoot, uint256 nonce }
domain: { name: "AlphaCouncilAttestation", version: "1",
          chainId: 8453, verifyingContract: 0x... }
```

3. 用户钱包签名 → 发给后端 → **后端代发** `Endorsement` 事件

**代发可接受**：签名由用户私钥产生，第三方验签即可确认背书；代发只是搬运。
**用户不需要持有 Base 上的 ETH** —— 否则补签门槛太高没人会用。

`verifyingContract` 与合约地址在部署后写入 `backend/.env` 与前端配置；
本 spec 中的 `0x...` 均为示意占位。

签名内容含 `userRoot` 与 `batchId`，绑定到具体批次内容，**不可挪用**。
复用 `app/auth/wallet.py` 的 `verify_evm_signature` 与 `WalletChallenge`。

### 7.6 前端验证器

**不登录也能用**的页面：输入任意钱包地址 → 查批次 → 取 manifest → 重建树验证 →
展示交易明细与背书状态。新增文案走 `frontend/lib/i18n.tsx`（中英双语）。

### 7.7 成本与降级

Graph Studio 免费额度够实验平台用。Studio 是中心化索引服务，但它只索引、不产生真相；
挂了可自建 indexer 或直读日志。索引延迟几分钟到十几分钟，前端需显示「最新批次可能仍在索引中」。

## 8. 对账与数据修正

### 8.1 修正必须追加

链上不可改，所以修正用第四类叶子（§4.1 ④）。原叶子原封不动，前端展示时取最新 correction。

**红线**：绝不因「结果不好看」而修正。修正只允许由数据源或算法变更触发，`reason_code` 必填。

### 8.2 余额对账

每批（或每日）做账户级校验：

```
Σ(叶子里的 pnl_net)  vs  Δ(剔除资金进出后的净值)  →  reconciliation_delta
```

净值变化取自 WS `account` 频道（`amount` 与 `cumDepositAmount` / `cumWithdrawAmount` /
`cumTransferInAmount`），**所以资金进出可以精确剔除**，这是早期版本做不到的。

差额的合法来源：资金费（`positions.cumFundingFee`，已可观测，可单独列出而非当误差）、
监听器上线前的存量仓位、爆仓清算费、WS 断线期间的漏记。

**处置**：差额写进独立对账记录，**绝不静默摊到各笔交易上**。

**告警条件**（双条件，避免小额交易因几分钱的误差刷屏）：

```
|delta| > max(5% × |Δ净值|, 1 USDT)
```

这条把「采集悄悄失准」变成主动告警，否则误差会积累到某天被发现全部记录都不可信。

## 9. 安全边界（不可越）

- **绝不进 calldata / IPFS 明文**：WEEX key/secret、Clerk token、账户 ID、
  `orderId` 原文、原始行情明细
- 提交钱包私钥只进 `backend/.env`，不进代码、不进日志
  （参见既有教训：曾把 WEEX key 写进 fixture 推到 GitHub）
- 钱包地址只用于签名与索引，**不获得任何交易权限**
- 上链是**旁路**：任何链上故障都不得影响交易路径
- `WEEX_VIRTUAL_ONLY=true` 保持不变

## 10. 已知局限（必须写进对外文案）

1. **链上只能证明「运营方在 T 时刻之后没改过这条记录」，不能证明盈亏为真**
2. **虚拟盘行情疑似合成**（`weex-virtual-api.md` §6），所以 §4.1 ③ 的模拟准确率
   可信度上限 = 行情可信度
3. **盈亏是交易所（虚拟盘）回报值，但虚拟盘本身是模拟环境** —— `realizePnl` 真实
   来自交易所，不代表真实市场可复现
4. **WS 断线期间的成交可能缺失**，只能靠 REST 兜底，而 REST 的窗口只有 100 条且不可翻页
5. **未平仓的仓位不上链**，开仓到平仓期间无链上痕迹
6. **合约有 owner**，运营方可以选择停止提交（但停止会留下可见的批次空档）
7. **`realizePnl` 不含手续费**，对外展示的净盈亏必须扣除 `fillFee`，否则会系统性高估

对外表述只能用「**可验证的记录**」，不能用「**可验证的盈利能力**」。

## 11. 代码结构

### 11.1 已实现（采集层，已在 `main`）

```
backend/app/streams/
  protocol.py     # 签名 / 消息分类 / 推送→领域事件  ← 纯函数，无 IO
  client.py       # WS 客户端：订阅 / PONG / 指数退避重连（socket 可注入）
  repository.py   # 事件落库，按交易所 ID 幂等
  reconciler.py   # REST order/history 兜底对账：补 symbol + 标记漏单
  listener.py     # 常驻进程：每账户一条 WS 连接 + 一个周期性对账
backend/workers/streams.py               # 进程入口
backend/scripts/probe_private_ws.py      # 只读探测脚本（留着复验）
backend/scripts/probe_order_history.py   # 只读探测脚本
```

协议层全是纯函数、socket 通过可注入的 `connect` 隔开 —— 订阅、心跳、事件转发、
断线退避都能用假 Transport 测完，不用起服务器（项目里没有 `pytest-asyncio`，
用 `asyncio.run` 包 sync 测试函数）。

### 11.2 待实现（链上部分）

```
backend/app/web3/
  merkle.py       # 树构建 + 证明生成/验证   ← 纯函数
  payload.py      # payload 组装 + 加密      ← 纯函数
  leaves.py       # 从采集层的表生成叶子（新增，见 §16）
  ipfs.py         # Pinata 客户端
  chain.py        # 合约调用（web3.py）
  queue.py        # 待上链队列（DB 表 + 状态机）
  committer.py    # 独立提交进程
  contracts/AttestationRegistry.sol
```

`web3.py`、`eth-account` 已在 venv 中，无需新装大依赖。

### 11.3 数据表

**已存在**（迁移 `012_exchange_stream_events`，已在开发库跑过）：

| 表 | 内容 |
|---|---|
| `exchange_fills` | `fill` 频道落库。**单笔盈亏（`realize_pnl`，不含手续费）与 `fee` 在这里** |
| `exchange_order_events` | `orders` 频道落库。`order_source` / `trigger_price` / `position_tpsl` 在这里 |
| `exchange_account_samples` | `account` 频道落库。净值曲线与资金进出在这里 |

**待新增**：

- `attestation_leaves` —— 待上链叶子（signal / result / settlement / correction / equity），
  状态机 `pending → pinned → committed`
- `attestation_batches` —— batchId / root / cid / txHash / 状态
- `wallet_endorsements` —— 用户补签

按 `CLAUDE.md` 约定：**新表改两处**（`app/db/models.py` + 一条
`create_all(checkfirst=True)` 同步迁移），不写 `op.create_table`。

## 12. 采集层（✅ 已实现）

### 12.1 主通道：常驻私有 WS 监听器

**独立的常驻进程** `workers/streams.py`，与 worker 完全解耦：

```
StreamListener.run_forever()
  ├─ 扫描启用中的虚拟账户（复用 credentials_for_account），每账户：
  │    ├─ 一条私有 WS 长连接，订阅 fill / orders / account
  │    ├─ 按 (频道, 交易所 ID) 去重后落库
  │    └─ 并发的周期性 REST 对账（见 §12.2）
  └─ 没有可用账户时定期重扫，**不退出**
```

**与 worker 分进程的理由**（都是踩过后确认的）：

- WS 是长连接，worker 是 5 分钟一轮的短事务；混在一起会让交易路径的失败面扩大到网络
- worker 整进程共用一个 `Session`（每轮末尾 `rollback()`）。WS 回调是异步触发的，
  会与 worker 的轮次交错写库 —— SQLAlchemy 的 `Session` 不是线程安全的，
  合并就得先解决这个，收益却只是少一个容器
- 采集挂了不该影响下单；分开则 `docker compose restart streams` 不碰交易

**为什么不需要配对算法**：实测确认 `fill` 频道每笔成交自带 `realizePnl`。
**加权平均成本法配对、Δbalance 反推、fee 推算全部不需要了** —— 那是早期版本在
「REST 拿不到盈亏」前提下的产物。

### 12.2 兜底：REST `order/history` 定期对账

WS 断线期间的事件不会重放，必须靠 REST 补。实现见 `app/streams/reconciler.py`。

**对账做两件事**：

1. **补 `symbol`** —— 因为两个推送频道都没有它（§2.2.1），只有 REST 有。
   两张表都要补，且它们的「未知」表示不同：`exchange_fills` 用 `NULL`、
   `exchange_order_events` 用空串。只补 NULL 的话订单表的 `symbol` 会永远为空。
2. **标记漏单** —— REST 窗口里的订单如果「比我们最早见过的订单新、却没通过 WS 见过」，
   就是漏掉的。**基准取我们最早的那条订单事件**：比它更早的发生在监听上线之前，
   本来就没人看着，不算漏；一条记录都没有时没有基准，一律不报。

**能补什么、不能补什么**（别把兜底当万能）：

| | |
|---|---|
| **能补** | `symbol` |
| **不能补** | 漏掉的成交带来的**盈亏** —— REST 没有 `realizePnl`，漏一笔就是永久缺一笔 |

所以漏单只能**标记**：写进 `collector_errors`
（`collector="weex_private_streams"`, `message="observation_gap"`），按 `occurrences`
归并计数，具体漏了哪些订单打到日志。**不要**把 orderId 拼进 message —— 那会让每次
漏单产生新行，归并计数就失效了。

**窗口约束**：`limit=100` 是硬上限且 `page` 被忽略（§2.5），**无法翻页回溯**。
对账间隔默认 300 秒 —— 5 分钟内产生 100 笔订单不现实，但**进程停摆数小时就会漏**。

### 12.3 净值采样

`account` 频道的 `amount` + `cumDepositAmount` / `cumWithdrawAmount` /
`cumTransferInAmount` 让第三方能算出「**剔除资金进出后的净值变化**」，
这是 P1 最硬的证据，也是 §8.2 对账的基准。

### 12.4 边界情况

| 情形 | 处置 |
|---|---|
| 条件单触发平仓 | `exit_reason` 由 `order_source`（`STOP_LOSS`/`STOP_PROFIT`）判定 |
| 手动市价平仓 | `exit_reason: "manual"`（`reduceOnly=true` 且非条件单来源） |
| 爆仓 | `exit_reason: "liquidation"`（`cum_liquidate_fee > 0`）；未实测 |
| WS 断线期间的成交 | 靠 §12.2 兜底；**盈亏补不回来**，只标 `observation_gap` |
| 开仓发生在监听器上线之前 | `entry_source: "unobserved"`，PnL 记 `null` 而非 0 |
| 净值变化 ≠ Σ净盈亏 | 差额单独记录（§8.2），不静默修正 |
| 落库失败 | **必须 `rollback()`** —— 会话处于待回滚状态后每次 flush 都抛 `PendingRollbackError`，整条连接就废了 |

## 13. 测试策略

**纯函数层（TDD，先写测试）**

- `merkle.py`：1 个叶子 / 2^n / 非 2^n / 空批次；证明生成与验证互逆
- `payload.py`：加解密往返、同输入同哈希、字段完整性
- `leaves.py`：从 `exchange_fills` / `exchange_order_events` / `exchange_account_samples`
  生成叶子，含 `symbol IS NULL` 的处理、`exit_reason` 判定、净盈亏计算

**集成层**

- 队列状态机 `pending → pinned → committed`，失败重试幂等
- 封批条件（满 N 条 / 超时）
- RPC 挂掉后重试**不重复提交**

**合约层（Foundry）**：重放被拒、`onlyOwner` 生效、事件字段正确

**端到端**：Base Sepolia 跑完整链路（造叶子 → 封批 → 上链 → subgraph 索引 → 前端验证）

**项目既有坑**（`CLAUDE.md`）：新表改两处；测试默认 `Session` 是 autoflush=True
会掩盖插入顺序问题；JSON 列存 Decimal 要 `model_dump(mode="json")`。

> 采集层自己的测试（协议、客户端、落库、对账，共 36 条）已经落地，
> 见 `backend/tests/unit/test_stream_*.py`。

## 14. 交付顺序

| # | 步骤 | 状态 |
|---|---|---|
| 0 | 只读探测：字段可得性 | ✅ 已完成（§15） |
| 1 | `app/streams/`：WS 客户端 + 事件映射 + 落库 | ✅ 已完成并合并进 main |
| 2 | REST 兜底对账 + `observation_gap` 检测 | ✅ 已完成（§12.2） |
| 3 | `merkle.py` + `payload.py` | ⬜ 待做 |
| 4 | `leaves.py`：② 结果叶子 + ⑤ 净值叶子 | ⬜ 待做（接口见 §16） |
| 5 | 队列 + 封批 + 提交器 | ⬜ 待做 |
| 6 | 合约 + Foundry 测试 | ⬜ 待做 |
| 7 | subgraph + 前端验证器 | ⬜ 待做 |
| 8 | 用户补签 | ⬜ 待做 |
| 9 | 上 Base 主网 | ⬜ 待做 |

第 0–2 步已完成。**下一步从第 4 步的接口契约（§16）开始** —— 叶子长什么样由
采集层实际存下的字段决定，不是由想象决定。

## 15. 实测记录（第 0 步，2026-10-15 完成）

两个只读探测脚本，均已实跑：

- `backend/scripts/probe_order_history.py` —— REST `order/history` 字段与分页行为
- `backend/scripts/probe_private_ws.py` —— 私有 WS 连接、订阅与推送字段

### 15.1 实测结论

| 问题 | 结论 |
|---|---|
| 手动单在不在 REST `order/history` 里？ | ✅ 在。`clientOrderId` 是 UUID v4，与系统单的 `alpha-<hex>` 可分 |
| REST 有没有盈亏/费用字段？ | ❌ 没有，18 个字段全无 |
| REST 分页可用吗？ | ❌ `page` 被忽略，`limit=100` 是硬上限且不可回溯 |
| REST 能看到条件单吗？ | ❌ 挂单中查不到，触发成交后也不留记录 |
| 有没有未文档化的平仓/成交流水端点？ | ❌ 探测 8 个候选路径全部 404 |
| **私有 WS 可用吗？** | ✅ **可用，四个频道全部订阅成功并收到推送** |
| **WS 给单笔盈亏吗？** | ✅ **给，`fill.realizePnl`，交易所回报值** |
| **WS 给真实手续费吗？** | ✅ 给，`fill.fillFee` + `orders.takerFeeRate` |
| **条件单触发可见吗？** | ✅ 可见，含 `triggerPrice` / `source=STOP_LOSS/STOP_PROFIT` / `positionTpsl` |
| 能识别手动 vs 系统下单吗？ | ✅ `orderSource` + `clientOrderId` 前缀（`orderSource` 枚举不统一，需两者并用） |

### 15.2 被推翻的早期假设

早期版本基于「REST 拿不到盈亏」设计了一整套推导模型，现已全部不需要：

| 早期设计 | 现状 |
|---|---|
| 加权平均成本法配对开平仓 | 不需要，每笔 `fill` 自带 `realizePnl` |
| 按 taker 0.08%/边推算手续费 | 不需要，`fillFee` 是真实值 |
| Δbalance 反推平仓价（解方程） | 不需要，`cumFillValue` / `cumFillSize` 直接给 |
| 每分钟轻量采样器提升归属精度 | 降级为兜底手段，不再是主力 |
| `pnl_source: derived-*` 系列标注 | 全部换成 `exchange-reported` |

### 15.3 仍未验证

| 项 | 影响 | 优先级 |
|---|---|---|
| 爆仓时的推送形态 | `exit_reason: "liquidation"` 的判定依据 | 中 |
| WS 长时间运行的重连行为 | 监听器稳定性 | 中 |
| REST 对账在订单 >100 条时的漏检 | `observation_gap` 阈值设定 | 低 |
| 虚拟盘能否手动调整余额 | §8.2 对账基准的解读 | 低 |

### 15.4 探测脚本的安全约束（已遵守）

- 纯读：只 `GET` 与 `SUBSCRIBE`，不调 `POST /order`，不建仓不平仓
- 输出脱敏：不打印 key/secret/passphrase，`orderId` 截断显示
- 断言 `WEEX_VIRTUAL_ONLY=true`，否则拒绝运行
- 快照文件含完整订单 ID，脚本会提示不要提交到 git

### 15.5 实现阶段的新发现（2026-10-15，采集层落地后）

**① 两个推送频道都没有 `symbol`** —— 见 §2.2.1。这条推翻了「从 orders 反查 symbol」
的设计，是**只有真实数据能暴露**的问题：WEEX 文档明确列了这个字段，
探测脚本的输出里也早就有 `None`，但直到采集层把真实成交落库、发现
`exchange_fills.symbol` 全为 NULL 才被抓住。修法是 REST 回填（§12.2）。

**② 一笔往返 = 两条 `fill`，只有后一条带盈亏。** 实测：

```
开仓 fill:  realizePnl = 0        fee = 0.0063336
平仓 fill:  realizePnl = 0.001    fee = 0.0063344
```

所以**往返净盈亏 = 平仓.fill.realizePnl − 开仓.fee − 平仓.fee**，
不是单条 fill 的 `realizePnl − fee`（那只是半程）。§4.1 ② 叶子的
`pnl_net` 定义已经按往返算，实现时别写错。

**③ 观测到的往返都被费用吃掉了。** 两次真实往返（都是最小量级）：
`+0.010` 毛 / `0.012792` 费 → 净 **−0.002792**；`+0.001` 毛 / `0.012668` 费 →
净 **−0.011668**。**净盈亏与毛盈亏的符号可能相反**，对外展示必须以净额为准。

（第一次是探测阶段用 `probe_private_ws.py` 跑的条件单实验，**没有落库**；
第二次是采集层上线后跑的，在库里。样本量很小，只能说明费用量级，
不足以说明策略盈亏。）

**④ 采集层与交易路径的 Session 不能共用。** worker 整进程一个 `Session`，
WS 回调是异步触发的，会与 worker 的轮次交错 —— SQLAlchemy 的 `Session` 不是
线程安全的。最终把采集层做成独立进程，这是主要原因之一。

**⑤ 已经落库的真实数据**：`exchange_fills` 2 条、`exchange_order_events` 2 条、
`exchange_account_samples` 2 条 —— **一次往返**（DOGE-USDT LONG，开在
`13:23:28`、平在 `13:23:57`，毛 `+0.001`、双边费合计 `0.012668`、净
`−0.011668`）。样本太少，实现叶子时先把这条跑通，别急着按它下结论。

## 16. 与采集层的接口契约

链上部分的输入**只有采集层已经落库的字段**。这一节把它写死，免得实现叶子时
又去猜 Push 里有什么。

### 16.1 ② 结果叶子 ← `exchange_fills` + `exchange_order_events`

| 叶子字段 | 来源 | 备注 |
|---|---|---|
| `symbol` | `exchange_fills.symbol` | **可能为 NULL**（§2.2.1）—— 生成叶子前必须已回填，见 §16.3 |
| `side` | `exchange_fills.position_side` | |
| `qty` | `exchange_fills.quantity` | 开仓腿与平仓腿应相等；不等时记两腿实际值 |
| `open_avg` / `close_avg` | `value / quantity` | 两张表都没有「均价」列，用成交额除数量 |
| `open_at` / `close_at` | `exchange_fills.filled_at` | 毫秒精度 |
| `fee` | 两腿 `exchange_fills.fee` 之和 | 往返费用 |
| `realize_pnl` | **平仓腿**的 `exchange_fills.realize_pnl` | 开仓腿恒为 0 |
| `pnl_net` | `realize_pnl − fee` | 见 §15.5 ② |
| `order_source` | `exchange_order_events.order_source` | 平仓腿那条 |
| `exit_reason` | 由 `order_source` + `reduce_only` 判定 | 见 §12.4 |
| `trigger_price` | `exchange_order_events.trigger_price` | 仅条件单有 |
| `client_order_id` | `exchange_order_events.client_order_id` | 手动单是 UUID，系统单是 `alpha-<hex>` |
| `open_fill_id` / `close_fill_id` | `exchange_fills.exchange_fill_id` | 让第三方能回到交易所侧核对 |

**配对方式**：两条 fill 的 symbol 与 position_side 相同、`order_side` 相反、
按时间相邻，且平仓腿 `reduce_only` 为真（来自对应的 order 事件）。
**没有用加权平均成本法** —— 交易所已经给了 `realizePnl`，配对只是为了把两腿
归成一条记录，不需要它来算盈亏。

### 16.2 ⑤ 净值叶子 ← `exchange_account_samples`

| 叶子字段 | 来源 |
|---|---|
| `amount` | `exchange_account_samples.amount` |
| `cum_deposit` / `cum_withdraw` / `cum_transfer_in` | 同名列，用于剔除资金进出 |
| `cum_fill_fee` / `cum_funding_fee` | 同名列，用于解释净值变化 |
| `account_version` | `exchange_account_samples.account_version`（幂等键的一部分） |
| `captured_at` | 同名列 |

### 16.3 生成叶子前的两个前置条件

1. **`symbol` 已回填。** 成交落库时它可能是 NULL，要等一轮对账。
   叶子生成器要么等对账跑过，要么跳过 symbol 为 NULL 的行、留到下一批。
   **不要把 `symbol: null` 写进链上** —— 30 分钟的批次延迟本来就有容忍度。
2. **`observation_gap` 出现在批次窗口内时，批次要带上「不完整」标记。**
   否则第三方会以为那段时间真的没有交易。具体字段形式在实现时定，
   但**这个语义必须在**：漏记必须可被看见，不能假装完整。
