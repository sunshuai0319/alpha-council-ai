# Alpha Council AI

面向 WEEX 虚拟盘的 AI 合约交易实验平台。系统把实时市场数据、免费新闻/宏观数据、本地 BGE-M3 + BGE-Reranker-v2-M3 检索和火山 Ark `deepseek-v4-pro-ga-260813` 委员会串成一个可审计的决策周期。

> 当前版本只允许 WEEX virtual futures。所有订单都经过确定性风控，默认只在虚拟盘执行；不接触实盘资金，也不托管用户资产。

## 组成

```text
Next.js + Clerk
        │ Bearer token
FastAPI ── TradingCycleService ── WEEX virtual API
   │               │
   ├── PostgreSQL  ├── LangGraph: market / quant / macro / committee
   ├── Milvus      └── RiskEngine → ExecutionService → reconciliation
   └── Worker: 5-minute cycle for enabled virtual accounts
```

PostgreSQL 和 Milvus 已按项目约束作为外部服务使用，两个 Compose 文件不会创建它们。

## 本地启动

要求：Python 3.13、uv、Node.js 22、npm，以及已经运行的 PostgreSQL 和 Milvus。模型文件需在运行后端的机器上可访问。

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
```

将 `backend/.env` 中的 PostgreSQL、Milvus、Ark、WEEX virtual 和本地模型路径替换为实际值。`WEEX_VIRTUAL_ONLY=true` 必须保持开启。Milvus 的 URI、账号、collection 可直接参考 `treasury-sentinel` 的 env 配置；不要把密钥提交到仓库。

启动 API：

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

另开终端启动 worker：

```bash
cd backend
uv run python -m workers.scheduler
```

启动前端：

```bash
cd frontend
npm install
npm run dev
```

Clerk Dashboard 中启用邮箱和 Google 登录；钱包登录按 Clerk 支持的 wallet 配置启用。前端默认地址为 `http://localhost:3000`，API 默认地址为 `http://localhost:8000/api`。

## 免费数据源

- WEEX：K 线、ticker、合约、虚拟盘账户、订单和成交。
- RSS：CoinDesk、Cointelegraph、Bitcoin Magazine，也可以配置 Google News RSS URL。
- FRED：公开 CSV 序列（默认包含 FEDFUNDS、CPIAUCSL、UNRATE、DFF、DGS10）。
- Federal Reserve 官方 RSS：宏观事件补充来源。

新闻正文先清洗、规范化 URL、内容 hash 去重，再由 Ark 摘要；结构化行情和账户事实不写进 RAG。

## 验证

```bash
cd backend
uv run pytest
uv run ruff check app tests workers
uv run mypy app

cd ../frontend
npm test -- --run
npm run typecheck
npm run lint
npm run build
```

API fixture smoke test 只在测试环境开启，不发送真实请求：

```bash
cd backend
APP_ENV=test uv run pytest tests/e2e/test_virtual_cycle.py -q
```

更多启动故障处理见 [`docs/runbook.md`](docs/runbook.md)，接口约定见 [`docs/api.md`](docs/api.md)。
