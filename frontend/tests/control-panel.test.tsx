import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "token", isLoaded: true, isSignedIn: true }),
}))

import { ConsolePage } from "@/components/console-page"
import { labelText, reasonLabel } from "@/lib/labels"
import { messages } from "@/lib/i18n"

// 用中文词条渲染，验证「选中文就该是中文」。
const zhT = (key: string, params?: Record<string, string>) => {
  let text = messages["zh-CN"][key] ?? key
  for (const [name, value] of Object.entries(params ?? {})) text = text.replace(`{${name}}`, value)
  return text
}

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
  positions: unknown[] = [],
) {
  const payloads: Record<string, unknown> = {
    "/market": { items: market },
    "/decisions": { items: decisions, total: decisionsTotal, page: 1, page_size: 20 },
    "/portfolio": { items: positions },
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
  })

  it("shows order fields on an entry but not on a hold", async () => {
    // 观望单没有仓位、杠杆、止损止盈可言 —— 显示出来只会是「0.0% / 20×」噪声。
    const entry = {
      ...decisionRecord,
      id: "d-entry",
      action: "SHORT",
      leverage: 20,
      proposal: {
        ...decisionRecord.proposal,
        action: "SHORT",
        position_size_pct: 0.2,
        leverage: 1, // 提案占位值，界面该显示账户实际杠杆
        confidence: 0.6,
        stop_loss: 77800,
        take_profit: 76300,
      },
    }
    stubApi("RUNNING", [virtualAccount], [], [entry])
    render(<ConsolePage view="trades" />)
    fireEvent.click(await screen.findByRole("button", { name: /BTC-USDT/ }))

    expect(await screen.findByText("77800")).toBeVisible()
    expect(screen.getByText("20×")).toBeVisible() // 账户实际杠杆，不是提案的 1×
    expect(screen.queryByText("1×")).toBeNull()

    cleanup()

    stubApi("RUNNING", [virtualAccount], [], [decisionRecord]) // 这条是 HOLD
    render(<ConsolePage view="trades" />)
    fireEvent.click(await screen.findByRole("button", { name: /BTC-USDT/ }))

    expect(await screen.findByText("智能体分析")).toBeVisible()
    expect(screen.queryByText("20×")).toBeNull()
    expect(screen.queryByText("仓位")).toBeNull()
  })

  it("localizes backend reason codes instead of showing them raw", async () => {
    // 后端存的是稳定的英文机器码；界面按语言翻译，选中文就该是中文。
    stubApi("RUNNING", [virtualAccount], [], [decisionRecord])
    render(<ConsolePage view="trades" />)
    fireEvent.click(await screen.findByRole("button", { name: /BTC-USDT/ }))

    // fixture 的 risk reason 是 hold_no_order，模型版本是 committee-agent-v1
    expect(await screen.findByText("规则信号未达开仓条件，未下单")).toBeVisible()
    expect(screen.getByText("旧版委员会（已停用）")).toBeVisible()
    expect(screen.queryByText("hold_no_order")).toBeNull()
    expect(screen.queryByText("committee-agent-v1")).toBeNull()
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

  it("blames the credentials, not the API, when the session token is rejected", async () => {
    // 401 是凭证问题：让用户去重启 API 只会白折腾，8 秒后的下一轮轮询会自己恢复。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "Invalid Clerk token" }), { status: 401 })),
    )

    render(<ConsolePage view="overview" />)

    expect(await screen.findByText(/登录凭证正在自动更新/)).toBeVisible()
    expect(screen.queryByText(/启动 API 并刷新/)).toBeNull()
  })

  it("offers no close button for a position that is already closed", async () => {
    // 实测踩到：后端把已平仓的行也当持仓返回，页面就给一个不存在的仓位配了
    // 「关闭 BTC-USDT」按钮。按钮必须只属于真正 OPEN 的仓位。
    stubApi("RUNNING", [], [], [], undefined, [
      {
        id: "p-closed",
        symbol: "BTC-USDT",
        side: "SHORT",
        quantity: 0,
        entry_price: 77381.1,
        unrealized_pnl: 0,
        status: "CLOSED",
      },
    ])
    render(<ConsolePage view="trades" />)
    await screen.findByText("BTC-USDT")

    expect(screen.queryByText(/关闭 BTC-USDT/)).toBeNull()
  })

  it("still offers the close button for an open position", async () => {
    stubApi("RUNNING", [], [], [], undefined, [
      {
        id: "p-open",
        symbol: "ETH-USDT",
        side: "LONG",
        quantity: 0.5,
        entry_price: 2400,
        unrealized_pnl: 12.5,
        status: "OPEN",
      },
    ])
    render(<ConsolePage view="trades" />)

    expect(await screen.findByText(/关闭 ETH-USDT/)).toBeVisible()
  })
})


describe("positions ledger", () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { cleanup() })

  const openPosition = {
    id: "p-1", symbol: "BTC-USDT", side: "LONG", quantity: 0.5,
    entry_price: 77000, mark_price: 77100, unrealized_pnl: 50, status: "OPEN",
  }
  const closedPosition = { ...openPosition, id: "p-2", symbol: "ETH-USDT", quantity: 0, status: "CLOSED" }

  it("offers a close button only for positions that are still open", async () => {
    // 实测踩到：已平仓的仓位下面仍然显示「关闭 XX」按钮，点下去平的是不存在的仓位。
    stubApi("RUNNING", [], [], [], 0, [openPosition, closedPosition])
    render(<ConsolePage view="trades" />)

    expect(await screen.findByText(/关闭 BTC-USDT/)).toBeVisible()
    expect(screen.queryByText(/关闭 ETH-USDT/)).toBeNull()
  })

  it("counts only open positions in the overview", async () => {
    // 概览的「持仓数」原来把所有行都算上，已平仓的也会被计数。
    stubApi("RUNNING", [], [], [], 0, [openPosition, closedPosition])
    render(<ConsolePage view="overview" />)

    // 文案是 "虚拟持仓"（overview.openPositions）
    const metric = await screen.findByText("虚拟持仓")
    expect(metric.parentElement).toHaveTextContent("1")
  })
})


