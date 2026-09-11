# 决策链重构 · 第 2 层：信号层 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「三个 agent 写散文 → 委员会拍板」换成「确定性规则信号器定方向 → LLM 封闭否决」，让系统在合理行情下愿意开仓，同时否决率可统计、可复盘。

**Architecture:** 新增 `app/signals/`（规则打分卡 + 风险预算反推），新增 `VetoVerdict` schema，LangGraph 里用「信号器 → LLM 否决」取代「散文委员会」。

**Tech Stack:** Python 3.13 / SQLAlchemy 2 / LangGraph / pytest。命令都在 `backend/` 下用 `uv` 执行。

**Spec:** `docs/superpowers/specs/2026-09-11-decision-pipeline-redesign-design.md` 第 2 层

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `app/signals/scorer.py`（新） | 打分卡：方向 / 强度 / 波动率门控 |
| `app/signals/sizing.py`（新） | 风险预算反推：stop_distance / 仓位 / SL / TP / 灾难止损 |
| `app/domain/schemas.py`（改） | `SignalProposal`、`PositionPlan`、`VetoVerdict`、枚举 |
| `app/agents/graph.py`（改） | 信号器节点 + LLM 否决节点取代委员会 |
| `app/services/cycle.py`（改） | 接进信号器 + 否决；`trading_decisions` 记 signal_score / veto_type |
| `alembic/versions/008_trading_decisions_signal_fields.py`（新） | 决策表加列 |
| `app/agents/llm.py`（改） | `AnalysisResult.model_version` / `trace_id` 加默认值 |
| `app/domain/schemas.py`（改） | 同上 |

---

## Task 1: 打分卡（`app/signals/scorer.py`）

输入 `technical_indicators`（5m/1h/4h），输出结构化 `SignalProposal`。权重按 spec：trend_alignment 0.40、momentum 0.25、volume_confirmation 0.15、volatility_regime 门控（不在 `[p20, p90]` 内 → HOLD）。

**Files:**
- Create: `app/signals/scorer.py`
- Create: `tests/unit/test_scorer.py`

- [ ] **Step 1: 写失败的测试**

```python
"""打分卡测试：方向由 1h/4h 趋势一致性 + 动量 + 量能决定。"""

import pytest

from app.signals.scorer import HOLD, LONG, SHORT, score_direction


def _indicators(**overrides):
    """构造一个各周期齐全的 indicators dict，方便按需覆盖。"""
    base = {
        "1h": {"trend": "BULLISH", "ema_spread_pct": 0.001, "rsi_14": 55},
        "4h": {"trend": "BULLISH", "ema_spread_pct": 0.002, "rsi_14": 58},
        "5m": {"trend": "BULLISH", "rsi_14": 60},
        "volume": {"1h": 0.1, "4h": 0.1},
    }
    return {**base, **overrides}


def test_aligned_trends_with_volume_confirm_long():
    assert score_direction(_indicators()) == LONG


def test_aligned_bearish_trends_short():
    ind = _indicators(
        **{"1h": {"trend": "BEARISH", "ema_spread_pct": -0.001, "rsi_14": 45},
           "4h": {"trend": "BEARISH", "ema_spread_pct": -0.002, "rsi_14": 42},
           "5m": {"trend": "BEARISH", "rsi_14": 40}}
    )
    assert score_direction(ind) == SHORT


def test_conflicting_timeframes_hold():
    ind = _indicators(
        **{"1h": {"trend": "BULLISH", "ema_spread_pct": 0.001, "rsi_14": 55},
           "4h": {"trend": "BEARISH", "ema_spread_pct": -0.002, "rsi_14": 42}}
    )
    assert score_direction(ind) == HOLD


def test_high_volatility_gate_holds():
    """波动率分位 > p90 → 直接 HOLD（门控，不看方向）。"""
    ind = _indicators()
    assert score_direction(ind, volatility_percentile=0.95) == HOLD
    assert score_direction(ind, volatility_percentile=0.5) == LONG


def test_low_volatility_gate_holds():
    assert score_direction(ind, volatility_percentile=0.1) == HOLD
```

> 注意：`_indicators` 里我用了 `"volume"` 键，实际结构以 spec 为准 —— 打分卡的 volume 输入可以简化成
> 一个 `volume_confirmation` 布尔值（1h/4h 量变同向），避免耦合具体指标字段。实现时保持接口最小。

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现打分卡**

核心逻辑：

