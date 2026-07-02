/**
 * Browser extension helper for the external AccessToken distribution API.
 *
 * Usage:
 *   import { ExternalAccessTokenApiClient } from './access-token-api-client.js'
 *
 *   const client = new ExternalAccessTokenApiClient({
 *     baseUrl: 'http://127.0.0.1:8000',
 *     apiToken: 'YOUR_ACCESS_TOKEN_API_TOKEN',
 *   })
 *
 *   const claim = await client.claimAccessToken({
 *     consumer: 'my-extension',
 *     limit: 1,
 *     leaseSeconds: 86400,
 *   })
 *
 *   const item = claim.items?.[0]
 *   if (item) {
 *     // use item.access_token
 *     await client.reportPaid(item.claim_id, {
 *       externalPaymentId: 'ext-001',
 *       message: 'stored successfully',
 *     })
 *   }
 */

function normalizeBaseUrl(baseUrl) {
  const value = String(baseUrl || '').trim()
  if (!value) {
    throw new Error('baseUrl is required')
  }
  return value.replace(/\/+$/, '')
}

async function readJsonResponse(response) {
  const text = await response.text()
  if (!text) {
    return {}
  }
  try {
    return JSON.parse(text)
  } catch {
    return { detail: text }
  }
}

async function ensureOk(response, fallbackMessage) {
  if (response.ok) {
    return readJsonResponse(response)
  }
  const body = await readJsonResponse(response)
  const detail =
    body?.detail ||
    body?.message ||
    body?.error ||
    fallbackMessage ||
    `HTTP ${response.status}`
  const err = new Error(String(detail))
  err.status = response.status
  err.body = body
  throw err
}

function createHeaders(apiToken, extraHeaders = {}) {
  const headers = {
    Authorization: `Bearer ${String(apiToken || '').trim()}`,
    'Content-Type': 'application/json',
    ...extraHeaders,
  }
  return headers
}

export class ExternalAccessTokenApiClient {
  constructor({
    baseUrl,
    apiToken,
    fetchImpl = globalThis.fetch,
  } = {}) {
    this.baseUrl = normalizeBaseUrl(baseUrl)
    this.apiToken = String(apiToken || '').trim()
    this.fetch = fetchImpl
    if (!this.apiToken) {
      throw new Error('apiToken is required')
    }
    if (typeof this.fetch !== 'function') {
      throw new Error('fetchImpl must be a function')
    }
  }

  _url(path) {
    return `${this.baseUrl}${path.startsWith('/') ? path : `/${path}`}`
  }

  async claimAccessToken({
    consumer = 'browser-extension',
    limit = 1,
    leaseSeconds = 86400,
    allowRefresh = true,
  } = {}) {
    const response = await this.fetch(this._url('/api/external/access-tokens/claim'), {
      method: 'POST',
      headers: createHeaders(this.apiToken),
      body: JSON.stringify({
        consumer,
        limit,
        lease_seconds: leaseSeconds,
        allow_refresh: allowRefresh,
      }),
    })
    return ensureOk(response, 'claim access token failed')
  }

  async getClaim(claimId) {
    if (!String(claimId || '').trim()) {
      throw new Error('claimId is required')
    }
    const response = await this.fetch(this._url(`/api/external/access-tokens/${encodeURIComponent(claimId)}`), {
      method: 'GET',
      headers: createHeaders(this.apiToken),
    })
    return ensureOk(response, 'get claim failed')
  }

  async reportPaid(
    claimId,
    {
      externalPaymentId = '',
      message = 'stored successfully',
      raw = {},
    } = {},
  ) {
    return this._writeResult(claimId, {
      status: 'paid',
      external_payment_id: externalPaymentId,
      message,
      raw,
    })
  }

  async reportFailed(
    claimId,
    {
      externalPaymentId = '',
      errorCode = 'failed',
      message = 'delivery failed',
      raw = {},
    } = {},
  ) {
    return this._writeResult(claimId, {
      status: 'failed',
      external_payment_id: externalPaymentId,
      error_code: errorCode,
      message,
      raw,
    })
  }

  async releaseClaim(claimId, { reason = 'released by browser extension' } = {}) {
    if (!String(claimId || '').trim()) {
      throw new Error('claimId is required')
    }
    const response = await this.fetch(this._url(`/api/external/access-tokens/${encodeURIComponent(claimId)}/release`), {
      method: 'POST',
      headers: createHeaders(this.apiToken),
      body: JSON.stringify({ reason }),
    })
    return ensureOk(response, 'release claim failed')
  }

  async _writeResult(claimId, payload) {
    if (!String(claimId || '').trim()) {
      throw new Error('claimId is required')
    }
    const response = await this.fetch(this._url(`/api/external/access-tokens/${encodeURIComponent(claimId)}/result`), {
      method: 'POST',
      headers: createHeaders(this.apiToken),
      body: JSON.stringify(payload),
    })
    return ensureOk(response, 'write claim result failed')
  }
}

export function createExternalAccessTokenApiClient(options) {
  return new ExternalAccessTokenApiClient(options)
}

