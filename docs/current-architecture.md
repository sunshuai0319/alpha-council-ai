# Alpha Council AI 当前架构与交易生命周期

日期：2026-09-14
状态：按当前仓库代码整理的 as-built 基线

> 本文是当前实现的事实来源。`docs/superpowers/specs/2026-09-09-alpha-council-ai-design.md`
> 和 `docs/superpowers/specs/2026-09-11-decision-pipeline-redesign-design.md` 主要记录
> 设计动机与演进过程；如果它们与本文冲突，以本文和代码为准。

## 1. 结论摘要

- 当前交易图不是旧设计中的“Market / Quant / Macro 并行分析 → Committee”。编译后的
  `TradingCycleGraph` 实际使用“新鲜度检查 → 确定性规则信号 → 三路 veto 扇出（新闻分支
  使用 RAG）→ 合并/校验 → 持久化”的路径。
- `market_node`、`quant_node`、`macro_node`、`committee_node` 和 `run_committee` 仍在
  `app/agents/graph.py` 中保留，但没有被 `build_trading_cycle_graph()` 注册到当前图，
  因而不是线上决策路径。
- RAG 已接入默认生产图，但只服务于非 HOLD 入场提案的新闻/宏观反向风险 veto。检索请求统一
  携带资产、LONG/SHORT 方向、反向风险 query、最近时间窗和召回上限；先查币种级证据，
  没有命中时回退到空资产标签的通用市场/宏观材料。没有反向证据是正常的 `veto_none`，
  不会单独阻塞规则入场；RAG 真正不可用或检索异常仍 fail-closed。价格、K 线、指标、
  余额、持仓、风控和下单不依赖 RAG。
- LLM veto 的缺失、超时、非法 JSON、schema 不合法或越界理由均 fail-closed，生成安全 HOLD；
  `veto_fail_closed` 与正常 `veto_applied` 分开记录。
- 默认止盈是 `2R`，即“止损距离的两倍”，不是入场价格的两倍。开仓时交易所侧默认同时
  附带静态 `2R` 止盈和 `3R` 灾难止损；本地持仓管理器仍负责动态止损、其他退出规则和 TP
  触发失败时的兜底。`EXCHANGE_TAKE_PROFIT_ENABLED=false` 可临时退回仅本地 TP。
- 当前持仓管理器还会在达到 `1.8R` 后锁存接近目标时间，超过 6 小时仍未达到 `2R` 就平仓，
  并对所有仓位施加 72 小时绝对持仓上限，避免盈利仓绕过原有 48 小时时间止损后长期占用仓位。
- 文档入库已统一为“规则资产识别 + LLM 资产 union + 基础币种归一化”；多资产文档按资产展开
  Milvus 行，并补充 `asset_scope` / `schema_version`。旧 v1 历史 collection 不会自动改写，
  需要使用安全回填脚本生成 v2 后再切换。

## 2. LangGraph 当前图

```mermaid
flowchart TD
    S([START]) --> L[load_context\n记录 Ark 模型版本（兼容字段 committee）和 trace]
    L --> F{validate_freshness}
    F -- 缺行情/行情过期 --> H1[safe_hold]
    F -- 通过 --> SG[signal_node\n确定性打分 + 仓位 + SL/TP]
    SG -- HOLD --> P[persist_decision]
    SG -- LONG/SHORT --> V[veto_fanout\n并行起点]
    V --> R[retrieve_evidence\nMilvus 向量召回 + Reranker]
    R --> N[news_veto_node\nLLM：新闻/宏观冲突]
    V --> ST[structure_veto_node\nLLM：技术结构/流动性]
    V --> D[data_integrity_node\n确定性：盘口/区间完整性]
    N --> M[merge_veto\n任一合法 veto 即 HOLD]
    ST --> M
    D --> M
    M --> PV[proposal_validator]
    PV -- 有错误 --> H2[safe_hold]
    PV -- 通过 --> P
    H1 --> P
    H2 --> P
    P --> E([END])
```

