# 观察进展

想快速看系统在干什么时，先看这份。

## 启动与停止

```bash
# 后端 API（前端要用）
cd backend && uv run uvicorn app.main:app --reload --port 8000

# 交易 worker（每 5 分钟一轮，**必须重启才会加载新配置与新代码**）
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
| `veto_type` | `NULL` = 没到否决环节（HOLD 短路，零 LLM 调用）<br>`veto_none` = 否决节点放行<br>`veto_applied` = 被否决拦下<br>`veto_invalid_ignored` = LLM 输出不合法，放行并计数 |

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
| `veto_invalid_ignored` 占比很高 | 否决 agent 的提示词有问题，实际在空转 |
| `CIRCUIT_BREAKER` 反复出现 | 连续失败或连亏触发；先看 `reason` 是外部故障还是策略 |
| `CYCLE_FAILURE` 里出现 5xx | 对端抖动，不应熔断账户（第 1 层已修，若复现说明修漏了） |
| 长期一笔不开 | 先看 `signal_score` 是否都低于 `STRATEGY_ENTRY_THRESHOLD`（默认 0.35） |
| `signal_score` 全是 NULL | worker 跑的是旧代码，重启它 |

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
