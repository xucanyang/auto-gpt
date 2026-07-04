import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Row,
  Segmented,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  InboxOutlined,
  KeyOutlined,
  LoadingOutlined,
  MailOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons'

import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import { apiFetch } from '@/lib/utils'
import { buildTaskProxyPayload, saveTaskProxySettingsToConfig, taskProxySettingsFromConfig, validateTaskProxySettings } from '@/lib/taskProxySettings'

const { Paragraph, Text, Title } = Typography

const CUSTOM_EMAIL_RECHECK_STORAGE_KEY = 'auto-chatgpt.custom-email-recheck.current-task'
const CUSTOM_EMAIL_RECHECK_EMAIL_KEY = 'auto-chatgpt.custom-email-recheck.email'
const SUB2API_IMPORT_MAX_BYTES = 5 * 1024 * 1024

type TaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'stopped'
type BulkInputMode = 'email_list' | 'sub2api_json'
type ProxyMode = 'direct' | 'pool' | 'specified' | 'dynamic'

function normalizeTaskStatus(value: unknown): TaskStatus {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'skipped') return 'stopped'
  if (['pending', 'running', 'done', 'failed', 'stopped'].includes(normalized)) return normalized as TaskStatus
  return 'pending'
}

function mapHistoryTaskStatus(status: unknown, snapshotStatus?: unknown): TaskStatus {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'failed') return 'failed'
  if (normalized === 'skipped' || normalized === 'stopped') return 'stopped'
  if (normalized === 'running') return 'running'
  return normalizeTaskStatus(snapshotStatus)
}

function normalizeTaskSnapshot(task: any, fallbackTaskId?: string) {
  if (!task) return null
  const normalizedId = task.id || task.task_id || fallbackTaskId || ''
  return {
    ...task,
    id: normalizedId,
    task_id: normalizedId,
    status: normalizeTaskStatus(task.status || task.status_snapshot || 'pending'),
    progress: task.progress || '0/0',
    skipped: task.skipped ?? 0,
    success: task.success ?? 0,
    errors: Array.isArray(task.errors) ? task.errors : [],
    pending_verification: task.pending_verification || null,
    error: task.error || '',
    meta: task.meta && typeof task.meta === 'object' ? task.meta : {},
  }
}

function statusTag(task: any) {
  const status = normalizeTaskStatus(task?.status)
  if (status === 'done') return <Tag color="success" icon={<CheckCircleOutlined />}>已完成</Tag>
  if (status === 'failed') return <Tag color="error" icon={<CloseCircleOutlined />}>失败</Tag>
  if (status === 'stopped') return <Tag color="warning">已停止</Tag>
  if (status === 'running') return <Tag color="processing" icon={<LoadingOutlined />}>运行中</Tag>
  return <Tag>未启动</Tag>
}

function resultTag(result: any, task: any) {
  const status = String(result?.status || '').trim()
  if (status === 'login_alive') return <Tag color="success">可登录</Tag>
  if (status === 'account_deactivated') return <Tag color="error">已停用</Tag>
  if (status === 'password_invalid') return <Tag color="error">密码错误</Tag>
  if (status === 'login_blocked') return <Tag color="warning">额外验证阻断</Tag>
  if (status === 'network_failed') return <Tag color="warning">网络/限流失败</Tag>
  if (status === 'email_otp_timeout') return <Tag color="warning">验证码超时</Tag>
  if (status === 'otp_rate_limited') return <Tag color="warning">OTP冷却</Tag>
  const taskStatus = normalizeTaskStatus(task?.status)
  if (taskStatus === 'running') return <Tag color="processing">测活中</Tag>
  if (taskStatus === 'failed') return <Tag color="error">失败</Tag>
  return <Tag>等待中</Tag>
}

function resultHelp(result: any, task: any) {
  const messageText = String(result?.message || task?.error || '').trim()
  if (messageText) return messageText
  const status = normalizeTaskStatus(task?.status)
  if (status === 'running') return '正在尝试登录 ChatGPT；如果需要邮箱验证码，下方会出现输入面板。'
  if (status === 'done') return '测活已完成。'
  return '输入邮箱后启动测活。'
}

function parseBulkEmails(rawValue?: string) {
  const parts = String(rawValue || '')
    .split(/[\n,;\s，；]+/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)

  const emails: string[] = []
  const invalid: string[] = []
  const duplicates: string[] = []
  const seen = new Set<string>()
  for (const item of parts) {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(item)) {
      invalid.push(item)
      continue
    }
    if (seen.has(item)) {
      duplicates.push(item)
      continue
    }
    seen.add(item)
    emails.push(item)
  }
  return { emails, invalid, duplicates }
}

