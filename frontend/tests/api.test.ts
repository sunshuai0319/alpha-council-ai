import { describe, expect, it, vi } from "vitest"

import { apiRequest } from "@/lib/api"

/** 普通取 token 给旧的，skipCache（重试）给新的。 */
function tokenGetter(stale: string, fresh: string) {
  return vi.fn(async (options?: { skipCache?: boolean }) => (options?.skipCache ? fresh : stale))
}

/** 只认新 token 的假 API：token 不对就回 401 Invalid Clerk token。 */
function authorizedOnlyOn(fresh: string, payload: unknown = { items: [] }) {
  return vi.fn(async (_url: string, init?: RequestInit) => {
    const authorized = (init?.headers as Headers).get("Authorization") === `Bearer ${fresh}`
    return authorized
      ? new Response(JSON.stringify(payload), { status: 200 })
      : new Response(JSON.stringify({ detail: "Invalid Clerk token" }), { status: 401 })
  })
}

describe("apiRequest authentication recovery", () => {
  it("refreshes the Clerk token and retries once after an unauthorized response", async () => {
    const getToken = vi.fn()
      .mockResolvedValueOnce("expired-token")
      .mockResolvedValueOnce("fresh-token")
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Invalid Clerk token" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    vi.stubGlobal("fetch", fetchMock)

    await expect(apiRequest("/events", getToken)).resolves.toEqual({ items: [] })

    expect(getToken).toHaveBeenNthCalledWith(1)
    expect(getToken).toHaveBeenNthCalledWith(2, { skipCache: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8000/api/events")
    expect(fetchMock.mock.calls[0][1]?.headers).toBeInstanceOf(Headers)
    expect((fetchMock.mock.calls[0][1]?.headers as Headers).get("Authorization")).toBe("Bearer expired-token")
    expect((fetchMock.mock.calls[1][1]?.headers as Headers).get("Authorization")).toBe("Bearer fresh-token")
  })

  it("asks Clerk for a new token once when a whole polling batch is unauthorized", async () => {
    // 6 个请求共用一个 token，会一起 401；各自去换新就是 6 次签发。
    const getToken = tokenGetter("stale-token", "fresh-token")
    const fetchMock = authorizedOnlyOn("fresh-token")
    vi.stubGlobal("fetch", fetchMock)

    const paths = ["/market", "/decisions", "/portfolio", "/events", "/accounts", "/control/status"]
    await expect(Promise.all(paths.map((path) => apiRequest(path, getToken)))).resolves.toHaveLength(6)

    const refreshes = getToken.mock.calls.filter(([options]) => options?.skipCache)
    expect(refreshes).toHaveLength(1)
    expect(fetchMock).toHaveBeenCalledTimes(12) // 6 次首轮 + 6 次重试
  })

  it("surfaces the unauthorized response when a fresh token cannot be minted", async () => {
    const getToken = tokenGetter("stale-token", "stale-token")
    vi.stubGlobal("fetch", authorizedOnlyOn("fresh-token"))

    await expect(apiRequest("/events", getToken)).rejects.toThrow(/Invalid Clerk token/)
  })
})
