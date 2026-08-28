import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Steps,
  Tag,
  Typography,
  message,
} from 'antd'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'

import { TaskLogPanel } from '@/components/TaskLogPanel'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

type ChangeEmailTaskModalProps = {
  account: {
    id?: number | string
    email?: string
  }
  open: boolean
  initialTaskId?: string | null
  onClose: () => void
  onRefresh?: () => Promise<void> | void
  onTaskStarted?: (taskId: string) => Promise<void> | void
}

type EmailChangeDetail = {
  reservation_ref?: string
  task_id?: string
  target_mailbox_ref?: string
  target_email?: string
  provider?: string
  phase?: string
  status?: string
  remove_social_subs?: boolean
  verify_submitted_at?: string
  remote_changed_at?: string
  remote_boundary_crossed?: boolean
  runtime_active?: boolean
  resumable?: boolean
  error?: string
  lease_expires_at?: string
}

type EmailChangeMailboxOptions = {
  tempmail_domains?: string[]
  pending?: EmailChangeDetail | null
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}

function providerLabel(provider: string) {
  switch (String(provider || '').trim().toLowerCase()) {
    case 'hme_ready_api': return 'HME Ready 自动分配'
    case 'tempmail_local': return 'TempMail 新建并锁定'
    case 'manual_email_otp': return '手动外部邮箱'
    default: return provider || '目标邮箱'
  }
}

function changePhaseStep(phase: string) {
  const value = String(phase || '').trim().toLowerCase()
  if (value === 'committed') return { current: 3, status: 'finish' as const }
  if (value === 'recovery_required') return { current: 2, status: 'error' as const }
  if (['remote_email_changed', 'waiting_target_login_otp', 'session_captured', 'identity_verified'].includes(value)) {
    return { current: 2, status: 'process' as const }
  }
  if (['waiting_change_otp', 'begin_sent'].includes(value)) return { current: 1, status: 'process' as const }
  if (['rate_limited', 'source_reauth_required'].includes(value)) return { current: 0, status: 'error' as const }
  return { current: 0, status: 'process' as const }
}

function isRemoteBoundaryCrossed(detail: EmailChangeDetail | null) {
  return Boolean(detail?.remote_boundary_crossed || detail?.remote_changed_at || detail?.verify_submitted_at)
}

