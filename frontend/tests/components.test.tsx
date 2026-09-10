import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { ActionMark, DecisionCard, EmptyState, RiskBadge, VirtualBadge } from "@/components/console-primitives"
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
    expect(screen.getByText("WEEX virtual futures · simulation only")).toBeVisible()
  })

  it("shows risk status and action on a decision card", () => {
    render(<DecisionCard decision={holdDecision} />)
    expect(screen.getByText("HOLD")).toBeVisible()
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
    expect(screen.getByText("LONG")).toBeVisible()
    expect(screen.getByText("REJECTED")).toBeVisible()
  })
})