function parseSub2ApiImportPreview(rawValue?: string) {
  const raw = String(rawValue || '')
  if (!raw.trim()) {
    return {
      accountCount: 0,
      emails: [] as string[],
      invalid: [] as string[],
      duplicates: [] as string[],
      skippedItems: [] as Array<{ email: string; reason: string }>,
      parseError: '',
    }
  }
  try {
    const payload = JSON.parse(raw)
    const extractItems = (value: any): any[] => {
      if (Array.isArray(value)) return value
      if (!value || typeof value !== 'object') return []
      const data = value.data && typeof value.data === 'object' ? value.data : null
      const candidates = [
        value.accounts,
        value.items,
        Array.isArray(value.data) ? value.data : null,
        data?.accounts,
        data?.items,
      ]
      for (const candidate of candidates) {
        if (Array.isArray(candidate)) return candidate
      }
      if (value.credentials || value.extra || value.name || value.email) {
        return [value]
      }
      return []
    }
    const items = extractItems(payload)
    if (!items.length) {
      return {
        accountCount: 0,
        emails: [],
        invalid: [],
        duplicates: [],
        skippedItems: [],
        parseError: '没找到 accounts 列表',
      }
    }
    const emails: string[] = []
    const invalid: string[] = []
    const duplicates: string[] = []
    const skippedItems: Array<{ email: string; reason: string }> = []
    const seen = new Set<string>()
    const emailPattern = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
    const pickEmail = (item: any) => {
      const extra = item?.extra && typeof item.extra === 'object' ? item.extra : {}
      const credentials = item?.credentials && typeof item.credentials === 'object' ? item.credentials : {}
      const candidates = [
        extra.email,
        item?.email,
        item?.name,
        credentials.email,
        credentials.username,
        credentials.login,
        credentials.account,
      ]
      for (const candidate of candidates) {
        const text = String(candidate || '').trim().toLowerCase()
        if (text) return text
      }
      return ''
    }
    items.forEach((item, index) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        skippedItems.push({ email: `[第${index + 1}条]`, reason: '条目不是对象' })
        return
      }
      const value = pickEmail(item)
      if (!value) {
        skippedItems.push({ email: `[第${index + 1}条]`, reason: '未找到邮箱字段' })
        return
      }
      if (!emailPattern.test(value)) {
        invalid.push(value)
        skippedItems.push({ email: value, reason: '邮箱格式不合法' })
        return
      }
      if (seen.has(value)) {
        duplicates.push(value)
        skippedItems.push({ email: value, reason: '重复邮箱' })
        return
      }
      seen.add(value)
      emails.push(value)
    })
    return {
      accountCount: items.length,
      emails,
      invalid,
      duplicates,
      skippedItems,
      parseError: '',
    }
  } catch (error_) {
    return {
      accountCount: 0,
      emails: [],
      invalid: [],
      duplicates: [],
      skippedItems: [],
      parseError: error_ instanceof Error ? error_.message : 'JSON 解析失败',
    }
  }
}

function buildProxyPayload(values: any) {
  return buildTaskProxyPayload(values)
}