对应代码为 `backend/app/agents/graph.py:653-735`。图本身不负责调用 WEEX、下单或对账；
这些动作由 `TradingCycleService` 在图外按顺序执行。

### 2.1 节点职责

| 节点 | 类型 | 当前职责 | 关键输入/输出 |
|---|---|---|---|
| `load_context` | 确定性 | 注入 Ark 模型版本和 trace id；`committee` 是历史兼容字段名 | `model_versions`、`trace_ids` |
| `validate_freshness` | 确定性 | 检查行情是否存在、是否超过新鲜度上限 | 失败写入 `errors` |
| `signal_node` | 确定性规则 | 按 `STRATEGY_ENTRY_TIMEFRAME` / `STRATEGY_TREND_TIMEFRAME`（默认 1h/4h，当前环境为 12h/1d）计算方向和 composite；按 ATR 反推仓位、止损、止盈、灾难止损 | `TradeProposal`、`signal_score` |
| `veto_fanout` | 确定性 | 将非 HOLD 提案分发给三路校验；避免结构/数据分支等待 RAG | 无新增字段 |
| `retrieve_evidence` | RAG | 接收统一 `RetrievalRequest`；先按资产过滤，资产无结果时回退到空资产标签的通用市场/宏观材料；默认最多 25 条候选、重排后 5 条结果，可选按影响期限过滤 | `retrieved_evidence` |
| `news_veto_node` | LLM veto | 只判断新闻/宏观是否与规则信号冲突 | `news_macro` verdict |
| `structure_veto_node` | LLM veto | 只判断技术结构失效或盘口流动性异常 | `structure_liquidity` verdict |
| `data_integrity_node` | 确定性 veto | 检查价差、倒挂盘口、24h 区间和现价范围 | `data_integrity` verdict |
| `merge_veto` | 确定性 | 任一路合法 `veto=true` 就生成安全 HOLD | `veto_type` |
| `proposal_validator` | 确定性 | 校验 symbol、入场仓位、止损、规则信号证据和有效期；RAG 空结果不作为单独错误 | `errors` |
| `safe_hold` | 确定性 | 生成不可下单的安全 HOLD 提案 | `TradeProposal(action=HOLD)` |
| `persist_decision` | 确定性 | 补充数据版本并结束图 | `data_versions` |

旧分析函数的当前状态：

| 旧函数 | 当前状态 |
|---|---|
| `market_node` / `quant_node` / `macro_node` | 保留实现，但没有加入 builder，不会在当前图执行 |
| `committee_node` / `run_committee` | 保留兼容实现，但当前图不执行；当前方向由 `signal_node` 决定 |
| `AnalysisResult` | 仍作为状态和历史持久化结构的一部分，当前主要承接未执行的兼容字段 |

### 2.2 图流程合理性与后续优化

当前图的主边界是合理的：确定性代码决定方向、仓位和风险参数；LLM 只做封闭枚举的
入场否决；RiskEngine、Execution 和 Reconciliation 保留在图外，分别掌握不可覆盖的
风控、交易所副作用和账户事实。这避免把可审计的资金动作交给模型，也避免让旧的
Committee 再次承担持仓退出职责。

当前图的低风险拓扑优化是：`veto_fanout` 提前到 RAG 之前。现在新闻分支是
`retrieve_evidence → news_veto_node`，结构和数据分支从扇出点直接启动，三路最终在
`merge_veto` 汇合。这样 RAG 延迟或暂时不可用不会阻塞确定性数据检查；如果 RAG 发生异常，
检索节点会记录 `retrieval_failed`，图仍按 fail-closed 进入安全 HOLD。若只是没有命中证据，
则由新闻 veto 节点正常判断 `veto_none`，不能把“没有反向材料”误当成“没有入场证据”。

本轮同时完成了 RAG 和降级策略收敛：

1. **已完成：RAG 契约统一**：查询由 `RetrievalRequest` 集中表达；图层负责方向化反向
   query 和时间窗，Retriever 负责币种级过滤、通用材料回退、候选召回和重排。
