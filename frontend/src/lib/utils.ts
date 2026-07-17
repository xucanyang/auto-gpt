export const API = '/api'
export const API_BASE = '/api'
const AUTH_TOKEN_KEY = 'auth_token'

// Protocol 2 means the task UI has the terminal-state polling fix.  The
// backend uses it only to distinguish a freshly deployed bundle from a stale
// tab which is still executing the pre-fix task modal code.
export const TASK_POLL_PROTOCOL_VERSION = '2'

export function getToken(): string {
  if (typeof localStorage === 'undefined') return ''
  return localStorage.getItem(AUTH_TOKEN_KEY) || ''
}

export function setToken(token: string): void {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(AUTH_TOKEN_KEY, token)
}

export function clearToken(): void {
  if (typeof localStorage === 'undefined') return
  localStorage.removeItem(AUTH_TOKEN_KEY)
}

export function invalidateSession(): void {
  clearToken()
  if (typeof window === 'undefined' || window.location.pathname === '/login') return
  window.location.replace('/login')
}

export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly detail: unknown

  constructor(status: number, message: string, detail: unknown, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function buildApiError(res: Response, text: string): ApiError {
  let payload: unknown = text
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    // Non-JSON error responses keep their original text.
  }

  const envelope = asRecord(payload)
  const detail = envelope && Object.prototype.hasOwnProperty.call(envelope, 'detail')
    ? envelope.detail
    : payload
  const detailRecord = asRecord(detail)
  const codeValue = detailRecord?.code ?? envelope?.code
  const code = typeof codeValue === 'string' && codeValue.trim() ? codeValue.trim() : undefined
  const messageValue = detailRecord?.message ?? envelope?.message
  const message = typeof messageValue === 'string' && messageValue.trim()
    ? messageValue.trim()
    : typeof detail === 'string' && detail.trim()
      ? detail.trim()
      : text.trim() || `${res.status} ${res.statusText}`.trim() || '请求失败'

  return new ApiError(res.status, message, detail, code)
}

export async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  return buildApiError(res, await res.text())
}

function apiUrl(path: string): string {
  if (path === API || path.startsWith(`${API}/`)) return path
  return `${API}${path.startsWith('/') ? path : `/${path}`}`
}

export async function apiRequest(path: string, opts?: RequestInit): Promise<Response> {
  const token = getToken()
  const headers = new Headers(opts?.headers)
  headers.set('X-Auto-Gpt-Task-Poll-Protocol', TASK_POLL_PROTOCOL_VERSION)
  if (typeof opts?.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const res = await fetch(apiUrl(path), {
    ...opts,
    headers,
  })
  if (res.status === 401) {
    const error = await apiErrorFromResponse(res)
    invalidateSession()
    throw new ApiError(error.status, '未认证，请重新登录', error.detail, error.code)
  }
  return res
}

export async function apiFetch(path: string, opts?: RequestInit) {
  const res = await apiRequest(path, opts)
  if (!res.ok) {
    throw await apiErrorFromResponse(res)
  }
  return res.json()
}

export async function logout(): Promise<void> {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), 5000)
  try {
    if (getToken()) {
      const response = await apiRequest('/auth/logout', {
        method: 'POST',
        keepalive: true,
        signal: controller.signal,
      })
      if (!response.ok) throw await apiErrorFromResponse(response)
    }
  } finally {
    globalThis.clearTimeout(timeout)
    invalidateSession()
  }
}