export default function CustomEmailRecheckPage() {
  const [form] = Form.useForm()
  const [task, setTask] = useState<any>(null)
  const [submitting, setSubmitting] = useState(false)
  const [bulkSubmitting, setBulkSubmitting] = useState(false)
  const [bulkImportOpen, setBulkImportOpen] = useState(false)
  const [bulkInputMode, setBulkInputMode] = useState<BulkInputMode>('email_list')
  const [bulkEmailsText, setBulkEmailsText] = useState('')
  const [sub2apiImportText, setSub2apiImportText] = useState('')
  const [sub2apiImportName, setSub2apiImportName] = useState('')
  const [proxyDefaults, setProxyDefaults] = useState(() => taskProxySettingsFromConfig({}))
  const [polling, setPolling] = useState(false)
  const pollTimerRef = useRef<number | null>(null)
  const sub2apiFileInputRef = useRef<HTMLInputElement | null>(null)
  const taskRef = useRef<any>(null)

  const watchedEmail = Form.useWatch('email', form)
  const proxyMode = String(Form.useWatch('proxy_mode', form) || 'pool') as ProxyMode
  const proxyFailover = Boolean(Form.useWatch('proxy_failover', form))
  const bulkParse = useMemo(() => parseBulkEmails(bulkEmailsText), [bulkEmailsText])
  const sub2apiPreview = useMemo(() => parseSub2ApiImportPreview(sub2apiImportText), [sub2apiImportText])

  useEffect(() => {
    let disposed = false
    const savedEmail = window.localStorage.getItem(CUSTOM_EMAIL_RECHECK_EMAIL_KEY)
      || window.localStorage.getItem('auto-chatgpt.manual_email_otp.email')
      || ''
    apiFetch('/config')
      .then((cfg) => {
        if (disposed) return
        const proxySettings = taskProxySettingsFromConfig(cfg)
        setProxyDefaults(proxySettings)
        form.setFieldsValue({
          email: savedEmail,
          password: '',
          save_on_success: true,
          ...proxySettings,
          account_delay_seconds: 0,
        })
      })
      .catch(() => {
        if (disposed) return
        const proxySettings = taskProxySettingsFromConfig({})
        setProxyDefaults(proxySettings)
        form.setFieldsValue({
          email: savedEmail,
          password: '',
          save_on_success: true,
          ...proxySettings,
          account_delay_seconds: 0,
        })
      })
    return () => { disposed = true }
}, [form])

  useEffect(() => {
    taskRef.current = task
  }, [task])

  const stopPolling = () => {
    if (pollTimerRef.current != null) {
      window.clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
    setPolling(false)
  }

  const pollTask = async (taskId: string) => {
    stopPolling()
    setPolling(true)

    const loadHistoryFallback = async (reason: string) => {
      try {
        const history = await apiFetch(`/tasks/logs/by-task/${encodeURIComponent(taskId)}`) as {
          detail?: {
            status_snapshot?: string
            progress?: string
            success?: number
            skipped?: number
            errors?: string[]
            meta?: any
            result?: any
          }
          status?: string
          error?: string
        }
        const detail = history?.detail && typeof history.detail === 'object' ? history.detail : {}
        const restoredTask = normalizeTaskSnapshot({
          ...(taskRef.current || {}),
          id: taskId,
          status: mapHistoryTaskStatus(history.status, detail.status_snapshot),
          progress: detail.progress || taskRef.current?.progress || '0/0',
          skipped: detail.skipped ?? taskRef.current?.skipped ?? 0,
          success: detail.success ?? taskRef.current?.success ?? 0,
          errors: Array.isArray(detail.errors) ? detail.errors : (taskRef.current?.errors || []),
          error: history.error || taskRef.current?.error || reason,
          meta: {
            ...(taskRef.current?.meta || {}),
            ...(detail.meta || {}),
            result: detail.result || taskRef.current?.meta?.result,
          },
        }, taskId)
        setTask(restoredTask)
        return true
      } catch {
        return false
      }
    }

    pollTimerRef.current = window.setInterval(async () => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`)
        const normalizedTask = normalizeTaskSnapshot(snapshot, taskId)
        setTask(normalizedTask)
        if (['done', 'failed', 'stopped'].includes(String(normalizedTask.status))) {
          stopPolling()
          void loadHistoryFallback('任务已结束')
        }
      } catch (error_: unknown) {
        const detail = error_ instanceof Error ? error_.message : '获取任务状态失败'
        const recovered = await loadHistoryFallback(detail)
        stopPolling()
        if (!recovered) {
          setTask((previous: any) => normalizeTaskSnapshot({
            ...(previous || {}),
            id: taskId,
            status: previous?.status || 'failed',
            error: detail,
          }, taskId))
          message.error(detail)
        }
      }
    }, 2000)
  }

  useEffect(() => {
    if (typeof window === 'undefined') return
    const saved = window.localStorage.getItem(CUSTOM_EMAIL_RECHECK_STORAGE_KEY)
    if (!saved) return
    try {
      const parsed = JSON.parse(saved)
      const restoredTask = normalizeTaskSnapshot(parsed, parsed?.id)
      if (!restoredTask?.id) return
      setTask(restoredTask)
      if (!['done', 'failed', 'stopped'].includes(String(restoredTask.status))) {
        void pollTask(restoredTask.id)
      }
    } catch {
      window.localStorage.removeItem(CUSTOM_EMAIL_RECHECK_STORAGE_KEY)
    }
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!task?.id) return
    const persistedTask = normalizeTaskSnapshot(task, task.id)
    window.localStorage.setItem(CUSTOM_EMAIL_RECHECK_STORAGE_KEY, JSON.stringify({
      id: persistedTask?.id,
      task_id: persistedTask?.task_id,
      status: persistedTask?.status,
      progress: persistedTask?.progress,
      skipped: persistedTask?.skipped,
      success: persistedTask?.success,
      errors: persistedTask?.errors,
      pending_verification: persistedTask?.pending_verification,
      error: persistedTask?.error,
      meta: persistedTask?.meta,
    }))
  }, [task])

  useEffect(() => () => stopPolling(), [])

  const handleSubmit = async () => {
    const values = await form.validateFields()
    const normalizedEmail = String(values.email || '').trim()
    const normalizedPassword = String(values.password || '')
    const saveOnSuccess = values.save_on_success === undefined ? true : Boolean(values.save_on_success)
    validateTaskProxySettings(values)
    const proxyPayload = buildProxyPayload(values)

    setSubmitting(true)
    try {
      const savedProxySettings = await saveTaskProxySettingsToConfig(values)
      setProxyDefaults(savedProxySettings)
      window.localStorage.setItem(CUSTOM_EMAIL_RECHECK_EMAIL_KEY, normalizedEmail)
      window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
      const response = await apiFetch('/tasks/chatgpt/custom-email-recheck', {
        method: 'POST',
        body: JSON.stringify({
          email: normalizedEmail,
          password: normalizedPassword,
          save_on_success: saveOnSuccess,
          ...proxyPayload,
        }),
      }) as { task_id?: string }

      const taskId = String(response?.task_id || '').trim()
      if (!taskId) throw new Error('创建测活任务成功，但未返回 task_id')

      const nextTask = normalizeTaskSnapshot({
        id: taskId,
        status: 'running',
        progress: '0/1',
        meta: {
          email: normalizedEmail,
          save_on_success: saveOnSuccess,
          proxy: {
            mode: proxyPayload.proxy_mode,
            country_code: proxyPayload.proxy_country_code,
            failover: proxyPayload.proxy_failover,
            max_candidates: proxyPayload.proxy_max_candidates,
            min_score: proxyPayload.proxy_min_score,
          },
          source: 'custom_email_recheck',
        },
      }, taskId)
      setTask(nextTask)

      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`)
        setTask(normalizeTaskSnapshot(snapshot, taskId))
      } catch {
        // 后续轮询兜底
      }
      void pollTask(taskId)
      message.success('邮箱测活任务已启动')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '创建邮箱测活任务失败'
      message.error(detail)
    } finally {
      setSubmitting(false)
    }
  }

  const handleSub2ApiFileSelect = async (file?: File | null) => {
    if (!file) return
    if (file.size > SUB2API_IMPORT_MAX_BYTES) {
      message.error('Sub2API 文件过大，请控制在 5MB 内')
      return
    }
    try {
      const text = await file.text()
      setBulkInputMode('sub2api_json')
      setSub2apiImportText(text)
      setSub2apiImportName(file.name)
      message.success(`已载入 ${file.name}`)
    } catch (error_) {
      message.error(error_ instanceof Error ? error_.message : '读取 Sub2API 文件失败')
    } finally {
      if (sub2apiFileInputRef.current) {
        sub2apiFileInputRef.current.value = ''
      }
    }
  }

  const handleBulkSubmit = async () => {
    const values = await form.validateFields([
      'password',
      'save_on_success',
      'proxy_mode',
      'proxy',
      'proxy_failover',
      'proxy_country_code',
      'proxy_min_score',
      'proxy_max_candidates',
      'account_delay_seconds',
    ])
    const normalizedPassword = String(values.password || '')
    const saveOnSuccess = values.save_on_success === undefined ? true : Boolean(values.save_on_success)
    validateTaskProxySettings(values)
    const proxyPayload = buildProxyPayload(values)
    const accountDelaySeconds = Math.min(Math.max(Number(values.account_delay_seconds || 0), 0), 600)
    const sourceFormat = bulkInputMode
    const emails = bulkParse.emails

    if (sourceFormat === 'email_list') {
      if (!emails.length) {
        message.error('请先粘贴至少一个合法邮箱')
        return
      }
      if (emails.length > 200) {
        message.error('单次最多导入 200 个邮箱，先拆成几批跑')
        return
      }
    } else {
      if (!sub2apiImportText.trim()) {
        message.error('请先导入 Sub2API JSON 文件')
        return
      }
      if (sub2apiPreview.parseError) {
        message.error(`Sub2API 文件格式有问题：${sub2apiPreview.parseError}`)
        return
      }
      if (!sub2apiPreview.emails.length) {
        message.error('Sub2API 文件里没有可用于测活的邮箱')
        return
      }
    }

    setBulkSubmitting(true)
    try {
      const savedProxySettings = await saveTaskProxySettingsToConfig(values)
      setProxyDefaults(savedProxySettings)
      const response = await apiFetch('/tasks/chatgpt/custom-email-recheck/batch', {
        method: 'POST',
        body: JSON.stringify({
          emails: sourceFormat === 'email_list' ? emails : [],
          source_format: sourceFormat,
          source_text: sourceFormat === 'sub2api_json' ? sub2apiImportText : '',
          source_filename: sourceFormat === 'sub2api_json' ? sub2apiImportName : '',
          password: normalizedPassword,
          save_on_success: saveOnSuccess,
          limit: 200,
          account_delay_seconds: accountDelaySeconds,
          ...proxyPayload,
        }),
      }) as {
        task_id?: string
        eligible?: number
        skipped?: number
        items?: string[]
        account_delay_seconds?: number
        skipped_items?: Array<{ email?: string; reason?: string }>
        source_format?: string
        source_filename?: string
        source_summary?: any
      }

      const resolvedEmails = Array.isArray(response?.items) ? response.items.map((item) => String(item || '').trim()).filter(Boolean) : []
      const firstEmail = resolvedEmails[0] || (sourceFormat === 'email_list' ? emails[0] : sub2apiPreview.emails[0] || '')
      if (firstEmail) {
        window.localStorage.setItem(CUSTOM_EMAIL_RECHECK_EMAIL_KEY, firstEmail)
        window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', firstEmail)
        form.setFieldsValue({ email: firstEmail })
      }

      const taskId = String(response?.task_id || '').trim()
      if (!taskId) throw new Error('创建批量测活任务成功，但未返回 task_id')

      const nextTask = normalizeTaskSnapshot({
        id: taskId,
        status: 'running',
        progress: `0/${resolvedEmails.length || Number(response?.eligible || 0)}`,
        meta: {
          email: firstEmail,
          emails: resolvedEmails,
          email_count: resolvedEmails.length || Number(response?.eligible || 0),
          save_on_success: saveOnSuccess,
          source_format: String(response?.source_format || sourceFormat || 'email_list'),
          source_filename: String(response?.source_filename || (sourceFormat === 'sub2api_json' ? sub2apiImportName : '') || ''),
          source_summary: response?.source_summary && typeof response.source_summary === 'object' ? response.source_summary : {},
          proxy: {
            mode: proxyPayload.proxy_mode,
            country_code: proxyPayload.proxy_country_code,
            failover: proxyPayload.proxy_failover,
            max_candidates: proxyPayload.proxy_max_candidates,
            min_score: proxyPayload.proxy_min_score,
          },
          account_delay_seconds: Number(response?.account_delay_seconds ?? accountDelaySeconds),
          source: 'batch_custom_email_recheck',
          skipped_items: Array.isArray(response?.skipped_items) ? response.skipped_items : [],
        },
      }, taskId)
      setTask(nextTask)

      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`)
        setTask(normalizeTaskSnapshot(snapshot, taskId))
      } catch {
        // 后续轮询兜底
      }
      void pollTask(taskId)
      message.success(`批量邮箱测活已启动：${resolvedEmails.length || Number(response?.eligible || 0)} 个邮箱`)
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '创建批量邮箱测活任务失败'
      message.error(detail)
    } finally {
      setBulkSubmitting(false)
    }
  }

  const isBatchTask = String(task?.meta?.source || '') === 'batch_custom_email_recheck'
  const batchResults = Array.isArray(task?.meta?.results) ? task.meta.results : []
  const batchSummary = task?.meta?.summary && typeof task.meta.summary === 'object' ? task.meta.summary : null
  const batchTotal = Number(task?.meta?.email_count || (Array.isArray(task?.meta?.emails) ? task.meta.emails.length : 0))
  const batchResultSuccess = batchResults.filter((item: any) => item?.ok).length
  const batchResultSkipped = batchResults.filter((item: any) => String(item?.status || '') === 'skipped').length
  const batchResultFailed = batchResults.filter((item: any) => item && !item.ok && String(item.status || '') !== 'skipped').length
  const batchSuccess = Math.max(Number(task?.success || 0), Number(batchSummary?.success || 0), batchResultSuccess)
  const batchSkipped = Math.max(Number(task?.skipped || 0), Number(batchSummary?.skipped || 0), batchResultSkipped)
  const batchFailed = Math.max(Array.isArray(task?.errors) ? task.errors.length : 0, Number(batchSummary?.failed || 0), batchResultFailed)
  const batchSourceFormat = String(task?.meta?.source_format || '').trim().toLowerCase()
  const bulkPreviewCount = bulkInputMode === 'email_list' ? bulkParse.emails.length : sub2apiPreview.emails.length
  const bulkIssueCount = bulkInputMode === 'email_list'
    ? bulkParse.invalid.length + bulkParse.duplicates.length
    : sub2apiPreview.invalid.length + sub2apiPreview.duplicates.length + sub2apiPreview.skippedItems.length + (sub2apiPreview.parseError ? 1 : 0)
  const summaryEmail = String(
    isBatchTask
      ? (task?.pending_verification?.email || task?.meta?.current_email || task?.meta?.email || '')
      : (task?.meta?.email || watchedEmail || ''),
  ).trim()
  const result = useMemo(() => {
    const metaResult = task?.meta?.result
    if (metaResult && typeof metaResult === 'object') return metaResult
    return null
  }, [task?.meta?.result])
  const savedAccountId = Number(result?.saved_account_id || 0)

  return (
    <div className="page-enter" style={{ maxWidth: 1160, margin: '0 auto' }}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card bordered={false} style={{ borderRadius: 18 }} bodyStyle={{ padding: 22 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 18, alignItems: 'flex-start', flexWrap: 'wrap' }}>
            <Space direction="vertical" size={7} style={{ flex: '1 1 560px', minWidth: 0 }}>
              <Tag color="blue" style={{ width: 'fit-content' }}>ChatGPT 登录探测</Tag>
              <Title level={2} style={{ margin: 0 }}>邮箱登录测活</Title>
              <Paragraph type="secondary" style={{ margin: 0, maxWidth: 760 }}>
                单个邮箱放在主操作区直接跑；批量导入收进折叠面板，需要时再展开。验证码、任务日志和结果仍在右侧与下方接管。
              </Paragraph>
            </Space>
            <Space wrap size={[8, 8]} style={{ justifyContent: 'flex-end' }}>
              <Tag color="processing">登录测活</Tag>
              <Tag color="cyan">验证码接管</Tag>
              <Tag color="green">成功可入库</Tag>
            </Space>
          </div>
        </Card>

        <Row gutter={[16, 16]} align="top">
          <Col xs={24} lg={14}>
            <Card
              title="单个邮箱测活参数设置"
              bordered={false}
              style={{ borderRadius: 18 }}
              extra={<Text type="secondary">批量导入在下方折叠</Text>}
            >
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <Form
                  form={form}
                  layout="vertical"
                  initialValues={{
                    email: '',
                    password: '',
                    save_on_success: true,
                    ...proxyDefaults,
                    account_delay_seconds: 0,
                  }}
                  onFinish={handleSubmit}
                >
                  <Form.Item
                    name="email"
                    label="单个邮箱测活"
                    rules={[
                      { required: true, message: '请输入邮箱地址' },
                      { type: 'email', message: '请输入合法邮箱地址' },
                    ]}
                  >
                    <Input size="large" prefix={<MailOutlined />} placeholder="name@example.com" autoComplete="email" />
                  </Form.Item>

                  <Form.Item
                    name="password"
                    label="登录密码（可选）"
                    extra="留空时优先走邮箱验证码登录；填写后会优先尝试密码登录。批量导入时会把这个密码作为整批默认密码。"
                  >
                    <Input.Password size="large" prefix={<KeyOutlined />} placeholder="可留空" autoComplete="current-password" />
                  </Form.Item>

                  <Form.Item name="proxy_mode" label="测活代理模式">
                    <Select
                      size="large"
                      options={[
                        { value: 'direct', label: '直连' },
                        { value: 'pool', label: '使用代理池' },
                        { value: 'specified', label: '指定代理' },
                        { value: 'dynamic', label: '动态代理' },
                      ]}
                    />
                  </Form.Item>

                  {proxyMode === 'specified' || proxyMode === 'dynamic' ? (
                    <Space style={{ width: '100%' }} align="start" wrap>
                      <Form.Item
                        name="proxy"
                        label={proxyMode === 'dynamic' ? '动态代理模板' : '指定代理'}
                        style={{ flex: '1 1 320px' }}
                        rules={[{ required: true, message: proxyMode === 'dynamic' ? '请输入动态代理模板' : '请输入指定代理地址' }]}
                      >
                        <Input size="large" placeholder={proxyMode === 'dynamic' ? 'socks5://user-region-JP-sid-xxxx-t-1:pass@host:port' : 'http://user:pass@host:port'} />
                      </Form.Item>
                      <Form.Item name="proxy_failover" label="失败处理" valuePropName="checked" style={{ width: 190 }}>
                        <Checkbox>{proxyMode === 'dynamic' ? '失败后刷新 sid 重试' : '失败后切换代理池'}</Checkbox>
                      </Form.Item>
                    </Space>
                  ) : null}

                  {proxyMode === 'pool' || proxyMode === 'dynamic' || (proxyMode === 'specified' && proxyFailover) ? (
                    <Space style={{ width: '100%' }} align="start" wrap>
                      <Form.Item
                        name="proxy_country_code"
                        label="出口国家"
                        style={{ flex: '1 1 180px' }}
                        rules={proxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
                      >
                        <Input size="large" placeholder={proxyMode === 'dynamic' ? '必填，例如 US / JP / SG' : '不限，或填 US / JP / SG'} maxLength={2} />
                      </Form.Item>
                      {proxyMode !== 'dynamic' ? (
                        <>
                      <Form.Item name="proxy_min_score" label="最低健康分" style={{ width: 150 }}>
                        <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                      <Form.Item name="proxy_max_candidates" label="最多候选" style={{ width: 150 }}>
                        <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                        </>
                      ) : null}
                    </Space>
                  ) : null}

                  <Form.Item
                    name="save_on_success"
                    valuePropName="checked"
                    extra="关闭后只记录任务结果，不新增或更新账号池。"
                  >
                    <Checkbox>测活成功后保存到账号池</Checkbox>
                  </Form.Item>

                  <Alert
                    showIcon
                    type="info"
                    style={{ marginBottom: 18 }}
                    message="这个入口只做登录测活"
                    description="代理模式与注册一致：直连不碰代理；指定代理默认只用填写节点，勾选失败切换后才使用代理池筛选项；代理池按健康分、冷却和实测出口国家挑选；动态代理只使用模板和出口国家，失败后刷新 sid 重试，不使用代理池的健康分/候选数。"
                  />

                  <Space wrap>
                    <Button type="primary" htmlType="submit" size="large" icon={<PlayCircleOutlined />} loading={submitting}>
                      开始测活
                    </Button>
                    <Button
                      size="large"
                      onClick={() => {
                        const savedEmail = window.localStorage.getItem(CUSTOM_EMAIL_RECHECK_EMAIL_KEY) || ''
                        form.resetFields()
                        form.setFieldsValue({
                          email: savedEmail,
                          password: '',
                          save_on_success: true,
                          ...proxyDefaults,
                          account_delay_seconds: 0,
                        })
                      }}
                    >
                      重置
                    </Button>
                  </Space>
                </Form>

                <Collapse
                  activeKey={bulkImportOpen ? ['bulk-import'] : []}
                  onChange={(keys) => {
                    const nextKeys = Array.isArray(keys) ? keys : [keys]
                    setBulkImportOpen(nextKeys.includes('bulk-import'))
                  }}
                  bordered={false}
                  expandIconPosition="end"
                  style={{
                    borderRadius: 14,
                    border: '1px solid rgba(99, 102, 241, 0.18)',
                    background: 'rgba(99, 102, 241, 0.06)',
                  }}
                  items={[
                    {
                      key: 'bulk-import',
                      label: (
                        <Space wrap size={[8, 6]}>
                          <InboxOutlined />
                          <Text strong>批量导入测活</Text>
                          <Text type="secondary">邮箱列表 / Sub2API JSON</Text>
                          <Tag color="purple">最多 200</Tag>
                          {bulkPreviewCount ? <Tag color="blue">已解析 {bulkPreviewCount}</Tag> : null}
                          {bulkIssueCount ? <Tag color="warning">需处理 {bulkIssueCount}</Tag> : null}
                        </Space>
                      ),
                      children: (
                        <Space direction="vertical" size={12} style={{ width: '100%' }}>
                          <Segmented<BulkInputMode>
                            block
                            value={bulkInputMode}
                            onChange={(value) => setBulkInputMode(value as BulkInputMode)}
                            options={[
                              { label: '邮箱列表', value: 'email_list' },
                              { label: 'Sub2API 文件', value: 'sub2api_json' },
                            ]}
                          />
                          {bulkInputMode === 'email_list' ? (
                            <>
                              <Input.TextArea
                                value={bulkEmailsText}
                                onChange={(event) => setBulkEmailsText(event.target.value)}
                                autoSize={{ minRows: 7, maxRows: 14 }}
                                placeholder={'每行一个邮箱，或用空格/逗号分隔\nalice@example.com\nbob@example.com'}
                              />
                              <Space wrap size={[8, 8]}>
                                <Tag color="blue">有效 {bulkParse.emails.length}</Tag>
                                {bulkParse.invalid.length ? <Tag color="error">无效 {bulkParse.invalid.length}</Tag> : null}
                                {bulkParse.duplicates.length ? <Tag color="warning">重复 {bulkParse.duplicates.length}</Tag> : null}
                              </Space>
                              {bulkParse.invalid.length || bulkParse.duplicates.length ? (
                                <Alert
                                  showIcon
                                  type="warning"
                                  message="导入内容里有一部分不会进入任务"
                                  description={[
                                    bulkParse.invalid.length ? `无效邮箱：${bulkParse.invalid.slice(0, 8).join('，')}` : '',
                                    bulkParse.duplicates.length ? `重复邮箱：${bulkParse.duplicates.slice(0, 8).join('，')}` : '',
                                  ].filter(Boolean).join('；')}
                                />
                              ) : null}
                            </>
                          ) : (
                            <>
                              <input
                                ref={sub2apiFileInputRef}
                                type="file"
                                accept=".json,application/json,text/plain"
                                style={{ display: 'none' }}
                                onChange={(event) => {
                                  const file = event.target.files?.[0]
                                  void handleSub2ApiFileSelect(file)
                                }}
                              />
                              <Alert
                                showIcon
                                type="info"
                                message="导入 Sub2API JSON 文件"
                                description="支持本项目账号页导出的 Sub2API JSON。提交任务时会从 accounts[*].extra.email / email / name 等字段解析邮箱，再批量测活。"
                              />
                              <Space wrap>
                                <Button size="large" icon={<InboxOutlined />} onClick={() => sub2apiFileInputRef.current?.click()}>
                                  选择 Sub2API 文件
                                </Button>
                                {sub2apiImportName ? <Tag color="blue">{sub2apiImportName}</Tag> : null}
                                {sub2apiImportText ? <Tag color="success">已载入 {Math.round(new Blob([sub2apiImportText]).size / 1024)} KB</Tag> : null}
                              </Space>
                              <Space wrap size={[8, 8]}>
                                {sub2apiPreview.accountCount ? <Tag color="blue">账号条目 {sub2apiPreview.accountCount}</Tag> : null}
                                {sub2apiPreview.emails.length ? <Tag color="success">可测活邮箱 {sub2apiPreview.emails.length}</Tag> : null}
                                {sub2apiPreview.invalid.length ? <Tag color="error">无效 {sub2apiPreview.invalid.length}</Tag> : null}
                                {sub2apiPreview.duplicates.length ? <Tag color="warning">重复 {sub2apiPreview.duplicates.length}</Tag> : null}
                                {sub2apiPreview.skippedItems.length ? <Tag color="warning">跳过 {sub2apiPreview.skippedItems.length}</Tag> : null}
                              </Space>
                              {sub2apiPreview.parseError ? (
                                <Alert
                                  showIcon
                                  type="error"
                                  message="Sub2API 文件解析失败"
                                  description={sub2apiPreview.parseError}
                                />
                              ) : null}
                              {!sub2apiPreview.parseError && sub2apiPreview.skippedItems.length ? (
                                <Alert
                                  showIcon
                                  type="warning"
                                  message="文件里有一部分条目不会进入任务"
                                  description={sub2apiPreview.skippedItems.slice(0, 8).map((item) => `${item.email}：${item.reason}`).join('；')}
                                />
                              ) : null}
                            </>
                          )}
                          <Form.Item
                            name="account_delay_seconds"
                            label="账号间隔秒数"
                            extra="每个邮箱处理完成后，等待指定秒数再开始下一个；0 表示不等待。停止任务时会中断等待。"
                            style={{ maxWidth: 260 }}
                          >
                            <InputNumber min={0} max={600} precision={1} step={1} style={{ width: '100%' }} addonAfter="秒" />
                          </Form.Item>
                          <Space wrap>
                            <Button
                              type="primary"
                              ghost
                              size="large"
                              icon={<InboxOutlined />}
                              loading={bulkSubmitting}
                              onClick={handleBulkSubmit}
                            >
                              批量开始测活
                            </Button>
                            <Button
                              size="large"
                              onClick={() => {
                                setBulkEmailsText('')
                                setSub2apiImportText('')
                                setSub2apiImportName('')
                              }}
                            >
                              清空导入内容
                            </Button>
                          </Space>
                        </Space>
                      ),
                    },
                  ]}
                />
              </Space>
            </Card>
          </Col>

          <Col xs={24} lg={10}>
            <Card title="当前任务" bordered={false} style={{ borderRadius: 18 }} extra={statusTag(task)}>
              <Space direction="vertical" size={14} style={{ width: '100%' }}>
                <Space wrap>
                  {isBatchTask ? <Tag color="processing">批量任务</Tag> : resultTag(result, task)}
                  {isBatchTask && batchSourceFormat === 'sub2api_json' ? <Tag color="cyan">Sub2API 导入</Tag> : null}
                  {polling ? <Tag icon={<LoadingOutlined />} color="processing">轮询中</Tag> : null}
                  {result?.saved ? <Tag color="green">已入库</Tag> : null}
                </Space>
                <Descriptions column={1} size="small" labelStyle={{ width: 84 }}>
                  <Descriptions.Item label="邮箱">{summaryEmail || '-'}</Descriptions.Item>
                  <Descriptions.Item label="结论">
                    {isBatchTask
                      ? `当前处理 ${summaryEmail || '-'}，已完成 ${String(task?.progress || '0/0')}`
                      : resultHelp(result, task)}
                  </Descriptions.Item>
                  {isBatchTask ? <Descriptions.Item label="总数">{batchTotal || '-'}</Descriptions.Item> : null}
                  {isBatchTask ? <Descriptions.Item label="成功/跳过/失败">{`${batchSuccess}/${batchSkipped}/${batchFailed}`}</Descriptions.Item> : null}
                  {isBatchTask && Number(task?.meta?.account_delay_seconds || 0) > 0
                    ? <Descriptions.Item label="账号间隔">{`${Number(task?.meta?.account_delay_seconds || 0)} 秒`}</Descriptions.Item>
                    : null}
                  {isBatchTask && batchSourceFormat === 'sub2api_json'
                    ? <Descriptions.Item label="导入文件">{String(task?.meta?.source_filename || '-')}</Descriptions.Item>
                    : null}
                  <Descriptions.Item label="账号 ID">{savedAccountId > 0 ? savedAccountId : '-'}</Descriptions.Item>
                  <Descriptions.Item label="进度">{String(task?.progress || '未启动')}</Descriptions.Item>
                  <Descriptions.Item label="任务 ID">{String(task?.id || '-')}</Descriptions.Item>
                </Descriptions>
                {isBatchTask && batchResults.length ? (
                  <Card
                    size="small"
                    type="inner"
                    title="批量结果摘要"
                    styles={{ body: { paddingTop: 12, maxHeight: 240, overflow: 'auto' } }}
                  >
                    <Space direction="vertical" size={8} style={{ width: '100%' }}>
                      {batchResults.slice(-12).reverse().map((item: any) => (
                        <div
                          key={`${item?.email || 'unknown'}:${item?.status || 'pending'}`}
                          style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}
                        >
                          <Text ellipsis style={{ maxWidth: 220 }}>{String(item?.email || '-')}</Text>
                          <Tag color={item?.ok ? 'success' : String(item?.status || '') === 'skipped' ? 'warning' : 'error'}>
                            {item?.ok ? '成功' : String(item?.status || '') === 'skipped' ? '跳过' : '失败'}
                          </Tag>
                        </div>
                      ))}
                    </Space>
                  </Card>
                ) : null}
                {task?.pending_verification ? (
                  <Text type="warning">正在等待验证码，请在下方输入。</Text>
                ) : (
                  <Text type="secondary">
                    {isBatchTask ? '批量任务按顺序逐个测活；如果某个邮箱需要验证码，这里会停在当前邮箱等待输入。' : '任务启动后，这里会显示最终结论；详细过程看下方日志。'}
                  </Text>
                )}
              </Space>
            </Card>
          </Col>
        </Row>

        {task?.id ? (
          <Card title="任务面板" bordered={false} style={{ borderRadius: 18 }}>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              {task?.pending_verification ? (
                <TaskVerificationPanel taskId={String(task.id)} verification={task.pending_verification} />
              ) : null}
              <TaskLogPanel taskId={String(task.id)} onDone={() => {
                void pollTask(String(task.id))
              }} />
            </Space>
          </Card>
        ) : null}
      </Space>
    </div>
  )
}
