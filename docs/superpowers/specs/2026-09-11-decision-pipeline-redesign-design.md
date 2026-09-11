# 交易决策链重构：确定性信号 + LLM 封闭否决 + 持仓管理

日期：2026-09-11

## 1. 问题

系统跑了 19 个决策周期，**全部 HOLD，订单表为空**（`trading_decisions` 19 条 `HOLD/ALLOWED`，`orders` 0 条）。
排查后确认不是单一原因，而是「一个硬断点 + 三个软倾向」叠加。

### 1.1 硬断点：宏观链路从来没通

| 问题 | 证据 |
|---|---|
| FRED 解析列名错 | `app/collectors/macro.py:82` 读 `row.get("value")`，但 FRED CSV 表头是 `observation_date,<SERIES_ID>`。库中 152 行 `macro_observations.value` **全为 NULL** |
| `macro_events` 从未进 state | `app/services/cycle.py:158` 构造 `TradingCycleState` 时没传该字段 → 宏观 agent 上下文恒为 `[]`，19 条决策的 macro 分析全部 `insufficient_data`、confidence 0.0 |
| 委员会看不到持仓与权益 | `TradingCycleState`（`app/domain/schemas.py:163`）没有 positions / equity 字段 → 委员会**永远不可能**输出有意义的 CLOSE |

### 1.2 软倾向

- **信号是散文，不是数据**。三个 analyst 输出的 `status` 是自由文本，实测出现 `OK` / `BEARISH` / `success` / `hold` / `completed` / `PROPOSED` 六种写法，委员会只能从中文段落里二次推断方向。
- **`AnalysisResult.model_version` / `trace_id` 必填无默认**，LLM 漏写就整条分析被 schema 拒掉 → 19 条里 4 条 quant 落到 `deterministic-fallback`。
- **委员会提示词只有一条否决规则**（`app/agents/graph.py:220`）：`"HOLD if evidence is missing, conflicting, stale, or insufficient"`，没有任何开仓门槛的量化定义。「冲突/证据不足」于是成为万能否决理由。
- **风险参数与 LLM 之间没有对齐**。`max_single_trade_risk_pct=0.005` ÷ `max_position_notional_pct=0.20` ⇒ 止损必须落在入场价 **2.5% 以内**；LLM 不知道这条，蒙错就被 `single_trade_risk` 拒。TP 完全没有校验，也不是必填。
- **RAG 检索是固定串** `"{asset} market outlook"`（`app/agents/graph.py:205`），召回 Consensys 重组、Trezor 钓鱼、Hunter Biden memecoin 等噪声，被当成「方向冲突的证据」。
- **外部故障被当成策略失败**。WEEX 503/401 计入 `max_consecutive_failures`，7 次触发 `repeated_cycle_failures`，把账户 PAUSED 了 **2 次**（另有 2 次 `risk_events.decision_id` 外键违例，即已知的 autoflush=False 坑）。

### 1.3 结论

全部 HOLD 在当时的输入下是**正确行为**——缺的是「有数值、有结构、有持仓上下文」的决策链，而不是一个更敢下单的 LLM。

## 2. 目标与非目标

**目标**：在 WEEX 虚拟盘上跑出**可验证的正收益**（有统计量的交易样本、可复盘的单笔 R 值）。

**非目标**：

- 不追求「更频繁下单」。选择性是必要的，长期 HOLD 只要理由正确就可接受。
- 不做高频/剥头皮。5m 主导在物理上不成立（见 §3.2）。
- 不引入深度学习模型。41 天样本量撑不起任何需要拟合的东西。

## 3. 关键决策

四条由使用者拍板，是本设计的约束而非选项：

1. **持仓尺度**：1h 定方向与入场，4h 做趋势过滤，5m 仅做择时确认；持仓 2 小时–2 天，允许隔夜。
2. **信号架构**：确定性规则打分器定方向 / 仓位 / SL / TP；LLM 降级为复核者。
3. **止损止盈**：交易所侧只挂宽灾难止损兜底，保本 / 移动止损 / 分批止盈由软件层在周期内执行。
4. **LLM 否决权**：封闭枚举，只可否决不可改方向；无合法枚举命中即放行。

### 3.1 为什么选 1h–4h