export function ChangeEmailTaskModal({
  account,
  open,
  initialTaskId,
  onClose,
  onRefresh,
  onTaskStarted,
}: ChangeEmailTaskModalProps) {
  const accountId = Number(account?.id || 0)
  const initialTaskIdValue = String(initialTaskId || '').trim()
  const [provider, setProvider] = useState('tempmail_local')
  const [domain, setDomain] = useState('')
  const [manualEmail, setManualEmail] = useState('')
  const [removeSocialSubs, setRemoveSocialSubs] = useState(false)
  const [domains, setDomains] = useState<string[]>([])
  const [loadingOptions, setLoadingOptions] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const [starting, setStarting] = useState(false)
  const [releasing, setReleasing] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [error, setError] = useState('')
  const [prepared, setPrepared] = useState<EmailChangeDetail | null>(null)
  const [taskId, setTaskId] = useState('')
  const [taskRunVersion, setTaskRunVersion] = useState(0)
  const requestGenerationRef = useRef(0)
  const mutationInFlightRef = useRef(false)

  const step = useMemo(() => changePhaseStep(prepared?.phase || ''), [prepared?.phase])
  const targetReady = Boolean(prepared?.reservation_ref && prepared?.target_mailbox_ref && prepared?.target_email)
  const remoteBoundaryCrossed = isRemoteBoundaryCrossed(prepared)
  const taskRunning = Boolean(prepared?.runtime_active || prepared?.status === 'running')

  const loadOptions = useCallback(async (generation = requestGenerationRef.current) => {
    if (!accountId) return
    if (requestGenerationRef.current !== generation) return
    setLoadingOptions(true)
    try {
      const payload = await apiFetch(
        `/tasks/chatgpt/email-change/mailboxes?account_id=${accountId}`,
      ) as EmailChangeMailboxOptions
      if (requestGenerationRef.current !== generation) return
      setDomains(Array.isArray(payload?.tempmail_domains) ? payload.tempmail_domains : [])
      const pending = payload?.pending
      if (pending?.reservation_ref) {
        setPrepared(pending)
        setProvider(String(pending.provider || 'tempmail_local'))
        setRemoveSocialSubs(Boolean(pending.remove_social_subs))
        setTaskId(String(pending.task_id || ''))
      }
    } catch (exc: unknown) {
      if (requestGenerationRef.current === generation) {
        setError(errorMessage(exc, '读取目标邮箱选项失败'))
      }
    } finally {
      if (requestGenerationRef.current === generation) setLoadingOptions(false)
    }
  }, [accountId])

  const loadTaskDetail = useCallback(async (
    requestedTaskId = taskId,
    generation = requestGenerationRef.current,
  ) => {
    if (!requestedTaskId) return null
    const detail = await apiFetch(
      `/tasks/chatgpt/email-change/tasks/${encodeURIComponent(requestedTaskId)}`,
    ) as EmailChangeDetail
    if (requestGenerationRef.current === generation) setPrepared(detail)
    return detail
  }, [taskId])

  useEffect(() => {
    const generation = requestGenerationRef.current + 1
    requestGenerationRef.current = generation
    if (!open || !accountId) return
    setError('')
    setPrepared(null)
    setTaskId('')
    setTaskRunVersion(0)
    setProvider('tempmail_local')
    setDomain('')
    setManualEmail('')
    setRemoveSocialSubs(false)
    if (initialTaskIdValue) {
      setTaskId(initialTaskIdValue)
    } else {
      void loadOptions(generation)
    }
    return () => {
      if (requestGenerationRef.current === generation) {
        requestGenerationRef.current += 1
      }
    }
  }, [accountId, initialTaskIdValue, loadOptions, open])

  useEffect(() => {
    if (!open || !taskId) return
    let cancelled = false
    let timer = 0
    const generation = requestGenerationRef.current
    const requestedTaskId = taskId
    const poll = async () => {
      try {
        const detail = await loadTaskDetail(requestedTaskId, generation)
        if (cancelled || requestGenerationRef.current !== generation) return
        const status = String(detail?.status || '')
        if (status === 'done') await onRefresh?.()
        if (['done', 'failed', 'partial', 'released'].includes(status)) return
      } catch (exc: unknown) {
        if (!cancelled && requestGenerationRef.current === generation) {
          setError(errorMessage(exc, '读取邮箱换绑状态失败'))
        }
      }
      if (!cancelled && requestGenerationRef.current === generation) {
        timer = window.setTimeout(poll, 1500)
      }
    }
    void poll()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [loadTaskDetail, onRefresh, open, taskId, taskRunVersion])

  const prepareTarget = async () => {
    if (!accountId || targetReady || mutationInFlightRef.current) return
    mutationInFlightRef.current = true
    setPreparing(true)
    setError('')
    try {
      const body: Record<string, unknown> = {
        account_id: accountId,
        provider,
        remove_social_subs: removeSocialSubs,
      }
      if (provider === 'manual_email_otp') body.target_email = manualEmail.trim()
      if (provider === 'tempmail_local') body.domain = domain
      const result = await apiFetch('/tasks/chatgpt/email-change/prepare-target', {
        method: 'POST',
        body: JSON.stringify(body),
      }) as EmailChangeDetail
      setPrepared(result)
      message.success(`目标邮箱已锁定：${result?.target_email || '-'}`)
    } catch (exc: unknown) {
      setError(errorMessage(exc, '准备目标邮箱失败'))
    } finally {
      mutationInFlightRef.current = false
      setPreparing(false)
    }
  }

  const startTask = async () => {
    const targetRef = String(prepared?.target_mailbox_ref || '').trim()
    if (!accountId || !targetRef || prepared?.status !== 'created' || mutationInFlightRef.current) return
    mutationInFlightRef.current = true
    setStarting(true)
    setError('')
    try {
      const result = await apiFetch('/tasks/chatgpt/email-change', {
        method: 'POST',
        body: JSON.stringify({ account_id: accountId, target_mailbox_ref: targetRef }),
      }) as { task_id?: string }
      const nextTaskId = String(result?.task_id || '').trim()
      if (!nextTaskId) throw new Error('任务已创建但未返回 task_id')
      requestGenerationRef.current += 1
      setTaskId(nextTaskId)
      setTaskRunVersion((value) => value + 1)
      setPrepared((current) => ({
        ...(current || {}),
        task_id: nextTaskId,
        phase: 'created',
        status: 'running',
        runtime_active: true,
      }))
      await onTaskStarted?.(nextTaskId)
      message.success('邮箱换绑任务已启动')
    } catch (exc: unknown) {
      setError(errorMessage(exc, '启动邮箱换绑失败'))
    } finally {
      mutationInFlightRef.current = false
      setStarting(false)
    }
  }

  const releaseTarget = async () => {
    const reservationRef = String(prepared?.reservation_ref || '').trim()
    if (!reservationRef || remoteBoundaryCrossed || taskRunning || mutationInFlightRef.current) return
    mutationInFlightRef.current = true
    setReleasing(true)
    setError('')
    try {
      await apiFetch(`/tasks/chatgpt/email-change/reservations/${encodeURIComponent(reservationRef)}/release`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      requestGenerationRef.current += 1
      setPrepared(null)
      setTaskId('')
      setTaskRunVersion((value) => value + 1)
      setManualEmail('')
      message.success('目标邮箱预留已释放')
      await loadOptions()
    } catch (exc: unknown) {
      setError(errorMessage(exc, '释放目标邮箱失败'))
    } finally {
      mutationInFlightRef.current = false
      setReleasing(false)
    }
  }

  const resumeTask = async () => {
    if (!taskId || !prepared?.resumable || taskRunning || mutationInFlightRef.current) return
    mutationInFlightRef.current = true
    setResuming(true)
    setError('')
    try {
      await apiFetch(`/tasks/${encodeURIComponent(taskId)}/resume`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      requestGenerationRef.current += 1
      setPrepared((current) => ({
        ...(current || {}),
        status: 'running',
        runtime_active: true,
        error: '',
      }))
      setTaskRunVersion((value) => value + 1)
      await onTaskStarted?.(taskId)
      message.success('邮箱换绑恢复任务已启动')
    } catch (exc: unknown) {
      setError(errorMessage(exc, '继续恢复失败'))
    } finally {
      mutationInFlightRef.current = false
      setResuming(false)
    }
  }

  return (
    <Modal
      title={`邮箱换绑 · ${account?.email || accountId}`}
      open={open}
      onCancel={onClose}
      width={820}
      maskClosable={false}
      footer={null}
      destroyOnClose={false}
    >
      {!taskId ? (
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="先锁定目标邮箱，再开始远端换绑"
            description="换绑确认验证码发送到目标邮箱；完成远端变更后，会再发送一次独立的新邮箱登录验证码。"
          />
          <Steps
            size="small"
            current={targetReady ? 1 : 0}
            items={[{ title: '选择目标邮箱' }, { title: '确认并启动' }, { title: '身份校验与提交' }]}
          />
          <Radio.Group
            value={provider}
            disabled={targetReady}
            onChange={(event) => setProvider(event.target.value)}
            optionType="button"
            buttonStyle="solid"
            options={[
              { label: 'HME Ready', value: 'hme_ready_api' },
              { label: 'TempMail 新建', value: 'tempmail_local' },
              { label: '手动外部邮箱', value: 'manual_email_otp' },
            ]}
          />
          {provider === 'hme_ready_api' ? (
            <Alert
              type="info"
              showIcon
              message="由 HME Ready 自动分配并签出租约"
              description="锁定时才会返回具体别名；本地 HME 列表不代表 Helper 可轮询租约，因此不提供指定别名。"
            />
          ) : null}
          {provider === 'tempmail_local' ? (
            <Select
              allowClear
              showSearch
              disabled={targetReady}
              loading={loadingOptions}
              value={domain || undefined}
              placeholder="选择 TempMail 域名；留空使用服务端固定域名"
              onChange={(value) => setDomain(String(value || ''))}
              options={domains.map((item) => ({ value: item, label: item }))}
              style={{ width: '100%' }}
            />
          ) : null}
          {provider === 'manual_email_otp' ? (
            <Input
              value={manualEmail}
              disabled={targetReady}
              onChange={(event) => setManualEmail(event.target.value)}
              placeholder="输入目标邮箱，例如 new@example.com"
              type="email"
              onPressEnter={prepareTarget}
            />
          ) : null}
          <Checkbox
            checked={removeSocialSubs}
            disabled={targetReady}
            onChange={(event) => setRemoveSocialSubs(event.target.checked)}
          >
            同时移除社交登录绑定
          </Checkbox>
          {removeSocialSubs ? (
            <Alert
              type="warning"
              showIcon
              message="将向 OpenAI 显式提交 remove_social_subs"
              description="此选项默认关闭；仅确认不再保留原社交登录方式时启用。"
            />
          ) : null}
          {targetReady && prepared ? (
            <Alert
              type="success"
              showIcon
              message={(
                <Space wrap>
                  <Text strong>{prepared.target_email}</Text>
                  <Tag>{providerLabel(prepared.provider || provider)}</Tag>
                </Space>
              )}
              description={prepared.lease_expires_at ? `锁定到期：${prepared.lease_expires_at}` : '该地址已冻结，任务启动时不会重新选择邮箱。'}
            />
          ) : null}
          {error ? <Alert type="error" showIcon message={error} /> : null}
          <Space wrap>
            {!targetReady ? (
              <Button
                onClick={prepareTarget}
                loading={preparing}
                disabled={loadingOptions || (provider === 'manual_email_otp' && !manualEmail.trim())}
              >
                锁定目标邮箱
              </Button>
            ) : (
              <Button danger icon={<DeleteOutlined />} onClick={releaseTarget} loading={releasing}>
                释放并重选
              </Button>
            )}
            <Button
              type="primary"
              onClick={startTask}
              loading={starting}
              disabled={!targetReady || prepared?.status !== 'created'}
            >
              开始邮箱换绑
            </Button>
          </Space>
        </Space>
      ) : (
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Alert
            type={prepared?.status === 'done' ? 'success' : remoteBoundaryCrossed ? 'warning' : prepared?.status === 'failed' ? 'error' : 'info'}
            showIcon
            message={`目标邮箱：${prepared?.target_email || '-'}`}
            description={
              prepared?.status === 'done'
                ? '远端邮箱、本地账号登录态和目标邮箱租约均已提交。'
                : remoteBoundaryCrossed
                  ? '远端确认边界已经越过，只能继续恢复，不能释放或重新选择目标邮箱。'
                  : prepared?.error || '换绑任务运行中；验证码面板会按当前阶段显示。'
            }
          />
          <Steps
            size="small"
            current={step.current}
            status={step.status}
            items={[{ title: '资格检查' }, { title: '目标邮箱确认 OTP' }, { title: '目标登录与身份校验' }, { title: '本地提交' }]}
          />
          {error ? <Alert type="error" showIcon message={error} /> : null}
          <Space wrap>
            {prepared?.resumable && !taskRunning ? (
              <Button type="primary" icon={<ReloadOutlined />} onClick={resumeTask} loading={resuming}>
                继续恢复
              </Button>
            ) : null}
            {!remoteBoundaryCrossed && !taskRunning && prepared?.status !== 'done' ? (
              <Button danger icon={<DeleteOutlined />} onClick={releaseTarget} loading={releasing}>
                释放目标邮箱
              </Button>
            ) : null}
          </Space>
          <TaskLogPanel
            key={`${taskId}:${taskRunVersion}`}
            taskId={taskId}
            onDone={async () => {
              try {
                await loadTaskDetail(taskId, requestGenerationRef.current)
              } finally {
                await onRefresh?.()
              }
            }}
          />
        </Space>
      )}
    </Modal>
  )
}
