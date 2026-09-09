# Alpha Council AI 设计规格

日期：2026-09-09

## 1. 目标与范围

Alpha Council AI 是面向 WEEX AI Wars II 的多智能体虚拟合约交易系统。首版使用 WEEX 虚拟盘，交易 BTC-USDT 与 ETH-USDT 永续合约，覆盖 5m、1h、4h 周期。系统每 5 分钟自动评估一次，生成交易提案，经确定性风控引擎校验后才允许虚拟盘下单。

首版目标是跑通“数据采集 → 清洗与摘要 → RAG → 多 Agent 分析 → 风控 → 虚拟盘执行 → 复盘展示”的闭环。暂不追求实盘交易、付费数据源、海量交易对、高频盘口策略或复杂分布式基础设施。

## 2. 已确认的约束

- LLM 使用火山引擎 Ark 模型 `deepseek-v4-pro-ga-260813`。
- Embedding 使用本地 BGE-M3；候选结果使用本地 BGE-Reranker-v2-M3 重排。
- PostgreSQL 与 Milvus 已部署，本项目直接连接，不创建或管理它们的容器。
- PostgreSQL、Milvus、Ark 和模型路径等配置参考 `treasury-sentinel/apps/api/.env`；本项目只提供脱敏 `.env.example`，不复制密钥。
- 前端和后端分别维护自己的 Dockerfile、Compose 与环境文件。
- 首版优先使用免费外部数据源。
- LLM 不得直接调用交易 API；所有新增仓位必须经过确定性风控引擎。

## 3. 总体架构

采用模块化单体后端加独立 Worker。后端 FastAPI 提供控制台 API，Worker 负责定时采集、内容处理、Embedding、Agent 决策、虚拟盘执行和订单同步。首版不引入 Kafka、Celery 等额外基础设施，以 APScheduler 或等价调度器触发任务，并使用 PostgreSQL 状态和唯一键保证幂等。

```text
免费数据源 ─┐
WEEX API   ─┼→ Collectors → PostgreSQL
            │                    │
            └→ News/Macro NLP ──┘
                                  └→ BGE-M3 → Milvus
                                               │
PostgreSQL market state ───────────────┐       │
Milvus knowledge retrieval             ├→ LangGraph Committee
DeepSeek-V4-Pro (Volcengine Ark)       ┘       │
                                               ↓
                                      Hard Risk Engine
                                               ↓
                                      WEEX Demo Futures
                                               ↓
                                      Order/PnL/Reconciliation
                                               ↓
                                      Dashboard API
```

目录边界如下：

```text
alpha-council-ai/
├── backend/
│   ├── app/              # FastAPI、Agent、RAG、风控、WEEX adapter
│   ├── workers/          # 采集、处理、决策、复盘任务
│   ├── migrations/
│   ├── docker-compose.yml
│   ├── .env.example
│   └── Dockerfile
├── frontend/
│   ├── app/              # Next.js Dashboard
│   ├── docker-compose.yml
│   ├── .env.example
│   └── Dockerfile
└── docs/
```

后端 Compose 只管理应用进程；PostgreSQL 和 Milvus 通过已有地址连接。前端只通过 `NEXT_PUBLIC_API_BASE_URL` 访问后端，不接触交易所、数据库或 LLM 密钥。

## 4. 免费数据源与采集策略

### 4.1 交易所

- WEEX 公共 API：ticker、K 线及可用的资金费率/市场快照。
- WEEX 虚拟盘私有 API：余额、权益、持仓、订单、成交和订单状态。
- 交易所访问统一封装在 `ExchangeClient` 接口之后，首版实现为 WEEX 虚拟盘客户端。

### 4.2 新闻

直接抓取公开 RSS，不依赖付费聚合 API。首批来源为 CoinDesk、Cointelegraph、Bitcoin Magazine 和 Google News RSS 关键词订阅。每条记录保留原文 URL、来源、标题、发布时间和抓取时间，并保存清洗后的正文或摘要输入。

### 4.3 宏观

优先使用 FRED 公开序列下载接口以及 Federal Reserve 官方日历/RSS，不把 FRED API Key 作为必需配置。首批跟踪联邦基金利率、CPI、失业率、美元指数代理序列和国债收益率。

### 4.4 数据降级

外部新闻或宏观源不可用时，交易仍可使用 WEEX 行情与本地指标运行；新闻和宏观只影响置信度与风险等级，不能绕过硬风控。CryptoPanic、X、Reddit、Glassnode、Dune 等注册或额度不稳定的渠道仅预留适配器，不作为首版依赖。

## 5. 数据模型与 Pipeline

Pipeline 状态为“采集 → 校验去重 → 入库 → 摘要 → 向量化 → 可检索”，每一步支持断点续跑和失败重试。

### 5.1 PostgreSQL 结构化数据

- `market_candles`：symbol、timeframe、OHLCV、open_time、source，按组合唯一。
- `market_snapshots`：ticker、order book 摘要、资金费率、未平仓量和采集时间。
- `account_snapshots`：余额、可用保证金、权益、保证金率。
- `positions`、`orders`、`fills`：虚拟盘仓位、订单、成交及请求/响应摘要。
- `trading_decisions`：Agent 决策、输入快照版本、提案、风控结果、最终动作。
- `pnl_snapshots`、`risk_events`：收益曲线、回撤、熔断和拒单原因。
- `source_documents`：URL、来源、标题、原文/清洗文本哈希、发布时间、抓取时间、语言和处理状态。
- `document_summaries`：LLM 摘要、事件类型、涉及资产、方向、影响期限、置信度和模型版本。