- **5m 在物理上不可行**：ATR(5m) 在 ETH 上 ≈ 0.11%，而一轮决策要串 4 次 LLM 调用（实测 10–40s）再套 5 分钟节拍，信号到手已过期 1–2 根 K 线。公开的 EMA/RSI/布林在 5 分钟尺度没有可持续边际。
- **数据面只支持 1h/4h**：只有 100 根历史（1h = 4.25 天，4h = 16.7 天）；虚拟盘无真实微观结构可依赖。价格结构类信号在 1h–4h 上更持久。
- **RAG 与宏观数据的天然尺度是小时到天**：RSS 新闻延迟约 1 小时，FRED 是日/月序列。用它们论证 5 分钟方向是范畴错误，用来过滤 1h–4h 的入场则合适。
- **成本约束**：taker 0.08%/边，往返 0.16%。周期越短换手越多，费用占比越高。
- **风险预算反推**：单笔风险上限 0.5% 权益 ⊗ 名义上限 20% 权益 ⇒ 实际单笔风险约 0.18%。要积累出可观测正收益，每笔平均盈利需 0.2%–0.4%，只有 1h–4h 的波动能稳定提供。

**已知取舍**：纯 4h 波段在 41 天回测窗口里可能只有 10–20 笔，看不到统计意义。1h 入场是样本量与单笔幅度之间的折中。

### 3.2 WEEX 实测约束（2026-09-11，虚拟盘）

| 事实 | 影响 |
|---|---|
| `slTriggerPrice` **真的生效**：持空仓、触发价挂市价上方 0.01%，价格越过 10 秒内仓位被自动平掉 | 灾难止损可用 |
| **平仓时触发单随仓自动撤销**：开仓后立刻平仓，价格越过原触发价 47 USD 无任何反应 | 软件层平仓与交易所侧止损不打架 |
| `cancelOrder` / `order/cancel` / `openOrders` 全部 **404** | 挂上的触发单**改不了、撤不掉** |
| 触发价方向由交易所校验，违反回 **500**（LONG 挂上方止损） | 方向错误只能靠 500 发现 |
| 触发单在任何端点都**观测不到**（不在 `order/history`、不在持仓 raw） | 只能靠行为验证其存活 |
| taker **0.08%/边**（`openFee 0.061816 / openValue 77.27`） | 回测必须计入，往返 0.16% |
| `klines` 单请求上限 **1000 根**，`startTime`/`endTime` **被静默忽略** | 1h 最多 41 天、4h 166 天，拿不到更长历史 |
| `ticker/24hr` 还返回 `openPrice`/`highPrice`/`lowPrice`/`priceChangePercent`/`quoteVolume`/`markPrice`/`indexPrice` | 现在全被丢弃 |
| `depth` / `trades` / `fundingRate` / `openInterest` **可用** | 文档与代码注释「虚拟盘无衍生品数据」是错的 |
| `depth` 价差 0.00013%、`openInterest` 数值偏大 | **很可能是合成数据**，只能当辅助确认项 |

## 4. 架构：四层递进

每层单独上线、单独可验证，坏了能定位到具体层。

```
采集 ──► 规则信号器 ──► LLM 封闭否决 ──► RiskEngine ──► 执行下单（挂灾难止损）
  │                                                          │
  └──────────► MarketContext（宏观/新闻）                     ▼
                                                       持仓登记
                                                          │
                                              每轮 PositionManager 巡检
```

### 第 1 层：数据层

**1.1 修 FRED 解析** — `app/collectors/macro.py:82` 改为按 series 名取值（`row.get(series_id)`），保留 `value`/`VALUE` 作为兼容回退。

**必须配一次性回填**：库中 152 行 `value=NULL` 已被 `_schedule.mark` 标记为抓取成功，不会再重抓。回填脚本按 `series_id` 重解析历史 CSV 或直接重抓（`macro_observations` 有 `id` 自增主键，按 `(series_id, observation_date)` upsert）。

**1.2 接通宏观上下文** — 新增 context loader，每轮注入 `state.macro_events`：

- `macro_observations` 按 `series_id` 取最新值 + 前值算变化
- FED 新闻：`source_documents.source='federal-reserve'`（现有 21 条）join `document_summaries`，取最近 N 条

**1.3 补齐 ticker 字段** — `MarketSnapshot` 扩展 `open_24h` / `high_24h` / `low_24h` / `price_change_pct` / `quote_volume_24h` / `mark_price` / `index_price`。需要增量迁移 `003`（按 CLAUDE.md 约定，`create_all` 不会动已有表）。

