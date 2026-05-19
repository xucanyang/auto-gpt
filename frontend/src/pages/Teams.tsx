import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import {
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  ImportOutlined,
  MailOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SaveOutlined,
  SearchOutlined,
  TeamOutlined,
  UserAddOutlined,
  UsergroupAddOutlined,
} from '@ant-design/icons'

import { apiFetch } from '@/lib/utils'

const { Text, Paragraph } = Typography

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'active', label: '可用' },
  { value: 'full', label: '已满' },
  { value: 'expired', label: '已过期' },
  { value: 'error', label: '异常' },
  { value: 'banned', label: '已封禁' },
]

const STATUS_COLORS: Record<string, string> = {
  active: 'success',
  full: 'warning',
  expired: 'default',
  error: 'error',
  banned: 'error',
  invited: 'processing',
  joined: 'success',
}

const LIVE_SYNC_META: Record<string, { label: string; color: string; description: string }> = {
  pending: {
    label: '同步中',
    color: 'processing',
    description: '列表已先显示数据库结果，实时成员数正在后台更新。',
  },
  live: {
    label: '实时',
    color: 'success',
    description: '本次列表直接从 auto-chatgpt 内嵌 Team 运行时同步。',
  },
  cached: {
    label: '缓存',
    color: 'processing',
    description: '本次列表使用短时缓存，减少重复实时请求。',
  },
  'stale-cache': {
    label: '过期缓存',
    color: 'warning',
    description: '实时请求失败，当前显示的是上一次成功同步的缓存。',
  },
  fallback: {
    label: 'DB兜底',
    color: 'warning',
    description: '实时成员同步失败，当前显示的是本地 Team DB 里的旧缓存。',
  },
  disabled: {
    label: '未接通',
    color: 'default',
    description: '内嵌 Team 运行时还没就绪，请先确认本地 Team DB 已初始化。',
  },
}

function formatDateTime(value?: string) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function compactText(value?: string, fallback = '-') {
  const text = String(value || '').trim()
  return text || fallback
}

function parseEmailBatch(rawValue?: string) {
  const parts = String(rawValue || '')
    .split(/[\n,;\s]+/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)

  const unique: string[] = []
  const seen = new Set<string>()
  for (const email of parts) {
    if (seen.has(email)) continue
    seen.add(email)
    unique.push(email)
  }
  return unique
}

function getLiveSyncMeta(state?: string) {
  return LIVE_SYNC_META[String(state || 'fallback')] || LIVE_SYNC_META.fallback
}