原文通过内容哈希与 canonical URL 去重。LLM 失败时保留原始文档并进入重试队列；Embedding 失败不得丢失原文和摘要。

### 5.2 Milvus

每个 chunk 使用 BGE-M3 向量，并保存 `document_id`、source、published_at、asset、event_type、sentiment、impact_horizon、embedding_model、content_hash 等标量字段，以便先过滤再召回。BGE-M3 召回候选，BGE-Reranker-v2-M3 对候选重排，最终只向 Agent 提供少量带来源证据。

实时价格、账户和风险数值始终以 PostgreSQL/WEEX 最新数据为准；RAG 只提供历史经验和解释性证据。

## 6. Agent 与决策协议

使用 LangGraph 编排以下节点：

- **Market Agent**：分析新闻摘要、市场快照和 RAG 证据，输出市场状态、事件方向、影响期限和置信度。
- **Quant Agent**：读取 5m/1h/4h K 线；Python 指标服务计算 EMA、RSI、MACD、ATR、布林带、波动率、成交量变化和趋势一致性，LLM 负责解释并形成信号。
- **Macro Agent**：分析宏观数据、官方事件窗口和历史宏观事件，输出风险背景。
- **Risk Agent**：以确定性代码为主，检查账户、仓位、ATR 止损、名义仓位、杠杆、日损失、连续亏损、数据新鲜度和全局暂停。LLM 可以生成说明，但不能改变硬规则结果。
- **Committee Agent**：使用 Ark 的 `deepseek-v4-pro-ga-260813` 汇总分析，输出严格 JSON：`HOLD/LONG/SHORT/CLOSE`、symbol、entry 偏好、stop loss、take profit、杠杆、仓位比例、置信度、理由和引用证据。
- **Execution/Reconciliation Service**：非 LLM，负责风控通过后的下单、幂等 client order ID、订单状态同步和异常修复。

任何 JSON 解析失败、证据不足、分析冲突或模型超时都默认 `HOLD`。LLM 只能生成交易提案，不能直接调用交易 API。

## 7. 决策周期、风控与故障处理

每 5 分钟触发一次：读取最新状态 → 检查健康与数据新鲜度 → 并行运行 Market/Quant/Macro → Committee 提案 → 确定性 Risk Engine 校验 → 虚拟盘下单或拒绝 → 同步订单/成交/仓位/PnL → 写入审计日志。

首版硬风控默认值配置化：

- 最大杠杆 3x。
- 单个交易对最大名义仓位为账户权益 20%。
- 单笔风险为权益 0.5%。
- 必须设置止损，距离由 ATR 与最小/最大边界共同约束。
- 单日亏损达到权益 5% 时停止开新仓，只允许减仓/平仓。
- 连续 3 次亏损进入冷却期。
- 数据过期、WEEX 异常、LLM 超时、RAG 无结果或订单状态不确定时，不新增仓位。
- 全局 `PAUSED` 状态优先级最高，恢复前必须通过健康检查。

下单超时后先查询订单，禁止盲目重试。每次决策保存输入数据版本、模型响应、风控命中规则、订单请求、交易所响应和最终仓位。Dashboard 支持暂停、恢复和手动平仓，但不提供绕过风控的能力。

## 8. API 与 Dashboard

后端 API 初始范围：

- `GET /api/health`
- `GET /api/market`
- `GET /api/decisions`
- `GET /api/portfolio`
- `GET /api/events`
- `POST /api/control/pause`
- `POST /api/control/resume`
- `POST /api/positions/{symbol}/close`

前端页面：总览、市场、AI 委员会、交易、事件与审计。总览展示权益、PnL、回撤、胜率和风险状态；委员会页面展示 Agent 观点、最终提案、RAG 引用和拒单原因；交易页面展示订单、成交、仓位和止损止盈。首版使用后端轮询，不引入 WebSocket。所有页面明确标识“WEEX Virtual Futures”。

## 9. 测试与验收

- 单元测试：指标、数据标准化、去重、窗口、RAG 元数据过滤、重排、仓位/止损和风险规则。
- 契约测试：WEEX 请求/响应映射，使用固定 Fixture，不触发真实订单。
- Agent 测试：固定输入验证 JSON 协议；超时、空证据和冲突意见必须 `HOLD`。
- 风控集成测试：超杠杆、超仓位、无止损、数据过期、日损、冷却期、暂停、重复订单和超时查询。
- Pipeline 测试：重复抓取不重复入库；LLM/Embedding 失败可重试；断点续跑完成后可检索。
- 端到端 Smoke Test：健康检查 → 行情 → 决策 → 风控 → 虚拟盘订单 → 成交同步 → Dashboard 可见。
- 前端测试：关键页面、暂停/恢复、手动平仓确认、错误状态和虚拟盘标识。

验收条件：本地 Docker 启动应用后，在仅配置 Ark、WEEX 虚拟盘凭据以及已有 PostgreSQL/Milvus 连接的情况下，能够完成至少一个决策周期；所有交易和拒单均有审计记录；任何依赖异常不会产生未授权新增仓位。

## 10. 非目标

- 不接入真实账户交易。
- 不部署 PostgreSQL 或 Milvus。
- 不依赖付费新闻、宏观或链上服务。
- 不在首版实现高频盘口策略、自动模型训练或复杂消息队列。
