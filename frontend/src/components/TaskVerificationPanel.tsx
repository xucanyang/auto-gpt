import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Input, Space, Tag, Typography, message } from 'antd'
import { CopyOutlined, LockOutlined } from '@ant-design/icons'

import { apiFetch } from '@/lib/utils'

const { Text } = Typography

export interface PendingVerificationChallenge {
  challenge_id: string
  phase: string
  phase_label: string
  email: string
  created_at?: number
  expires_at?: number
  timeout_seconds?: number
}

type TaskVerificationPanelProps = {
  taskId: string
  verification?: PendingVerificationChallenge | null
}

function formatRemainingSeconds(seconds: number): string {
  const normalized = Math.max(0, Math.ceil(seconds))
  const minutes = Math.floor(normalized / 60)
  const secs = normalized % 60
  if (minutes <= 0) return `${secs}s`
  return `${minutes}m ${secs}s`
}

export function TaskVerificationPanel({
  taskId,
  verification,
}: TaskVerificationPanelProps) {
  const [code, setCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [now, setNow] = useState(Date.now())
  const inputRef = useRef<any>(null)
  const cardRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    setCode('')
    window.setTimeout(() => {
      inputRef.current?.focus?.()
      cardRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
    }, 50)
  }, [verification?.challenge_id])

  useEffect(() => {
    if (!verification?.challenge_id) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [verification?.challenge_id])

  const secondsLeft = useMemo(() => {
    if (!verification?.expires_at) return 0
    return Math.max(0, verification.expires_at * 1000 - now) / 1000
  }, [now, verification?.expires_at])

  if (!verification?.challenge_id) {
    return null
  }

  const handleCopyEmail = async () => {
    const email = String(verification?.email || '').trim()
    if (!email) return
    try {
      await navigator.clipboard.writeText(email)
      message.success('邮箱已复制')
    } catch {
      message.error('复制邮箱失败')
    }
  }

  const handleSubmit = async () => {
    const normalizedCode = code.trim()
    if (!normalizedCode) {
      message.error('请输入验证码')
      return
    }

    setSubmitting(true)
    try {
      await apiFetch(`/tasks/${taskId}/submit-verification`, {
        method: 'POST',
        body: JSON.stringify({
          challenge_id: verification.challenge_id,
          code: normalizedCode,
        }),
      })
      message.success(`${verification.phase_label} 已提交`)
      setCode('')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '提交验证码失败'
      message.error(detail)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div ref={cardRef}>
      <Card
        title="等待人工验证码"
        style={{ marginTop: 16, borderColor: '#f59e0b' }}
        extra={
          <Space size={8}>
            <Tag color={secondsLeft <= 30 ? 'error' : 'warning'}>
              剩余 {formatRemainingSeconds(secondsLeft)}
            </Tag>
            <Button size="small" icon={<CopyOutlined />} onClick={handleCopyEmail}>
              复制邮箱
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          showIcon
          type="warning"
          message={verification.phase_label || '邮箱验证码'}
          description={
            <Space direction="vertical" size={4}>
              <Text>邮箱：{verification.email || '未知邮箱'}</Text>
              <Space wrap>
                <Tag color="gold">阶段：{verification.phase || 'email_otp'}</Tag>
                <Tag>challenge：{verification.challenge_id}</Tag>
              </Space>
            </Space>
          }
        />
        <Input
          ref={inputRef}
          prefix={<LockOutlined />}
          value={code}
          placeholder="请输入邮箱里的验证码"
          onChange={(event) => setCode(event.target.value)}
          onPressEnter={handleSubmit}
          autoComplete="one-time-code"
          autoFocus
        />
        <Text type="secondary">
          如果你邮箱里收到了新验证码，直接覆盖输入并回车即可；这个面板会跟着任务阶段自动切到下一次 challenge。
        </Text>
          <Space>
            <Button type="primary" onClick={handleSubmit} loading={submitting} disabled={!code.trim()}>
              提交验证码
            </Button>
            <Button onClick={() => setCode('')} disabled={!code}>
              清空
            </Button>
          </Space>
        </Space>
      </Card>
    </div>
  )
}