2. **已完成：安全降级统一**：RAG 真正不可用、检索异常、LLM veto 不可用或输出非法时
   统一 fail-closed；RAG 正常返回空结果时仅表示没有发现反向风险，不自动阻塞规则信号。

仍建议按优先级继续优化：

1. **P2 状态收敛**：删除或隔离旧的 analyst/committee 状态字段，统一模型版本命名，
   并为每个节点记录耗时、输入快照版本、输出 verdict 和降级原因，便于定位是信号弱、
   否决、证据不足还是外部服务异常。
2. **P3 生命周期编排**：图继续只负责“本轮入场决策”；持仓管理、平仓重试和对账仍由
   周期服务负责。若未来需要图化，应拆成独立的 `PositionExitGraph`，避免在同一图中
   混合纯函数决策和不可逆交易副作用。

## 3. 从调度到下单的完整流程

```text
TradingScheduler.run_forever（默认每 300 秒）
  ├─ DocumentPipeline.run_once（RSS/FRED/Federal Reserve → 摘要 → 向量入 Milvus）
  └─ 每个 enabled virtual account × 每个 symbol
       └─ TradingCycleService.run
            1. WEEX 采集 K 线、ticker、盘口/成交/资金费率/OI
            2. 计算技术指标，并把市场数据写入 PostgreSQL
            3. PositionManager 管理已有仓位（在暂停判断和新开仓前）
            4. 读取余额，把 equity 注入 TradingCycleState
            5. 执行 LangGraph
            6. RiskEngine 做不可被模型覆盖的硬风控
            7. ExecutionService 仅对 ALLOWED 提案下单
            8. ReconciliationService 同步订单、仓位、账户余额和 PnL
            9. 写入 trading_decisions / risk_events
```

代码依据：

- 调度与账户/品种循环：`backend/workers/scheduler.py:48-74`
- 采集、持仓管理、图、风控、执行、对账：`backend/app/services/cycle.py:160-357`
- 新仓数量：名义价值为 `balance × position_size_pct`，再除以现价，见
  `backend/app/services/cycle.py:752-781`
- 下单幂等与超时状态：`backend/app/execution/service.py:61-123`

## 4. Agent 与非 Agent 边界

### 4.1 当前实际参与决策的“Agent”

1. **Rule Signal Agent（非 LLM）**：`signal_node` 调用 `score_signal()`。趋势、动量和
   量能产生 `composite`；达到阈值才产生 LONG/SHORT，否则直接 HOLD。
2. **News/Macro Veto Agent（LLM）**：只能基于 `retrieved_evidence` 和 `macro_events`
   判断新闻、宏观 regime 是否反向，不得改方向、仓位或止损。
3. **Structure/Liquidity Veto Agent（LLM）**：只能基于指标和 `microstructure` 判断结构
   失效或盘口流动性异常。
4. **Data Integrity Veto（非 LLM）**：把能通过代码直接证明的异常交给确定性检查，避免
   用 LLM 判断价差倒挂等事实。

LLM 的实际调用统一走 `ArkChatClient.complete_json()`，温度为 0；瞬时 5xx/超时可重试。
解析失败、schema 不合法、缺少 LLM 或理由越界时，单个 veto 记为 fail-closed，
`merge_veto` 生成安全 HOLD；合法 `veto=true` 仍记录为 `veto_applied`。

### 4.2 重要边界

- LLM 没有交易工具，也不能直接调用 WEEX。
- `RiskEngine` 在 LangGraph 之后运行，是最终硬闸门。
- `ExecutionService` 不理解 agent 推理，只接受 `risk_decision.allowed == true`；开仓发送
  `3R` 灾难止损和（默认启用时）静态 `2R` `tpTriggerPrice`。两者都是开仓时确定的静态价，
  保本/移动止损仍由本地执行。
- 平仓主要由 `PositionManager`、手动平仓 API 或 WEEX 触发单完成，不依赖旧的 Committee
  输出 CLOSE。

## 5. RAG 是否已经使用

### 5.1 答案

**已经使用，但不是所有路径都使用。** 默认 `TradingCycleService._default_graph()` 会组装：

