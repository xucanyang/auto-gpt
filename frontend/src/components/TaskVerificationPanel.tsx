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
  metadata?: Record<string, any>
  actions?: string[]
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
  const [actionLoading, setActionLoading] = useState('')
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

  const phase = String(verification.phase || '').trim().toLowerCase()
  const metadata = verification.metadata || {}
  const isPhoneLike = phase.includes('phone') || phase.includes('sms') || phase.includes('paypal')
  const isPhoneOtp = phase.includes('phone')
  const phone = String(metadata.phone || metadata.masked_phone || verification.email || '').trim()
  const accountEmail = String(metadata.account_email || '').trim()
  const currentChannel = String(metadata.channel || '').trim().toLowerCase()
  const availableChannels = Array.isArray(metadata.available_channels) ? metadata.available_channels.map((item) => String(item)) : []
  const actions = Array.isArray(verification.actions) ? verification.actions : []
  const targetLabel = isPhoneLike ? '接收方' : '邮箱'
  const inputPlaceholder = isPhoneOtp
    ? '请输入 SMS / WhatsApp 手机验证码'
    : isPhoneLike
      ? '请输入短信 / PayPal OTP'
      : '请输入邮箱里的验证码'
  const helperText = isPhoneOtp
    ? '当前只验证这个手机号，不会换新手机号；可切换 SMS / WhatsApp 通道、重新发送，或跳过当前账号。60 秒无输入会自动跳过。'
    : isPhoneLike
      ? '收到 PayPal 短信 OTP 后直接输入并回车；这个面板会跟着任务阶段自动切到下一次 challenge。'
    : '如果你邮箱里收到了新验证码，直接覆盖输入并回车即可；这个面板会跟着任务阶段自动切到下一次 challenge。'

  const handleCopyTarget = async () => {
    const target = String(verification?.email || '').trim()
    if (!target) return
    try {
      await navigator.clipboard.writeText(target)
      message.success(`${targetLabel}已复制`)
    } catch {
      message.error(`复制${targetLabel}失败`)
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

  const handleAction = async (action: string, payload: Record<string, any> = {}) => {
    setActionLoading(`${action}:${payload.channel || ''}`)
    try {
      await apiFetch(`/tasks/${taskId}/verification-action`, {
        method: 'POST',
        body: JSON.stringify({
          challenge_id: verification.challenge_id,
          action,
          payload,
        }),
      })
      message.success(action === 'switch_channel' ? '已请求切换通道' : '已请求重新发送')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '验证码动作失败'
      message.error(detail)
    } finally {
      setActionLoading('')
    }
  }

  const handleSkipCurrent = async () => {
    setActionLoading('skip')
    try {
      await apiFetch(`/tasks/${taskId}/skip-current`, { method: 'POST' })
      message.success('已请求跳过当前账号')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '跳过当前账号失败'
      message.error(detail)
    } finally {
      setActionLoading('')
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
            <Button size="small" icon={<CopyOutlined />} onClick={handleCopyTarget}>
              复制{targetLabel}
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
              <Text>{targetLabel}：{verification.email || `未知${targetLabel}`}</Text>
              {isPhoneOtp && accountEmail ? <Text>账号：{accountEmail}</Text> : null}
              {isPhoneOtp && phone ? <Text>手机号：{phone}</Text> : null}
              <Space wrap>
                <Tag color="gold">阶段：{verification.phase || 'email_otp'}</Tag>
                {isPhoneOtp && currentChannel ? <Tag color={currentChannel === 'sms' ? 'blue' : 'green'}>通道：{currentChannel.toUpperCase()}</Tag> : null}
                <Tag>challenge：{verification.challenge_id}</Tag>
              </Space>
              {isPhoneOtp && metadata.reason ? <Text type="secondary">{String(metadata.reason)}</Text> : null}
              {isPhoneOtp && metadata.action_status === 'failed' && metadata.action_error ? (
                <Text type="danger">动作失败：{String(metadata.action_error)}</Text>
              ) : null}
              {isPhoneOtp && metadata.last_action_detail ? (
                <Text type="secondary">最近动作：{String(metadata.last_action_detail)}</Text>
              ) : null}
            </Space>
          }
        />
        <Input
          ref={inputRef}
          prefix={<LockOutlined />}
          value={code}
          placeholder={inputPlaceholder}
          onChange={(event) => setCode(event.target.value)}
          onPressEnter={handleSubmit}
          autoComplete="one-time-code"
          autoFocus
        />
        <Text type="secondary">
          {helperText}
        </Text>
          <Space>
            <Button type="primary" onClick={handleSubmit} loading={submitting} disabled={!code.trim()}>
              提交验证码
            </Button>
            {isPhoneOtp && actions.includes('switch_channel') && availableChannels.includes('sms') ? (
              <Button
                onClick={() => handleAction('switch_channel', { channel: 'sms' })}
                loading={actionLoading === 'switch_channel:sms'}
                disabled={currentChannel === 'sms'}
              >
                切换 SMS
              </Button>
            ) : null}
            {isPhoneOtp && actions.includes('switch_channel') && availableChannels.includes('whatsapp') ? (
              <Button
                onClick={() => handleAction('switch_channel', { channel: 'whatsapp' })}
                loading={actionLoading === 'switch_channel:whatsapp'}
                disabled={currentChannel === 'whatsapp'}
              >
                切换 WhatsApp
              </Button>
            ) : null}
            {isPhoneOtp && actions.includes('resend') ? (
              <Button onClick={() => handleAction('resend')} loading={actionLoading === 'resend:'}>
                重新发送
              </Button>
            ) : null}
            {isPhoneOtp ? (
              <Button danger onClick={handleSkipCurrent} loading={actionLoading === 'skip'}>
                跳过当前账号
              </Button>
            ) : null}
            <Button onClick={() => setCode('')} disabled={!code}>
              清空
            </Button>
          </Space>
        </Space>
      </Card>
    </div>
  )
}
