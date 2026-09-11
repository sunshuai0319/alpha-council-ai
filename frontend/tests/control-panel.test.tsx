import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "token", isLoaded: true, isSignedIn: true }),
}))

import { ConsolePage } from "@/components/console-page"

const virtualAccount = {
  id: "a-1",
  provider: "weex",
  environment: "virtual",
  enabled: true,
  configured: true,
  risk_limits: null,
  effective_risk_limits: {
    max_leverage: 20,
    max_position_notional_pct: 0.2,
    max_single_trade_risk_pct: 0.005,
    max_daily_loss_pct: 0.05,
    max_consecutive_losses: 3,
  },
  platform_limits: {
    max_position_notional_pct: 0.2,
    max_single_trade_risk_pct: 0.005,
    max_daily_loss_pct: 0.05,
    max_consecutive_losses: 3,
  },
}

function stubApi(controlStatus: string, accounts: unknown[] = [], market: unknown[] = []) {
  const payloads: Record<string, unknown> = {
    "/market": { items: market },
    "/decisions": { items: [] },
    "/portfolio": { items: [] },
    "/events": { items: [] },
    "/accounts": { items: accounts },
    "/control/status": { status: controlStatus },
  }
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = new URL(url).pathname.replace(/^\/api/, "")
      return new Response(JSON.stringify(payloads[path] ?? {}), { status: 200 })
    }),
  )
}

describe("console control panel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  // 组件内部每 8 秒轮询一次；不卸载会留下定时器，在 jsdom 拆除后报错。
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("shows the paused state the server reports", async () => {
    stubApi("PAUSED")

    render(<ConsolePage view="overview" />)

    // 状态曾经是本地 useState("RUNNING")，刷新后即使账户已被熔断暂停也显示"运行中"。
    expect(await screen.findByText("智囊团已暂停")).toBeVisible()
  })

  it("shows the running state the server reports", async () => {
    stubApi("RUNNING")

    render(<ConsolePage view="overview" />)

    expect(await screen.findByText("智囊团运行中")).toBeVisible()
  })

  it("explains why the overview is empty instead of only showing dashes", async () => {
    stubApi("RUNNING") // 无账户、无行情

    render(<ConsolePage view="overview" />)

    expect(await screen.findByText(/还没有交易账户/)).toBeVisible()
  })

  it("tells the user to enable the account when it exists but is off", async () => {
    stubApi("RUNNING", [{ ...virtualAccount, enabled: false }])

    render(<ConsolePage view="overview" />)

    expect(await screen.findByText(/交易账户尚未启用/)).toBeVisible()
  })

  it("stays silent once market data exists", async () => {
    stubApi("RUNNING", [virtualAccount], [{ symbol: "BTC-USDT", captured_at: 1, last_price: 100 }])

    render(<ConsolePage view="overview" />)

    await screen.findByText("智囊团运行中")
    expect(screen.queryByText(/还没有交易账户|交易账户尚未启用|账户已启用，但还没有交易周期数据/)).toBeNull()
  })

  it("shows leverage read-only: the virtual account cannot change it", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="overview" />)

    expect(await screen.findByLabelText("杠杆")).toHaveAttribute("readonly")
    expect(screen.getByLabelText("杠杆")).toHaveValue("20x")
  })

  it("shows stored risk limits as percentages", async () => {
    stubApi("RUNNING", [
      { ...virtualAccount, risk_limits: { max_position_notional_pct: 0.05 } },
    ])

    render(<ConsolePage view="overview" />)

    // 0.05 存的是小数，输入框要显示 5
    expect(await screen.findByLabelText("仓位上限 (%)")).toHaveValue(5)
  })

  it("falls back to the platform limit when no preference is stored", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="overview" />)

    expect(await screen.findByLabelText("仓位上限 (%)")).toHaveValue(20)
  })
})