```text
BGEEmbedder + MilvusVectorStore + BGEReranker
                ↓
             Retriever
                ↓
       build_trading_cycle_graph(retriever=...)
```

证据链：

- 组装默认 retriever：`backend/app/services/cycle.py:152-162`
- 非 HOLD 才进入检索：`backend/app/agents/graph.py:698-709`
- 统一请求、方向化 query 和图注入：`backend/app/agents/graph.py:223-272`、
  `backend/app/rag/query.py`
- 资产/影响期限/发布时间过滤、向量候选和重排：`backend/app/rag/retriever.py:99-158`
- 文档入库：RSS/Federal Reserve 文本经清洗、去重、Ark 摘要、分块、BGE-M3 embedding
  后写入 Milvus，见 `backend/app/workers/pipeline.py:56-156`、`163-244`

### 5.2 RAG 在哪里生效

| 场景 | 是否使用 RAG | 说明 |
|---|---:|---|
| 规则方向、composite、仓位、SL/TP | 否 | 由指标、现价、ATR、策略参数确定 |
| `news_veto_node` | 是 | 作为新闻/宏观反向证据输入 |
| `structure_veto_node` | 否 | 使用指标和盘口/流动性数据 |
| 价格、K 线、余额、持仓 | 否 | 以 WEEX/数据库事实为准 |
| RiskEngine | 否 | 纯确定性风控 |
| Execution/Reconciliation | 否 | 纯交易所适配与状态同步 |
| HOLD 周期 | 否 | `signal_node` 后直接持久化，节省 RAG/LLM 调用 |

### 5.3 两个容易误判的细节

1. 当前 RAG 只为新闻 veto 提供反向风险证据：LONG 查询 bearish/downside/negative
   catalyst，SHORT 查询 bullish/upside/positive catalyst；默认时间窗为最近 24 小时，
   默认召回 5 条、候选 25 条，可由 `RAG_LOOKBACK_HOURS`、`RAG_EVIDENCE_LIMIT`、
   `RAG_CANDIDATE_LIMIT` 和 `RAG_IMPACT_HORIZON` 配置。
2. `TradingCycleService` 的默认图和 `run(..., llm=...)` 覆盖路径都通过同一个
   `EvidenceRetriever` 契约构图；后者可显式传 `retriever`，未传时使用默认
   Milvus + BGE + reranker 工厂，不再出现“默认路径有 RAG、覆盖路径没有 RAG”的分叉。

RAG 失败会返回空证据并追加 `retrieval_failed`，图随后转安全 HOLD；RAG 正常但没有命中时
不会追加错误，`news_veto_node` 可以返回 `veto_none`。入场只要求规则提案自身带有指标证据，
不要求 RAG 必须找到材料。结构化行情与账户事实不写入 RAG。

### 5.3 BGE-M3 与 reranker 的实际调用时机

检索顺序是：BGE-M3 将 query 向量化 → Milvus 进行元数据过滤和向量候选召回 →
BGE-Reranker-v2-M3 对候选文本进行 cross-encoder 重排 → 返回最多 5 条证据。
两种模型都是 lazy load，且只有非 HOLD 路径进入 RAG；如果币种过滤和通用回退都没有候选，
会在 reranker 之前直接返回空列表，因此不会触发 reranker。Milvus 与 RAG 路径现在分别记录
`app.rag.milvus` 和 `app.rag.retriever` 的 INFO 日志：可看到集合是否就绪、search/insert
的过滤条件、向量维度、原始命中数、过滤后候选数、reranker 是否执行、返回数和耗时；insert
日志还包含本批次的 `asset_counts`，不记录连接 URI、token 或 query 正文。

### 5.4 Milvus 数据分布诊断（2026-09-14 实例快照）

当前实例的 PostgreSQL 与 Milvus 对照结果如下：

