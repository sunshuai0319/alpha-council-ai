# Alpha Council AI 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 构建一个使用 WEEX 虚拟盘的、具备免费数据采集、RAG、多 Agent 决策、硬风控、Clerk 鉴权和 Dashboard 的 SaaS-ready 虚拟合约交易平台。

**架构：** 后端采用 FastAPI 模块化单体与独立 Worker，直接连接已经部署的 PostgreSQL 和 Milvus；前端采用 Next.js，通过后端 API 访问全部业务数据。LangGraph 只负责分析和提案编排，确定性风控与 WEEX 执行服务拥有最终控制权。首版不部署 PostgreSQL/Milvus，不接入实盘资金。

**技术栈：** Python 3.13、FastAPI、SQLAlchemy 2、Alembic、Pydantic、LangGraph、LangChain、PostgreSQL、Milvus、PyTorch/FlagEmbedding、Next.js、TypeScript、Clerk、Docker Compose、pytest、Vitest、Playwright。

## 2026-09-10 实现审计进度

> 本表是对照设计规格、代码、测试和运行配置后的任务级进度。状态：**已完成** = 计划目标已具备代码和验证证据；**部署验证** = 需要真实 Clerk/WEEX/Ark/Milvus 凭据的在线检查，不属于本地代码缺口。

| 任务 | 状态 | 审计结论 |
| --- | --- | --- |
| 1. 骨架与外部服务配置 | 已完成 | API/Worker、前端、脱敏环境配置和外部 PostgreSQL/Milvus Compose 约束已实现并通过配置校验。 |
| 2. 数据库模型、迁移与领域类型 | 已完成 | 领域 Schema、主要业务表、自然键/外部 ID 约束和 Alembic 初始迁移已实现。 |
| 3. Clerk 鉴权与用户隔离 | 已完成 | Clerk JWT、JWKS、受保护路由、本地用户同步、Webhook 事件幂等同步、钱包 nonce 单次验证/绑定、虚拟账户引用管理和用户范围查询均已实现。 |
| 4. WEEX、RSS、宏观采集 | 已完成（采集层） | WEEX 虚拟盘适配器、BTC/ETH 5m/1h/4h、RSS、FRED/央行 RSS 和 Fixture 测试已实现；尚未代表真实凭据环境完成在线契约验证。 |
| 5. 文档处理、DeepSeek、BGE/Milvus RAG | 已完成 | 清洗、去重、Ark 摘要、BGE-M3、Reranker、Milvus metadata 检索和 RSS/FRED/Fed RSS Worker 流水线已实现；失败状态和重试次数持久化。 |
| 6. LangGraph Agent Committee | 已完成 | State、分析节点、委员会 JSON Schema、安全降级和版本化 EMA/RSI/ATR/Bollinger/波动率/成交量指标服务均已实现。 |
| 7. 硬风控、虚拟盘执行与对账 | 已完成 | 风控边界、幂等 client order ID、超时查询，以及按用户/虚拟账户持久化订单、成交、持仓、账户和 PnL 对账均已实现并接入周期。 |
| 8. 决策周期、调度器与 API | 已完成 | 决策周期、知识流水线、健康/市场/决策/组合/事件/暂停/恢复/平仓/账户/钱包 API、5 分钟调度、持久化暂停状态和调度失败风险事件均已实现。 |
| 9. Next.js Dashboard 与 Clerk 界面 | 已完成（有命名调整） | Dashboard、轮询、虚拟盘标识、错误/风险展示和控制按钮已实现；实际使用 Next 15 约定的 `middleware.ts`，页面分组名称与原计划不同但功能覆盖一致。 |
| 10. 集成、E2E 与运行文档 | 已完成 | Fixture 虚拟周期、前端组件/E2E、README、Runbook、API 文档和 Compose 校验已完成。 |
| 11. Web3 证明边界留档 | 已完成（文档范围） | DecisionAttestation、链下敏感数据边界和未来 EVM L2 路线已记录，未接入交易路径。 |

