import { useEffect, useRef, useState } from 'react'
import { StopOutlined } from '@ant-design/icons'
import { Alert, Button, Popconfirm, Space, Tabs, Tag, Tooltip, Typography, message } from 'antd'

import {
  REGISTRATION_DOMAIN_TASK_MODE_ROTATING,
  fetchRegistrationDomainTaskGroup,
  isRegistrationDomainTaskGroupActive,
  normalizeRegistrationDomainTaskGroup,
  type RegistrationDomainTaskGroup,
} from '@/lib/registrationDomainTasks'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

type RegistrationDomainTaskGroupTabsProps = {
  group: RegistrationDomainTaskGroup
  activeTaskId: string
  onSelectTask: (taskId: string) => void
  onGroupChange?: (group: RegistrationDomainTaskGroup) => void
}

const ACTIVE_DOMAIN_STATES = new Set(['starting', 'active', 'draining'])
const GROUP_STATE_LABELS: Record<string, string> = {
  running: '轮换中',
  stopping: '停止中',
  failing: '异常收口',
  completed: '已完成',
  stopped: '已停止',
  failed: '失败',
  interrupted: '已中断',
}
const DOMAIN_STATE_LABELS: Record<string, string> = {
  starting: '启动中',
  active: '运行中',
  draining: '收口中',
  completed: '完成',
  quality_rejected: '已淘汰',
  failed: '失败',
  stopped: '停止',
  interrupted: '中断',
  cancelled: '未启动',
  start_failed: '启动失败',
}

function countOf(group: RegistrationDomainTaskGroup, ...states: string[]) {
  return states.reduce((total, state) => total + Number(group.counts[state] || 0), 0)
}

function qualityNumber(value: unknown) {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0
}

function groupStateColor(state: string) {
  if (state === 'completed') return 'success'
  if (state === 'failed') return 'error'
  if (state === 'stopping' || state === 'failing' || state === 'interrupted') return 'warning'
  return state === 'running' ? 'processing' : 'default'
}

function domainStateColor(state: string) {
  if (state === 'completed') return 'success'
  if (state === 'failed' || state === 'start_failed') return 'error'
  if (state === 'quality_rejected' || state === 'draining' || state === 'interrupted') return 'warning'
  return ACTIVE_DOMAIN_STATES.has(state) ? 'processing' : 'default'
}