**1.4 新增 `MarketMicrostructure`** — 独立模型、独立表，不并入 `MarketSnapshot`。

理由：**失败模式不同**。`ticker` 成功而 `depth` 失败是常态；混在一个模型里用 Optional 字段就分不清「没采到」和「采到但是空」，而信号器对这两种情况处理完全不同。拆开也避免波及已有 `risk` / `execution` 调用点。

```
spread_bps, depth_imbalance          ← depth 前 5 档
taker_buy_ratio                      ← trades 的 isBuyerMaker
funding_rate, funding_rate_avg_3     ← fundingRate
open_interest, open_interest_change_pct  ← openInterest + 上轮快照（需落库才能比）
```

**1.5 K 线抓到 1000 根并累积** — `limit` 从 100 提到 1000，按 `symbol+timeframe+open_time` upsert。

**1.6 采集失败不再熔断账户** — 外部故障（503/401/超时）与策略连亏分开计数。`repeated_cycle_failures` 不应把账户置为 PAUSED；改为记录 `DATA_SOURCE_DEGRADED` 事件，账户状态不变。

### 第 2 层：信号层

**2.1 规则信号器** `app/signals/`（新模块）

输入：`technical_indicators` + `MarketSnapshot` + `MarketMicrostructure`（可缺失）+ 宏观上下文
输出：结构化提案（方向 / 仓位 / SL / TP / 灾难止损 / 分数分解）

打分卡，每项 -1..+1，加权求和成 `composite ∈ [-1, 1]`：

| 项 | 权重 | 说明 |
|---|---|---|
| `trend_alignment` | 0.40 | 1h/4h EMA 排列一致性 |
| `momentum` | 0.25 | RSI 偏离 50 + 斜率 |
| `volume_confirmation` | 0.15 | 量变与方向同号 |
| `volatility_regime` | **门控** | ATR(14, 4h) 在已累积历史里的分位，落在 `[p20, p90]` 之外直接 HOLD |
| `oi_confirmation` / `funding_crowding` / `order_flow` | **只记录，不参与打分** | 权重待前向数据验证后再定 |

第一版只上 4 项：41 天样本撑不起 7 个权重，且微观结构项很可能是合成数据。先记录它们与前向收益的相关性，有证据再纳入。

触发条件（显式常数，进 `config.py`）：`composite ≥ entry_threshold`（默认 0.35）**且** 4h 趋势不反向 **且** 波动率门控通过 → LONG；对称 → SHORT；否则 HOLD。

**微观结构缺失时不得整体 HOLD** —— 缺项按权重归零后重新归一化，而不是让缺失变成否决理由。

**2.2 仓位与 SL/TP 由风险预算反推**

```
stop_distance     = k × ATR(14, 1h)                    # k 默认 1.5
risk_budget       = equity × max_single_trade_risk_pct # 0.5%
notional          = risk_budget ÷ (stop_distance ÷ entry)
notional          = min(notional, equity × max_position_notional_pct)  # 20%
position_size_pct = notional ÷ equity
SL                = entry ∓ stop_distance
TP                = entry ± r_multiple × stop_distance # r_multiple 默认 2.0
灾难止损           = entry ∓ 3 × stop_distance          # 唯一挂交易所侧的
```

这绕开了「LLM 填 SL 不知道 2.5% 隐含上限」的老问题。`RiskEngine` 仍是最终硬边界，不变。

**2.3 LLM 封闭否决**

```python
class VetoVerdict(BaseModel):
    veto: bool
    reasons: list[VetoReason]   # REGIME_CONFLICT / NEWS_SHOCK /
                                # STRUCTURE_INVALIDATED / LIQUIDITY_ANOMALY /
                                # DATA_INTEGRITY
    evidence_refs: list[str]    # 必须引用本次输入里的真实条目
    reasoning_summary: str
```

三种结局，必须可区分、可统计：

| 结局 | 条件 | 处理 |
|---|---|---|
| `veto_none` | `veto=False` | 放行 |
| `veto_applied` | `veto=True` 且枚举与证据都合法 | 拦截 |
| `veto_invalid_ignored` | `veto=True` 但 `evidence_refs` 为空或理由不在枚举内 | **放行 + 计数** |

`veto_invalid_ignored` 占比长期偏高 = 提示词有问题，必须暴露。连续 **5** 次否决无效则告警。

