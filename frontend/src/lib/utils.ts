export const API = '/api'
export const API_BASE = '/api'

export function getToken(): string {
  return localStorage.getItem('auth_token') || ''
}

export function setToken(token: string): void {
  localStorage.setItem('auth_token', token)
}

export function clearToken(): void {
  localStorage.removeItem('auth_token')
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

export async function apiFetch(path: string, opts?: RequestInit) {
  const token = getToken()
  const baseHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) baseHeaders['Authorization'] = `Bearer ${token}`
  const res = await fetch(API + path, {
    ...opts,
    headers: { ...baseHeaders, ...(opts?.headers as Record<string, string> || {}) },
  })
  if (res.status === 401) {
    const text = await res.text()
    const error = buildApiError(res, text)
    clearToken()
    if (window.location.pathname !== '/login') {
      window.location.href = '/login'
    }
    throw new ApiError(error.status, '未认证，请重新登录', error.detail, error.code)
  }
  if (!res.ok) {
    const text = await res.text()
    throw buildApiError(res, text)
  }
  return res.json()
}