| 位置 | 总量 | 关键分布 |
|---|---:|---|
| `source_documents` | 117（其中 116 条为 `INDEXED` 且正文非空） | Cointelegraph 72、Bitcoin Magazine 22、Federal Reserve 22 |
| `document_summaries` | 117 | 68 条 `assets=[]`；BTC 39 次、SOL 3 次、ETH 2 次，其余长尾标签各 1 次左右 |
| v1 collection（旧） | 169 chunks | BTC 91、空资产 68；周期字段仍有多种未归一化值 |
| v2 collection（已回填） | 175 chunks | BTC 87、空资产 68、ETH 5、SOL 3、LINK 3，其余资产各 1 |

这组数字说明：**主要问题在采集和资产标注覆盖，不是 Milvus 向量索引把数据“聚”到了 BTC。**
旧 v1 的 169 个 chunk 只是保存上游已经写入的 `asset` 值；BCH/LTC 在这批历史新闻中没有
被标注，并不意味着它们的向量相似度低。v2 已按多资产展开、补齐规则识别并清除报价/稳定币
伪资产，但历史来源本身仍明显偏向 BTC 和宏观材料。

这组快照同时保留了旧 v1 基线和回填后的 v2 结果。根因按优先级为：

1. 历史版本的 `detect_assets()` 规则只覆盖 BTC、ETH、SOL；BCH、LTC 等交易品种不会被确定性识别。
2. 历史索引把 `assets=[]` 写成空资产，且每个 chunk 只保存 `summary.assets[0]`，多资产文章会丢失
   第二个及后续资产。当前代码已修复这两点，但旧的 116 条文档不会因为重启自动重算。
3. “通用市场/宏观”和“未知/漏标”都使用 `asset=''`，统计与过滤时无法区分；来源也只有少数
   RSS/Fed feed，天然造成 BTC/宏观新闻多、长尾币种少。
4. 历史数据的 `event_type` 与 `impact_horizon` 也存在 `short_term`、`short-term`、`short_to_medium`
   等未统一值。这不会直接制造 BTC 偏斜，但会让过滤条件漏召回。v2 将期限收敛到
   `SHORT/MEDIUM/LONG/SHORT_MEDIUM/UNKNOWN`，未知自由文本统一为 `UNKNOWN`，避免模型输出
   超长值写入 Milvus VARCHAR 字段；`ensure_collection()` 不会迁移既有 collection。

当前代码与数据迁移状态：

1. **已实现采集标注统一**：从 `TRADING_SYMBOLS` 生成基础资产白名单；标题+正文规则识别与 Ark
   `assets` 做 union；支持 BTC、ETH、SOL、BCH、LTC 等别名及 `BCH-USDT`/`BCHSUSDT`；事件类型
   和影响期限归一化；每个 chunk 按所有资产展开，而不是只保留第一项。
2. **已实现 v1/v2 兼容写入**：v2 schema 增加 `asset_scope`、`schema_version`；写入前读取真实
   collection 字段，旧 v1 collection 会自动丢弃新增字段而继续可写，不执行隐式迁移。
3. **已完成安全回填**：`backend/scripts/reindex_documents.py` 已从 116 条 PostgreSQL 已索引文档
   重新分块、使用同一 BGE-M3 向量化并 upsert 到 v2；结果为 116/116 成功、175 行、失败 0，
   期望主键与实际主键 175/175 一致。源库和旧 v1 collection 未删除；当前 `.env` 已指向 v2，
   已运行的长驻进程仍需重启后才会加载这一配置。
4. **仍需补覆盖率治理**：每轮输出 `asset_counts`、source 分布、unknown/generic 比例和各
   交易品种最近文档时间；对启用交易品种设置最低覆盖告警。需要长尾币种时增加相应官方/高质量
   feed，不能靠放宽向量相似度把别的币种新闻冒充成目标币种证据。

当前 v2 schema 采用“每个 chunk 按资产展开一行”，并保留 `asset_scope`、`content_hash`、
`embedding_model`、`schema_version`。检索仍采用“目标资产 → 空资产通用市场/宏观”两阶段；
由于旧表历史上只有一个空字符串，`asset_scope` 先用于统计与后续治理，不能把历史空标签直接
当作已知的通用材料。

安全回填示例：

```bash
cd backend
uv run python scripts/reindex_documents.py --dry-run
uv run python scripts/reindex_documents.py \
  --target-collection alpha_council_documents_bge_m3_v2
```