```python
"""规则打分卡：只依赖 technical_indicators，返回方向。"""

from typing import Any

HOLD = "HOLD"
LONG = "LONG"
SHORT = "SHORT"

TREND_WEIGHT = 0.40
MOMENTUM_WEIGHT = 0.25
VOLUME_WEIGHT = 0.15

#: 波动率门控：分位落在这个区间之外直接 HOLD。
VOLATILITY_P20 = 0.20
VOLATILITY_P90 = 0.90


def score_direction(indicators: dict[str, Any], volatility_percentile: float | None = None) -> str:
    """给方向打分：composite ∈ [-1, 1]，≥ ENTRY_THRESHOLD 才开仓。

    volatility_percentile 为 None 时跳过门控（数据不足不判死刑）。
    """
    if volatility_percentile is not None and not (VOLATILITY_P20 <= volatility_percentile <= VOLATILITY_P90):
        return HOLD
    trend = _trend_alignment(indicators)
    momentum = _momentum(indicators)
    volume = _volume_confirmation(indicators)
    composite = trend * TREND_WEIGHT + momentum * MOMENTUM_WEIGHT + volume * VOLUME_WEIGHT
    if composite >= 0.35:
        return LONG
    if composite <= -0.35:
        return SHORT
    return HOLD
```

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

---

## Task 2: 风险预算反推（`app/signals/sizing.py`）

按 spec 2.2：`stop_distance = 1.5 × ATR(14,1h)`，`notional = risk_budget / (stop_distance / entry)`，封顶 20% 权益，`SL/TP` 由距离推导。

**Files:**
- Create: `app/signals/sizing.py`
- Create: `tests/unit/test_sizing.py`

- [ ] **Step 1: 写失败的测试**

```python
from decimal import Decimal

from app.signals.sizing import PositionPlan, size_position


def _plan(**overrides):
    base = {
        "equity": Decimal(10000),
        "entry": Decimal(100),
        "atr": Decimal(2),        # 1.5×2=3 点止损
        "side": "LONG",
        "risk_pct": Decimal("0.005"),
        "max_notional_pct": Decimal("0.20"),
    }
    return size_position(**{**base, **overrides})


def test_stop_distance_is_k_times_atr():
    plan = _plan()
    assert plan.stop_distance == Decimal("3")


def test_position_size_caps_at_max_notional():
    """risk_budget=50，stop_pct=3% → notional=1666；cap=20%×10000=2000，不触发 cap。"""
    plan = _plan()
    assert plan.position_size_pct == Decimal("0.1666")
    assert plan.notional == Decimal("1666.666")


def test_large_risk_caps_position():
    """stop_distance 极小会让 notional 超过 20% 上限 → 应封顶到 20%。"""
    plan = _plan(atr=Decimal("0.1"))  # stop_distance=0.15 → notional 巨大
    assert plan.position_size_pct == Decimal("0.20")
    assert plan.notional == Decimal("2000")


def test_sl_and_tp_are_placed_correctly():
    plan = _plan()
    assert plan.stop_loss == Decimal("97")
    assert plan.take_profit == Decimal("106")
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**

```python
"""仓位与 SL/TP 由风险预算反推，不由 LLM 拍。"""

from dataclasses import dataclass
from decimal import Decimal

DEFAULT_RISK_PCT = Decimal("0.005")       # 单笔风险上限
DEFAULT_MAX_NOTIONAL_PCT = Decimal("0.20")  # 名义敞口上限
DEFAULT_ATR_MULTIPLIER = Decimal("1.5")    # 止损 = k × ATR(1h)
DEFAULT_RR = Decimal("2.0")               # TP = entry ± 2 × stop_distance


@dataclass(frozen=True)
class PositionPlan:
    side: str
    stop_distance: Decimal
    position_size_pct: Decimal
    notional: Decimal
    stop_loss: Decimal
    take_profit: Decimal


