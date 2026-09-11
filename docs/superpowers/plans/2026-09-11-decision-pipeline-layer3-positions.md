# 决策链重构 · 第 3 层：持仓管理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 开出去的仓要有人管 —— 保本、移动止损、时间止损、结构失效，以及把「单品种一仓」和 TP 方向变成硬校验。

**前提（已实测确认）**：`slTriggerPrice` 生效且平仓时随仓自动撤销；**没有撤单/改单接口**，所以移动止损只能是「本地记账 + 按需下 reduceOnly 市价单」。

**Spec:** `docs/superpowers/specs/2026-09-11-decision-pipeline-redesign-design.md` 第 3 层
**实测依据:** `docs/weex-virtual-api.md`

---

## 关键设计

**交易所侧只挂宽灾难止损（3×stop_distance），软件层管紧的那条。**

理由：交易所的触发单**改不了也撤不掉**（实测无 cancel 端点）。如果挂紧止损，价格一碰就直接平仓，移动止损根本没有机会执行 —— 移动止损的价值全没了。所以交易所那条只做「worker 挂掉时的兜底」，代价是极端情况亏损到 3R 而不是 1R，这是 spec 接受的取舍。

**软件的「有效止损」存在本地**（`positions.effective_stop`），每轮周期拿现价比对，触及就下 reduceOnly 市价单。

---

## Task 1: 风险引擎补 TP 方向与 R:R 校验

现在 TP 完全不校验（LLM/信号填反了照样挂上去），也没有最低盈亏比。

**Files:** `app/risk/engine.py`、`tests/unit/test_risk.py`

- [ ] 写失败测试：LONG 的 TP ≤ entry → `take_profit_wrong_side`；SHORT 的 TP ≥ entry → 同；R:R < 1.5 → `reward_risk_too_low`；`is_reducing` 时两条都豁免
- [ ] 确认失败
- [ ] 实现（沿用 `is_reducing` 豁免，与现有 `stop_loss_required` 同段）
- [ ] 确认通过 + 提交

---

## Task 2: 单品种一仓

现在每轮都可能新开仓。虽然名义上限间接挡住了，但那是间接的，且 5m 一仓的小单子会被放行。

**Files:** `app/services/cycle.py`、`tests/api/test_cycle_service.py`

- [ ] 写失败测试：已有同 symbol 持仓时，新开仓被拒（`position_already_open`）；CLOSE 不受影响
- [ ] 确认失败
- [ ] 实现：`_evaluate_proposal` 里已有 `positions`，加一条检查
- [ ] 确认通过 + 提交

---

## Task 3: 交易所侧改挂宽灾难止损

**Files:** `app/domain/schemas.py`（`TradeProposal` 加 `disaster_stop`）、`app/agents/graph.py`（signal_node 填）、`app/execution/service.py`（下单用 disaster_stop）、`tests/unit/test_execution*.py`

- [ ] 写失败测试：非 HOLD 提案带 `disaster_stop` 时，下单请求的 `stop_loss` 用的是 disaster_stop 而不是 stop_loss
- [ ] 确认失败
- [ ] 实现
- [ ] 确认通过 + 提交

---

## Task 4: positions 加列（迁移 009）

要管仓就得记住：初始止损（R 的基准）、有效止损、开仓时刻、持仓期内的最有利价（移动止损用）。

**Files:** `app/db/models.py`、`alembic/versions/009_position_management.py`、`tests/integration/test_migrations.py`

- [ ] 写失败测试
- [ ] 实现（照 002 模板：`inspect` 判存在再 `op.add_column`）
- [ ] 跑真实库迁移
- [ ] 提交

---

## Task 5: PositionManager

每轮周期跑，独立于开仓决策。输入：本地持仓行 + 交易所仓位 + 现价 + ATR(1h) + 当前 signal_score。

| 规则 | 触发 | 动作 |
|---|---|---|
| 保本 | 浮盈 ≥ 1R | `effective_stop` 移到 entry |
| 移动止损 | 浮盈 ≥ 1R | 跟随 `最高价 ∓ 1×ATR(1h)`，**只上移不下移** |
| 时间止损 | 持仓 > 48h 且浮盈 < 0.3R | 平仓 |
| 结构失效 | `composite` 反向穿越 0 | 平仓 |
| 有效止损触及 | 现价 ≤/≥ effective_stop | 平仓（reduceOnly） |

分批止盈（1R/2R 各平 1/3）**本轮不做** —— 它需要部分平仓的数量簿记与多次下单，风险与复杂度都更高，先让主干跑顺。

**Files:** `app/positions/manager.py`（新）、`app/services/cycle.py`（接线）、`tests/unit/test_position_manager.py`

- [ ] 写失败测试（逐条规则，纯函数式：给状态 → 断言动作）
- [ ] 实现
- [ ] 接进 cycle（在 `_execute` 之前跑，先处理已有仓位）
- [ ] 确认通过 + 提交

---

## Task 6: 端到端校验

- [ ] 真实开一笔仓，确认交易所挂的是 3×ATR 的宽止损
- [ ] 构造浮盈 ≥ 1R 的场景，确认有效止损上移到 entry
- [ ] 确认单品种一仓生效（已有仓位时新信号被拒）

---

## 完成标准

- [ ] TP 方向与 R:R 有硬校验，平仓单豁免
- [ ] 已有仓位时不再加仓
- [ ] 交易所侧是宽灾难止损，软件层管紧止损
- [ ] 保本 / 移动止损 / 时间止损 / 结构失效 四条规则生效且只上移不下移
- [ ] `uv run pytest` / `ruff` / `mypy` 全绿

## 诚实说明

本层的参数（1R 保本、1×ATR 移动、48h 时间止损、0.3R 阈值）**同样是手拍的**。它们和打分的权重一样，只有第 4 层回测才能验证是否真的改善结果。本层解决的是「有没有管理」，不是「管理得好不好」。
