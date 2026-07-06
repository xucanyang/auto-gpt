import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge, Button, Card, Descriptions, message, Segmented, Space, Tag, theme } from 'antd'
import { CopyOutlined, FastForwardOutlined, StopOutlined } from '@ant-design/icons'

import IdeaSubmitSummary from '@/components/idea/IdeaSubmitSummary'
import { API_BASE, apiFetch, getToken } from '@/lib/utils'

interface TaskLogPanelProps {
  taskId: string
  onDone?: () => Promise<void> | void
}

type TaskTerminalStatus = 'idle' | 'done' | 'failed' | 'stopped'
type LogViewMode = 'info' | 'debug'
type TaskCurrentState = {
  task?: string
  task_label?: string
  item_index?: number
  item_total?: number
  email?: string
  account_id?: number
  phone?: string
  phase?: string
  phase_label?: string
  stage_index?: number
  stage_total?: number
  started_at?: string
  last_message?: string
  next_step?: string
  resource_touched?: boolean
}

const LOG_VIEW_STORAGE_KEY = 'task-log-panel-view-mode'

function parseLogLine(rawLine: string) {
  const line = String(rawLine || '')
  const timeMatch = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/)
  const time = timeMatch?.[1] || ''
  const normalized = line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '')
  const isDebug = /^\[[^\]]*DEBUG[^\]]*\]/i.test(normalized)
  const text = isDebug ? normalized.replace(/^\[[^\]]*DEBUG[^\]]*\]\s*/, '') : normalized
  return { raw: line, text, isDebug, time }
}