**关键：`证据不足` 不再是合法否决理由。** 证据不足时规则信号器自己就输出 HOLD 了——这一条堵死了当前「全部 HOLD」的元凶。

**2.4 三个 analyst 改造** — 保留三个角色以维持委员会叙事，但输出改为结构化：`direction ∈ {-1, 0, 1}` / `strength ∈ [0,1]` / `horizon` / `key_observations`，从「决定要不要交易」退为「给否决节点提供结构化观察」。

**2.5 修必填字段** — `AnalysisResult.model_version` / `trace_id` 改为有默认值。

**2.6 RAG 检索改造** — 现在是固定串 `"{asset} market outlook"`（`app/agents/graph.py:205`），召回的是与交易无关的公司新闻。改为按方向与时间窗构造查询，并过滤 `impact_horizon`：信号器判 LONG 时检索利空证据、判 SHORT 时检索利多证据（**反向取证**——否决节点要找的是反面证据，不是附和材料），时间窗限制在最近 24h。检索目标从「给委员会凑证据」变成「给否决节点提供可能推翻本次判断的材料」。

### 第 3 层：风控与持仓管理

**3.0 委员会不再需要持仓上下文** —— §1.1 指出的「委员会看不到持仓，永远不可能输出 CLOSE」不再靠喂数据解决：平仓决策整体移交 `PositionManager`（§3.3），LLM 只参与开仓前的一次否决。委员会输出 `CLOSE` 的路径保留但不再是正常退出通道，由 PositionManager 或人工控制台触发。

**3.1 风险引擎补两条校验**（现完全缺失，`app/risk/engine.py`）：

- **TP 方向校验**：LONG 的 TP 必须 > entry，SHORT 必须 < entry
- **R:R 下限**：`(TP - entry) / (entry - SL) ≥ min_rr`（默认 1.5）

两条都沿用现有 `is_reducing` 豁免，不拦平仓。CLOSE 单继续只受账户状态类检查约束。

**3.2 单品种同向只持一仓** — 现在 `_execute` 每轮都可能新开仓，5 分钟一轮会连续加仓击穿风险预算。

**3.3 PositionManager** — 每轮独立于开仓决策运行：

| 规则 | 触发 | 动作 |
|---|---|---|
| 保本 | 浮盈 ≥ 1R | 有效止损上移到 entry |
| 移动止损 | 浮盈 ≥ 1R 后 | 跟随 `最高价 ∓ ATR(14, 1h)`，只上移不下移 |
| 分批止盈 | 浮盈 ≥ 1R / 2R | 各平 1/3，余下交给移动止损 |
| 时间止损 | 持仓 > 48h 且浮盈 < 0.3R | 平仓 |
| 结构失效 | `composite` 反转穿越 0 | 平仓 |
| 灾难止损 | 交易所触发单 | 兜底（仅当软件层挂掉） |

**实现约束**：没有撤单接口，所以「移动止损」是**本地记账 + 按需下 reduceOnly 市价单**，不是改交易所那条触发单。有效止损存本地（`positions` 表加列 `effective_stop`）。交易所侧只挂 `3 × stop_distance` 的宽止损，正常情况下永远不该被触发。

§3.2 已实测确认：软件层平仓时交易所会撤掉关联触发单，两者不会互相打架。

### 第 4 层：验证

**4.1 轻量回测器** `app/backtest/` — 喂 `market_candles` 累积的 1000 根 1h/4h，走**同一套** `SignalProposer + RiskEngine` + 模拟撮合（开盘价成交、ATR 止损、计入 0.16% 往返手续费）。输出：交易数 / 胜率 / 平均 R / 最大回撤。

**能力边界必须说清**：`depth` / `trades` / `fundingRate` / `openInterest` 没有历史，回测**验证不了**微观结构项。它的用途是**筛参数**（阈值、k、R:R、持仓上限），不是证明策略有效。

**4.2 前向验证** — `trading_decisions` 加列记录 `signal_score` / `composite` 分解 / `veto_type`。跑几天后统计：否决率、平均 R、哪些分项与未来收益真的相关。这是唯一能验证微观结构项的手段。

## 5. 数据模型变更

| 变更 | 类型 |
|---|---|
| `market_snapshots` 加 7 列 | 增量迁移 `003` |
| `market_microstructures` 新表 | 改 `app/db/models.py` 即可（`create_all(checkfirst=True)` 自动补建） |
| `positions` 加 `effective_stop` 等列 | 增量迁移 `003` |
| `trading_decisions` 加 `signal_score` / `veto_type` | 增量迁移 `003` |

