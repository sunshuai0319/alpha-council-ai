import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { eventTypeLabel, labelText, reasonParts } from "@/lib/labels"
import { messages } from "@/lib/i18n"

const zhT = (key: string, params?: Record<string, string>) => {
  let text = messages["zh-CN"][key] ?? key
  for (const [name, value] of Object.entries(params ?? {})) text = text.replace(`{${name}}`, value)
  return text
}

import { ActionMark, DecisionCard, EmptyState, RiskBadge, VirtualBadge, formatPercent } from "@/components/console-primitives"
import type { Decision } from "@/lib/types"

const holdDecision: Decision = {
  id: "decision-1",
  cycle_id: "cycle-123456789",
  symbol: "BTC-USDT",
  action: "HOLD",
  status: "ALLOWED",
  proposal: {
    confidence: 0.74,
    leverage: 1,
    position_size_pct: 0,
    reasoning_summary: "Signals disagree; stay flat until the next confirmed candle.",
  },
  risk_decision: { reasons: ["hold_no_order"] },
}

describe("console primitives", () => {
  it("keeps the simulation boundary visible", () => {
    render(<VirtualBadge />)
    expect(screen.getByText("WEEX 虚拟合约 · 仅模拟")).toBeVisible()
  })

  it("shows risk status and action on a decision card", () => {
    render(<DecisionCard decision={holdDecision} />)
    expect(screen.getByText(zhT("action.hold"))).toBeVisible()
    expect(screen.getByText("ALLOWED")).toBeVisible()
    expect(screen.getByText(/Signals disagree/)).toBeVisible()
  })

  it("renders an explicit empty state for a flat book", () => {
    render(<EmptyState title="No open virtual positions" body="The book is flat." />)
    expect(screen.getByText("No open virtual positions")).toBeVisible()
    expect(screen.getByText("The book is flat.")).toBeVisible()
  })

  it("maps long and rejected states to readable labels", () => {
    render(<><ActionMark action="LONG" /><RiskBadge status="REJECTED" /></>)
    expect(screen.getByText(zhT("action.long"))).toBeVisible()
    expect(screen.getByText("REJECTED")).toBeVisible()
  })
})


describe("event localization", () => {
  it("translates the event type and each reason, including joined ones", () => {
    // 后端可能把多条原因用 `;` 拼起来，逐条翻译；认不出的原文原样保留。
    expect(labelText(eventTypeLabel("CIRCUIT_BREAKER"), zhT)).toBe("熔断")
    expect(reasonParts("entry_evidence_missing;hold_no_order").map((p) => labelText(p, zhT)))
      .toEqual(["缺少证据引用", "规则信号未达开仓条件，未下单"])
    // WEEX 报错是自由文本，翻不了也不该被吞
    const raw = "WEEX request failed: GET /capi/v3/sim/position/allPosition: 503"
    expect(labelText(reasonParts(raw)[0], zhT)).toBe(raw)
  })
})


describe("percent formatting", () => {
  it("keeps a small funding rate from collapsing to zero", () => {
    // 资金费率量级 ~1e-5；固定 1 位小数会把它截成 0.0%，负值还带个多余的负号
    // （-0.0%）。资金费率这类小数值要更多位数。
    expect(formatPercent(0.00006176, 4)).toBe("0.0062%")
    expect(formatPercent(-0.00002364, 4)).toBe("-0.0024%")
  })

  it("defaults to one decimal for position sizes and the like", () => {
    expect(formatPercent(0.05)).toBe("5.0%")
  })
})
