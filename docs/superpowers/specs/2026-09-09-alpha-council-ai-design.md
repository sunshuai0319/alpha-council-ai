# Alpha Council AI 设计规格

日期：2026-09-09

## 1. 目标与范围

Alpha Council AI 是一个可演示、可扩展为 SaaS 的多智能体虚拟合约交易平台。首版使用 WEEX 虚拟盘，交易 BTC-USDT 与 ETH-USDT 永续合约，覆盖 5m、1h、4h 周期。系统每 5 分钟自动评估一次，生成交易提案，经确定性风控引擎校验后才允许虚拟盘下单。

首版目标是跑通“数据采集 → 清洗与摘要 → RAG → 多 Agent 分析 → 风控 → 虚拟盘执行 → 复盘展示”的闭环，并为用户级数据隔离和后续 SaaS 化保留稳定边界。参赛不是首版成功标准，是否参加 WEEX 比赛由后续实盘资金、规则和风险评估决定。暂不追求实盘交易、付费数据源、海量交易对、高频盘口策略或复杂分布式基础设施。

## 2. 已确认的约束

- LLM 使用火山引擎 Ark 模型 `deepseek-v4-pro-ga-260813`。
- Embedding 使用本地 BGE-M3；候选结果使用本地 BGE-Reranker-v2-M3 重排。
- PostgreSQL 与 Milvus 已部署，本项目直接连接，不创建或管理它们的容器。
- PostgreSQL、Milvus、Ark 和模型路径等配置参考 `treasury-sentinel/apps/api/.env`；本项目只提供脱敏 `.env.example`，不复制密钥。
- 前端和后端分别维护自己的 Dockerfile、Compose 与环境文件。
- 首版优先使用免费外部数据源。
- 首版使用 Clerk 免费版提供登录和 Session，启用邮箱、Google 和 MetaMask；不在首版实现用户名密码、订阅计费、团队协作或 Apple 登录。
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

## 6. Agent 架构与决策协议

### 6.1 图结构

使用 LangGraph 编排一个按交易周期运行的 `TradingCycleGraph`。图的共享状态是结构化对象 `TradingCycleState`，至少包含：

- `tenant_id`、`user_id`、`cycle_id`、`started_at`、`symbol`。
- `market_snapshot`、`candles_by_timeframe`、`technical_indicators`。
- `news_items`、`macro_events`、`retrieved_evidence`。
- `market_analysis`、`quant_analysis`、`macro_analysis`、`risk_assessment`。
- `trade_proposal`、`risk_decision`、`execution_result`、`errors`。
- `data_versions`、`model_versions`、`trace_ids`，用于审计和复盘。

主图结构如下：

```text
load_context
    ↓
validate_freshness ──失败──→ safe_hold
    ↓
┌──────────────┬──────────────┬──────────────┐
│ market_node  │ quant_node   │ macro_node   │  并行分析
└──────────────┴──────────────┴──────────────┘
          ↓
retrieve_evidence → committee_node
          ↓
proposal_validator ──失败──→ safe_hold
          ↓
deterministic_risk_node ──拒绝──→ persist_decision
          ↓
execution_node → reconcile_node → persist_decision
```

`load_context` 只从当前用户/租户可见的数据读取上下文；`validate_freshness` 检查行情、账户和系统状态。Market、Quant、Macro 三个节点相互独立，可以单测、重试和替换。`committee_node` 只产生交易提案，不拥有下单工具。`deterministic_risk_node` 是不可被 LLM 覆盖的闸门；`execution_node` 和 `reconcile_node` 不使用 LLM。任何失败路径都汇聚到 `safe_hold` 或仅允许减仓/平仓的安全动作。

### 6.2 Agent 职责、输入与输出