export function RegistrationDomainTaskGroupTabs({
  group,
  activeTaskId,
  onSelectTask,
  onGroupChange,
}: RegistrationDomainTaskGroupTabsProps) {
  const [stopping, setStopping] = useState(false)
  const groupRef = useRef(group)
  const activeTaskIdRef = useRef(activeTaskId)
  const onSelectTaskRef = useRef(onSelectTask)
  const onGroupChangeRef = useRef(onGroupChange)

  useEffect(() => {
    groupRef.current = group
  }, [group])
  useEffect(() => {
    activeTaskIdRef.current = activeTaskId
  }, [activeTaskId])
  useEffect(() => {
    onSelectTaskRef.current = onSelectTask
  }, [onSelectTask])
  useEffect(() => {
    onGroupChangeRef.current = onGroupChange
  }, [onGroupChange])

  useEffect(() => {
    if (group.mode !== REGISTRATION_DOMAIN_TASK_MODE_ROTATING) return
    let cancelled = false
    let timer: number | null = null

    const pull = async () => {
      if (document.visibilityState === 'hidden') {
        timer = window.setTimeout(pull, 10_000)
        return
      }
      try {
        const refreshed = await fetchRegistrationDomainTaskGroup(apiFetch, group.groupId)
        if (cancelled) return
        if (!refreshed) {
          if (isRegistrationDomainTaskGroupActive(groupRef.current)) {
            timer = window.setTimeout(pull, 3000)
          }
          return
        }
        groupRef.current = refreshed
        onGroupChangeRef.current?.(refreshed)

        const selected = refreshed.tasks.find((item) => item.taskId === activeTaskIdRef.current)
        if (!selected || !ACTIVE_DOMAIN_STATES.has(selected.state)) {
          const nextActive = refreshed.tasks.find((item) => ACTIVE_DOMAIN_STATES.has(item.state))
          if (nextActive && nextActive.taskId !== activeTaskIdRef.current) {
            onSelectTaskRef.current(nextActive.taskId)
          }
        }
        if (isRegistrationDomainTaskGroupActive(refreshed)) {
          timer = window.setTimeout(pull, 2000)
        }
      } catch {
        if (!cancelled && isRegistrationDomainTaskGroupActive(groupRef.current)) {
          timer = window.setTimeout(pull, 3000)
        }
      }
    }

    void pull()
    return () => {
      cancelled = true
      if (timer != null) window.clearTimeout(timer)
    }
  }, [group.groupId, group.mode])

  const stopGroup = async () => {
    setStopping(true)
    try {
      const response = await apiFetch(
        `/tasks/register/domain-groups/${encodeURIComponent(group.groupId)}/stop`,
        {
          method: 'POST',
          body: JSON.stringify({ mode: 'after_current' }),
        },
      )
      const refreshed = normalizeRegistrationDomainTaskGroup(response)
      if (!refreshed) throw new Error('停止请求已返回，但任务组状态无效')
      groupRef.current = refreshed
      onGroupChangeRef.current?.(refreshed)
      message.success('已请求所有活动域名完成当前账号后停止')
    } catch (error) {
      message.error(error instanceof Error ? error.message : '停止域名轮换失败')
    } finally {
      setStopping(false)
    }
  }

  const activeCount = countOf(group, 'starting', 'active', 'draining')
  const pendingCount = countOf(group, 'pending')
  const rejectedCount = countOf(group, 'quality_rejected')
  const completedCount = countOf(group, 'completed')
  const triggeredDomains = group.domains.filter((item) => Object.keys(item.trigger).length > 0)
  const visibleTriggeredDomains = triggeredDomains.slice(-5)
  const nextPending = group.domains.find((item) => item.state === 'pending')
  const selectedDomain = group.domains.find((item) => item.taskId === activeTaskId)
  const selectedQuality = selectedDomain?.quality || {}

  return (
    <div className="registration-domain-task-group">
      <Space size={6} wrap className="registration-domain-task-group-summary">
        <Text strong>域名任务组</Text>
        {group.mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING ? (
          <Tag color={groupStateColor(group.state)}>
            {GROUP_STATE_LABELS[group.state] || group.state}
          </Tag>
        ) : (
          <Tag color="processing">已创建 {group.tasks.length}/{group.requestedDomainCount}</Tag>
        )}
        <Tag>每任务 {group.requestedCountPerTask} 个</Tag>
        {group.mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING ? (
          <>
            <Tag color="processing">运行 {activeCount}</Tag>
            <Tag>等待 {pendingCount}</Tag>
            {rejectedCount > 0 ? <Tag color="warning">淘汰 {rejectedCount}</Tag> : null}
            {completedCount > 0 ? <Tag color="success">完成 {completedCount}</Tag> : null}
            {group.state === 'running' ? (
              <Popconfirm
                title="停止整个域名轮换任务组？"
                description="活动域名完成当前账号后停止，等待域名不再启动。"
                okText="停止轮换"
                cancelText="取消"
                onConfirm={stopGroup}
              >
                <Tooltip title="停止域名轮换">
                  <Button
                    size="small"
                    danger
                    loading={stopping}
                    icon={<StopOutlined />}
                    aria-label="停止域名轮换"
                  >
                    停止轮换
                  </Button>
                </Tooltip>
              </Popconfirm>
            ) : null}
          </>
        ) : null}
      </Space>
      {group.mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING && nextPending ? (
        <Text type="secondary" className="registration-domain-task-group-next">
          下一个：{nextPending.domain}
        </Text>
      ) : null}
      {group.mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING && selectedDomain ? (
        <Text type="secondary" className="registration-domain-task-group-quality">
          当前 {selectedDomain.domain}｜开户拒绝{' '}
          {qualityNumber(selectedQuality.registration_disallowed)}/
          {qualityNumber(selectedQuality.registration_decisions)}（
          {qualityNumber(selectedQuality.registration_rejection_rate_percent)}%）｜提链成功{' '}
          {qualityNumber(selectedQuality.link_success)}｜连续未提链{' '}
          {qualityNumber(selectedQuality.link_current_miss_streak)}
        </Text>
      ) : null}
      {group.stopReason ? (
        <Alert
          type={group.state === 'failed' ? 'error' : group.state === 'interrupted' ? 'warning' : 'info'}
          showIcon
          message={group.stopReason}
        />
      ) : null}
      {triggeredDomains.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`${triggeredDomains.length} 个域名触发质量淘汰`}
          description={`${visibleTriggeredDomains
            .map((item) => `${item.domain}：${String(item.trigger.message || item.trigger.code || '质量阈值触发')}`)
            .join('；')}${triggeredDomains.length > visibleTriggeredDomains.length ? `；另 ${triggeredDomains.length - visibleTriggeredDomains.length} 个请查看对应任务日志` : ''}`}
        />
      ) : null}
      {group.errors.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`${group.errors.length} 个域名任务创建失败`}
          description={group.errors.map((item) => `${item.domain}：${item.message}`).join('；')}
        />
      ) : null}
      <Tabs
        activeKey={activeTaskId}
        onChange={onSelectTask}
        items={group.tasks.map((item) => ({
          key: item.taskId,
          label: (
            <Space size={4}>
              <Tooltip title={item.domain}>
                <span className="registration-domain-task-tab-domain">{item.domain}</span>
              </Tooltip>
              {group.mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING ? (
                <Tag color={domainStateColor(item.state)}>
                  {DOMAIN_STATE_LABELS[item.state] || item.state}
                </Tag>
              ) : null}
            </Space>
          ),
        }))}
      />
    </div>
  )
}