export default function Teams() {
  const [settingsForm] = Form.useForm()
  const [inviteForm] = Form.useForm()
  const [batchInviteForm] = Form.useForm()
  const [importForm] = Form.useForm()
  const [quickInviteForm] = Form.useForm()
  const [editForm] = Form.useForm()
  const { message: messageApi, modal } = App.useApp()

  const [loading, setLoading] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)
  const [teams, setTeams] = useState<any[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [total, setTotal] = useState(0)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [selectedTeamIds, setSelectedTeamIds] = useState<number[]>([])
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [batchDeleting, setBatchDeleting] = useState(false)

  const [memberDrawerOpen, setMemberDrawerOpen] = useState(false)
  const [memberLoading, setMemberLoading] = useState(false)
  const [memberItems, setMemberItems] = useState<any[]>([])
  const [memberActionLoading, setMemberActionLoading] = useState<Record<string, boolean>>({})
  const [memberTab, setMemberTab] = useState('members')
  const [currentTeam, setCurrentTeam] = useState<any>(null)

  const [deletingTeamId, setDeletingTeamId] = useState<number | null>(null)
  const [clearingMembers, setClearingMembers] = useState(false)
  const [quickInviting, setQuickInviting] = useState(false)

  const [inviteOpen, setInviteOpen] = useState(false)
  const [inviting, setInviting] = useState(false)
  const [batchInviteOpen, setBatchInviteOpen] = useState(false)
  const [batchInviting, setBatchInviting] = useState(false)

  const [importOpen, setImportOpen] = useState(false)
  const [importing, setImporting] = useState(false)

  const [editOpen, setEditOpen] = useState(false)
  const [editLoading, setEditLoading] = useState(false)
  const [savingEdit, setSavingEdit] = useState(false)

  const loadSettings = async () => {
    const data = await apiFetch('/team-lite/settings')
    settingsForm.setFieldsValue(data)
  }

  const syncLiveCounts = async (teamIds: number[]) => {
    const ids = teamIds.filter((id) => Number(id) > 0)
    if (!ids.length) return

    try {
      const result = await apiFetch('/team-lite/teams/live-sync', {
        method: 'POST',
        body: JSON.stringify({ ids }),
      })
      const syncItems = Array.isArray(result.items) ? result.items : []
      const syncMap = new Map<number, any>()
      for (const item of syncItems) {
        syncMap.set(Number(item.id), item)
      }

      setTeams((prev) => prev.map((team) => {
        const syncItem = syncMap.get(Number(team.id))
        if (!syncItem) return team
        const nextTeam = {
          ...team,
          live_sync_state: syncItem.live_sync_state || team.live_sync_state,
          live_sync_error: syncItem.live_sync_error || '',
          live_members_synced: ['live', 'cached', 'stale-cache'].includes(syncItem.live_sync_state),
        }

        if (['live', 'cached', 'stale-cache'].includes(syncItem.live_sync_state)) {
          const joinedCount = Number(syncItem.joined_count || 0)
          const invitedCount = Number(syncItem.invited_count || 0)
          nextTeam.current_members = joinedCount
          nextTeam.invited_members = invitedCount
          nextTeam.remaining_slots = Math.max(0, Number(nextTeam.max_members || 0) - joinedCount)
        }

        return nextTeam
      }))
    } catch {
      // Keep the fast DB response on screen even if background sync fails.
    }
  }

  const loadTeams = async (
    nextPage = page,
    nextPageSize = pageSize,
    nextSearch = search,
    nextStatus = status,
  ) => {
    setLoading(true)
    try {
      const query = new URLSearchParams({
        page: String(nextPage),
        page_size: String(nextPageSize),
      })
      if (nextSearch.trim()) query.set('search', nextSearch.trim())
      if (nextStatus) query.set('status', nextStatus)
      const data = await apiFetch(`/team-lite/teams?${query.toString()}`)
      const nextItems = Array.isArray(data.items) ? data.items : []
      setTeams(nextItems)
      setSelectedTeamIds((prev) => prev.filter((id) => nextItems.some((item: any) => Number(item.id) === Number(id))))
      setPage(Number(data.page || nextPage))
      setPageSize(Number(data.page_size || nextPageSize))
      setTotal(Number(data.total || 0))
      void syncLiveCounts(nextItems.map((item: any) => Number(item.id)))
    } catch (error: any) {
      messageApi.error(error.message || '加载 Team 列表失败')
      setTeams([])
      setSelectedTeamIds([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadSettings().catch(() => {})
    loadTeams().catch(() => {})
  }, [])

  const saveSettings = async () => {
    const values = await settingsForm.validateFields()
    setSavingSettings(true)
    try {
      await apiFetch('/team-lite/settings', {
        method: 'PUT',
        body: JSON.stringify(values),
      })
      messageApi.success('Team 设置已保存')
      await loadTeams(1, pageSize, search, status)
    } catch (error: any) {
      messageApi.error(error.message || '保存设置失败')
    } finally {
      setSavingSettings(false)
    }
  }

  const openEditTeam = async (team: any) => {
    setCurrentTeam(team)
    setEditOpen(true)
    setEditLoading(true)
    try {
      const result = await apiFetch(`/team-lite/teams/${team.id}/info`)
      const detail = result.team || {}
      editForm.setFieldsValue({
        email: detail.email || team.email || '',
        account_id: detail.account_id || team.account_id || '',
        team_name: detail.team_name || team.team_name || '',
        status: detail.status || team.status || 'active',
        max_members: detail.max_members ?? team.max_members ?? 0,
        client_id: detail.client_id || '',
        access_token: detail.access_token || '',
        refresh_token: detail.refresh_token || '',
        session_token: detail.session_token || '',
      })
      setCurrentTeam((prev: any) => ({ ...(prev || {}), ...(detail || {}) }))
    } catch (error: any) {
      messageApi.error(error.message || '加载 Team 详情失败')
      setEditOpen(false)
    } finally {
      setEditLoading(false)
    }
  }

  const submitEditTeam = async () => {
    if (!currentTeam?.id) return
    const values = await editForm.validateFields()
    setSavingEdit(true)
    try {
      const result = await apiFetch(`/team-lite/teams/${currentTeam.id}/update`, {
        method: 'POST',
        body: JSON.stringify(values),
      })
      messageApi.success(result.message || 'Team 已更新')
      const nextTeam = { ...currentTeam, ...values }
      setCurrentTeam(nextTeam)
      setEditOpen(false)
      await loadTeams(page, pageSize, search, status)
      if (memberDrawerOpen) {
        await openMembers(nextTeam)
      }
    } catch (error: any) {
      messageApi.error(error.message || '更新 Team 失败')
    } finally {
      setSavingEdit(false)
    }
  }

  const refreshTeam = async (team: any) => {
    try {
      messageApi.loading({ content: `正在刷新 Team #${team.id}...`, key: `refresh-${team.id}` })
      const result = await apiFetch(`/team-lite/teams/${team.id}/refresh`, {
        method: 'POST',
      })
      messageApi.success({ content: result.message || `Team #${team.id} 刷新完成`, key: `refresh-${team.id}` })
      await loadTeams(page, pageSize, search, status)
      if (memberDrawerOpen && currentTeam?.id === team.id) {
        await openMembers(team)
      }
    } catch (error: any) {
      messageApi.error({ content: error.message || '刷新失败', key: `refresh-${team.id}` })
    }
  }

  const batchRefreshTeams = async () => {
    if (!selectedTeamIds.length) return
    setBatchRefreshing(true)
    try {
      const result = await apiFetch('/team-lite/teams/batch-refresh', {
        method: 'POST',
        body: JSON.stringify({ ids: selectedTeamIds }),
      })
      messageApi.success(result.message || `已刷新 ${selectedTeamIds.length} 个 Team`)
      await loadTeams(page, pageSize, search, status)
    } catch (error: any) {
      messageApi.error(error.message || '批量刷新失败')
    } finally {
      setBatchRefreshing(false)
    }
  }

  const batchDeleteTeams = () => {
    if (!selectedTeamIds.length) return
    modal.confirm({
      title: '批量删除 Team',
      content: `确定删除已选的 ${selectedTeamIds.length} 个 Team 吗？这个操作不可撤销。`,
      okText: '批量删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setBatchDeleting(true)
        try {
          const result = await apiFetch('/team-lite/teams/batch-delete', {
            method: 'POST',
            body: JSON.stringify({ ids: selectedTeamIds }),
          })
          messageApi.success(result.message || '批量删除完成')
          setSelectedTeamIds([])
          await loadTeams(1, pageSize, search, status)
        } catch (error: any) {
          messageApi.error(error.message || '批量删除失败')
          throw error
        } finally {
          setBatchDeleting(false)
        }
      },
    })
  }

  const openMembers = async (team: any) => {
    setCurrentTeam(team)
    setMemberTab('members')
    quickInviteForm.resetFields()
    setMemberDrawerOpen(true)
    setMemberLoading(true)
    try {
      const [memberResult, infoResult] = await Promise.all([
        apiFetch(`/team-lite/teams/${team.id}/members`),
        apiFetch(`/team-lite/teams/${team.id}/info`),
      ])
      const items = Array.isArray(memberResult.members) ? memberResult.members : []
      setMemberItems(items)
      const detail = infoResult.team || {}
      setCurrentTeam((prev: any) => ({ ...(prev || team), ...detail }))
    } catch (error: any) {
      messageApi.error(error.message || '加载成员失败')
      setMemberItems([])
    } finally {
      setMemberLoading(false)
    }
  }

  const openInvite = (team: any) => {
    setCurrentTeam(team)
    inviteForm.resetFields()
    setInviteOpen(true)
  }

  const inviteMemberRequest = async (teamId: number, email: string) => {
    return apiFetch(`/team-lite/teams/${teamId}/invite`, {
      method: 'POST',
      body: JSON.stringify({ email }),
    })
  }

  const inviteMemberToTeam = async (team: any, email: string, options?: { closeModal?: boolean; resetQuickForm?: boolean }) => {
    const normalizedEmail = String(email || '').trim().toLowerCase()
    if (!team?.id || !normalizedEmail) return

    const closeModal = !!options?.closeModal
    const resetQuickForm = !!options?.resetQuickForm

    if (closeModal) setInviting(true)
    if (resetQuickForm) setQuickInviting(true)

    try {
      const result = await inviteMemberRequest(team.id, normalizedEmail)
      messageApi.success(result.message || '邀请已发送')
      if (closeModal) {
        setInviteOpen(false)
      }
      if (resetQuickForm) {
        quickInviteForm.resetFields()
      }
      if (memberDrawerOpen) {
        await openMembers(team)
      }
      await loadTeams(page, pageSize, search, status)
    } catch (error: any) {
      messageApi.error(error.message || '邀请失败')
    } finally {
      if (closeModal) setInviting(false)
      if (resetQuickForm) setQuickInviting(false)
    }
  }

  const submitInvite = async () => {
    const values = await inviteForm.validateFields()
    if (!currentTeam?.id) return
    await inviteMemberToTeam(currentTeam, values.email, { closeModal: true })
  }

  const submitQuickInvite = async () => {
    const values = await quickInviteForm.validateFields()
    if (!currentTeam?.id) return
    await inviteMemberToTeam(currentTeam, values.email, { resetQuickForm: true })
  }

  const openBatchInvite = () => {
    batchInviteForm.resetFields()
    setBatchInviteOpen(true)
  }

  const submitBatchInvite = async () => {
    if (!currentTeam?.id) return
    const values = await batchInviteForm.validateFields()
    const emails = parseEmailBatch(values.emails)
    if (!emails.length) {
      messageApi.error('请输入至少一个邮箱')
      return
    }

    setBatchInviting(true)
    const failures: string[] = []
    let successCount = 0

    try {
      for (const email of emails) {
        try {
          await inviteMemberRequest(currentTeam.id, email)
          successCount += 1
        } catch (error: any) {
          failures.push(`${email}: ${error.message || '发送失败'}`)
        }
      }

      if (successCount) {
        messageApi.success(`批量邀请完成：成功 ${successCount}，失败 ${failures.length}`)
      }
      if (failures.length) {
        Modal.info({
          title: '部分邮箱邀请失败',
          width: 720,
          content: (
            <div style={{ maxHeight: 320, overflow: 'auto' }}>
              <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                {failures.join('\n')}
              </Paragraph>
            </div>
          ),
        })
      }

      if (successCount) {
        setBatchInviteOpen(false)
        batchInviteForm.resetFields()
        if (memberDrawerOpen) {
          await openMembers(currentTeam)
        }
        await loadTeams(page, pageSize, search, status)
      }
    } finally {
      setBatchInviting(false)
    }
  }

  const deleteTeam = (team: any) => {
    modal.confirm({
      title: `删除 Team #${team.id}`,
      content: `确定删除 ${compactText(team.team_name, team.email || `Team #${team.id}`)} 吗？这个操作不可撤销。`,
      okText: '删除',
      okButtonProps: { danger: true, loading: deletingTeamId === team.id },
      cancelText: '取消',
      onOk: async () => {
        setDeletingTeamId(team.id)
        try {
          const result = await apiFetch(`/team-lite/teams/${team.id}/delete`, {
            method: 'POST',
          })
          messageApi.success(result.message || 'Team 已删除')
          if (currentTeam?.id === team.id) {
            setMemberDrawerOpen(false)
            setCurrentTeam(null)
            setMemberItems([])
          }
          setSelectedTeamIds((prev) => prev.filter((id) => id !== team.id))
          await loadTeams(1, pageSize, search, status)
        } catch (error: any) {
          messageApi.error(error.message || '删除 Team 失败')
          throw error
        } finally {
          setDeletingTeamId(null)
        }
      },
    })
  }

  const openImport = () => {
    importForm.setFieldsValue({ import_type: 'batch' })
    setImportOpen(true)
  }

  const submitImport = async () => {
    const values = await importForm.validateFields()
    setImporting(true)
    try {
      const payload = {
        import_type: values.import_type,
        content: values.content,
        email: values.email,
        access_token: values.access_token,
        refresh_token: values.refresh_token,
        session_token: values.session_token,
        client_id: values.client_id,
        account_id: values.account_id,
      }
      const result = await apiFetch('/team-lite/teams/import', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      messageApi.success(result.message || '导入请求已提交')
      setImportOpen(false)
      await loadTeams(1, pageSize, search, status)
    } catch (error: any) {
      messageApi.error(error.message || '导入失败')
    } finally {
      setImporting(false)
    }
  }

  const checkMember = async (email: string, force = false) => {
    if (!currentTeam?.id || !email) return
    const key = `${currentTeam.id}:${email}:${force ? 'force' : 'normal'}`
    setMemberActionLoading((prev) => ({ ...prev, [key]: true }))
    try {
      const query = new URLSearchParams({ email, force: force ? 'true' : 'false' })
      const result = await apiFetch(`/team-lite/teams/${currentTeam.id}/members/check?${query.toString()}`)
      if (result.joined || result.matched) {
        messageApi.success(`${email} 已 joined`)
      } else {
        messageApi.info(`${email} 还没 joined`)
      }
      await openMembers(currentTeam)
      await loadTeams(page, pageSize, search, status)
    } catch (error: any) {
      messageApi.error(error.message || '成员校验失败')
    } finally {
      setMemberActionLoading((prev) => ({ ...prev, [key]: false }))
    }
  }

  const revokeInvite = async (email: string) => {
    if (!currentTeam?.id || !email) return
    const key = `${currentTeam.id}:${email}:revoke`
    setMemberActionLoading((prev) => ({ ...prev, [key]: true }))
    try {
      const result = await apiFetch(`/team-lite/teams/${currentTeam.id}/invites/revoke`, {
        method: 'POST',
        body: JSON.stringify({ email }),
      })
      messageApi.success(result.message || '邀请已撤回')
      await openMembers(currentTeam)
      await loadTeams(page, pageSize, search, status)
    } catch (error: any) {
      messageApi.error(error.message || '撤回邀请失败')
    } finally {
      setMemberActionLoading((prev) => ({ ...prev, [key]: false }))
    }
  }


const removeMember = async (userId: string) => {
  if (!currentTeam?.id || !userId) return
  const key = `${currentTeam.id}:${userId}:delete`
  setMemberActionLoading((prev) => ({ ...prev, [key]: true }))
  try {
    const result = await apiFetch(`/team-lite/teams/${currentTeam.id}/members/delete`, {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
    })
    messageApi.success(result.message || '成员已删除')
    await openMembers(currentTeam)
    await loadTeams(page, pageSize, search, status)
  } catch (error: any) {
    messageApi.error(error.message || '删除成员失败')
  } finally {
    setMemberActionLoading((prev) => ({ ...prev, [key]: false }))
  }
}

const clearAllMembers = async () => {
  if (!currentTeam?.id) return
  modal.confirm({
    title: `清空 ${compactText(currentTeam.team_name, `Team #${currentTeam.id}`)} 成员`,
    content: `将删除 ${memberSummary.joined} 个已 joined 成员，并撤回 ${memberSummary.invited} 个邀请。Owner 会被保留。`,
    okText: '立即清空',
    okButtonProps: { danger: true, loading: clearingMembers },
    cancelText: '取消',
    onOk: async () => {
      setClearingMembers(true)
      try {
        const result = await apiFetch(`/team-lite/teams/${currentTeam.id}/members/delete-all`, {
          method: 'POST',
        })
        messageApi.success(result.message || '已清空成员')
        await openMembers(currentTeam)
        await loadTeams(page, pageSize, search, status)
      } catch (error: any) {
        messageApi.error(error.message || '清空成员失败')
        throw error
      } finally {
        setClearingMembers(false)
      }
    },
  })
}

const settings = Form.useWatch([], settingsForm)
  const missingConnection = !String(settings?.team_manager_db_path || '').trim()
  const importType = Form.useWatch('import_type', importForm) || 'batch'

  const summary = useMemo(() => {
    const available = teams.filter((item) => item.status === 'active').length
    const remaining = teams.reduce((sum, item) => sum + Number(item.remaining_slots || 0), 0)
    const cachedCount = teams.filter((item) => ['cached', 'stale-cache'].includes(item.live_sync_state)).length
    const problemCount = teams.filter((item) => ['fallback', 'disabled', 'stale-cache'].includes(item.live_sync_state)).length
    return {
      availableCount: available,
      remainingSlots: remaining,
      cachedCount,
      problemCount,
    }
  }, [teams])

  const joinedMembers = useMemo(
    () => memberItems.filter((item) => String(item.status || '').toLowerCase() !== 'invited'),
    [memberItems],
  )

  const inviteHistoryItems = useMemo(() => {
    return [...memberItems]
      .filter((item) => String(item.status || '').toLowerCase() === 'invited')
      .sort((a, b) => String(b.added_at || '').localeCompare(String(a.added_at || '')))
  }, [memberItems])

  const memberSummary = useMemo(() => {
    const joined = memberItems.filter((item) => String(item.status || '').toLowerCase() === 'joined').length
    const invited = inviteHistoryItems.length
    return { joined, invited }
  }, [memberItems, inviteHistoryItems])

  const rowSelection: any = {
    selectedRowKeys: selectedTeamIds,
    onChange: (keys: Array<string | number | bigint>) => setSelectedTeamIds(keys.map((key) => Number(key))),
  }

  const teamColumns = [
    {
      title: 'Team',
      key: 'team',
      width: 180,
      render: (_: any, record: any) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
          <Text strong ellipsis={{ tooltip: record.team_name || `Team #${record.id}` }}>
            {compactText(record.team_name, `Team #${record.id}`)}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: record.email || '-' }}>
            {record.email || '-'}
          </Text>
        </div>
      ),
    },
    {
      title: '来源',
      key: 'source_account',
      width: 180,
      render: (_: any, record: any) => {
        const source = record.source_account || {}
        if (!source.account_db_id) {
          return <Text type="secondary">-</Text>
        }
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
            <Text ellipsis={{ tooltip: source.email }}>{compactText(source.email)}</Text>
            <Space wrap size={4}>
              <Tag>{compactText(source.workspace_label || source.workspace_scope)}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>账号 #{source.account_db_id}</Text>
            </Space>
          </div>
        )
      },
    },
    {
      title: '成员',
      key: 'members',
      width: 130,
      render: (_: any, record: any) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <Text>{record.current_members}/{record.max_members}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            待加入 {record.invited_members || 0} · 剩余 {record.remaining_slots}
          </Text>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (value: string) => <Tag color={STATUS_COLORS[value] || 'default'}>{compactText(value)}</Tag>,
    },
    {
      title: '订阅',
      dataIndex: 'subscription_plan',
      key: 'subscription_plan',
      width: 120,
      render: (value: string) => (
        <Text ellipsis={{ tooltip: value || '-' }}>{compactText(value)}</Text>
      ),
    },
    {
      title: '最后同步',
      dataIndex: 'last_sync',
      key: 'last_sync',
      width: 150,
      render: (value: string) => <Text type="secondary">{formatDateTime(value)}</Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      render: (_: any, record: any) => (
        <Space wrap size={[4, 4]}>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => refreshTeam(record)}>
            刷新
          </Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEditTeam(record)}>
            编辑
          </Button>
          <Button size="small" icon={<UsergroupAddOutlined />} onClick={() => openMembers(record)}>
            成员
          </Button>
          <Button type="primary" size="small" icon={<UserAddOutlined />} onClick={() => openInvite(record)}>
            邀请
          </Button>
          <Button danger size="small" icon={<DeleteOutlined />} loading={deletingTeamId === record.id} onClick={() => deleteTeam(record)}>
            删除
          </Button>
        </Space>
      ),
    },
  ]

  const renderInvitedActions = (record: any) => {
    const normalKey = `${currentTeam?.id}:${record.email}:normal`
    const forceKey = `${currentTeam?.id}:${record.email}:force`
    const revokeKey = `${currentTeam?.id}:${record.email}:revoke`
    return (
      <Space size={4} wrap>
        <Button
          size="small"
          icon={<SearchOutlined />}
          loading={!!memberActionLoading[normalKey]}
          onClick={() => checkMember(record.email, false)}
        >
          校验
        </Button>
        <Button
          size="small"
          loading={!!memberActionLoading[forceKey]}
          onClick={() => checkMember(record.email, true)}
        >
          强校验
        </Button>
        <Button
          size="small"
          icon={<RollbackOutlined />}
          loading={!!memberActionLoading[revokeKey]}
          onClick={() => {
            modal.confirm({
              title: '撤回邀请',
              content: `确定撤回对 ${record.email} 的邀请吗？`,
              okText: '撤回',
              cancelText: '取消',
              onOk: () => revokeInvite(record.email),
            })
          }}
        >
          撤回
        </Button>
      </Space>
    )
  }

  const memberColumns = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (value: string) => (
        <Tag color={STATUS_COLORS[value] || 'default'}>{compactText(value)}</Tag>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 140,
      render: (value: string) => compactText(value),
    },
    {
      title: '加入时间',
      dataIndex: 'added_at',
      key: 'added_at',
      width: 180,
      render: (value: string) => formatDateTime(value),
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_: any, record: any) => {
        if (record.status === 'joined' && record.user_id && record.role !== 'account-owner') {
          const deleteKey = `${currentTeam?.id}:${record.user_id}:delete`
          return (
            <Button
              danger
              size="small"
              icon={<DeleteOutlined />}
              loading={!!memberActionLoading[deleteKey]}
              onClick={() => {
                modal.confirm({
                  title: '删除成员',
                  content: `确定从 Team 中删除 ${record.email || record.user_id} 吗？`,
                  okText: '删除',
                  okButtonProps: { danger: true },
                  cancelText: '取消',
                  onOk: () => removeMember(record.user_id),
                })
              }}
            >
              删除成员
            </Button>
          )
        }
        return <Text type="secondary">-</Text>
      },
    },
  ]

  const inviteHistoryColumns = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (value: string) => <Tag color={STATUS_COLORS[value] || 'default'}>{compactText(value)}</Tag>,
    },
    {
      title: '邀请时间',
      dataIndex: 'added_at',
      key: 'added_at',
      width: 180,
      render: (value: string) => formatDateTime(value),
    },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      render: (_: any, record: any) => renderInvitedActions(record),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 700, margin: 0 }}>Team</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>独立 Team Lite 工作台，已经支持列表、导入、邀请、编辑、批量动作和成员管理。</p>
        </div>
        <Space wrap>
          <Button icon={<ImportOutlined />} onClick={openImport}>导入 Team</Button>
          <Button icon={<ReloadOutlined />} onClick={() => loadTeams(page, pageSize, search, status)} loading={loading}>
            刷新列表
          </Button>
        </Space>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <Card>
            <Statistic title="当前筛选 Team 数" value={total} prefix={<TeamOutlined />} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic title="当前页可用 Team" value={summary.availableCount} prefix={<MailOutlined />} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic title="当前页剩余席位" value={summary.remainingSlots} prefix={<UsergroupAddOutlined />} />
          </Card>
        </Col>
      </Row>

      <Card title="Team 本地设置" extra={<Button type="primary" icon={<SaveOutlined />} onClick={saveSettings} loading={savingSettings}>保存</Button>}>
        <Form form={settingsForm} layout="vertical">
          <Row gutter={[16, 0]}>
            <Col xs={24} md={16}>
              <Alert
                type="info"
                showIcon
                message="当前已切到 local-only 模式"
                description="Team 列表、导入、邀请、成员同步都直接走 auto-chatgpt 内嵌运行时，不再依赖 team-manager 对外 API。"
              />
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="team_manager_db_path" label="本地 Team DB 路径" tooltip="默认使用容器内 /runtime/team_manage.db；首次启动会从只读种子库自动初始化。" rules={[{ required: true, message: '请输入 Team DB 路径' }]}>
                <Input prefix={<DatabaseOutlined />} placeholder="/runtime/team_manage.db" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Card>

      {missingConnection ? (
        <Alert type="warning" showIcon message="请先确认 Team DB 路径可用，再使用 Team 管理与实时同步功能。" />
      ) : null}

      {!missingConnection && summary.problemCount > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`当前页有 ${summary.problemCount} 个 Team 没拿到实时成员，正在使用缓存或 DB 兜底。`}
        />
      ) : null}

      {!missingConnection && summary.problemCount === 0 && summary.cachedCount > 0 ? (
        <Alert
          type="info"
          showIcon
          message={`当前页有 ${summary.cachedCount} 个 Team 使用短时缓存，速度更快，但仍比 DB 兜底更接近实时。`}
        />
      ) : null}

      <Card>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', justifyContent: 'space-between', marginBottom: 16 }}>
          <Space wrap>
            <Input.Search
              allowClear
              placeholder="搜索邮箱 / Account ID / Team 名称"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onSearch={(value) => loadTeams(1, pageSize, value, status)}
              style={{ width: 320 }}
            />
            <Select
              value={status}
              options={STATUS_OPTIONS}
              onChange={(value) => {
                setStatus(value)
                loadTeams(1, pageSize, search, value)
              }}
              style={{ minWidth: 160 }}
            />
          </Space>

          <Space wrap>
            <Text type="secondary">已选 {selectedTeamIds.length} 个</Text>
            <Button onClick={() => setSelectedTeamIds([])} disabled={!selectedTeamIds.length}>清空选择</Button>
            <Button icon={<ReloadOutlined />} onClick={batchRefreshTeams} loading={batchRefreshing} disabled={!selectedTeamIds.length}>
              批量刷新
            </Button>
            <Button danger icon={<DeleteOutlined />} onClick={batchDeleteTeams} loading={batchDeleting} disabled={!selectedTeamIds.length}>
              批量删除
            </Button>
          </Space>
        </div>

        <Table
          rowKey="id"
          rowSelection={rowSelection}
          columns={teamColumns}
          dataSource={teams}
          loading={loading}
          tableLayout="fixed"
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            onChange: (nextPage, nextPageSize) => loadTeams(nextPage, nextPageSize, search, status),
          }}
        />
      </Card>

      <Drawer
        title={currentTeam ? `Team 成员 - ${compactText(currentTeam.team_name, `Team #${currentTeam.id}`)}` : 'Team 成员'}
        open={memberDrawerOpen}
        onClose={() => setMemberDrawerOpen(false)}
        width={920}
        extra={currentTeam ? (
          <Space>
            <Button icon={<DeleteOutlined />} danger loading={clearingMembers} onClick={clearAllMembers}>
              清空成员
            </Button>
            <Button icon={<EditOutlined />} onClick={() => openEditTeam(currentTeam)}>
              编辑
            </Button>
            <Button icon={<DeleteOutlined />} danger onClick={() => deleteTeam(currentTeam)}>
              删除 Team
            </Button>
          </Space>
        ) : null}
      >
        <Card size="small" style={{ marginBottom: 16 }} title="Team 详情">
          <Row gutter={[16, 12]}>
            <Col xs={24} md={12}>
              <Text type="secondary">Team 名称</Text>
              <div>{compactText(currentTeam?.team_name, currentTeam?.email || '-')}</div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">邮箱</Text>
              <div>{compactText(currentTeam?.email)}</div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">Account ID</Text>
              <div style={{ fontFamily: 'monospace', fontSize: 12 }}>{compactText(currentTeam?.account_id)}</div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">状态 / 订阅</Text>
              <div>
                <Space wrap size={6}>
                  <Tag color={STATUS_COLORS[currentTeam?.status] || 'default'}>{compactText(currentTeam?.status)}</Tag>
                  <Tag>{compactText(currentTeam?.subscription_plan)}</Tag>
                  {currentTeam?.plan_type ? <Tag>{currentTeam.plan_type}</Tag> : null}
                </Space>
              </div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">实时同步</Text>
              <div>
                <Space wrap size={6}>
                  {(() => {
                    const meta = getLiveSyncMeta(currentTeam?.live_sync_state)
                    return <Tag color={meta.color}>{meta.label}</Tag>
                  })()}
                  <Text type="secondary">实时 {currentTeam?.current_members ?? 0} / DB {currentTeam?.db_current_members ?? 0}</Text>
                </Space>
              </div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">容量</Text>
              <div>{currentTeam?.current_members ?? 0}/{currentTeam?.max_members ?? 0} · 待加入 {currentTeam?.invited_members ?? 0}</div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">到期时间</Text>
              <div>{formatDateTime(currentTeam?.expires_at)}</div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">创建时间 / 最后同步</Text>
              <div>{formatDateTime(currentTeam?.created_at)} · {formatDateTime(currentTeam?.last_sync)}</div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">Owner 角色 / 设备认证</Text>
              <div>
                <Space wrap size={6}>
                  <Tag>{compactText(currentTeam?.account_role)}</Tag>
                  <Tag color={currentTeam?.device_code_auth_enabled ? 'success' : 'default'}>
                    {currentTeam?.device_code_auth_enabled ? '设备认证已开' : '设备认证未开'}
                  </Tag>
                  <Tag color={Number(currentTeam?.error_count || 0) > 0 ? 'error' : 'default'}>
                    错误 {Number(currentTeam?.error_count || 0)}
                  </Tag>
                </Space>
              </div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">Primary Account</Text>
              <div>
                {currentTeam?.primary_account?.account_name || currentTeam?.primary_account?.account_id
                  ? `${compactText(currentTeam?.primary_account?.account_name)} · ${compactText(currentTeam?.primary_account?.account_id)}`
                  : '-'}
              </div>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary">来源账号</Text>
              <div>
                {currentTeam?.source_account?.account_db_id
                  ? `${compactText(currentTeam?.source_account?.email)} · ${compactText(currentTeam?.source_account?.workspace_label || currentTeam?.source_account?.workspace_scope)} · 账号 #${currentTeam?.source_account?.account_db_id}`
                  : '-'}
              </div>
            </Col>
          </Row>

          {currentTeam?.live_sync_error ? (
            <Alert
              style={{ marginTop: 16 }}
              type="warning"
              showIcon
              message={currentTeam.live_sync_error}
            />
          ) : null}

          <div style={{ marginTop: 16 }}>
            <Text type="secondary">team_accounts</Text>
            {Array.isArray(currentTeam?.team_accounts) && currentTeam.team_accounts.length ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
                {currentTeam.team_accounts.map((account: any) => (
                  <div
                    key={`${account.id}-${account.account_id}`}
                    style={{
                      border: '1px solid rgba(120, 120, 120, 0.16)',
                      borderRadius: 10,
                      padding: '10px 12px',
                    }}
                  >
                    <Space wrap size={6}>
                      <Text strong>{compactText(account.account_name, account.account_id)}</Text>
                      {account.is_primary ? <Tag color="success">primary</Tag> : null}
                      <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 12 }}>{compactText(account.account_id)}</Text>
                      <Text type="secondary">{formatDateTime(account.created_at)}</Text>
                    </Space>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ marginTop: 8 }}><Text type="secondary">暂无 team_accounts 记录</Text></div>
            )}
          </div>
        </Card>

        <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
          <Col span={12}>
            <Card size="small">
              <Statistic title="已 joined" value={memberSummary.joined} />
            </Card>
          </Col>
          <Col span={12}>
            <Card size="small">
              <Statistic title="待加入" value={memberSummary.invited} />
            </Card>
          </Col>
        </Row>

        <Card size="small" style={{ marginBottom: 16 }} title="填写邮箱发送邀请" extra={
          <Button icon={<UsergroupAddOutlined />} onClick={openBatchInvite}>批量邀请</Button>
        }>
          <Form form={quickInviteForm} layout="inline" onFinish={submitQuickInvite}>
            <Form.Item
              name="email"
              style={{ flex: 1, minWidth: 280, marginBottom: 0 }}
              rules={[
                { required: true, message: '请输入邮箱' },
                { type: 'email', message: '邮箱格式不正确' },
              ]}
            >
              <Input placeholder="user@example.com" />
            </Form.Item>
            <Form.Item style={{ marginBottom: 0 }}>
              <Button type="primary" htmlType="submit" icon={<UserAddOutlined />} loading={quickInviting}>
                发送邀请
              </Button>
            </Form.Item>
            <Form.Item style={{ marginBottom: 0 }}>
              <Button onClick={() => openInvite(currentTeam)}>
                高级弹窗
              </Button>
            </Form.Item>
          </Form>
        </Card>

        <Tabs
          activeKey={memberTab}
          onChange={setMemberTab}
          items={[
            {
              key: 'members',
              label: `成员列表 (${joinedMembers.length})`,
              children: (
                <Table
                  rowKey={(record) => `${record.status}-${record.user_id || record.email}`}
                  columns={memberColumns}
                  dataSource={joinedMembers}
                  loading={memberLoading}
                  pagination={false}
                  locale={{ emptyText: '暂无已加入成员' }}
                  scroll={{ x: 920 }}
                />
              ),
            },
            {
              key: 'invites',
              label: `邀请历史 (${inviteHistoryItems.length})`,
              children: inviteHistoryItems.length ? (
                <Table
                  rowKey={(record) => `${record.email}-${record.added_at}`}
                  columns={inviteHistoryColumns}
                  dataSource={inviteHistoryItems}
                  loading={memberLoading}
                  pagination={false}
                  scroll={{ x: 920 }}
                />
              ) : (
                <Empty description="暂无待加入邀请" />
              ),
            },
          ]}
        />
      </Drawer>

      <Modal
        title={currentTeam ? `邀请成员到 ${compactText(currentTeam.team_name, `Team #${currentTeam.id}`)}` : '邀请成员'}
        open={inviteOpen}
        onCancel={() => setInviteOpen(false)}
        onOk={submitInvite}
        okText="发送邀请"
        confirmLoading={inviting}
        destroyOnHidden
      >
        <Form form={inviteForm} layout="vertical">
          <Form.Item
            name="email"
            label="邮箱"
            rules={[
              { required: true, message: '请输入邮箱' },
              { type: 'email', message: '邮箱格式不正确' },
            ]}
          >
            <Input placeholder="user@example.com" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={currentTeam ? `批量邀请到 ${compactText(currentTeam.team_name, `Team #${currentTeam.id}`)}` : '批量邀请'}
        open={batchInviteOpen}
        onCancel={() => setBatchInviteOpen(false)}
        onOk={submitBatchInvite}
        okText="开始批量邀请"
        confirmLoading={batchInviting}
        destroyOnHidden
      >
        <Form form={batchInviteForm} layout="vertical">
          <Form.Item
            name="emails"
            label="邮箱列表"
            rules={[{ required: true, message: '请输入至少一个邮箱' }]}
            extra="支持按换行、空格、逗号、分号分隔多个邮箱。"
          >
            <Input.TextArea rows={10} placeholder={'a@example.com\nb@example.com\nc@example.com'} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="导入 Team"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={submitImport}
        okText="提交导入"
        confirmLoading={importing}
        width={760}
        destroyOnHidden
      >
        <Form form={importForm} layout="vertical" initialValues={{ import_type: 'batch' }}>
          <Form.Item name="import_type" label="导入模式">
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: '批量文本', value: 'batch' },
                { label: '单个账号', value: 'single' },
              ]}
            />
          </Form.Item>

          {importType === 'batch' ? (
            <Form.Item
              name="content"
              label="批量内容"
              rules={[{ required: true, message: '请输入批量导入内容' }]}
              extra="支持旧 Team Manager 的 batch 文本格式，例如每行一条：邮箱,AT,RT,ST,ClientID"
            >
              <Input.TextArea rows={10} placeholder="email@example.com,access_token,refresh_token,session_token,client_id" />
            </Form.Item>
          ) : (
            <>
              <Form.Item name="email" label="邮箱">
                <Input placeholder="team-owner@example.com" />
              </Form.Item>
              <Form.Item name="account_id" label="Account ID">
                <Input placeholder="acc_xxx" />
              </Form.Item>
              <Form.Item name="access_token" label="Access Token">
                <Input.TextArea rows={3} placeholder="access_token" />
              </Form.Item>
              <Form.Item name="refresh_token" label="Refresh Token">
                <Input.TextArea rows={2} placeholder="refresh_token" />
              </Form.Item>
              <Form.Item name="session_token" label="Session Token">
                <Input.TextArea rows={2} placeholder="session_token" />
              </Form.Item>
              <Form.Item name="client_id" label="Client ID">
                <Input placeholder="app_EMoamEEZ73f0CkXaXp7hrann" />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>

      <Modal
        title={currentTeam ? `编辑 Team - ${compactText(currentTeam.team_name, `Team #${currentTeam.id}`)}` : '编辑 Team'}
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={submitEditTeam}
        okText="保存修改"
        confirmLoading={savingEdit}
        width={820}
        destroyOnHidden
      >
        <Form form={editForm} layout="vertical" disabled={editLoading}>
          <Row gutter={[16, 0]}>
            <Col xs={24} md={12}>
              <Form.Item name="team_name" label="Team 名称">
                <Input placeholder="Team name" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="email" label="邮箱">
                <Input placeholder="team@example.com" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="account_id" label="Account ID">
                <Input placeholder="acc_xxx" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="client_id" label="Client ID">
                <Input placeholder="app_EMoamEEZ73f0CkXaXp7hrann" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="status" label="状态">
                <Select options={STATUS_OPTIONS.slice(1)} />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="max_members" label="最大成员数">
                <InputNumber min={1} max={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="access_token" label="Access Token">
            <Input.TextArea rows={3} placeholder="access_token" />
          </Form.Item>
          <Form.Item name="refresh_token" label="Refresh Token">
            <Input.TextArea rows={2} placeholder="refresh_token" />
          </Form.Item>
          <Form.Item name="session_token" label="Session Token">
            <Input.TextArea rows={2} placeholder="session_token" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
