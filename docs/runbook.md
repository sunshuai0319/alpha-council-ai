# Alpha Council AI 运行手册

## 安全边界

生产配置必须保持 `WEEX_VIRTUAL_ONLY=true`，并使用 WEEX 的 virtual futures 凭据。`ExecutionService` 只接收 `RiskDecision.allowed == true` 的提案；模型异常、RAG 异常、数据过期、账户不可用和订单状态不确定都会进入 HOLD、拒绝或 UNKNOWN，不会自动重试下单。

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

### Ark 超时或返回非法 JSON

Market、Quant、Macro 单个分析失败会退化为 neutral analysis；委员会输出解析失败会生成 `safe-hold`。检查 Ark endpoint、模型名、超时和配额，不要通过放宽 schema 来“修复”交易。

### Milvus 或本地模型不可用

RAG 检索失败会被记录到 cycle errors；证据不足时提案不能开新仓。检查 collection 名称、向量维度（BGE-M3 为 1024）、模型路径和容器网络。重新启动后会按文档 hash 幂等补齐索引。

### Clerk 登录失败或 API 返回 401

确认浏览器使用了当前环境的 publishable key，API 的 `CLERK_JWKS_URL`/issuer 与 Clerk 实例一致，前端请求包含 `Authorization: Bearer <session-token>`。后端以 Clerk `sub` 同步本地 `users`，不接受请求体中的 user id 作为授权依据。

### 风控拒绝、暂停或 UNKNOWN

先查看 Dashboard 的 Risk events 和 `/api/events`。确认数据年龄、杠杆、单品种名义本金、止损、日亏损和连续亏损条件。`UNKNOWN` 订单必须先通过 client order id 对账，不得手工重复提交同一 proposal。

## 暂停与恢复

Dashboard 的 Pause/Resume 会以当前 Clerk 用户为范围写入进程内控制状态。暂停期间周期仍可采集行情，但不会执行新增订单。进程重启后控制状态恢复为 RUNNING；如果需要持久化 kill switch，应在生产化阶段把它写入用户级配置表并由 worker 启动时加载。

## 数据源优先级

优先使用 WEEX、公开 RSS、FRED CSV 和 Federal Reserve 官方 RSS。免费来源只用于研究上下文；交易所实时事实来自 WEEX，不能用新闻摘要覆盖价格、账户余额或仓位。

## 测试环境

`POST /api/test/run-cycle` 只有在 `APP_ENV=test` 且请求头为 `X-Test-Exchange: fixture` 时可用。它使用确定性 `FixtureExchangeClient`，不会访问 WEEX 或发送订单。生产环境即使路径可见，也返回 404。