### 审计结论与后续优先级

当前已经完成“WEEX 虚拟行情 → Agent 提案 → 硬风控 → Fixture/虚拟执行 → 决策展示”的可验证闭环，适合作为本地 Demo 和继续开发基线；不应将当前状态描述为完整生产 SaaS 或完整自动化数据平台。

本次审计补齐项已全部实现。真实外部服务连接仍需部署凭据验证，不作为本地实现阻塞。

本次审计验证：后端 `47 passed`，Ruff 通过；前端组件测试 `4 passed`、TypeScript、ESLint、Next build 和 Playwright `1 passed`；既有 Alembic SQLite smoke test 与 Compose 配置校验沿用通过结果。

### 2026-09-10 步骤完成标记

- [x] 任务 3：Clerk Webhook 幂等同步、钱包 nonce 单次验证、虚拟账户 API 与用户隔离。
- [x] 任务 5：RSS/FRED/Fed RSS → 清洗 → DeepSeek 摘要 → BGE-M3 → Milvus，失败状态和重试次数落库。
- [x] 任务 6：EMA/RSI/ATR/Bollinger/波动率/成交量确定性指标及版本审计。
- [x] 任务 7：订单/成交/持仓/账户快照/PnL 对账持久化与周期接入。
- [x] 任务 8：持久化暂停、调度失败风险事件和知识流水线调度接入。
- [x] 验证：后端全量 `47 passed`、Ruff 通过；Mypy 修复后执行最终确认。

---

## 文件清单与职责

### 后端

- 创建：`backend/pyproject.toml`、`backend/uv.lock`、`backend/Dockerfile`、`backend/docker-compose.yml`、`backend/.env.example`、`backend/.gitignore`：Python 依赖、应用镜像、Worker/API 启动方式和脱敏配置。
- 创建：`backend/app/config.py`、`backend/app/logging.py`、`backend/app/main.py`：配置校验、结构化日志、FastAPI 入口和健康检查。
- 创建：`backend/app/db/session.py`、`backend/app/db/models.py`、`backend/alembic.ini`、`backend/alembic/env.py`、`backend/alembic/versions/001_initial_schema.py`：数据库连接、ORM 模型和迁移。
- 创建：`backend/app/domain/schemas.py`、`backend/app/domain/enums.py`：跨模块共享的交易、分析、风控和鉴权类型。
- 创建：`backend/app/auth/clerk.py`、`backend/app/auth/dependencies.py`、`backend/app/auth/webhooks.py`：Clerk Token 验证、用户解析、业务鉴权和 Webhook 同步。
- 创建：`backend/app/exchange/base.py`、`backend/app/exchange/weex.py`、`backend/app/exchange/fixtures.py`：交易所接口、WEEX 虚拟盘实现和契约 Fixture。
- 创建：`backend/app/collectors/weex.py`、`backend/app/collectors/rss.py`、`backend/app/collectors/macro.py`：WEEX、RSS、FRED/官方宏观数据采集。
- 创建：`backend/app/processing/documents.py`、`backend/app/processing/summarizer.py`、`backend/app/processing/embeddings.py`：文档清洗去重、Ark 摘要和本地向量化。
- 创建：`backend/app/rag/retriever.py`、`backend/app/rag/reranker.py`：Milvus 过滤召回和 BGE 重排。
- 创建：`backend/app/agents/state.py`、`backend/app/agents/prompts.py`、`backend/app/agents/nodes.py`、`backend/app/agents/graph.py`：TradingCycleState、Prompt、Agent 节点和 LangGraph。
- 创建：`backend/app/risk/engine.py`、`backend/app/execution/service.py`、`backend/app/reconciliation/service.py`：硬风控、幂等下单和订单对账。
- 创建：`backend/app/services/cycle.py`、`backend/app/services/portfolio.py`、`backend/app/services/events.py`：决策周期、组合查询和审计查询。
- 创建：`backend/app/api/routes/health.py`、`backend/app/api/routes/market.py`、`backend/app/api/routes/decisions.py`、`backend/app/api/routes/portfolio.py`、`backend/app/api/routes/control.py`、`backend/app/api/routes/auth.py`：业务 API。
- 创建：`backend/workers/scheduler.py`、`backend/workers/tasks.py`：采集、处理、决策和同步调度。
- 创建：`backend/tests/` 下对应单元、集成、契约和安全测试文件：锁定各模块行为。