def size_position(
    *,
    equity: Decimal,
    entry: Decimal,
    atr: Decimal,
    side: str,
    risk_pct: Decimal = DEFAULT_RISK_PCT,
    max_notional_pct: Decimal = DEFAULT_MAX_NOTIONAL_PCT,
    atr_multiplier: Decimal = DEFAULT_ATR_MULTIPLIER,
    rr: Decimal = DEFAULT_RR,
) -> PositionPlan:
    stop_distance = atr * atr_multiplier
    if stop_distance <= 0 or entry <= 0:
        raise ValueError("stop_distance and entry must be positive")
    risk_budget = equity * risk_pct
    notional_raw = risk_budget / (stop_distance / entry)
    notional = min(notional_raw, equity * max_notional_pct)
    position_size_pct = notional / equity
    stop_loss = entry - stop_distance if side == "LONG" else entry + stop_distance
    take_profit = entry + stop_distance * rr if side == "LONG" else entry - stop_distance * rr
    return PositionPlan(
        side=side,
        stop_distance=stop_distance,
        position_size_pct=position_size_pct,
        notional=notional,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
```

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

---

## Task 3: VetoVerdict 封闭枚举

**Files:**
- Modify: `app/domain/schemas.py`
- Create: `tests/unit/test_veto.py`

- [ ] **Step 1: 写失败的测试**

```python
import pytest

from pydantic import ValidationError

from app.domain.schemas import VetoReason, VetoVerdict


def test_veto_requires_evidence_when_true():
    with pytest.raises(ValidationError):
        VetoVerdict(veto=True, reasons=[VetoReason.NEWS_SHOCK], evidence_refs=[], reasoning_summary="x")


def test_unknown_reason_is_rejected():
    with pytest.raises(ValidationError):
        VetoVerdict(veto=True, reasons=["WHATEVER"], evidence_refs=["doc:1"], reasoning_summary="x")


def test_valid_verdicts_parse():
    ok = VetoVerdict(veto=False, reasons=[], evidence_refs=[], reasoning_summary="no objection")
    assert ok.veto is False
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**

在 `app/domain/schemas.py`：

```python
class VetoReason(StrEnum):
    REGIME_CONFLICT = "REGIME_CONFLICT"
    NEWS_SHOCK = "NEWS_SHOCK"
    STRUCTURE_INVALIDATED = "STRUCTURE_INVALIDATED"
    LIQUIDITY_ANOMALY = "LIQUIDITY_ANOMALY"
    DATA_INTEGRITY = "DATA_INTEGRITY"


class VetoVerdict(BaseModel):
    veto: bool
    reasons: list[VetoReason] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""

    @model_validator(mode="after")
    def veto_requires_evidence(self) -> "VetoVerdict":
        if self.veto and not self.evidence_refs:
            raise ValueError("veto=True requires evidence_refs")
        return self
```

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

---

## Task 4: 决策表加 signal_score / veto_type

**Files:**
- Modify: `app/db/models.py`
- Create: `alembic/versions/008_trading_decisions_signal_fields.py`
- Modify: `tests/integration/test_migrations.py`

照 Task 5/007 的模板：增量迁移给已有表加列。`signal_score` REAL nullable、`veto_type` VARCHAR(32) nullable。

- [ ] **Step 1: 写失败的测试**
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现模型 + 迁移**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

---

## Task 5: 接进决策周期 + analyst 必填字段

**Files:**
- Modify: `app/agents/graph.py`（信号器节点 + 否决节点取代委员会）
- Modify: `app/services/cycle.py`（调用新图、落库 signal_score/veto_type）
- Modify: `app/agents/llm.py`（`AnalysisResult` 必填字段加默认值）
- Modify: `tests/integration/test_graph.py`、`tests/api/test_cycle_service.py`

本 Task 最复杂，包含：
- 图结构：`signal_node → veto_node` 取代 `fanout → committee`
- `AnalysisResult.model_version` / `trace_id` 加默认值
- `trading_decisions` 落 `signal_score` / `veto_type`

- [ ] **Step 1: 写失败的图测试**
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现图改造**
- [ ] **Step 4: 实现必填字段默认值**
- [ ] **Step 5: 接进 cycle + 落库**
- [ ] **Step 6: 跑全量测试 + ruff + mypy**
- [ ] **Step 7: 提交**

---

## Task 6: 端到端校验

- [ ] **Step 1: 跑一个真实周期，确认 `signal_score` 落库、`veto_type` 正确**
- [ ] **Step 2: 观察 LLM 否决率（三种结局分布）**
- [ ] **Step 3: 若全 HOLD，核对是「方向分数不够」还是「LLM 否决」**

---

## 完成标准

- [ ] 打分卡：对齐趋势 + 量能 → LONG；冲突 → HOLD；波动率门控生效
- [ ] 仓位反推：stop_distance = 1.5×ATR，notional 封顶 20% 权益
- [ ] `VetoVerdict` 封闭枚举，`veto=True` 无证据被拒
- [ ] 决策表有 `signal_score` / `veto_type`
- [ ] LLM 只能否决、不能改方向；否决率可统计
- [ ] `uv run pytest` / `ruff` / `mypy` 全绿
