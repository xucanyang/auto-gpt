import { ApiError, apiErrorFromResponse, apiRequest } from './utils.ts'

export type ServerSentEvent = {
  data: string
  event: string
  id?: string
  retry?: number
}

export class SseParser {
  private buffer = ''
  private dataLines: string[] = []
  private eventType = ''
  private lastEventId = ''
  private retry: number | undefined
  private hasData = false
  private firstLine = true

  push(chunk: string): ServerSentEvent[] {
    this.buffer += chunk
    return this.drain(false)
  }

  finish(chunk = ''): ServerSentEvent[] {
    this.buffer += chunk
    const events = this.drain(true)
    const finalEvent = this.dispatch()
    if (finalEvent) events.push(finalEvent)
    return events
  }

  private drain(final: boolean): ServerSentEvent[] {
    const events: ServerSentEvent[] = []
    while (this.buffer.length > 0) {
      let delimiterIndex = -1
      for (let index = 0; index < this.buffer.length; index += 1) {
        const char = this.buffer[index]
        if (char === '\n' || char === '\r') {
          delimiterIndex = index
          break
        }
      }

      if (delimiterIndex < 0) break
      if (!final && this.buffer[delimiterIndex] === '\r' && delimiterIndex === this.buffer.length - 1) {
        break
      }

      const line = this.buffer.slice(0, delimiterIndex)
      const isCrLf = this.buffer[delimiterIndex] === '\r' && this.buffer[delimiterIndex + 1] === '\n'
      this.buffer = this.buffer.slice(delimiterIndex + (isCrLf ? 2 : 1))
      const event = this.processLine(line)
      if (event) events.push(event)
    }

    if (final && this.buffer.length > 0) {
      const event = this.processLine(this.buffer)
      this.buffer = ''
      if (event) events.push(event)
    }
    return events
  }

  private processLine(rawLine: string): ServerSentEvent | null {
    const line = this.firstLine ? rawLine.replace(/^\uFEFF/, '') : rawLine
    this.firstLine = false
    if (line === '') return this.dispatch()
    if (line.startsWith(':')) return null

    const separator = line.indexOf(':')
    const field = separator < 0 ? line : line.slice(0, separator)
    let value = separator < 0 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'data') {
      this.hasData = true
      this.dataLines.push(value)
    } else if (field === 'event') {
      this.eventType = value
    } else if (field === 'id' && !value.includes('\0')) {
      this.lastEventId = value
    } else if (field === 'retry' && /^\d+$/.test(value)) {
      this.retry = Number(value)
    }
    return null
  }

  private dispatch(): ServerSentEvent | null {
    if (!this.hasData) {
      this.eventType = ''
      return null
    }

    const event: ServerSentEvent = {
      data: this.dataLines.join('\n'),
      event: this.eventType || 'message',
    }
    if (this.lastEventId) event.id = this.lastEventId
    if (this.retry !== undefined) event.retry = this.retry

    this.dataLines = []
    this.eventType = ''
    this.hasData = false
    return event
  }
}

export type ConsumeEventStreamOptions = {
  signal?: AbortSignal
  headers?: HeadersInit
  onOpen?: (response: Response) => void
  onEvent: (event: ServerSentEvent) => boolean | void | Promise<boolean | void>
}

export async function consumeEventStream(
  path: string,
  options: ConsumeEventStreamOptions,
): Promise<void> {
  const headers = new Headers(options.headers)
  headers.set('Accept', 'text/event-stream')
  headers.set('Cache-Control', 'no-cache')

  const response = await apiRequest(path, {
    method: 'GET',
    headers,
    cache: 'no-store',
    signal: options.signal,
  })
  if (!response.ok) throw await apiErrorFromResponse(response)
  if (!response.body) {
    throw new ApiError(response.status, '日志流未返回可读数据', null)
  }
  options.onOpen?.(response)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SseParser()

  try {
    while (true) {
      const { done, value } = await reader.read()
      const events = done
        ? parser.finish(decoder.decode())
        : parser.push(decoder.decode(value, { stream: true }))

      for (const event of events) {
        if (await options.onEvent(event) === false) {
          await reader.cancel()
          return
        }
      }
      if (done) return
    }
  } finally {
    reader.releaseLock()
  }
}

export function isAbortError(error: unknown): boolean {
  return error !== null
    && typeof error === 'object'
    && 'name' in error
    && error.name === 'AbortError'
}
