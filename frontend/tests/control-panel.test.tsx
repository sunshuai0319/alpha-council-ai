import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "token", isLoaded: true, isSignedIn: true }),
}))

import { ConsolePage } from "@/components/console-page"

function stubApi(controlStatus: string) {
  const payloads: Record<string, unknown> = {
    "/market": { items: [] },
    "/decisions": { items: [] },
    "/portfolio": { items: [] },
    "/events": { items: [] },
    "/accounts": { items: [] },
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
    expect(await screen.findByText("委员会已暂停")).toBeVisible()
  })

  it("shows the running state the server reports", async () => {
    stubApi("RUNNING")

    render(<ConsolePage view="overview" />)

    expect(await screen.findByText("委员会运行中")).toBeVisible()
  })
})