### 前端

- 创建：`frontend/package.json`、`frontend/next.config.ts`、`frontend/Dockerfile`、`frontend/docker-compose.yml`、`frontend/.env.example`、`frontend/.gitignore`：Next.js 应用、容器和 Clerk/API 配置。
- 创建：`frontend/app/layout.tsx`、`frontend/app/(auth)/sign-in/[[...sign-in]]/page.tsx`、`frontend/app/(auth)/sign-up/[[...sign-up]]/page.tsx`、`frontend/proxy.ts`：Clerk Provider、登录注册页面和路由保护。
- 创建：`frontend/lib/api.ts`、`frontend/lib/types.ts`、`frontend/components/`：带 Clerk Token 的 API 客户端、前端类型和复用组件。
- 创建：`frontend/app/(dashboard)/page.tsx`、`market/page.tsx`、`decisions/page.tsx`、`trading/page.tsx`、`events/page.tsx`：Dashboard 页面。
- 创建：`frontend/tests/` 下 Vitest 与 Playwright 测试：组件、鉴权和关键流程测试。

### 文档

- 修改：`README.md`：本地启动、配置复制、虚拟盘声明、数据源和测试命令。
- 创建：`docs/runbook.md`、`docs/api.md`：运行手册、故障处理和 API 约定。

## 任务 1：建立后端/前端骨架与外部服务配置【已完成】

**文件：** 上述项目配置文件、`backend/app/config.py`、`frontend/app/layout.tsx`。

- [ ] **步骤 1：编写后端配置测试**

```python
# backend/tests/unit/test_config.py
from app.config import Settings


def test_settings_require_external_service_urls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/a")
    monkeypatch.setenv("MILVUS_URI", "http://milvus:19530")
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    settings = Settings()
    assert settings.milvus_uri == "http://milvus:19530"
    assert settings.postgres_url.startswith("postgresql+psycopg://")
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/unit/test_config.py -q`

预期：因 `app.config` 尚不存在而失败。

- [ ] **步骤 3：实现配置和启动骨架**

`Settings` 使用 Pydantic Settings，字段包括 `DATABASE_URL`、`MILVUS_URI`、`MILVUS_COLLECTION`、`EMBEDDING_MODEL_PATH`、`RERANKER_MODEL_PATH`、`ARK_API_KEY`、`ARK_BASE_URL`、`ARK_MODEL=deepseek-v4-pro-ga-260813`、`WEEX_BASE_URL`、`CLERK_JWKS_URL`、`CLERK_SECRET_KEY` 和风控默认值。WEEX 私有凭证由用户在控制台创建虚拟账户时保存和使用。启动时只校验格式和必需配置，不打印密钥。

