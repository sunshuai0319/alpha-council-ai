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
    const freshToken = await getToken({ skipCache: true })
    if (freshToken && freshToken !== token) response = await request(freshToken)
  }
  if (!response.ok) {
    const detail = await response.text()
    throw new ApiError(detail || `Request failed with ${response.status}`, response.status)
  }
  return response.json() as Promise<T>
}
