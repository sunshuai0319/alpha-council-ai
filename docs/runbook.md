# Alpha Council AI 运行手册

## 安全边界

生产配置必须保持 `WEEX_VIRTUAL_ONLY=true`，并使用 WEEX 的 virtual futures 凭据。`ExecutionService` 只接收 `RiskDecision.allowed == true` 的提案；RAG 异常、数据过期、账户不可用和订单状态不确定都会进入 HOLD、拒绝或 UNKNOWN。LLM veto 的非法输出按默认 fail-closed 处理并记录 `veto_fail_closed`，不会自动重试下单。

当前 agent、LangGraph 图、RAG 边界、采集标注和止盈止损生命周期见
[`docs/current-architecture.md`](current-architecture.md)。该文档区分了已实现代码与仍需部署/校准的事项。

## 启动检查

1. `GET /api/health` 返回 `status=ok` 且 `weex_mode=virtual`。
2. PostgreSQL 已执行 `uv run alembic upgrade head`。
3. `MILVUS_URI`、`MILVUS_COLLECTION` 和本地 embedding/reranker 路径可访问。
4. Ark 模型为 `deepseek-v4-pro-ga-260813`，API key 只存在后端环境变量。
5. Clerk publishable key、JWKS URL、issuer 和 webhook signing secret 已配置。
6. WEEX API key、secret、passphrase 属于 virtual 账户，且未出现在日志或前端环境变量中。

## 故障处理

### WEEX 行情不可用

Collector 按 symbol/timeframe 隔离错误；本轮状态带有错误，图会生成安全 HOLD。检查 `WEEX_BASE_URL`、网络、限流和合约 symbol（例如 `BTC-USDT` 在 virtual API 中映射为 `BTCSUSDT`）。恢复后 worker 在下一周期重新采集。

**WEEX 会间歇性返回 503**（`order/history` 与 `position/allPosition` 都观测到过），
属于对端抖动，不是本系统的问题——重试通常即可。**注意不要让它熔断账户**：早期版本把
外部 5xx 计入 `max_consecutive_failures`，7 次就把账户 PAUSED 了，需要人工恢复。
外部故障应与策略连亏分开计数。

排查接口行为（哪些端点存在、触发单是否生效、精度怎么校验）先看
`docs/weex-virtual-api.md`，里面有实测记录和复验方法。用 `curl` 探测时记住
**404 = 路径不存在，400/401/405 = 路径存在但用法不对**。

### Ark 超时或返回非法 JSON

Market、Quant、Macro 单个分析失败会退化为 neutral analysis；委员会输出解析失败会生成 `safe-hold`。检查 Ark endpoint、模型名、超时和配额，不要通过放宽 schema 来“修复”交易。

### Milvus 或本地模型不可用

RAG 检索失败会被记录到 cycle errors 并安全 HOLD；正常没有命中反向材料不阻塞规则入场，
Retriever 会从币种级证据回退到空资产标签的通用市场/宏观材料。排查时检查 collection 名称、
向量维度（BGE-M3 为 1024）、BGE-M3 与 BGE-Reranker-v2-M3 模型路径和容器网络。重新启动后
会按文档 hash 幂等补齐索引。标准日志中 `milvus search start/complete` 能确认是否调用，
`rag retrieval search` 能区分原始命中与应用过滤后的候选，`rag reranker start/complete`
能确认是否真正进入重排序；`milvus insert` 的 `asset_counts` 可用于核对采集标注分布。

若发现某个交易品种长期为 0 条，先查 PostgreSQL `document_summaries.assets` 和日志中的
`asset_counts`，不要先调大向量召回范围。当前代码已覆盖长尾资产、LLM 漏标 union、多资产展开
和期限归一化；线上 v1 历史数据仍需显式回填，重启不会自动修复。本次已完成 v2 回填（116 篇、
175 行、失败 0），旧 v1 保留作为回滚基线；后续新增文档按 v2 结构写入。使用新 collection 做校验：

```bash
cd backend
uv run python scripts/reindex_documents.py --dry-run
uv run python scripts/reindex_documents.py --target-collection alpha_council_documents_bge_m3_v2
```

脚本只读 PostgreSQL 和旧 collection，向新 v2 collection upsert，不清空源数据或旧 collection。
回填后检查 `asset_scope`、`schema_version`、每个交易品种的行数；当前 `.env` 已切换到 v2，必须
重启长驻 API/worker 进程后才会实际加载新 collection。

### Clerk 登录失败或 API 返回 401

确认浏览器使用了当前环境的 publishable key，API 的 `CLERK_JWKS_URL`/issuer 与 Clerk 实例一致，前端请求包含 `Authorization: Bearer <session-token>`。后端以 Clerk `sub` 同步本地 `users`，不接受请求体中的 user id 作为授权依据。

### 风控拒绝、暂停或 UNKNOWN

先查看 Dashboard 的 Risk events 和 `/api/events`。确认数据年龄、杠杆、单品种名义本金、止损、日亏损和连续亏损条件。`UNKNOWN` 订单必须先通过 client order id 对账，不得手工重复提交同一 proposal。

### 仓位长期停留在 1.8R/1.9R

当前默认目标是 `2R`，开仓时交易所侧会同时挂静态 TP 和 `3R` 灾难止损，本地
`PositionManager` 仍负责动态止损和 TP 兜底。达到 `1.8R` 后会锁存 near-target 时间，
超过 6 小时仍未达到 `2R` 会退出；所有仓位另有 72 小时最大持仓时长。若仓位仍长期占用
保证金，先检查 `EXCHANGE_TAKE_PROFIT_ENABLED`、WEEX `position/allPosition`、本地
`positions` 和最近的 reconciliation；重点确认交易所实际仓位是否已归零。`UNKNOWN/OPEN`
不会提前把本地仓位标为 `CLOSED`，下一次对账确认后再收敛。

## 暂停与恢复

Dashboard 的 Pause/Resume 会以当前 Clerk 用户为范围写入进程内控制状态。暂停期间周期仍可采集行情，但不会执行新增订单。进程重启后控制状态恢复为 RUNNING；如果需要持久化 kill switch，应在生产化阶段把它写入用户级配置表并由 worker 启动时加载。

## 数据源优先级

优先使用 WEEX、公开 RSS、FRED CSV 和 Federal Reserve 官方 RSS。免费来源只用于研究上下文；交易所实时事实来自 WEEX，不能用新闻摘要覆盖价格、账户余额或仓位。

## 测试环境

`POST /api/test/run-cycle` 只有在 `APP_ENV=test` 且请求头为 `X-Test-Exchange: fixture` 时可用。它使用确定性 `FixtureExchangeClient`，不会访问 WEEX 或发送订单。生产环境即使路径可见，也返回 404。
