import { cleanup, fireEvent, render, screen } from "@testing-library/react"
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
  credentials: {
    api_key: "test-api-key-not-a-real-credential",
    api_secret: "test-secret-abcdefghijklmnopqrstuvwxyz0123456789",
    passphrase: "test-passphrase",
  },
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

const decisionRecord = {
  id: "d-1",
  cycle_id: "cycle-abcdef123456",
  symbol: "BTC-USDT",
  action: "HOLD",
  status: "ALLOWED",
  leverage: 20,
  proposal: {
    action: "HOLD",
    symbol: "BTC-USDT",
    position_size_pct: 0,
    leverage: 1,
    confidence: 0.4,
    reasoning_summary: "Mixed signals; hold.",
    model_version: "committee-agent-v1",
    invalidation_conditions: ["macro data insufficient"],
    evidence_refs: ["doc-1"],
  },
  analyses: {
    market: { status: "neutral", confidence: 0.4, reasoning_summary: "market is flat", model_version: "m1" },
    quant: { status: "BEARISH", confidence: 0.6, reasoning_summary: "quant is bearish", model_version: "q1" },
  },
  risk_decision: { status: "ALLOWED", reasons: ["hold_no_order"] },
  execution_result: { status: "NO_ORDER" },
  created_at: "2026-09-11T05:00:00Z",
}

function stubApi(
  controlStatus: string,
  accounts: unknown[] = [],
  market: unknown[] = [],
  decisions: unknown[] = [],
  decisionsTotal: number = decisions.length,
) {
  const payloads: Record<string, unknown> = {
    "/market": { items: market },
    "/decisions": { items: decisions, total: decisionsTotal, page: 1, page_size: 20 },
    "/portfolio": { items: [] },
    "/events": { items: [], total: 0, page: 1, page_size: 20 },
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

    render(<ConsolePage view="settings" />)

    expect(await screen.findByLabelText("杠杆")).toHaveAttribute("readonly")
    expect(screen.getByLabelText("杠杆")).toHaveValue("20x")
  })

  it("shows stored risk limits as percentages", async () => {
    stubApi("RUNNING", [
      { ...virtualAccount, risk_limits: { max_position_notional_pct: 0.05 } },
    ])

    render(<ConsolePage view="settings" />)

    // 0.05 存的是小数，输入框要显示 5
    expect(await screen.findByLabelText("仓位上限 (%)")).toHaveValue(5)
  })

  it("falls back to the platform limit when no preference is stored", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="settings" />)

    expect(await screen.findByLabelText("仓位上限 (%)")).toHaveValue(20)
  })

  it("syncs the interface language to the server", async () => {
    stubApi("RUNNING")

    render(<ConsolePage view="overview" />)

    await vi.waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      const put = calls.find(
        ([url, init]) => String(url).includes("/preferences/locale") && init?.method === "PUT",
      )
      expect(put).toBeTruthy()
    })
  })

  it("keeps configuration off the overview", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="overview" />)

    await screen.findByText("AI 智囊团值守中。")
    expect(screen.queryByText("WEEX 虚拟账户")).toBeNull()
    expect(screen.queryByText("最近快照")).toBeNull()
  })

  it("renders the account card on the settings view", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="settings" />)

    expect(await screen.findByText("WEEX 虚拟账户")).toBeVisible()
    expect(screen.getByText("停用")).toBeVisible()
  })

  it("paginates the risk event list", async () => {
    stubApi("RUNNING")

    render(<ConsolePage view="events" />)

    await vi.waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      expect(calls.some(([url]) => String(url).includes("/events?page=1"))).toBe(true)
    })
  })

  it("paginates the trade ledger", async () => {
    // 25 条、每页 20 → 两页
    stubApi("RUNNING", [virtualAccount], [], [decisionRecord], 25)

    render(<ConsolePage view="trades" />)

    expect(await screen.findByText("第 1 / 2 页")).toBeVisible()
    fireEvent.click(screen.getByText("下一页"))
    await vi.waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      expect(calls.some(([url]) => String(url).includes("/decisions?page=2"))).toBe(true)
    })
  })

  it("expands a trade row to reveal the full decision data", async () => {
    stubApi("RUNNING", [virtualAccount], [], [decisionRecord])

    render(<ConsolePage view="trades" />)

    fireEvent.click(await screen.findByRole("button", { name: /BTC-USDT/ }))

    expect(await screen.findByText("智能体分析")).toBeVisible()
    expect(screen.getByText("quant is bearish")).toBeVisible()
    expect(screen.getByText("提案")).toBeVisible()
    expect(screen.getByText("风控")).toBeVisible()
    expect(screen.getByText("执行")).toBeVisible()
    // 杠杆显示账户实际值 20×，不是提案占位的 1×
    expect(screen.getByText("20×")).toBeVisible()
    expect(screen.queryByText("1×")).toBeNull()
  })

  it("shows a resume toast and auto-dismisses it after 5 seconds", async () => {
    vi.useFakeTimers()
    vi.spyOn(window, "confirm").mockReturnValue(true)
    try {
      stubApi("PAUSED")
      render(<ConsolePage view="overview" />)
      // flush 初始数据加载（refresh 并发 6 个 fetch，多层 await 要多次冲刷）
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(0)

      fireEvent.click(screen.getByText("恢复周期"))
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByText("周期已恢复，正在触发下一次决策。")).toBeVisible()

      await vi.advanceTimersByTimeAsync(5000)
      expect(screen.queryByText("周期已恢复，正在触发下一次决策。")).toBeNull()
    } finally {
      vi.useRealTimers()
      vi.restoreAllMocks()
    }
  })

  it("masks stored credentials until the user reveals them", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="settings" />)

    const secret = "test-secret-abcdefghijklmnopqrstuvwxyz0123456789"
    // 默认脱敏：明文绝不出现在页面上，只露首尾
    expect(await screen.findByText("test-s••••••••6789")).toBeVisible()
    expect(screen.queryByText(secret)).toBeNull()

    // 点「查看全部内容」→ 明文展示，这样粘错前缀时一眼能看出来
    fireEvent.click(screen.getByText("查看全部内容"))
    expect(await screen.findByText(secret)).toBeVisible()
    expect(screen.queryByText("test-s••••••••6789")).toBeNull()
  })
})