脚本默认只处理 PostgreSQL 中已有摘要且状态为 `INDEXED`、正文非空的文档；默认目标名为当前
collection 的 `_v1 → _v2`。它不删除源数据或旧 collection，失败文档会继续处理并在最终统计中报告。

## 6. 当前止盈、止损和仓位释放

### 6.1 当前参数与计算

`StrategyParams` 默认值：

```text
stop_distance = 1.5 × ATR(1h)
SL             = entry ∓ stop_distance
TP             = entry ± 2.0 × stop_distance   # 2R
灾难止损       = entry ∓ 3.0 × stop_distance   # 3R
```

仓位先按单笔风险预算反推，再受 20% 名义仓位上限约束，见
`backend/app/signals/sizing.py:41-75` 和 `backend/app/signals/params.py:39-49`。

### 6.2 当前实际执行

- `ExecutionService._order_request_for()` 将 `disaster_stop` 用作交易所的 `stop_loss`，并在
  `EXCHANGE_TAKE_PROFIT_ENABLED=true` 时把策略 `take_profit` 作为交易所侧静态 TP；
  `WeexClient.place_order()` 分别发送 `slTriggerPrice` 与 `tpTriggerPrice`。
- 本地 `Position` 会保存初始止损、有效止损、止盈和 `near_target_at`，见
  `backend/app/services/cycle.py` 与 `backend/app/db/models.py`。
- `PositionManager` 按“有效止损 → 软件止盈 → 结构失效 → near-target 超时 → 最大持仓时长
  → 原有低盈利时间止损 → 保本/移动止损”的优先级执行；目标判断使用 `peak_price`，因此
  5 分钟采样跨过目标后又回落也不会漏掉止盈。

因此当前行为是：

```text
达到 2R      → 交易所侧优先触发 TP；本地 PositionManager 同时保留兜底平仓
达到 1.8/1.9R → 锁存 near-target 时间；超过 6h 仍未到 2R 则平仓
达到 1R 后回撤 → 可能由本地 effective_stop 触发软件平仓
持仓超过 72h → max_hold 平仓
持仓超过 48h 且浮盈 < 0.3R → time_stop 平仓
持仓超过 48h 但浮盈 ≥ 0.3R → 仍受 72h 最大持仓上限约束
```

外部平仓后的再入场也有单独保护：如果 WEEX 仓位已经消失而本地记录尚未完成本轮
reconciliation，风控先返回 `exchange_position_missing`，不会用旧的 `OPEN` 状态重新下单；
对账把本地记录收敛为 `CLOSED` 后，默认继续阻止同一品种新开仓 6 小时，返回
`reentry_cooldown`。这覆盖 WEEX 控制台人工平仓、交易所侧 TP/SL 和强平等退出，避免持续的
旧信号立即把仓位重新开回来。冷却时间由 `REENTRY_COOLDOWN_SECONDS` 配置，设为 `0` 可关闭。
真实仓位重新出现时仍按交易所现状管理，不会被本地冷却逻辑阻止。

### 6.3 “占保证金”的准确解释

主要被占用的是**仍然存在的仓位保证金和账户总敞口**，而不是已经单独创建了一个可见的
止盈止损订单。WEEX 虚拟盘没有撤单、改单和挂单列表接口；附带触发价无法被本系统观测、
修改或单独撤销。只要仓位仍在：

- `RiskEngine` 会把其名义价值计入 `current_notional`；
- 同一 symbol 的新开仓直接命中 `position_already_open`；
- 跨品种新仓还会受 `current_notional + proposed_notional` 的总敞口上限约束。

WEEX 的实测结论是平仓时关联触发单会随仓撤销；所以软件层发出成功的 reduce-only 平仓后，
理论上不会遗留触发单。若本地仍显示 OPEN，应优先检查下一次 reconciliation 是否成功，而
不是重复下单。

## 7. 止盈止损优化建议

以下区分已落地行为与后续目标，不把未实现的行为写成当前事实。

### P0：先统一触发单所有权