- **Market Agent**：分析新闻摘要、市场快照和 RAG 证据，输出市场状态、事件方向、影响期限和置信度。
- **Quant Agent**：读取 5m/1h/4h K 线；Python 指标服务计算 EMA、RSI、MACD、ATR、布林带、波动率、成交量变化和趋势一致性，LLM 负责解释并形成信号。
- **Macro Agent**：分析宏观数据、官方事件窗口和历史宏观事件，输出风险背景。
- **Risk Agent**：以确定性代码为主，检查账户、仓位、ATR 止损、名义仓位、杠杆、日损失、连续亏损、数据新鲜度和全局暂停。LLM 可以生成说明，但不能改变硬规则结果。
- **Committee Agent**：使用 Ark 的 `deepseek-v4-pro-ga-260813` 汇总分析，输出严格 JSON：`HOLD/LONG/SHORT/CLOSE`、symbol、entry 偏好、stop loss、take profit、杠杆、仓位比例、置信度、理由和引用证据。
- **Execution/Reconciliation Service**：非 LLM，负责风控通过后的下单、幂等 client order ID、订单状态同步和异常修复。

所有 LLM 节点使用版本化 Prompt 和结构化输出 Schema。节点输出必须包含 `status`、`confidence`、`reasoning_summary`、`evidence_refs`、`model_version` 和 `trace_id`；分析节点不得输出订单参数以外的任意工具调用。Committee 提案还必须包含 `proposal_id`、`action`、`symbol`、`side`、`position_size_pct`、`leverage`、`stop_loss`、`take_profit`、`valid_until` 和 `invalidation_conditions`。

任何 JSON 解析失败、证据不足、分析冲突或模型超时都默认 `HOLD`。LLM 只能生成交易提案，不能直接调用交易 API。每个节点的输入快照和输出都写入 `trading_decisions` 关联的 trace，便于解释“哪个 Agent 依据什么信息提出了什么建议”。

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

## 8. 用户登录、鉴权与 SaaS 演进

### 8.1 结论

建议首版加入最小可用鉴权，并采用 Clerk 免费版。原因是用户级交易配置、策略、虚拟账户、决策记录和审计数据从第一天就应具备归属关系；否则后续增加付费 SaaS 时，需要重做数据库主键、API 授权、前端状态和敏感配置隔离。鉴权不会改变虚拟盘交易逻辑，也不会要求首版实现计费。

Clerk 适合承担身份层，但不替代业务授权层：Clerk 负责“用户是谁、Session 是否有效”，后端负责“用户能访问哪一个虚拟账户、策略和交易资源，以及是否允许执行控制操作”。Clerk 免费版的具体额度和功能边界可能随产品政策变化，规格不绑定具体数量；进入公开商业化前需重新核对当前计划限制。

### 8.2 首版登录方式评估与选择

| 方式 | 决定 | 产品定位 | 主要注意事项 |
| --- | --- | --- | --- |
| 用户名密码 | 不做 | 如有需要，用户名只作为展示名/策略作者名 | 避免维护第二套密码身份、找回密码和账号合并逻辑 |
| 邮箱 | 必选 | 主账号恢复、通知、风控告警和未来账单联系 | 首次注册需完成邮箱验证；不要允许未验证邮箱执行交易控制操作 |
| Google | 推荐 | 最低摩擦的 Web SaaS 登录 | 生产环境需要配置正式 Google OAuth 凭据；开启邮箱子地址防护 |
| MetaMask | 推荐但可选 | Web3 用户登录、绑定钱包和未来链上证明 | 钱包丢失没有传统找回路径；签名只证明地址控制权，不代表交易授权 |
| Apple | 首版不做 | 当前不是必要入口 | 以后根据 iOS 用户占比和商业化需求再评估 |

推荐的 Clerk 配置是：**邮箱 + Google 作为主登录方式，MetaMask 作为可选 Web3 登录/绑定方式，用户名仅作为用户资料中的展示字段，不参与认证**。首版不建立独立的用户名密码登录体系。

账号流程分为两种：