describe("closed round detail", () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { cleanup() })

  it("shows the realized P&L of a closed round", async () => {
    // 后端一直在记 realized_pnl / average_price，但界面从不显示 ——
    // 于是「这笔平仓赚没赚」在界面上看不到，而那正是最该看的数。
    const closed = {
      ...decisionRecord,
      id: "d-close",
      action: "CLOSE",
      execution_result: {
        status: "FILLED", client_order_id: "alpha-1", exchange_order_id: "o-1",
        average_price: "77343.2", realized_pnl: "-12.34",
      },
    }
    stubApi("RUNNING", [], [], [closed], 1)
    render(<ConsolePage view="trades" />)

    // 展开这条决策
    fireEvent.click(await screen.findByRole("button", { name: /BTC-USDT/ }))

    expect(await screen.findByText("本回合盈亏")).toBeVisible()
    expect(screen.getByText("-12.34")).toBeVisible()
    expect(screen.getByText("成交均价")).toBeVisible()
  })
})


describe("decision filters", () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { cleanup() })

  function stubFiltered() {
    const seen: string[] = []
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const parsed = new URL(url)
      const path = parsed.pathname.replace(/^\/api/, "")
      if (path === "/decisions") seen.push(parsed.search)
      const payloads: Record<string, unknown> = {
        "/market": { items: [] },
        "/decisions": { items: [decisionRecord], total: 1, page: 1, page_size: 20, symbols: ["BTC-USDT", "ETH-USDT"] },
        "/portfolio": { items: [] },
        "/events": { items: [], total: 0, page: 1, page_size: 20 },
        "/accounts": { items: [] },
        "/control/status": { status: "RUNNING" },
      }
      return new Response(JSON.stringify(payloads[path] ?? {}), { status: 200 })
    }))
    return seen
  }

  it("offers the symbols present in the history, and passes the filter to the API", async () => {
    const seen = stubFiltered()
    render(<ConsolePage view="trades" />)

    const select = await screen.findByLabelText("品种")
    // 选项来自后端回传的 symbols，不是硬编码的品种表
    expect(screen.getByRole("option", { name: "ETH-USDT" })).toBeVisible()

    fireEvent.change(select, { target: { value: "ETH-USDT" } })

    await vi.waitFor(() => {
      expect(seen.some((query) => query.includes("symbol=ETH-USDT"))).toBe(true)
    })
  })

  it("returns to the first page when a filter changes", async () => {
    // 换了筛选还停在第 3 页会显示空白 —— 过滤后的结果没有那么多页。
    const seen = stubFiltered()
    render(<ConsolePage view="trades" />)

    fireEvent.change(await screen.findByLabelText("动作"), { target: { value: "SHORT" } })

    await vi.waitFor(() => {
      const filtered = seen.filter((query) => query.includes("action=SHORT"))
      expect(filtered.length).toBeGreaterThan(0)
      expect(filtered.every((query) => query.includes("page=1"))).toBe(true)
    })
  })
})


describe("rule signal labels", () => {
  it("translates the entry summary, which is free text rather than a code", () => {
    expect(labelText(reasonLabel("rule signal SHORT score=-0.5477"), zhT)).toBe("规则信号 SHORT，分数 -0.55")
  })

  it("translates the hold score code", () => {
    expect(labelText(reasonLabel("signal_hold_score_0.26"), zhT)).toBe("信号分 0.26，未达开仓阈值")
  })

  it("leaves an unknown code alone rather than blanking it", () => {
    // 显示一个陌生码远好过显示空白 —— 它本身就是排查线索。
    expect(labelText(reasonLabel("some_future_code_42"), zhT)).toBe("some_future_code_42")
  })
})


describe("reason codes in the collapsed list", () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { cleanup() })

  it("shows the ledger row in Chinese, not the raw code", async () => {
    // 实测报的问题：详情做了本地化，但折叠的列表行仍在显示 signal_hold_score_0.18。
    const holdWithCode = {
      ...decisionRecord,
      proposal: { ...decisionRecord.proposal, reasoning_summary: "signal_hold_score_0.18" },
    }
    stubApi("RUNNING", [virtualAccount], [], [holdWithCode])
    render(<ConsolePage view="trades" />)

    expect(await screen.findByText("信号分 0.18，未达开仓阈值")).toBeVisible()
    expect(screen.queryByText("signal_hold_score_0.18")).toBeNull()
  })

  it("localizes the code on the overview card too", async () => {
    const holdWithCode = {
      ...decisionRecord,
      proposal: { ...decisionRecord.proposal, reasoning_summary: "signal_hold_score_0.18" },
    }
    stubApi("RUNNING", [virtualAccount], [], [holdWithCode])
    render(<ConsolePage view="overview" />)

    expect(await screen.findAllByText("信号分 0.18，未达开仓阈值")).not.toHaveLength(0)
    expect(screen.queryByText("signal_hold_score_0.18")).toBeNull()
  })
})