export function TaskLogPanel({ taskId, onDone }: TaskLogPanelProps) {
  const { token } = theme.useToken()
  const [lines, setLines] = useState<string[]>([])
  const [error, setError] = useState('')
  const [terminalStatus, setTerminalStatus] = useState<TaskTerminalStatus>('idle')
  const [taskSnapshot, setTaskSnapshot] = useState<any>(null)
  const [current, setCurrent] = useState<TaskCurrentState | null>(null)
  const [currentNow, setCurrentNow] = useState(() => Date.now())
  const [pageVisible, setPageVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  )
  const [skipLoading, setSkipLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [stopRequested, setStopRequested] = useState(false)
  const [viewMode, setViewMode] = useState<LogViewMode>(() => {
    if (typeof window === 'undefined') return 'info'
    const saved = window.localStorage.getItem(LOG_VIEW_STORAGE_KEY)
    return saved === 'debug' ? 'debug' : 'info'
  })
  const panelRef = useRef<HTMLDivElement>(null)
  const onDoneRef = useRef(onDone)
  const nextSinceRef = useRef(0)
  const terminalNotifyRef = useRef('')
  const doneCallbackNotifyRef = useRef('')

  const isFinished = terminalStatus !== 'idle' || stopRequested

  const parsedLines = useMemo(() => lines.map(parseLogLine), [lines])
  const infoCount = useMemo(() => parsedLines.filter((line) => !line.isDebug).length, [parsedLines])
  const debugCount = useMemo(() => parsedLines.filter((line) => line.isDebug).length, [parsedLines])
  const visibleLines = useMemo(
    () => parsedLines.filter((line) => (viewMode === 'debug' ? line.isDebug : !line.isDebug)),
    [parsedLines, viewMode],
  )

  const handleCopyAll = async () => {
    try {
      await navigator.clipboard.writeText(visibleLines.map((line) => line.raw).join('\n'))
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleSkipCurrent = async () => {
    if (isFinished) return
    setSkipLoading(true)
    try {
      const response = await apiFetch(`/tasks/${taskId}/skip-current`, { method: 'POST' }) as {
        control?: { targeted_skip_attempts?: number }
      }
      const targeted = Number(response.control?.targeted_skip_attempts || 0)
      message.success(
        targeted > 1
          ? `已发送跳过 ${targeted} 个进行中账号请求`
          : '已发送跳过当前账号请求',
      )
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setSkipLoading(false)
    }
  }

  const handleStopTask = async () => {
    if (isFinished) return
    setStopLoading(true)
    try {
      await apiFetch(`/tasks/${taskId}/stop`, { method: 'POST' })
      setStopRequested(true)
      message.success('已发送停止任务请求，正在停止进行中的线程')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setStopLoading(false)
    }
  }

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(LOG_VIEW_STORAGE_KEY, viewMode)
  }, [viewMode])

  useEffect(() => {
    if (typeof document === 'undefined') return
    const updateVisibility = () => {
      setPageVisible(document.visibilityState === 'visible')
    }
    updateVisibility()
    document.addEventListener('visibilitychange', updateVisibility)
    return () => {
      document.removeEventListener('visibilitychange', updateVisibility)
    }
  }, [])

  useEffect(() => {
    if (!current?.started_at) return
    const timer = window.setInterval(() => setCurrentNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [current?.started_at])

  useEffect(() => {
    if (!taskId || !pageVisible) return
    const controller = new AbortController()
    let cancelled = false
    const baseRetryMs = 1000
    const maxRetryMs = 8000
    nextSinceRef.current = 0
    setLines([])
    setError('')
    setTerminalStatus('idle')
    setStopRequested(false)
    setTaskSnapshot(null)

    const sleep = async (ms: number) =>
      new Promise((resolve) => setTimeout(resolve, ms))

    const notifyTaskDone = (status?: TaskTerminalStatus | string) => {
      const key = `${taskId}:done`
      if (doneCallbackNotifyRef.current === key) return
      doneCallbackNotifyRef.current = key

      window.setTimeout(() => {
        if (cancelled) return
        void Promise.resolve(onDoneRef.current?.()).catch((error_: unknown) => {
          const detail = error_ instanceof Error ? error_.message : '刷新页面状态失败'
          message.warning(`任务已结束，但刷新页面状态失败：${detail}`)
        })
      }, status === 'failed' ? 0 : 500)
    }

    const initSnapshot = async (): Promise<boolean> => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`) as {
          logs?: string[]
          status?: TaskTerminalStatus | string
          control?: { stop_requested?: boolean }
          meta?: { current?: TaskCurrentState }
        }
        if (cancelled) return true

        setTaskSnapshot(snapshot)
        const snapshotLines = Array.isArray(snapshot.logs) ? snapshot.logs : []
        setLines(snapshotLines)
        nextSinceRef.current = snapshotLines.length
        setStopRequested(Boolean(snapshot.control?.stop_requested))
        setCurrent(snapshot?.meta?.current && typeof snapshot.meta.current === 'object' ? snapshot.meta.current : null)

        if (snapshot.status === 'done' || snapshot.status === 'failed' || snapshot.status === 'stopped') {
          setTerminalStatus(snapshot.status)
          // Keep the completed task's snapshot logs visible. The SSE stream immediately ends
          // for terminal tasks and otherwise would leave the panel looking empty.
          notifyTaskDone(snapshot.status)
          return true
        }
      } catch (error_: unknown) {
        if (!cancelled) {
          const detail = error_ instanceof Error ? error_.message : '获取任务快照失败'
          setError(detail)
        }
      }
      return false
    }

    const connectStreamOnce = async (): Promise<boolean> => {
      try {
        const token = getToken()
        const headers: Record<string, string> = {}
        if (token) headers.Authorization = `Bearer ${token}`

        const since = nextSinceRef.current
        const response = await fetch(`${API_BASE}/tasks/${taskId}/logs/stream?since=${since}`, {
          headers,
          signal: controller.signal,
        })

        if (!response.ok) {
          setError(`日志流连接失败 (${response.status})`)
          return true
        }

        if (!response.body) {
          setError('日志流未返回可读数据')
          return false
        }

        setError('')
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (!cancelled) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const parts = buffer.split('\n\n')
          buffer = parts.pop() || ''

          for (const part of parts) {
            const match = part.match(/^data:\s*(.+)$/m)
            if (!match) continue
            try {
              const payload = JSON.parse(match[1]) as {
                line?: string
                done?: boolean
                status?: TaskTerminalStatus
              }
              if (payload.line) {
                nextSinceRef.current += 1
                setLines((previous) => [...previous, payload.line!])
              }
              if (payload.done) {
                setTerminalStatus(payload.status || 'done')
                void apiFetch(`/tasks/${taskId}`)
                  .then((snapshot) => {
                    if (!cancelled) setTaskSnapshot(snapshot)
                  })
                  .catch(() => undefined)
                // Notify the parent after the terminal state is visible so pages can refresh
                // their business data without making the final log lines disappear first.
                notifyTaskDone(payload.status || 'done')
                return true
              }
            } catch {
              // ignore malformed SSE payload
            }
          }
        }

        return false
      } catch (error_: unknown) {
        if (!cancelled && !(error_ instanceof DOMException && error_.name === 'AbortError')) {
          return false
        }
        return true
      }
    }

    const connectStream = async () => {
      const shouldStopImmediately = await initSnapshot()
      if (shouldStopImmediately || cancelled) return

      let retryCount = 0
      while (!cancelled) {
        const shouldStop = await connectStreamOnce()
        if (shouldStop || cancelled) return

        retryCount += 1
        const retryMs = Math.min(baseRetryMs * (2 ** (retryCount - 1)), maxRetryMs)
        setError(`日志流连接中断，${retryMs / 1000}s 后重试（第 ${retryCount} 次）`)
        await sleep(retryMs)
      }
    }

    void connectStream()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [taskId, pageVisible])

  useEffect(() => {
    if (!panelRef.current) return
    panelRef.current.scrollTop = panelRef.current.scrollHeight
  }, [lines])

  useEffect(() => {
    if (!taskId || terminalStatus !== 'failed') return
    const key = `${taskId}:failed`
    if (terminalNotifyRef.current === key) return
    terminalNotifyRef.current = key
    message.error('任务失败，请查看日志里的失败原因')
  }, [taskId, terminalStatus])

  const footerText =
    terminalStatus === 'done'
      ? { text: '任务完成', color: '#10b981' }
      : terminalStatus === 'stopped'
        ? { text: '任务已停止', color: '#d97706' }
        : terminalStatus === 'failed'
          ? { text: '任务失败', color: '#dc2626' }
          : null

  const currentElapsedText = useMemo(() => {
    if (!current?.started_at) return ''
    const started = Date.parse(current.started_at)
    if (!Number.isFinite(started)) return ''
    const diff = Math.max(0, Math.floor((currentNow - started) / 1000))
    const minutes = Math.floor(diff / 60)
    const seconds = diff % 60
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }, [current?.started_at, currentNow])

  const ideaSubmitSummary = taskSnapshot?.meta?.idea_submit_summary
  const showIdeaSubmitSummary = String(taskSnapshot?.source || taskSnapshot?.meta?.source || '').trim() === 'baxigpt_cdk_submit'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        <Space>
          <Button
            size="small"
            icon={<FastForwardOutlined />}
            onClick={handleSkipCurrent}
            loading={skipLoading}
            disabled={isFinished}
          >
            跳过当前账号
          </Button>
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            onClick={handleStopTask}
            loading={stopLoading}
            disabled={isFinished}
          >
            停止任务
          </Button>
        </Space>
        <Space>
          <Segmented
            size="small"
            value={viewMode}
            onChange={(value) => setViewMode(value as LogViewMode)}
            options={[
              {
                label: (
                  <Space size={4}>
                    <span>Info</span>
                    <Badge count={infoCount} size="small" style={{ backgroundColor: '#64748b' }} />
                  </Space>
                ),
                value: 'info',
              },
              {
                label: (
                  <Space size={4}>
                    <span>Debug</span>
                    <Badge count={debugCount} size="small" style={{ backgroundColor: '#7c3aed' }} />
                  </Space>
                ),
                value: 'debug',
              },
            ]}
          />
          <Button size="small" icon={<CopyOutlined />} onClick={handleCopyAll} disabled={visibleLines.length === 0}>
            复制日志
          </Button>
        </Space>
      </div>

      {current && (current.phase_label || current.email || current.phone || current.account_id) ? (
        <Card
          size="small"
          style={{
            marginBottom: 8,
            borderColor: 'rgba(255, 255, 255, 0.14)',
            background: '#1c1f2e',
          }}
          bodyStyle={{ padding: 12 }}
        >
          <Descriptions
            size="small"
            column={2}
            labelStyle={{ color: '#b0bcd4', fontWeight: 600 }}
            contentStyle={{ color: '#f1f5f9' }}
            items={[
              {
                key: 'current-target',
                label: '当前',
                children: (
                  <Space size={6} wrap>
                    {current.item_index && current.item_total ? (
                      <Tag color="blue">{current.item_index}/{current.item_total}</Tag>
                    ) : null}
                    <span>{current.email || current.phone || (current.account_id ? `账号 ${current.account_id}` : '-')}</span>
                  </Space>
                ),
              },
              {
                key: 'current-phase',
                label: '阶段',
                children: (
                  <Space size={6} wrap>
                    {current.stage_index && current.stage_total ? (
                      <Tag color="processing">{current.stage_index}/{current.stage_total}</Tag>
                    ) : null}
                    <span>{current.phase_label || current.phase || '-'}</span>
                  </Space>
                ),
              },
              {
                key: 'current-elapsed',
                label: '已等待',
                children: currentElapsedText || '-',
              },
              {
                key: 'current-next',
                label: '下一步',
                children: current.next_step || current.last_message || '-',
              },
              {
                key: 'current-touched',
                label: '资源触碰',
                children:
                  typeof current.resource_touched === 'boolean'
                    ? <Tag color={current.resource_touched ? 'warning' : 'default'}>{current.resource_touched ? '已触碰' : '未触碰'}</Tag>
                    : '-',
              },
              {
                key: 'current-message',
                label: '状态',
                children: current.last_message || '-',
              },
            ]}
          />
        </Card>
      ) : null}

      {showIdeaSubmitSummary ? <IdeaSubmitSummary summary={ideaSubmitSummary} /> : null}

      <div
        ref={panelRef}
        className="log-panel"
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'auto',
          background: token.colorBgContainer,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: 8,
          padding: 12,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontSize: 12,
          color: token.colorText,
          minHeight: 240,
          maxHeight: 'calc(100vh - 320px)',
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {visibleLines.length === 0 && !error && (
          <div style={{ color: token.colorTextTertiary }}>
            {lines.length === 0 ? '等待日志...' : `当前 ${viewMode === 'debug' ? 'Debug' : 'Info'} 视图下没有可显示的日志`}
          </div>
        )}
        {error && <div style={{ color: '#dc2626' }}>{error}</div>}
        {visibleLines.map((line, index) => {
          return (
            <div
              key={`${index}-${line.raw}`}
              style={{
                lineHeight: 1.65,
                margin: line.isDebug ? '2px 0' : 0,
                padding: line.isDebug ? '2px 8px' : 0,
                border: line.isDebug ? `1px solid ${token.colorPrimaryBorder}` : '1px solid transparent',
                borderRadius: line.isDebug ? 4 : 0,
                background: line.isDebug ? token.colorPrimaryBg : 'transparent',
                color: line.isDebug
                  ? token.colorPrimaryText
                  : line.text.includes('✓') || line.text.includes('成功')
                    ? token.colorSuccessText
                    : line.text.includes('✗') || line.text.includes('失败') || line.text.includes('错误')
                      ? token.colorErrorText
                      : line.text.includes('停止') || line.text.includes('跳过')
                        ? token.colorWarningText
                        : token.colorText,
              }}
            >
              {line.raw}
            </div>
          )
        })}
      </div>

      {footerText ? (
        <div style={{ fontSize: 12, color: footerText.color, marginTop: 8 }}>
          {footerText.text}
        </div>
      ) : null}
    </div>
  )
}

export default TaskLogPanel
