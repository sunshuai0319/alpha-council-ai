# 决策链重构 · 第 4 层：参数外置 + 回测器 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把写死的策略参数收进一个可覆盖的配置对象，再写一个回测器，用**同一套信号与风控代码**在已累积的历史上跑出交易样本 —— 回答「这组参数到底有没有边际」。

**Spec:** `docs/superpowers/specs/2026-09-11-decision-pipeline-redesign-design.md` 第 4 层
**实测依据:** `docs/weex-virtual-api.md`（taker 费率 0.08%/边）

---

## 先说清楚回测的定位

**它是筛子，不是优化器。** 41 天的 1h 数据大约只有几十笔交易，统计意义很弱。所以：

- ✅ 能回答：「哪些参数区域稳健」「是不是对参数极度敏感（= 没有边际）」「会不会明显亏」
- ❌ 不能回答：「最优参数是多少」—— 那在这个样本量上就是过拟合

**回测能力边界**（必须写进 docstring）：`depth`/`trades`/`fundingRate`/`openInterest` 没有历史，所以回测**验证不了微观结构相关的部分**（structure veto agent 的一半输入）。回测覆盖的是打分卡 + 仓位反推 + 持仓管理这条主链。

---

## Task 1: 参数外置（`StrategyParams`）

8 个参数分散在三个模块的模块级常量里，改一个要动代码。收进一个 frozen dataclass，并允许从 `Settings` 覆盖。

**Files:** `app/signals/params.py`（新）、`app/signals/scorer.py`、`app/signals/sizing.py`、`app/positions/manager.py`、`app/config.py`、`tests/unit/test_params.py`

- [ ] 写失败测试：默认值与现有常量一致；`from_settings()` 能用 env 覆盖
- [ ] 实现 `StrategyParams`（含 `scorer`/`sizing`/`manager` 全部阈值）
- [ ] 三个模块改为接受 `params`，默认 `StrategyParams()` —— **默认行为必须与现在逐位一致**，否则既有测试全红
- [ ] 确认既有 235 个测试仍全绿
- [ ] 提交

> 关键约束：这一步是**纯重构**，不改任何数值、不改任何行为。测试全绿就是证明。

---

## Task 2: 回测引擎（纯函数）

**Files:** `app/backtest/engine.py`（新）、`tests/unit/test_backtest.py`

接口设计：

```python
def run_backtest(
    candles_by_timeframe: dict[str, list[Candle]],   # 至少含 1h 与 4h
    *,
    params: StrategyParams = StrategyParams(),
    initial_equity: Decimal = Decimal(10_000),
    fee_pct: Decimal = Decimal("0.0008"),            # 实测 taker 0.08%/边
) -> BacktestResult
```

模拟规则（**保守**，宁可低估收益）：

| 环节 | 做法 |
|---|---|
| 入场 | 信号出现在 bar i 的收盘 → 以 **bar i+1 的开盘价**成交（不给「同一根 K 线内成交」的便宜） |
| 仓位 | 复用 `size_position`（同一套风险预算） |
| 止损/止盈 | 用 **bar 的 high/low** 判定是否触及；同一根 K 线内两者都触及时**按止损算**（悲观） |
| 保本/移动止损 | 用 bar 的有利极值更新 peak，在收盘时按 `manage()` 推进有效止损 |
| 结构失效 / 时间止损 | 每根 K 线用当前 `score_signal` 与持仓时长判定 |
| 手续费 | 开仓 + 平仓各收 `fee_pct × 名义` |
| 同时持仓 | 单品种一仓（与线上一致） |

输出 `BacktestResult`：`trades`、`trade_count`、`win_rate`、`avg_r`、`expectancy_r`、`max_drawdown_pct`、`total_return_pct`、`exit_reason_counts`。

- [ ] 写失败测试：构造确定性的 K 线 → 断言已知结果（例如一条必然触发止损的序列 → 1 笔交易、-1R 左右、扣掉手续费）
- [ ] 实现
- [ ] 提交

---

## Task 3: 参数敏感性扫描

**Files:** `app/backtest/sweep.py`（新）、`tests/unit/test_backtest_sweep.py`

对 2–3 个最关键参数做网格扫描（入场阈值、ATR 倍数、R:R），输出每个组合的指标。

**重点是看稳健性，不是找最优**：如果相邻参数的结果天差地别，说明那是噪声拟合出来的。

- [ ] 写测试：扫描函数对每个组合调用回测并返回有序结果
- [ ] 实现
- [ ] 提交

---

## Task 4: 用真实历史跑一次并给出结论

- [ ] 从 `market_candles` 读 1h/4h 历史，跑回测
- [ ] 跑参数扫描，看结果分布
- [ ] **把结论写进 docs**（包括「样本太小、不能下结论」这种结论也要写）

---

## 完成标准

- [ ] 参数全部可配置，改参不用改代码
- [ ] 回测器复用线上同一套信号/仓位/持仓管理代码（不是重写一遍）
- [ ] 回测含手续费，入场用下一根开盘价，同 K 线冲突按止损算
- [ ] 能给出交易数 / 胜率 / 平均 R / 最大回撤
- [ ] 有敏感性分析
- [ ] 结论如实写进文档，不粉饰
- [ ] `uv run pytest` / `ruff` / `mypy` 全绿

## 明确不做

- 不拟合参数到「最优」（样本量不支持）
- 不做 tick 级或 5m 级回测（5m 历史只有 3.5 天，且线上不用 5m 定方向）
- 不引入外部数据源补历史（`klines` 单请求上限 1000 根且无分页，这是硬边界）