1. 普通 SaaS 用户通过邮箱或 Google 创建 Clerk 账号，再在设置页绑定 MetaMask；绑定前必须对 nonce 签名，绑定结果保存到本地 `wallet_addresses`，并标记链、地址、验证时间和是否主钱包。
2. Web3 用户直接用 MetaMask 登录时，Clerk 先验证 nonce 签名；首次进入产品时引导其补充并验证邮箱。该用户可以先使用虚拟盘，但在未来启用高价值功能、策略发布或链上证明前必须完成邮箱和钱包双重绑定。

邮箱相同的已验证账号可以进行账号关联；不能仅因为字符串相同就自动合并未验证邮箱或不同钱包身份。账户关联、钱包解绑和主钱包变更都写入审计日志，并需要当前 Session 再次验证。

### 8.3 首版范围

- `users`：用户身份、状态、创建时间和最后登录时间。
- `users.clerk_user_id`：保存 Clerk User ID，作为外部身份主键；本地 `users.id` 作为业务主键。用户名是 profile 字段，不单独建立密码身份表。
- Clerk Session：前端通过 Clerk SDK 管理登录态，调用后端时携带 Session Token；后端验证 Token 并提取 `sub`/Clerk User ID，不自建第二套密码系统。
- `wallet_addresses`：Clerk Web3 钱包 ID、地址、链、验证时间、主钱包标识和状态；钱包地址必须通过 nonce 签名验证。
- `trading_accounts`：用户自己的 WEEX 虚拟盘凭据引用、环境标识和启用状态；密钥只存后端，数据库中加密或存密钥引用，绝不返回前端。
- `strategies`、`risk_profiles`、`trading_decisions`、`orders`、`positions`、`pnl_snapshots` 均带 `user_id`；未来可增加 `tenant_id`。
- 所有业务 API 在服务层进行用户归属校验，不能只依赖前端传入的 ID。
- Worker 任务必须携带 `user_id`/`tenant_id` 上下文；查询、RAG 过滤和执行均不得跨用户读取。
- Clerk 用户创建/更新/删除通过 Clerk Webhook 同步本地 `users` 表；Webhook 必须验签、幂等处理，并只同步必要字段。
- 前端使用 Clerk 的 Provider、登录/注册组件和受保护路由；后端 API 即使被绕过前端也必须独立验证 Token。
- 开发环境可使用 Clerk 测试实例；生产配置禁止通用默认账户和绕过鉴权的控制接口。

### 8.3 演进策略与选项

- **无鉴权**：最省时间，但不适合后续 SaaS，不推荐。
- **应用内最小鉴权**：完全自建身份系统，控制力强但需要自行维护密码安全、Session、找回密码和防攻击策略。
- **Clerk 免费版（推荐）**：首版直接使用托管身份、Session、邮箱/Google/MetaMask 入口和前端组件，后端只验证 Token 并维护业务授权；上线速度和安全基线更好，代价是依赖第三方和受计划额度约束。

首版推荐 Clerk：建立正确的身份与授权边界，但严格控制功能范围，不提前实现收费订阅。未来可在此边界上增加 Clerk Organizations、角色权限、套餐额度、策略数量和运行频率。Clerk 不应保存或返回 WEEX 私钥；交易凭据仍由后端统一管理。当前免费计划是否继续覆盖所需功能和用户规模，商业化前必须重新核对官方计划说明。

## 9. Web3 产品化路线评估

### 9.1 最推荐：可验证的 AI 交易决策与绩效凭证

将每个决策周期的关键摘要（输入数据版本、Agent 结论、风控结果、订单结果和绩效报告）规范化后计算 hash，原文和敏感数据继续留在 PostgreSQL，链上只存 hash、时间戳、系统版本和报告 URI。用户可以验证某次决策报告在生成后未被篡改。

这条路线最适合当前产品：它不需要托管资金，不改变虚拟盘交易流程，也能把 Agent 的可解释性升级为 Web3 的可验证性。后续可部署在低成本 EVM L2，并使用现有 Web3/智能合约能力实现 `DecisionAttestation` 合约。

