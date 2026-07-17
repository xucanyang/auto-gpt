import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, test } from 'node:test'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

const moduleDir = await mkdtemp(join(tmpdir(), 'auto-gpt-frontend-tests-'))
after(() => rm(moduleDir, { force: true, recursive: true }))

async function transpile(sourceRelative, outputName) {
  const sourceUrl = new URL(sourceRelative, import.meta.url)
  const source = (await readFile(sourceUrl, 'utf8')).replaceAll('./utils.ts', './utils.mjs')
  const result = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: sourceUrl.pathname,
  })
  await writeFile(join(moduleDir, outputName), result.outputText)
}

await transpile('../src/lib/utils.ts', 'utils.mjs')
await transpile('../src/lib/eventStream.ts', 'eventStream.mjs')
const eventStream = await import(pathToFileURL(join(moduleDir, 'eventStream.mjs')).href)
const utils = await import(pathToFileURL(join(moduleDir, 'utils.mjs')).href)
const { consumeEventStream, isAbortError, SseParser } = eventStream
const { ApiError, apiRequest, getToken, setToken } = utils

function installStorage() {
  const values = new Map()
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    clear: () => values.clear(),
    key: (index) => [...values.keys()][index] ?? null,
    get length() {
      return values.size
    },
  }
}

test('SseParser handles CRLF boundaries and multiline data', () => {
  const parser = new SseParser()
  assert.deepEqual(parser.push('event: log\r\nid: 42\r\ndata: first\r'), [])
  assert.deepEqual(parser.push('\ndata: second\r\n\r'), [])
  assert.deepEqual(parser.push('\n'), [{
    data: 'first\nsecond',
    event: 'log',
    id: '42',
  }])
})

test('SseParser ignores comments, retains retry and flushes final events', () => {
  const parser = new SseParser()
  assert.deepEqual(parser.push(': keepalive\nretry: 1500\ndata:\n\n'), [{
    data: '',
    event: 'message',
    retry: 1500,
  }])
  assert.deepEqual(parser.finish('data: final event'), [{
    data: 'final event',
    event: 'message',
    retry: 1500,
  }])
})

test('consumeEventStream sends the session only in the Authorization header', async () => {
  installStorage()
  setToken('top-secret-session')
  let requestedUrl = ''
  let requestedHeaders = new Headers()
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    requestedUrl = String(input)
    requestedHeaders = new Headers(init?.headers)
    return new Response('data: {"line":"ok"}\r\n\r\n', {
      headers: { 'Content-Type': 'text/event-stream' },
    })
  }

  const events = []
  try {
    await consumeEventStream('/pipeline/logs/stream', {
      onEvent: (event) => events.push(event),
    })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(requestedUrl, '/api/pipeline/logs/stream')
  assert.equal(requestedUrl.includes('top-secret-session'), false)
  assert.equal(requestedUrl.includes('access_token='), false)
  assert.equal(requestedHeaders.get('Authorization'), 'Bearer top-secret-session')
  assert.equal(requestedHeaders.get('Accept'), 'text/event-stream')
  assert.deepEqual(events.map((event) => JSON.parse(event.data)), [{ line: 'ok' }])
})

test('apiRequest clears the stored token on a 401 response', async () => {
  installStorage()
  setToken('expired-session')
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: { code: 'session_revoked', message: 'session revoked' },
  }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  })

  try {
    await assert.rejects(
      apiRequest('/protected'),
      (error) => error instanceof ApiError
        && error.status === 401
        && error.code === 'session_revoked',
    )
  } finally {
    globalThis.fetch = originalFetch
  }
  assert.equal(getToken(), '')
})

test('consumeEventStream forwards aborts without retrying or swallowing them', async () => {
  installStorage()
  const originalFetch = globalThis.fetch
  const controller = new AbortController()
  globalThis.fetch = async (_input, init) => new Promise((_resolve, reject) => {
    const rejectAbort = () => reject(new DOMException('aborted', 'AbortError'))
    if (init?.signal?.aborted) {
      rejectAbort()
      return
    }
    init?.signal?.addEventListener('abort', rejectAbort, { once: true })
  })

  try {
    const stream = consumeEventStream('/pipeline/logs/stream', {
      signal: controller.signal,
      onEvent: () => undefined,
    })
    controller.abort()
    await assert.rejects(stream, isAbortError)
  } finally {
    globalThis.fetch = originalFetch
  }
})
