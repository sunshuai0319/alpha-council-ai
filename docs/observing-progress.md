# 观察进展

想快速看系统在干什么时，先看这份。

## 启动与停止

```bash
# 后端 API（前端要用）
cd backend && uv run uvicorn app.main:app --reload --port 8000

# 交易 worker（基准间隔 5 分钟；上一轮完整执行结束后计时，**必须重启才会加载新配置与新代码**）
cd backend && uv run python -m workers.scheduler
```

worker 是长驻进程：**改了 `.env`（尤其策略周期）必须重启它**，否则跑的还是旧配置。

## 每次看这几条就够

`psql` 或任意数据库客户端，连 `alpha_council_ai` 库：

```sql
-- 1. 最近决策：信号有没有出手、LLM 有没有否决
SELECT created_at, symbol, action, signal_score, veto_type
FROM trading_decisions
ORDER BY created_at DESC
LIMIT 20;
```

| 字段 | 怎么读 |
|---|---|
| `signal_score` | 规则打分卡的 composite。`NULL` = 用的是 2026-09-11 之前的旧代码 |
| `veto_type` | `NULL` = 没到否决环节（HOLD 短路，零 RAG/LLM 调用）<br>`veto_none` = 否决节点放行（RAG 可以是空结果）<br>`veto_applied` = 合法否决拦下<br>`veto_fail_closed` = veto 不可用或输出不合法，安全拦下 |

```sql
-- 2. 真实成交的回合与盈亏（前向验证的核心）
SELECT created_at, symbol, action, signal_score,
       execution_result->>'status'       AS exec_status,
       execution_result->>'average_price' AS avg_price,
       execution_result->>'realized_pnl'  AS realized_pnl
FROM trading_decisions
WHERE action IN ('LONG', 'SHORT')
ORDER BY created_at DESC
LIMIT 20;
```

> 虚拟盘没有成交流水，`realized_pnl` 由相邻 balance 快照推导（不含手续费）。

```sql
-- 3. 否决率：三个 veto agent 各自拦了多少、有多少是无效输出
SELECT veto_type, count(*) FROM trading_decisions GROUP BY 1 ORDER BY 2 DESC;
```

```sql
-- 4. 熔断与外部故障（外部 5xx 不应触发熔断）
SELECT created_at, event_type, status, left(reason, 80)
FROM risk_events ORDER BY created_at DESC LIMIT 20;
```

```sql
-- 5. 权益曲线（判断赚没赚）
SELECT captured_at, equity, realized_pnl, unrealized_pnl
FROM pnl_snapshots ORDER BY captured_at DESC LIMIT 50;
```

```sql
-- 6. 微观结构在攒没有（路线 C 的原料）
SELECT count(*), max(captured_at) FROM market_microstructures;
```

```sql
-- 7. 采集到的周线深度（换周期后应看 12h/1d）
SELECT symbol, timeframe, count(*), max(to_timestamp(open_time/1000))
FROM market_candles GROUP BY 1,2 ORDER BY 1,2;
```

## 判断「策略到底行不行」

**不要看单笔盈亏**，看**平均 R**。R = 单笔风险（|入场−止损|×数量），是倍数不是钱。

```sql
-- 已平仓回合的平均 R —— 这是唯一有意义的指标
WITH closed AS (
  SELECT execution_result->>'realized_pnl' AS pnl,
         proposal->>'stop_loss'            AS sl,
         proposal->>'position_size_pct'    AS pct,
         execution_result->>'average_price' AS entry
  FROM trading_decisions
  WHERE action = 'CLOSE' AND execution_result->>'realized_pnl' IS NOT NULL
)
SELECT count(*) AS trades,
       avg(pnl::numeric) AS avg_pnl
FROM closed;
```

单笔 R 的完整计算需要「开仓价 / 平仓价 / 初始止损」三者，目前散在 `proposal` 与
`execution_result` 两个 JSON 列里。**样本攒够之前不必手工算** —— 回测里已有同一套
指标实现（`app/backtest/engine.py` 的 `BacktestResult`）。

## 什么时候该警惕

| 现象 | 含义 |
|---|---|
| `veto_fail_closed` 占比很高 | 否决 agent 的 Ark 调用、输出格式或提示词契约异常；这些周期不会放行新仓，应先修复外部服务或 schema |
| `CIRCUIT_BREAKER` 反复出现 | 连续失败或连亏触发；先看 `reason` 是外部故障还是策略 |
| `CYCLE_FAILURE` 里出现 5xx | 对端抖动，不应熔断账户（第 1 层已修，若复现说明修漏了） |
| 长期一笔不开 | 先看 `signal_score` 是否都低于 `STRATEGY_ENTRY_THRESHOLD`（默认 0.35） |
| `signal_score` 全是 NULL | worker 跑的是旧代码，重启它 |
| 出现 `entry_signal_evidence_missing` | 规则提案缺少指标证据；这不是“RAG 没搜到新闻”，应检查 signal_node 输出 |

RAG 的正常空结果不会阻塞开仓：Retriever 会先按币种检索，未命中时回退到空资产标签的
通用市场/宏观材料；两级都无结果时交给 `news_veto_node` 判断。只有模型未配置、路径不存在、
Milvus 不可用或检索异常才会记录 `retrieval_failed` 并安全 HOLD。

## 看 Milvus 是否真的被调用

应用将 Milvus/RAG 调用写到标准日志（默认 `LOG_LEVEL=INFO`）。重点搜索：

```bash
grep -E "milvus (client|collection|search|insert)|rag (retrieval|asset fallback|reranker)" <worker日志>
```

判断方式：

- `milvus search start/complete`：已调用 Milvus；`raw_hits=0` 表示向量库没有返回候选。
- `rag retrieval search ... candidates=0`：可能是 Milvus 返回为空，也可能是应用层时间/资产/事件过滤后为空。
- `rag asset fallback`：目标币种没有候选，开始查空资产的通用市场/宏观材料。
- `rag reranker start/complete`：已经有候选并进入 BGE-Reranker；两级召回都为空时不会执行。
- `milvus insert/upsert ... asset_counts=...`：文档入库或回填批次各资产写入多少行，可直接发现
  标注偏斜；v2 还会记录 `asset_scope`/`schema_version` 到 collection。

日志不会输出连接 token 或 query 正文。修改代码或 `.env` 后，必须重启长驻 worker 才会加载。

历史 v1 数据不会因重启自动重算。本次已完成 v2 回填（116 篇、175 行、失败 0），并将本地
`.env` 切换到 v2；如果要在另一环境修复资产覆盖，可先执行：

```bash
cd backend
uv run python scripts/reindex_documents.py --dry-run
uv run python scripts/reindex_documents.py --target-collection alpha_council_documents_bge_m3_v2
```

回填脚本只读取 PostgreSQL 中已 `INDEXED` 且有正文/摘要的文档，使用同一 BGE-M3 重新向量化，
写入新 collection，不删除旧 collection；完成后应核对总行数、资产分布和重复键，再修改配置。
长驻 API/worker 进程必须重启后才会加载新的 collection 和代码。

## 复现回测

```bash
cd backend
PYTHONPATH=. uv run python -c "
from app.backtest.engine import run_backtest
from app.backtest.sweep import sweep, robustness
from app.signals.params import StrategyParams
# 完整用法见 docs/backtest-findings.md 末尾
"
```
