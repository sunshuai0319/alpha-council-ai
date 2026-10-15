# 手动交易与盈亏的链上存证设计

- 日期：2026-10-15
- 状态：待实现
- 技术栈：IPFS（Pinata）+ Base + The Graph + 双层 Merkle Tree

## 1. 背景与目标

系统在 `notify` 模式下通过 Lark 推送交易信号，用户在 WEEX 虚拟盘上**手动**决定是否开仓、开多少。
目前这些手动交易与最终盈亏**完全没有留痕** —— worker 只在有系统执行时才拉 `order/history`
（`app/services/cycle.py:410`），通知模式下这条路径永不触发。

本设计把「信号 → 用户手动执行 → 实际盈亏」这条链路做成**可被第三方独立验证**的链上存证。

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

## 2. 数据可得性结论（已从代码与实测文档确认）

| 层 | 可得性 | 依据 |
|---|---|---|
| 交易事件（价格、数量、方向、时间） | 可得 | `order/history` 是账户级终态订单流水，含用户手动下的单 |
| 单笔已实现盈亏 | **不可得** | 虚拟盘无 `userTrades`（`app/exchange/weex.py:537` 直接 raise），`order/history` 不含 `realizedPnl` |
| 账户级已实现盈亏 | 仅区间聚合 | Δbalance（`app/reconciliation/service.py:210`），含手续费与资金费，**无法归属到单笔** |

**结论：链上 PnL 必然是推导值，必须标注算法来源与版本，并携带足以复算的证据。**

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

### 4.1 四类叶子

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

`entry_price` 是信号建议的入场价，`valid_until` 是信号有效期 —— 两者是 §4.1 ③
模拟结算与 §4.3 归因窗口判定的输入。

**② 实际结果**（用户平仓后入队）

```
signal_hash              # 引用①，建立因果；用户自发交易时为 null
followed                 # true / false
attribution              # "matched" | "ambiguous" | "none"
symbol, side, qty
open_avg, close_avg, open_at, close_at
fee, pnl_gross, pnl_net
pnl_source               # "derived-avg-cost-v1"
fee_source               # "derived-taker" | "exchange-reported"
entry_source             # "observed" | "unobserved"
exit_reason              # "manual" | "exchange_stop" | "unknown"
```

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
reason_code              # "fee-model-fix" | "pairing-fix" | "source-change"
corrected_payload_cid
```

### 4.2 叶子必须携带证据的理由

如果 payload 里只有 `pnl: +12.3`，链上只证明「这个数字没被改过」，**不证明它是真的**，
与「证明系统有效」直接冲突。最小证据集：

| 字段 | 不可省的理由 |
|---|---|
| symbol / side / qty | 没有它算不出盈亏 |
| open_avg / close_avg | 同上 |
| open_at / close_at | 同上，且证明「信号在前、结果在后」 |
| fee | 往返 0.16%（`weex-virtual-api.md` §5），小额交易里能吃掉全部利润 |
| signal_hash | 因果链 |

**开仓与平仓不单独成叶子**，合并为一笔闭合交易 —— 未平仓的仓位没有盈亏可证，
不必上链。代价是开仓到平仓期间链上无记录，可接受。

### 4.3 归因规则

| 情形 | 判定 |
|---|---|
| 信号窗口内该 symbol 出现**同方向**开仓成交 | `followed: true`，记录实际 qty 与建议仓位的偏差 |
| 窗口内无任何成交 | `followed: false` |
| 既有跟随信号、又有自发交易 | `attribution: "ambiguous"`，不强行归因 |

只上非 HOLD 的信号（与 Lark 推送口径一致）。HOLD 不入链，否则一天 288 次 × 2 symbols 全是噪音。

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

每批（或每日）做账户级校验：`Σ(推导 pnl_net)` vs `Δbalance` → `reconciliation_delta`。

差额的合法来源：资金费（8h 一期，未计入推导）、系统上线前的存量仓位、
虚拟盘可能的手动调余额、maker/taker 费用假设误差。

**处置**：差额写进独立对账记录，**绝不静默摊到各笔交易上**。

**告警条件**（双条件，避免小额交易因几分钱的误差刷屏）：

```
|delta| > max(5% × |Δbalance|, 1 USDT)
```

这条把「推导模型悄悄失准」变成主动告警，否则误差会积累到某天被发现全部记录都不可信。

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
3. **PnL 是推导值**，不是交易所回报值。费用按 taker 0.08%/边推算
4. **未平仓的仓位不上链**，开仓到平仓期间无链上痕迹
5. **合约有 owner**，运营方可以选择停止提交（但停止会留下可见的批次空档）

对外表述只能用「**可验证的记录**」，不能用「**可验证的盈利能力**」。

## 11. 代码结构

```
backend/app/web3/
  merkle.py       # 树构建 + 证明生成/验证   ← 纯函数
  payload.py      # payload 组装 + 加密      ← 纯函数
  ipfs.py         # Pinata 客户端
  chain.py        # 合约调用（web3.py）
  queue.py        # 待上链队列（DB 表 + 状态机）
  committer.py    # 独立提交进程
  contracts/AttestationRegistry.sol
