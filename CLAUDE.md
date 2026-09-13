# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

面向 WEEX 虚拟盘的 AI 合约交易实验平台。核心是「采集 → 确定性信号 + LLM veto → 硬性风控 → 模拟执行」的可审计决策周期，只运行在 WEEX virtual futures 上，不接触实盘资金。

Monorepo 结构：

- `backend/` — FastAPI + worker（Python 3.13，uv 管理）。API 在 `app/main.py`，交易周期在 `app/services/cycle.py`，5 分钟调度器在 `workers/scheduler.py`。
- `frontend/` — Next.js 15（App Router）+ Clerk 鉴权 + React 19。
- `deploy/` — nginx 网关配置；`docker-compose.yml` 统一编排 web/api/worker/gateway。
- `docs/` — `api.md`（接口约定）、`runbook.md`（排障手册）。

PostgreSQL 与 Milvus 是外部已有服务，两个 compose 文件都不会创建它们，通过 `backend/.env` 连接。

## 常用命令

后端（在 `backend/` 下，用 uv）：

```bash
uv sync                      # 安装依赖
uv run alembic upgrade head  # 跑迁移
uv run uvicorn app.main:app --reload --port 8000   # 启动 API
uv run python -m workers.scheduler                  # 启动 worker（5 分钟周期）
uv run pytest                # 全部测试
uv run pytest tests/unit/test_risk.py -q            # 单个测试文件
uv run pytest tests/unit/test_risk.py::test_xxx -q  # 单个测试
uv run ruff check app tests workers
uv run mypy app
```

前端（在 `frontend/` 下）：

```bash
npm run dev        # http://localhost:3000
npm test -- --run  # vitest（单次运行；watch 模式直接 npm test）
npm run typecheck
npm run lint
npm run build
npm run test:e2e   # Playwright（tests/e2e，需要 Clerk 配置）
```

部署：`docker compose up -d --build`；改了 `NEXT_PUBLIC_*` 必须 `docker compose build web && docker compose up -d web`（构建时内联进 bundle，运行期改无效）。

## 决策周期架构

worker 每 5 分钟（`decision_interval_seconds`）对每个启用的虚拟账户、每个 symbol（`BTC-USDT`/`ETH-USDT`）跑一轮 `TradingCycleService.run()`：

1. **采集**：`WeexCollector` 按 `market_timeframes` 拉取默认 5m/1h/4h K 线（每次最多 1000 根）与 ticker 快照，并采集可用的盘口/成交/资金费率/OI。`captured_at` 必须是本地观测时刻——WEEX ticker 的 `closeTime` 是 24h 滚动窗口边界，比当前落后约 12 分钟，用作时间戳会让 90s 新鲜度门永远失败。
2. **决策图**：LangGraph（`app/agents/graph.py`）当前实际执行“行情新鲜度 → 确定性规则信号/仓位/SL/TP → 非 HOLD 三路扇出 → news 分支做 RAG、structure/data-integrity 分支并行 → 合并/校验”。旧的 market / quant / macro / committee 函数仍保留但不在当前编译图路径上。行情/RAG/校验错误进入安全 HOLD；LLM veto 缺失或非法输出按 fail-closed 记录为 `veto_fail_closed`。
3. **风控**：`RiskEngine.evaluate`（`app/risk/engine.py`）是不可变硬上限，模型输出无法覆盖。数据过期、杠杆越界、名义敞口超限、缺止损、日亏损、连亏、日交易额度等任一不过 → 拒绝；熔断信号（日亏损/连亏/权益非正）会暂停账户。CLOSE 单只受账户状态类检查约束，不被敞口/杠杆限制拦截（拦平仓会锁死仓位）。
4. **执行**：`ExecutionService`（`app/execution/service.py`）只接受 `risk_decision.allowed == true` 的提案；下单超时用 `stable_client_order_id` 回查对账，状态未知则返回 `UNKNOWN` 而非重试。
5. **对账**：`ReconciliationService` 从 balance 快照推导已实现盈亏（虚拟盘无成交流水）。

并行还有一条 `DocumentPipeline`（`app/workers/pipeline.py`）：RSS 新闻 + FRED CSV + 美联储 RSS → 清洗去重 → Ark 摘要 → BGE-M3 分块嵌入写 Milvus。RAG 只作为非 HOLD 入场路径的新闻/宏观 veto 证据，不参与规则方向、风控或下单。FRED 月度序列按 `fred_monthly_interval_seconds` 重抓，失败的序列下一轮立刻重试（`_schedule.mark` 只推进成功的）。

## 关键约定与坑

### 三个 env 文件分工

- `./.env` — 仅 docker compose 部署用（`GATEWAY_PORT`、`MODEL_DIR`、前端 `NEXT_PUBLIC_*`）。
- `./backend/.env` — 后端与 worker（本地与容器共用）。
- `./frontend/.env` — 前端本地开发；容器只取它的 `CLERK_SECRET_KEY`。

`WEEX_VIRTUAL_ONLY` 必须保持 `true`；模型文件路径本地指向宿主机，容器内覆盖为 `/models`（只读挂载）。

### WEEX 虚拟盘语义（实测确认，与正式合约盘不同）

**完整的端点清单、实验记录与复验方法见 `docs/weex-virtual-api.md`** —— 改动交易相关代码前先读它。要点：