交易所侧默认附带不可变的静态 `2R tpTriggerPrice` 和 `3R disaster_stop`；软件层继续负责
正常 TP 兜底、保本和移动止损。由于虚拟盘没有撤单/改单/挂单查询接口，交易所 TP 只能在
开仓时一次性设置，不能用于动态调整；`EXCHANGE_TAKE_PROFIT_ENABLED=false` 保留了安全
降级开关。

交易所 TP/灾难止损作为进程/API 故障时的交易所侧兜底；本地 `PositionManager` 仍下
reduce-only 市价单处理动态规则。任意一侧平仓后依赖 reconciliation 确认交易所和本地状态
一致，不能仅根据本地订单记录判断仓位已经释放。

### P1：把 TP 纳入 PositionManager，并用“超过”而不是“等于”

当前已按 R 为单位实现明确优先级：

1. 有效止损触发，立即平仓；
2. 当前有利浮动 `favorable_r >= target_r`，平仓（不要等待价格精确等于 TP，避免 5 分钟
   采样跨过触发价）；
3. 结构失效或时间规则平仓；
4. 否则推进保本/移动止损。

### P2：为“接近目标但未到目标”定义资本释放规则

严格 2R 是策略目标，不应让它变成无限期占仓。当前已配置并启用以下规则；参数仍应通过
回测/前向数据校准，而不是永久拍死：

- `near_target_r`：例如 1.8R，达到后允许“提前止盈”或进入利润锁定阶段；
- `near_target_timeout_hours=6`：达到 near-target 后等待的最长时间，超时直接 reduce-only
  平仓，避免 1.8/1.9R 长时间悬挂；
- `max_hold_hours=72`：无论盈利与否的绝对持仓上限，修补原 `time_stop` 对所有
  `favorable >= 0.3R` 永不退出的问题；
- 继续保留 `breakeven_r=1.0` 和 ATR trailing，确保未到 near-target 时仍能保护已有利润。

当前第一版采用“达到 `near_target_r` 后开始计时，超时全平”的保守规则；若前向数据确认有
边际，再做 1R/1.5R/2R 分批止盈。分批止盈需要新增剩余数量、已执行阶段、部分平仓幂等键
和对账逻辑，不能只在现有 `take_profit` 字段上改一个数字。

### P3：补齐状态与可观测性

建议增加并落库：

- `exit_reason`：`target`、`near_target_timeout`、`effective_stop`、`time_stop`、
  `structure_invalidated`、`disaster_stop`；
- 当前 R、峰值 R、持仓时长、达到 near-target 的时间；
- 从开仓到释放保证金的秒数，以及因 `position_already_open`/`max_notional` 被拒的次数；
- reconciliation 后的本地/交易所仓位差异。

这样才能区分“策略有意持仓”和“交易所已平但本地幽灵仓位”，并验证优化是否真的提高了
资金周转，而不是仅仅增加平仓次数。

## 8. 建议的验证用例

已补齐并通过以下回归测试：

| 用例 | 期望 |
|---|---|
| LONG 当前价格跨过 2R | `PositionManager` 产生 CLOSE，原因是 `take_profit` |
| LONG 价格为 1.8R/1.9R | 锁存 near-target 时间，不假装已经到 2R |
| 1.9R 持仓超过 near-target timeout | reduce-only CLOSE，释放本地与交易所仓位 |
| 盈利仓达到 max_hold_hours | 无条件退出，避免“盈利所以永不 time-stop” |
| 开仓请求 | 默认同时收到静态 2R `tpTriggerPrice` 与 3R `slTriggerPrice`；关闭开关时只收到 SL |
| WEEX/人工/触发单已平仓 | reconciliation 清理 `status`、数量、止损、止盈和有效止损字段 |
| 软件平仓返回 UNKNOWN/OPEN | 不提前把本地仓位标为 CLOSED；下一次对账确认后再关闭 |

回测仍应计入双边手续费，并比较交易数、平均 R、最大回撤、平均持仓时长、资金占用时长
和被拒开仓次数；不能只看胜率。