backend/scripts/probe_order_history.py   # 前置只读探测
```

`web3.py` + `eth-account` 已在 venv 中，无需新装大依赖。

### 11.1 新增数据表

- `attestation_leaves` —— 待上链叶子（signal / result / settlement / correction），
  状态机 `pending → pinned → committed`
- `attestation_batches` —— batchId / root / cid / txHash / 状态
- `wallet_endorsements` —— 用户补签

按 `CLAUDE.md` 约定：**新表改两处**（`app/db/models.py` + 一条
`create_all(checkfirst=True)` 同步迁移），不写 `op.create_table`。

## 12. 采集层改造

现状：`app/services/cycle.py:410` 只在有系统执行时才拉 `order/history`。改为**每轮无条件拉取**。

```
每轮周期 / 每用户
  └─ 拉 order/history（分页直到覆盖水位线）
       └─ 用 (time, orderId) 水位线去重，只处理新订单
            └─ 分类：开仓 / 平仓
                 └─ 配对（加权平均成本法）→ 生成叶子
                      └─ 入待上链队列
```

**配对用加权平均成本法**（不是 FIFO）：

```
avg_entry = Σ(price×qty) / Σqty      # 加仓时更新，不产生新叶子
pnl_net = (exit_price − avg_entry) × close_qty × 方向 − 双边费
```

理由：加仓/分批平仓时不产生碎片化小记录，且与交易所 `avgPrice` 语义一致。

**边界情况**：

| 情形 | 处置 |
|---|---|
| 交易所自动止损平仓 | 标 `exit_reason: "unknown"`，不猜 |
| 一轮内订单 > 100 条 | 分页 + 水位线，不能只读 `page=0` |
| 开仓发生在系统观测之前 | `entry_source: "unobserved"`，PnL 为 `null` 而非 0 |
| 推导 PnL ≠ Δbalance | 差额单独记录（§8.2），不静默修正 |

## 13. 测试策略

**纯函数层（TDD，先写测试）**

- `merkle.py`：1 个叶子 / 2^n / 非 2^n / 空批次；证明生成与验证互逆
- `payload.py`：加解密往返、同输入同哈希、字段完整性
- 配对与 PnL：加权平均、部分平仓、加仓、未观测开仓 → `null`

**集成层**

- 队列状态机 `pending → pinned → committed`，失败重试幂等
- 封批条件（满 N 条 / 超时）
- RPC 挂掉后重试**不重复提交**

**合约层（Foundry）**：重放被拒、`onlyOwner` 生效、事件字段正确

**端到端**：Base Sepolia 跑完整链路（造叶子 → 封批 → 上链 → subgraph 索引 → 前端验证）

**项目既有坑**（`CLAUDE.md`）：新表改两处；测试默认 `Session` 是 autoflush=True
会掩盖插入顺序问题；JSON 列存 Decimal 要 `model_dump(mode="json")`。

## 14. 交付顺序

| # | 步骤 | 验证方式 |
|---|---|---|
| 0 | **只读探测脚本**（§15） | 确认字段可得性 —— **可能推翻后面的设计** |
| 1 | `merkle.py` + `payload.py` | 单元测试全绿 |
| 2 | 采集层改造：拉 order/history + 水位线 + 配对 | 先只入库、不上链，比对 Δbalance |
| 3 | 队列 + 封批 + 提交器 | Base Sepolia |
| 4 | 合约 + Foundry 测试 | 测试网 |
| 5 | subgraph + 前端验证器 | 用真实批次验证 |
| 6 | 用户补签 | EIP-712 验签 |
| 7 | 上 Base 主网 | — |

**第 0 步必须先做**，因为它的结果可能推翻 §12 的推导模型。若前提不成立，退路见 §15.3。

## 15. 前置实测（第 0 步）

按 `weex-virtual-api.md` §7 的复验方法论，写**只读**探测脚本
`backend/scripts/probe_order_history.py`：

- 用 `TradingCycleService._exchange_for_user(user_id)` 造客户端
- **只调 `order/history`，不下单、不平仓**
- 打印字段全集、`clientOrderId` 形态、分页行为
- 使用方式：用户手动开一笔仓 → 跑一次；平仓 → 再跑一次

### 15.1 待确认清单

| # | 问题 | 影响 |
|---|---|---|
| 1 | 手动单**在不在** `order/history` 里？`clientOrderId` 长什么样？ | 决定手动/系统单能否区分；不成立则整个方案前提塌陷 |
| 2 | 手动平仓单的 `reduceOnly` / `positionSide` / `clientOrderId` | 决定开平仓能否自动分类 |
| 3 | `slTriggerPrice` 触发平仓后那笔单的形态 | 决定 `exit_reason` 能否自动判定 |
| 4 | 不带 `symbol` 参数拉 `order/history`：返回全部 symbol 吗？分页怎么用？保留多久？ | 决定水位线策略 |
| 5 | **order raw 里有没有 `commission` / `fee` / `realizedPnl`？** | 有则 PnL 无需推导，精度大幅提升 |
| 6 | 虚拟盘能否手动调整余额 | 影响 §8.2 对账基准的解读 |

第 5 条最值得先查，它直接决定后面写多少代码。

### 15.2 探测脚本的安全约束

- 纯读接口，不调 `POST /order`
- 输出脱敏：不打印 key/secret，`orderId` 截断显示
- 用虚拟盘账号（`WEEX_VIRTUAL_ONLY=true`）

### 15.3 退路

若发现**手动单根本不在 `order/history` 里**，方案退回到「只用 Δbalance 做账户级证明」：

- 能证明：账户净值曲线未被事后篡改（P1 的弱化版）
- 不能证明：单笔盈亏、信号归因（P2 完全不可达）
- §4.1 的 ①③ 叶子仍成立，② 退化为「账户级区间盈亏」