按 CLAUDE.md 约定：新表只改 models，已有表加列必须写显式增量迁移（照 `002` 模板，`inspect(bind)` 判存在再 `op.add_column`）。

## 6. 错误处理与降级

沿用项目「快速失败、绝不默认放行」的边界，逐项明确：

| 情况 | 处理 |
|---|---|
| 微观结构采集失败 | 权重归零后重新归一化，**不**整体 HOLD |
| 宏观数据缺失 | 同上；宏观只影响 `volatility_regime` 之外的制度判断，不作为独立否决 |
| RAG 检索失败 | 沿用现状：返回空证据，不产生交易理由 |
| 规则信号器异常 | HOLD + 记录异常类名（`safe-hold`） |
| LLM 否决节点异常 | 视为 `veto_none` 放行？**否** —— 视为 HOLD。规则信号器已经给出方向，但否决节点异常意味着上下文不可信，按项目「模型异常一律 HOLD」的既有边界处理 |
| WEEX 5xx / 超时 | 只记 `DATA_SOURCE_DEGRADED`，**不**熔断账户 |
| 下单超时 | 沿用现状：`stable_client_order_id` 回查对账，状态未知返回 `UNKNOWN`，不重试 |
| 软件层平仓失败 | 记录并下一轮重试；交易所侧灾难止损兜底 |

## 7. 测试策略

- **FRED 解析**：喂真实表头的 CSV（`observation_date,CPIAUCSL`），断言 value 非 None；断言 `.` 与空串仍映射为 None
- **宏观 context loader**：SQLite + `PRAGMA foreign_keys=ON`，模拟生产会话（autoflush=False）
- **打分卡**：构造指标 → 断言方向、仓位、SL 距离
- **波动率门控**：分位超出区间 → 断言 HOLD
- **微观结构缺失**：断言权重归一化而非 HOLD
- **风险反推**：断言 `stop_distance == 1.5 × ATR(1h)`、`notional ≤ 20% equity`、单笔风险 ≤ 0.5%
- **TP / RR 校验**：方向填反 → 拒绝；RR < 1.5 → 拒绝；`is_reducing` 时豁免
- **VetoVerdict**：枚举外理由 → schema 拒绝；`veto=True` + 空 evidence → 判定为 `veto_invalid_ignored` 且放行
- **PositionManager**：保本 / 移动止损只上移 / 分批止盈 / 时间止损 / 结构失效，逐条独立测
- **单品种一仓**：已有同向仓位时不重复开仓
- **回测器**：固定 fixture K 线，断言已知结果
- **熔断隔离**：模拟 WEEX 503 连续 N 次，断言账户**未**被 PAUSED

## 8. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| 1 | 数据层（1.1–1.6） | `macro_observations.value` 非空；决策的 macro 分析不再是 `insufficient_data`；WEEX 5xx 不再 PAUSED 账户 |
| 2 | 信号层（2.1–2.5） | 回测器能跑出交易样本；LLM 否决率与三种结局分布可统计；`veto_invalid_ignored` 不占多数 |
| 3 | 风控与持仓管理（3.1–3.3） | 真实开仓成交；SL 距离与仓位符合风险预算；保本/移动止损在持仓期按预期推进 |
| 4 | 验证（4.1–4.2） | 参数经回测筛过；前向记录可统计平均 R 与否决率 |

## 9. 风险与未决

- **样本量不足是最大风险**。41 天 1h 数据 + hackathon 时间窗，可能得不出统计显著的结论。缓解：优先保证「每笔 R 可复盘」，而非追求胜率数字好看。
- **微观结构数据可能是合成的**（`depth` 价差 0.00013%、`openInterest` 数值异常）。因此第一版只记录不使用；若前向数据显示无预测力，直接删除该层。
- **`klines` 无法拉更长历史**（`startTime` 被忽略）。若后续需要更多样本，只能考虑外部数据源，不在本设计范围内。
- **虚拟盘无成交流水**，已实现盈亏靠相邻 balance 快照推导（现状不变）。
- 移动止损的触发粒度受 5 分钟周期限制，急跌时可能滑过有效止损线。这是周期粒度换来的代价，回测需按「下一根开盘价成交」保守估计。