- **私有端点只有 4 个**：`GET balance`、`GET position/allPosition`、`POST order`、`GET order/history`。无单笔查单（`GET /sim/order` 是 405）、无成交流水（`userTrades` 404）、**无撤单/改单/挂单列表**（`cancelOrder`、`openOrders` 全 404）。`order/history` 只含终态订单。
- **公开行情端点远不止 klines + ticker**：`depth`、`trades`、`fundingRate`、`openInterest` 都可用（早期「虚拟盘无衍生品数据」的结论是错的）。但这些数值疑似合成数据，只可当辅助确认项。`symbol` 必须不带横杠（`BTCUSDT`）。
- **`klines` 单请求上限 1000 根，且 `startTime`/`endTime` 被静默忽略** —— 历史深度就是 1000 根封顶。
- **`ticker/24hr` 返回 11 个字段，代码只用了 2 个**；`closeTime` 是 24h 滚动窗口边界（落后约 750s），**不能当 `captured_at`**，否则 90s 新鲜度门永远失败、一单不下。
- **`slTriggerPrice` 真生效**（价格越过即平仓），**且平仓时随仓自动撤销**，不留残渣。所以「交易所侧灾难止损 + 软件层按需平仓」不会互相打架。
- **taker 费率 0.08%/边**，往返 0.16%。回测必须计入。
- **balance 不含未实现盈亏**：`equity = balance + unrealized_pnl` 是对的；已实现盈亏 = 相邻快照 Δbalance。
- **虚拟盘固定 20x 杠杆且调不了**：`max_leverage` 必须与之一致（默认 20），否则一开仓就再也无法加仓。
- **精度是硬校验，违反直接 500**：quantity 按 `quantityPrecision`（BTC-USDT 为 4）、触发价按 `pricePrecision` 向下舍入；`slTriggerPrice`/`tpTriggerPrice` 也要舍入，别只舍 quantity。触发价方向错误（LONG 挂高于市价的止损）同样回 500。实现见 `app/exchange/weex.py` 的 `_round_down`。
- 私有端点 401 错误码可区分根因：`-1044` key 无效、`-1049` passphrase 不匹配、`-1047` secret 不匹配。用户粘 `WEEX_API_SECRET=` 前缀会导致签名永远失败——保存凭证时已剥前缀（`app/api/routes/accounts.py`）。

### SQLAlchemy 会话（autoflush=False）

`SessionLocal`（`app/db/session.py`）是 `autoflush=False`。父子表只有裸外键列、无 `relationship()` 时，同一 flush 提交可能子先于父插入 → 外键违例。**创建父记录后必须立即 `self.db.flush()`**（见 `DocumentPipeline._process`）。测试默认 `Session(engine)` 是 `autoflush=True`，会掩盖这类问题；写依赖顺序的测试要模拟生产会话 + SQLite `PRAGMA foreign_keys=ON`。worker 共享会话在任何失败后必须 `rollback()`，否则 `PendingRollbackError` 终止进程。

### Alembic 迁移约定

- **新增表要改两处**：`app/db/models.py`（定义）+ 一条 `create_all(checkfirst=True)` 的同步迁移（照 `003`/`007`，两行即可）。不要写 `op.create_table`（会 `DuplicateTable` 事故）。
  ⚠️ 只改模型**不够**：`001` 的 `create_all` 只在库处于 `001` 时跑过一次，已迁移的库不会再执行它。实测只改模型后 `alembic upgrade head` 完，新表依然不存在——**新库因为 `001` 用当前模型才碰巧建出来，这个假象很容易骗过测试**。
- **给已存在的表加列/索引**，`create_all` 不会动已有表，必须写显式增量迁移（照 `002` 模板：`inspect(bind)` 判存在再 `op.add_column`，保证新旧库都可重复执行）。
- 绝不要 `checkfirst=False`。迁移被 DDL 锁卡住时，先查 `pg_stat_activity` 里 `state='idle in transaction'` 的会话（worker 已修掉，但停掉 worker 再跑迁移最稳）。测试见 `backend/tests/integration/test_migrations.py`。

### Clerk 鉴权

- 会话 token **没有 `aud` claim**，`CLERK_AUDIENCE` 必须留空（留空时走 `verify_aud: False`）；填了任何值会让所有请求 401。token 寿命仅 ~60s。
- 前端 `middleware.ts` 保护 dashboard/market/committee/trades/events；业务 API 从 token 的 `sub` 映射本地用户，不接受请求体指定 user id。

### 数据与序列化

- JSON 列存 Decimal（成交均价/已实现盈亏）必须 `model_dump(mode="json")`，否则序列化失败（见 `cycle.py` 的 `_persist`）。
- 查询 JSON 路径判断非空要用 `.as_string().is_not(None)`，直接 `IS NOT NULL` 对 `JSON_QUOTE` 恒真（见 `_daily_trades`）。
- 结构性行情与账户事实**不写进 RAG**：RAG 只存新闻/宏观，行情事实留在数据库供审计。

### 前端

- 所有文案走 `frontend/lib/i18n.tsx` 的 `messages`（zh-CN / en-US 双语，`useI18n()` 取用），控制台 UI 用 `components/console-page.tsx` + `console-primitives.tsx`。新增文案必须同时补两种语言。
- API 请求统一走 `lib/api.ts` 的 `apiRequest`（自动带 Clerk token、401 时换新 token 重试一次）。
- 组件在需要 hooks/状态时标 `"use client"`；页面组件默认 server component。

## 安全边界

生产必须保持虚拟盘：`WEEX_VIRTUAL_ONLY=true`，凭据属于 virtual 账户，不落日志、不进前端环境变量。风控是最终边界：RAG 异常、数据过期、账户不可用、订单状态不确定一律 HOLD/拒绝/UNKNOWN；LLM veto 非法输出记录 `veto_fail_closed` 并安全 HOLD，不自动重试下单。测试接口 `POST /api/test/run-cycle` 只在 `APP_ENV=test` + `X-Test-Exchange: fixture` 时可用，不触网、不发真实订单。
