export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api"

type GetToken = (options?: { skipCache?: boolean }) => Promise<string | null>

// 一个轮询周期里 6 个请求会同时撞上 401，各自去 Clerk 换个新 token 就是 6 次签发。
// 按 getToken 合并成一次，等所有请求拿到同一个新 token 再重试。
const pendingRefreshes = new WeakMap<GetToken, Promise<string | null>>()

function refreshToken(getToken: GetToken): Promise<string | null> {
  const pending = pendingRefreshes.get(getToken)
  if (pending) return pending
  const refresh = getToken({ skipCache: true }).finally(() => pendingRefreshes.delete(getToken))
  pendingRefreshes.set(getToken, refresh)
  return refresh
}

export async function apiRequest<T>(
  path: string,
  getToken: GetToken,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set("Accept", "application/json")
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json")
  }

  const request = async (token: string | null) => {
    const requestHeaders = new Headers(headers)
    if (token) {
      requestHeaders.set("Authorization", `Bearer ${token}`)
    } else {
      requestHeaders.delete("Authorization")
    }
    return fetch(`${apiBaseUrl}${path}`, {
      ...init,
      headers: requestHeaders,
      cache: "no-store",
    })
  }

  const token = await getToken()
  let response = await request(token)
  if (response.status === 401) {
    const freshToken = await refreshToken(getToken)
    if (freshToken && freshToken !== token) response = await request(freshToken)
  }
  if (!response.ok) {
    const detail = await response.text()
    throw new ApiError(detail || `Request failed with ${response.status}`, response.status)
  }
  return response.json() as Promise<T>
}