### 9.2 第二阶段：钱包连接与用户资产风险视图

保留 Clerk 作为产品账号体系，增加可选钱包连接（钱包地址只作为用户绑定的外部资产身份，不替代 Clerk 登录）。通过用户签名完成地址绑定，读取公开链上资产、稳定币余额、借贷仓位或协议风险，作为 Risk Agent 的补充输入。首版只读，不要求用户转账或授权合约。

### 9.3 第三阶段：非托管策略市场

将经过虚拟盘验证的策略包装成版本化策略模板，支持用户订阅、复制参数或购买研究信号；策略代码和用户资金彼此隔离，系统只提供信号、回测和自动化模拟执行。可将策略版本、绩效摘要和作者归属做链上登记。

### 9.4 不建议早期做的方向

- 发平台 Token 或用 Token 激励交易：会引入复杂经济模型、投机行为和合规风险，不能证明产品价值。
- 托管用户资金、统一代客实盘交易或合约跟单：安全、牌照、资金隔离和事故责任显著扩大，不适合作为早期 SaaS 迭代。
- 把全部行情、新闻和 Agent 原文上链：成本高、隐私差，也没有必要；只做 hash/证明即可。

产品定位建议从“参赛交易机器人”转为“AI-native, verifiable trading intelligence platform”：先以虚拟盘和订阅式研究/策略工具验证用户价值，再考虑钱包、链上证明和非托管策略市场。WEEX 比赛不是产品验收条件，是否参赛由后续实盘资金与风险评估单独决定。

## 10. API 与 Dashboard

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

## 11. 测试与验收

- 单元测试：指标、数据标准化、去重、窗口、RAG 元数据过滤、重排、仓位/止损和风险规则。
- 契约测试：WEEX 请求/响应映射，使用固定 Fixture，不触发真实订单。
- Agent 测试：固定输入验证 JSON 协议；超时、空证据和冲突意见必须 `HOLD`。
- 风控集成测试：超杠杆、超仓位、无止损、数据过期、日损、冷却期、暂停、重复订单和超时查询。
- Pipeline 测试：重复抓取不重复入库；LLM/Embedding 失败可重试；断点续跑完成后可检索。
- 端到端 Smoke Test：健康检查 → 行情 → 决策 → 风控 → 虚拟盘订单 → 成交同步 → Dashboard 可见。
- 前端测试：关键页面、暂停/恢复、手动平仓确认、错误状态和虚拟盘标识。
- 鉴权测试：未登录拒绝、用户 A 不能读取或操作用户 B 的账户/订单/决策、Worker 上下文隔离、注销后 Token 失效和虚拟盘凭据不出现在 API 响应中。
- Clerk 集成测试：Token 验证、Webhook 验签与幂等、Clerk 用户删除后的本地状态处理、前端受保护路由和后端独立拒绝未授权请求。
- 登录方式测试：邮箱验证、Google 登录、MetaMask nonce 签名、已有 Clerk 账号绑定钱包、错误链/错误地址签名、钱包解绑和账号关联冲突。
- Web3 证明测试（后续阶段）：同一决策摘要生成稳定 hash；链上证明与链下报告可校验；敏感字段不进入交易 calldata。

验收条件：本地 Docker 启动应用后，在仅配置 Ark、WEEX 虚拟盘凭据以及已有 PostgreSQL/Milvus 连接的情况下，能够完成至少一个决策周期；所有交易和拒单均有审计记录；任何依赖异常不会产生未授权新增仓位。

## 12. 非目标

- 不接入真实账户交易。
- 不部署 PostgreSQL 或 Milvus。
- 不依赖付费新闻、宏观或链上服务。
- 不在首版实现高频盘口策略、自动模型训练或复杂消息队列。
- 不在首版实现付费订阅、账单、组织协作、OAuth 社交登录或多租户计费策略。
- 不在首版实现链上决策证明、钱包连接、Token、托管资金或策略市场；这些属于后续产品迭代路线。
