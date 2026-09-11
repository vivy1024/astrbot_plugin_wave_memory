export interface ApiError extends Error {
  status?: number
  detail?: string
  payload?: unknown
}

export interface ApiRequestInit extends RequestInit {
  noAuth?: boolean
  timeoutMs?: number
}

export class ApiRequestCancelledError extends Error {
  constructor(message = '请求已取消。') {
    super(message)
    this.name = 'AbortError'
  }
}

export class ApiRequestTimeoutError extends Error {
  constructor(message = 'API 请求超时（15 秒上限），请检查网络或服务端响应健康。') {
    super(message)
    this.name = 'TimeoutError'
  }
}

export function isRequestCancelled(error: unknown): boolean {
  return error instanceof ApiRequestCancelledError
    || (error instanceof Error && error.name === 'AbortError')
}

const TOKEN_KEY = 'wavememory.webui.token'

let authFailureHandler: (() => void) | undefined

export function getStoredToken(): string | null {
  if (typeof window === 'undefined') {
    return null
  }
  return window.localStorage.getItem(TOKEN_KEY)
}

export function setStoredToken(token: string | null): void {
  if (typeof window === 'undefined') {
    return
  }
  if (token) {
    window.localStorage.setItem(TOKEN_KEY, token)
  } else {
    window.localStorage.removeItem(TOKEN_KEY)
  }
}

export function clearStoredToken(): void {
  setStoredToken(null)
}

export function setAuthFailureHandler(handler: (() => void) | undefined): void {
  authFailureHandler = handler
}

export function toApiPath(path: string): string {
  if (/^https?:\/\//i.test(path)) {
    return path
  }
  return path.startsWith('/') ? path : `/${path}`
}

async function parseResponse(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    return response.json()
  }
  const text = await response.text()
  if (!text) {
    return null
  }
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function createApiError(response: Response, payload: unknown): ApiError {
  const detail =
    typeof payload === 'object' && payload !== null && 'detail' in payload
      ? String((payload as { detail?: unknown }).detail)
      : typeof payload === 'object' && payload !== null && 'error' in payload
        ? typeof (payload as { error?: unknown }).error === 'object' && (payload as { error?: unknown }).error !== null && 'message' in ((payload as { error?: unknown }).error as object)
          ? String(((payload as { error: { message?: unknown } }).error).message)
          : String((payload as { error?: unknown }).error)
        : typeof payload === 'object' && payload !== null && 'message' in payload
          ? String((payload as { message?: unknown }).message)
          : response.statusText
  const error = new Error(detail || `HTTP ${response.status}`) as ApiError
  error.status = response.status
  error.detail = detail
  error.payload = payload
  return error
}

export async function fetchJson<T>(path: string, init: ApiRequestInit = {}): Promise<T> {
  const { noAuth, headers, body, signal, timeoutMs = 15000, ...fetchInit } = init
  if (signal?.aborted) throw new ApiRequestCancelledError()
  const requestHeaders = new Headers(headers)
  const token = getStoredToken()

  if (body && !(body instanceof FormData) && !requestHeaders.has('Content-Type')) {
    requestHeaders.set('Content-Type', 'application/json')
  }
  if (!noAuth && token && token !== 'no-auth' && !requestHeaders.has('Authorization')) {
    requestHeaders.set('Authorization', `Bearer ${token}`)
  }

  // 无论调用方是否传入 signal，都保留统一超时；外部取消与超时使用
  // 不同错误文案，避免“停止轮询”被误报成网络超时。
  const controller = new AbortController()
  let timedOut = false
  const abortFromCaller = () => controller.abort(signal?.reason)
  if (signal?.aborted) controller.abort(signal.reason)
  else signal?.addEventListener('abort', abortFromCaller, { once: true })
  const timeoutId = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  try {
    const response = await fetch(toApiPath(path), {
      ...fetchInit,
      body,
      headers: requestHeaders,
      signal: controller.signal,
    })
    const payload = await parseResponse(response)

    if (!response.ok) {
      if (response.status === 401) {
        clearStoredToken()
        // AuthGate 会在原地址上显示登录页；保留 hash，登录后即可返回
        // 用户正在查看的页面、筛选条件和对象深链。
        authFailureHandler?.()
      }
      throw createApiError(response, payload)
    }

    return payload as T
  } catch (error) {
    if (timedOut) {
      const seconds = Math.round(timeoutMs / 1000)
      throw new ApiRequestTimeoutError(`API 请求超时（${seconds} 秒上限），请检查网络或服务端响应健康。`)
    }
    if (isRequestCancelled(error)) {
      throw new ApiRequestCancelledError()
    }
    throw error
  } finally {
    clearTimeout(timeoutId)
    signal?.removeEventListener('abort', abortFromCaller)
  }
}