后端 Compose 仅定义 `api` 与 `worker`，通过环境变量连接外部 PostgreSQL/Milvus；前端 Compose 仅定义 `web`，通过 `NEXT_PUBLIC_API_BASE_URL` 调用后端。

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && uv run pytest tests/unit/test_config.py -q`

预期：`1 passed`。

- [ ] **步骤 5：Commit**

```bash
git add backend frontend
git commit -m "chore: scaffold application services"
```

## 任务 2：建立数据库模型、迁移和共享领域类型【已完成】

**文件：** `backend/app/db/*`、`backend/app/domain/*`、`backend/alembic/*`、`backend/tests/unit/test_domain.py`。

- [ ] **步骤 1：编写领域类型和幂等约束测试**

```python
def test_candle_identity_is_symbol_timeframe_open_time():
    from app.domain.schemas import Candle

    candle = Candle(symbol="BTC-USDT", timeframe="5m", open_time=1700000000,
                    open=1, high=2, low=0.5, close=1.5, volume=10)
    assert candle.identity == ("BTC-USDT", "5m", 1700000000)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/unit/test_domain.py -q`

预期：因共享 Schema 尚不存在而失败。

- [ ] **步骤 3：实现领域类型与 ORM**

定义 `Candle`、`MarketSnapshot`、`TradeProposal`、`RiskDecision`、`ExecutionResult`、`TradingCycleState` 等 Pydantic 类型；定义 `users`、`wallet_addresses`、`trading_accounts`、`market_candles`、`market_snapshots`、`account_snapshots`、`positions`、`orders`、`fills`、`trading_decisions`、`pnl_snapshots`、`risk_events`、`source_documents`、`document_summaries`。所有用户业务表包含 `user_id`，交易数据按自然键和外部订单 ID 建立唯一约束。

- [ ] **步骤 4：运行迁移和测试**

运行：`cd backend && uv run alembic upgrade head && uv run pytest tests/unit/test_domain.py -q`

预期：迁移成功，`1 passed`。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/db backend/app/domain backend/alembic
git commit -m "feat: add trading domain schema"
```

## 任务 3：实现 Clerk 鉴权、用户同步和用户隔离【部分完成】

**文件：** `backend/app/auth/*`、`backend/app/api/routes/auth.py`、`backend/tests/security/test_auth.py`、前端 Clerk 文件。

- [ ] **步骤 1：编写鉴权失败测试**

```python
def test_business_route_rejects_missing_clerk_user(client):
    response = client.get("/api/portfolio")
    assert response.status_code == 401


def test_user_scope_never_returns_other_users_orders(order_repo, user_a, user_b):
    order_repo.create(user_id=user_b.id, external_id="b-1")
    assert order_repo.list_for_user(user_a.id) == []
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/security/test_auth.py -q`

预期：因鉴权依赖和用户范围查询尚未实现而失败。

- [ ] **步骤 3：实现 Clerk 验证和 Webhook**

实现 `get_current_user(request)`：验证 Clerk Session Token，提取 `sub` 作为 `clerk_user_id`，按需创建本地用户。实现 `require_user` FastAPI Dependency；所有业务服务只接收已解析的本地 `user_id`，禁止使用请求体中的用户 ID 作为授权依据。实现 Clerk Webhook 验签、事件去重和用户创建/更新/删除同步。前端使用 `@clerk/nextjs` 的 Provider、`clerkMiddleware()`、`SignIn`、`SignUp`、`UserButton`；`lib/api.ts` 用 Clerk `getToken()` 添加 Bearer Token。

- [ ] **步骤 4：运行安全测试确认通过**

运行：`cd backend && uv run pytest tests/security/test_auth.py -q`

预期：未认证请求为 `401`，跨用户订单查询为空，Webhook 重复事件只产生一条本地变更。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/auth backend/app/api/routes/auth.py backend/tests/security frontend
git commit -m "feat: add Clerk authentication and isolation"
```

## 任务 4：实现 WEEX、RSS 和宏观数据采集【已完成（采集层）】

**文件：** `backend/app/exchange/*`、`backend/app/collectors/*`、`backend/tests/contract/*`、`backend/tests/unit/test_collectors.py`。

- [ ] **步骤 1：编写固定 Fixture 测试**

```python
def test_weex_candle_response_maps_to_candle(weex_client):
    candle = weex_client.parse_candle([1700000000000, "1", "2", "0.5", "1.5", "10"])
    assert candle.symbol == "BTC-USDT"
    assert candle.timeframe == "5m"
    assert candle.close == 1.5
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/contract tests/unit/test_collectors.py -q`

预期：因 ExchangeClient 与采集器尚未实现而失败。

- [ ] **步骤 3：实现适配器与采集任务**

先查阅 WEEX 当前官方 API 文档，锁定虚拟盘环境、签名、限流、K 线字段、订单状态和查询接口；把 URL、请求参数和响应映射集中在 `weex.py`。实现公共行情、虚拟盘账户、订单和成交同步。RSS 使用 `feedparser`，支持固定来源和 Google News 关键词 URL；按 canonical URL 与内容 hash 去重。宏观采集使用 FRED 公开下载接口和 Federal Reserve 官方日历/RSS。所有采集函数返回统一的 `CollectorResult`，失败记录错误而不终止其他来源。

- [ ] **步骤 4：运行采集测试确认通过**

运行：`cd backend && uv run pytest tests/contract tests/unit/test_collectors.py -q`

预期：Fixture 映射、RSS 去重和宏观字段标准化测试通过；测试不发送真实下单请求。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/exchange backend/app/collectors backend/tests/contract backend/tests/unit/test_collectors.py
git commit -m "feat: add market news and macro collectors"
```

## 任务 5：实现文档处理、DeepSeek 摘要与 BGE/Milvus RAG【部分完成】

**文件：** `backend/app/processing/*`、`backend/app/rag/*`、`backend/tests/unit/test_documents.py`、`backend/tests/integration/test_rag.py`。

- [ ] **步骤 1：编写文档和检索失败测试**

```python
def test_same_url_or_content_hash_is_inserted_once(document_repo):
    first = document_repo.upsert(url="https://example.com/a", content="same")
    second = document_repo.upsert(url="https://example.com/a", content="same")
    assert first.id == second.id


def test_retriever_applies_asset_filter_before_rerank(fake_milvus, reranker):
    results = retrieve("BTC ETF", asset="BTC", limit=3)
    assert all(item.asset == "BTC" for item in results)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/unit/test_documents.py tests/integration/test_rag.py -q`

预期：因文档处理和 RAG 模块尚不存在而失败。

- [ ] **步骤 3：实现处理链**

实现 URL 规范化、正文清洗、语言/资产/事件标签和 hash 去重。调用 Ark OpenAI-compatible endpoint，模型固定为 `deepseek-v4-pro-ga-260813`，使用 Pydantic Schema 解析摘要字段：`summary`、`event_type`、`assets`、`direction`、`impact_horizon`、`confidence`。LLM 超时保留原文并写失败状态。使用本地 `EMBEDDING_MODEL_PATH` 加载 BGE-M3，使用 `RERANKER_MODEL_PATH` 加载 BGE-Reranker-v2-M3；Milvus collection 通过配置读取，写入指定标量字段。召回流程为 metadata filter → BGE-M3 top-k → reranker top-n → 带 URL 的证据对象。

- [ ] **步骤 4：运行离线和连接测试**

运行：`cd backend && uv run pytest tests/unit/test_documents.py -q`

预期：离线清洗、去重、摘要 Schema、失败重试测试通过。连接测试命令：`DATABASE_URL=... MILVUS_URI=... uv run pytest tests/integration/test_rag.py -q`，预期 Milvus 可用时通过，不可用时明确报告连接失败。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/processing backend/app/rag backend/tests/unit/test_documents.py backend/tests/integration/test_rag.py
git commit -m "feat: add document processing and rag retrieval"
```

## 任务 6：实现 LangGraph Agent Committee【部分完成】

**文件：** `backend/app/agents/*`、`backend/tests/unit/test_agents.py`、`backend/tests/integration/test_graph.py`。

- [ ] **步骤 1：编写 Agent 协议测试**

```python
def test_invalid_committee_json_becomes_hold(fake_llm, graph_state):
    fake_llm.response = "not-json"
    result = run_committee(graph_state, fake_llm)
    assert result.action == "HOLD"


def test_graph_contains_parallel_analysis_nodes():
    graph = build_trading_cycle_graph()
    assert {"market_node", "quant_node", "macro_node"}.issubset(graph.node_names)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/unit/test_agents.py tests/integration/test_graph.py -q`

预期：因 State、节点和图尚不存在而失败。

- [ ] **步骤 3：实现 State、节点和路由**

定义 `TradingCycleState`，实现 `load_context`、`validate_freshness`、`market_node`、`quant_node`、`macro_node`、`retrieve_evidence`、`committee_node`、`proposal_validator`、`safe_hold` 和 `persist_decision`。Market/Quant/Macro 并行运行；Committee 仅输出 `TradeProposal`，不绑定工具；解析失败、证据不足、冲突和超时统一进入 `safe_hold`。Prompt 中明确实时价格和账户数据不从 RAG 获取，输出必须包含 `proposal_id`、`action`、`symbol`、`side`、`position_size_pct`、`leverage`、`stop_loss`、`take_profit`、`valid_until`、`invalidation_conditions` 和 `evidence_refs`。配置的模型为 `deepseek-v4-pro-ga-260813`。

- [ ] **步骤 4：运行 Agent 测试确认通过**

运行：`cd backend && uv run pytest tests/unit/test_agents.py tests/integration/test_graph.py -q`

预期：固定输入、空证据、过期数据、冲突意见和非法 JSON 均按协议得到安全结果。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/agents backend/tests/unit/test_agents.py backend/tests/integration/test_graph.py
git commit -m "feat: add langgraph trading committee"
```

## 任务 7：实现确定性风控、虚拟盘执行与对账【部分完成】

**文件：** `backend/app/risk/*`、`backend/app/execution/*`、`backend/app/reconciliation/*`、`backend/tests/unit/test_risk.py`、`backend/tests/integration/test_execution.py`。

- [ ] **步骤 1：编写风控失败测试**

```python
def test_risk_rejects_position_above_equity_limit():
    decision = evaluate_risk(equity=10000, current_notional=1500,
                             proposed_notional=1000, leverage=2,
                             stop_loss=95, entry=100, daily_loss_pct=0,
                             consecutive_losses=0, paused=False, data_age_s=5)
    assert decision.allowed is False
    assert "max_notional" in decision.reasons


def test_order_timeout_queries_before_retry(fake_exchange):
    fake_exchange.place_order_timeout_once = True
    result = execute_idempotently(fake_exchange, proposal_id="p-1")
    assert fake_exchange.place_order_calls == 1
    assert fake_exchange.query_order_calls == 1
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/unit/test_risk.py tests/integration/test_execution.py -q`

预期：因 RiskEngine 和执行服务尚不存在而失败。

- [ ] **步骤 3：实现硬规则和执行服务**

实现最大杠杆 3x、单交易对权益 20%、单笔风险 0.5%、必须止损、单日亏损 5% 熔断、连续 3 次亏损冷却、数据新鲜度、全局 `PAUSED` 和订单状态不确定拒绝新增仓位。`ExecutionService` 只接收 `allowed=True` 的 `RiskDecision`，生成稳定 client order ID；超时先查询，查询不到才记录 `UNKNOWN`，不重复下单。`ReconciliationService` 拉取订单/成交/仓位，按外部 ID 幂等写库并计算 PnL。

- [ ] **步骤 4：运行风控与执行测试确认通过**

运行：`cd backend && uv run pytest tests/unit/test_risk.py tests/integration/test_execution.py -q`

预期：所有风控边界通过，任何拒绝路径不调用下单方法。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/risk backend/app/execution backend/app/reconciliation backend/tests/unit/test_risk.py backend/tests/integration/test_execution.py
git commit -m "feat: add hard risk gate and demo execution"
```

## 任务 8：实现决策周期、调度器和后端 API【部分完成】

**文件：** `backend/app/services/*`、`backend/app/api/routes/*`、`backend/workers/*`、`backend/tests/api/*`。

- [ ] **步骤 1：编写 API 和调度失败测试**

```python
def test_pause_endpoint_changes_control_state(authenticated_client):
    response = authenticated_client.post("/api/control/pause")
    assert response.status_code == 200
    assert response.json()["status"] == "PAUSED"


def test_cycle_failure_persists_hold_decision(cycle_service, failing_llm):
    result = cycle_service.run(user_id="u-1", llm=failing_llm)
    assert result.action == "HOLD"
    assert result.persisted is True
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/api -q`

预期：因路由和调度服务尚不存在而失败。

- [ ] **步骤 3：实现服务与路由**

实现 `TradingCycleService.run(user_id, symbol)`：装载上下文、运行图、执行风控、同步结果和保存 trace。实现 `GET /api/health`、`/market`、`/decisions`、`/portfolio`、`/events`，以及暂停、恢复、人工平仓接口；所有查询按当前 Clerk 用户过滤。Worker 使用 APScheduler 每 5 分钟对启用的用户/交易账户触发 BTC/ETH 周期；任务失败写 `risk_events` 并保持系统安全状态。应用入口注册路由、CORS、错误映射和结构化日志。

- [ ] **步骤 4：运行 API 测试确认通过**

运行：`cd backend && uv run pytest tests/api -q`

预期：认证 API、用户隔离、暂停/恢复、错误映射和决策持久化测试通过。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services backend/app/api backend/workers backend/tests/api
git commit -m "feat: add trading cycle api and workers"
```

## 任务 9：实现 Next.js Dashboard 与 Clerk 登录界面【已完成（命名调整）】

**文件：** `frontend/app/*`、`frontend/components/*`、`frontend/lib/*`、`frontend/tests/*`。

- [ ] **步骤 1：编写关键页面测试**

```tsx
it("labels the product as virtual futures", () => {
  render(<OverviewPage />)
  expect(screen.getByText("WEEX Virtual Futures")).toBeInTheDocument()
})

it("shows a rejected decision reason", () => {
  render(<DecisionCard decision={{ action: "HOLD", riskReason: "max_notional" }} />)
  expect(screen.getByText("max_notional")).toBeInTheDocument()
})
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd frontend && npm test -- --run`

预期：因 Next.js 页面和组件尚不存在而失败。

- [ ] **步骤 3：实现页面与 API 客户端**

在 `layout.tsx` 的 `<body>` 内放置 `ClerkProvider`；使用 `proxy.ts` 保护 Dashboard 路由。实现总览、市场、AI 委员会、交易、事件审计页面。API 客户端从 Clerk 获取 Token，错误状态显示数据新鲜度和安全提示。暂停、恢复、手动平仓按钮必须二次确认，并展示虚拟盘标识；首版使用 5-10 秒轮询，不实现 WebSocket。

- [ ] **步骤 4：运行前端单测确认通过**

运行：`cd frontend && npm test -- --run`

预期：组件测试通过，虚拟盘标识、拒单原因和错误状态可见。

- [ ] **步骤 5：Commit**

```bash
git add frontend
git commit -m "feat: add authenticated trading dashboard"
```

## 任务 10：补齐集成、端到端测试和运行文档【已完成】

**文件：** `backend/tests/e2e/test_virtual_cycle.py`、`frontend/tests/e2e/auth.spec.ts`、`README.md`、`docs/runbook.md`、`docs/api.md`。

- [ ] **步骤 1：编写完整虚拟盘 Smoke Test**

```python
def test_virtual_cycle_from_health_to_dashboard(api, fake_weex, test_user):
    assert api.get("/api/health", user=test_user).status_code == 200
    cycle = api.post("/api/test/run-cycle", user=test_user,
                     headers={"X-Test-Exchange": "fixture"})
    assert cycle.json()["risk_status"] in {"ALLOWED", "REJECTED"}
    assert api.get("/api/decisions", user=test_user).json()["items"]
```

- [ ] **步骤 2：运行 Smoke Test 确认当前差距**

运行：`cd backend && uv run pytest tests/e2e/test_virtual_cycle.py -q`

预期：在 API、Worker、Fixture 和 Dashboard 全部接通前失败；失败信息用于定位未接线模块。

- [ ] **步骤 3：实现测试 Fixture、文档和启动检查**

提供不触发真实订单的 `FixtureExchangeClient` 和测试环境开关；实现 `/api/test/run-cycle` 仅在测试环境启用。README 写明：复制 `backend/.env.example` 与 `frontend/.env.example`，PostgreSQL/Milvus 使用外部部署，模型路径要求本地挂载，WEEX 必须是虚拟盘。Runbook 写明 WEEX、Ark、Milvus、Embedding、Clerk 故障时的安全行为；API 文档写明请求、响应和认证头。

- [ ] **步骤 4：运行完整验证**

运行：

```bash
cd backend && uv run pytest
cd ../frontend && npm test -- --run
cd .. && docker compose -f backend/docker-compose.yml config
cd .. && docker compose -f frontend/docker-compose.yml config
```

预期：后端测试全部通过，前端测试全部通过，两个 Compose 配置通过校验且没有 PostgreSQL/Milvus service 定义。连接到已有服务后，Smoke Test 完成一个决策周期；依赖异常时只能产生 `HOLD`/拒绝结果。

- [ ] **步骤 5：Commit**

```bash
git add backend/tests/e2e frontend/tests/e2e README.md docs
git commit -m "test: verify virtual trading workflow"
```

## 任务 11：产品化 Web3 证明边界的设计留档【已完成（文档范围）】

**文件：** `docs/web3-roadmap.md`、`backend/app/web3/README.md`。

- [ ] **步骤 1：记录链上证明接口，不接入交易路径**

定义链下 `DecisionAttestation` 数据结构：`decision_id`、`report_hash`、`created_at`、`system_version`、`report_uri`；明确原文、用户数据、行情和 WEEX 凭据不进入 calldata。文档说明未来使用 EVM L2 的 `DecisionAttestation` 合约登记 hash，当前版本不部署合约、不连接用户资金。

- [ ] **步骤 2：Commit**

```bash
git add docs/web3-roadmap.md backend/app/web3/README.md
git commit -m "docs: define verifiable trading roadmap"
```

## 计划自检

### 规格覆盖度

- 虚拟盘、BTC/ETH、5m/1h/4h 和 5 分钟周期：任务 4、6、7、8、10。
- 免费 WEEX/RSS/FRED/官方宏观数据：任务 4。
- PostgreSQL/Milvus 外部部署：任务 1、5、10。
- BGE-M3、BGE-Reranker-v2-M3、Ark DeepSeek 模型：任务 1、5、6。
- LangGraph Agent 图、状态、Schema 和安全降级：任务 6。
- 硬风控、幂等执行、对账和审计：任务 2、7、8。
- Clerk 邮箱、Google、MetaMask、用户隔离和 Webhook：任务 3、9、10。
- Dashboard、API 和轮询：任务 8、9。
- Web3 可验证决策、钱包扩展边界和非托管路线：任务 11。
- 单元、契约、集成、鉴权、前端和端到端验收：任务 2-10。

### 完整性与一致性检查

- 每个实现步骤都有明确文件、命令、预期结果或代码契约，没有未定义的任务引用。
- 共享用户标识统一为 Clerk `clerk_user_id` → 本地 `users.id`；业务服务统一接收本地 `user_id`。
- 委员会输出统一为 `TradeProposal`；风控输出统一为 `RiskDecision`；执行结果统一为 `ExecutionResult`。
- PostgreSQL/Milvus 只作为外部配置，不出现在任一 Compose service 中。
- 所有新增仓位都经过 `deterministic_risk_node` 和 `ExecutionService`，失败默认 `HOLD`。

### 执行顺序

任务 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10；任务 11 可在任务 2 完成后独立提交，但不阻塞虚拟交易闭环。
