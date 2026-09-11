import { describe, expect, it } from "vitest"

import { emptyOverviewHint } from "@/lib/console-hints"
import type { MarketSnapshot, TradingAccount } from "@/lib/types"

const account = (enabled: boolean): TradingAccount => ({
  id: "a-1",
  provider: "weex",
  environment: "virtual",
  enabled,
  configured: true,
  credentials: null,
  risk_limits: null,
  effective_risk_limits: null,
  platform_limits: null,
})

const snapshot: MarketSnapshot = { symbol: "BTC-USDT", captured_at: 1, last_price: 100 }

describe("empty overview hint", () => {
  it("explains that no trading account exists yet", () => {
    expect(emptyOverviewHint([], [])).toBe("overview.hintNoAccount")
  })

  it("explains that the account is not enabled", () => {
    expect(emptyOverviewHint([account(false)], [])).toBe("overview.hintAccountDisabled")
  })

  it("explains that the first cycle has not produced data yet", () => {
    expect(emptyOverviewHint([account(true)], [])).toBe("overview.hintAwaitingCycle")
  })

  it("stays silent once market data exists", () => {
    expect(emptyOverviewHint([account(true)], [snapshot])).toBeNull()
  })
})
