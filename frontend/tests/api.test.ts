import { describe, expect, it, vi } from "vitest"

import { apiRequest } from "@/lib/api"

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
})
