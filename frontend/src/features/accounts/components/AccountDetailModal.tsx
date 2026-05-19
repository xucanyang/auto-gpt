import { Button, Form, Input, Modal, Select, Tag, Typography, theme } from 'antd'

const { Text } = Typography

function SummaryField({
  label,
  value,
  code = false,
}: {
  label: string
  value?: string
  code?: boolean
}) {
  const { token } = theme.useToken()
  if (!value) return null

  const content = code ? value : value
  const isBlock = code || content.length > 96 || content.includes('\n')

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '104px minmax(0, 1fr)',
        gap: 12,
        alignItems: 'start',
      }}
    >
      <Text type="secondary" style={{ fontSize: 12, lineHeight: '20px' }}>
        {label}
      </Text>
      {isBlock ? (
        <pre
          style={{
            margin: 0,
            padding: code ? '8px 10px' : 0,
            borderRadius: code ? token.borderRadius : 0,
            border: code ? `1px solid ${token.colorBorder}` : 'none',
            background: code ? token.colorBgElevated : 'transparent',
            color: code ? token.colorText : token.colorTextSecondary,
            fontFamily: code ? 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace' : 'inherit',
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowWrap: 'anywhere',
            maxHeight: code ? 160 : 'none',
            overflow: code ? 'auto' : 'visible',
          }}
        >
          {content}
        </pre>
      ) : (
        <Text style={{ display: 'block', color: token.colorTextSecondary, lineHeight: '20px' }}>
          {content}
        </Text>
      )}
    </div>
  )
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  const { token } = theme.useToken()

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        borderRadius: token.borderRadiusLG,
        border: `1px solid ${token.colorBorder}`,
        background: token.colorFillAlter,
      }}
    >
      <div style={{ marginBottom: 10, fontWeight: 600, color: token.colorText }}>{title}</div>
      {children}
    </div>
  )
}

export function LocalProbeSummary({
  probe,
  authStateMeta,
  planMeta,
  codexStateMeta,
  formatSyncTime,
}: {
  probe: any
  authStateMeta: (state?: string) => { color: string; label: string }
  planMeta: (plan?: string) => { color: string; label: string }
  codexStateMeta: (state?: string) => { color: string; label: string }
  formatSyncTime: (value?: string) => string
}) {
  const checkedAt = probe?.checked_at || probe?.auth?.checked_at || probe?.subscription?.checked_at || probe?.codex?.checked_at
  const auth = probe?.auth || {}
  const subscription = probe?.subscription || {}
  const codex = probe?.codex || {}

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={authStateMeta(auth.state).color}>认证: {authStateMeta(auth.state).label}</Tag>
        <Tag color={planMeta(subscription.plan).color}>订阅: {planMeta(subscription.plan).label}</Tag>
        <Tag color={codexStateMeta(codex.state).color}>Codex: {codexStateMeta(codex.state).label}</Tag>
      </div>
      <SummaryField label="探测时间" value={checkedAt ? formatSyncTime(checkedAt) : ''} />
      <SummaryField label="认证信息" value={auth.message} code />
      <SummaryField label="工作区套餐" value={subscription.workspace_plan_type} />
      <SummaryField label="Codex 信息" value={codex.message} code />
    </div>
  )
}

export function CliproxySyncSummary({ sync, formatSyncTime }: { sync: any; formatSyncTime: (value?: string) => string }) {
  const meta = (() => {
    if (!sync || Object.keys(sync).length === 0) return { color: 'default', label: '未同步' }
    if (sync.remote_state === 'unreachable') return { color: 'error', label: '不可连接' }
    if (sync.remote_state === 'not_found') return { color: 'default', label: '远端未发现' }
    if (!sync.uploaded) return { color: 'default', label: '未发现' }
    if (sync.remote_state === 'usable') return { color: 'success', label: '远端可用' }
    if (sync.remote_state === 'account_deactivated') return { color: 'error', label: '远端已失效' }
    if (sync.remote_state === 'access_token_invalidated') return { color: 'error', label: '远端AT失效' }
    if (sync.remote_state === 'unauthorized') return { color: 'error', label: '远端未授权' }
    if (sync.remote_state === 'payment_required') return { color: 'warning', label: '远端需付费/权限' }
    if (sync.remote_state === 'quota_exhausted') return { color: 'warning', label: '远端额度耗尽' }
    if (sync.status === 'active') return { color: 'processing', label: '远端Active' }
    if (sync.status === 'refreshing') return { color: 'processing', label: '远端刷新中' }
    if (sync.status === 'pending') return { color: 'default', label: '远端待处理' }
    if (sync.status === 'error') return { color: 'error', label: '远端错误' }
    if (sync.status === 'disabled') return { color: 'default', label: '远端禁用' }
    return { color: 'default', label: '未同步' }
  })()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
      </div>
      <SummaryField label="状态信息" value={sync?.status_message} code />
      <SummaryField label="auth-file" value={sync?.name} />
      <SummaryField label="API URL" value={sync?.base_url} />
      <SummaryField label="同步时间" value={sync?.last_synced_at ? formatSyncTime(sync.last_synced_at) : ''} />
      <SummaryField label="远端刷新时间" value={sync?.last_refresh ? formatSyncTime(sync.last_refresh) : ''} />
      <SummaryField label="下次重试时间" value={sync?.next_retry_after ? formatSyncTime(sync.next_retry_after) : ''} />
      <SummaryField label="探测信息" value={sync?.last_probe_message} code />
    </div>
  )
}

function Sub2ApiSyncSummary({ sync, formatSyncTime }: { sync: any; formatSyncTime: (value?: string) => string }) {
  const meta = (() => {
    if (!sync || Object.keys(sync).length === 0) return { color: 'default', label: '未同步' }
    if (sync.remote_state === 'unreachable') return { color: 'error', label: 'DB不可达' }
    if (sync.remote_state === 'ambiguous') return { color: 'warning', label: '多条候选' }
    if (sync.remote_state === 'cross_workspace_only') return { color: 'processing', label: '其他工作区已存在' }
    if (sync.remote_state === 'deleted_exact_match') return { color: 'warning', label: '已删可重传' }
    if (sync.remote_state === 'not_found') return { color: 'default', label: '远端未发现' }
    if (sync.remote_state === 'exists') return { color: 'success', label: '远端已存在' }
    if (sync.status === 'active') return { color: 'processing', label: '远端Active' }
    if (sync.status === 'error') return { color: 'error', label: '远端错误' }
    return { color: 'default', label: '未同步' }
  })()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
        {sync?.matched_by ? <Tag>{`matched_by: ${sync.matched_by}`}</Tag> : null}
      </div>
      <SummaryField label="远端账号 ID" value={sync?.remote_account_id ? String(sync.remote_account_id) : ''} />
      <SummaryField label="匹配方式" value={sync?.matched_by} />
      <SummaryField label="候选数量" value={sync?.candidate_count ? String(sync.candidate_count) : ''} />
      <SummaryField label="最近探测" value={sync?.checked_at ? formatSyncTime(sync.checked_at) : ''} />
      <SummaryField label="最近尝试" value={sync?.last_attempt_at ? formatSyncTime(sync.last_attempt_at) : ''} />
      <SummaryField label="状态信息" value={sync?.message || sync?.last_message} code />
      <SummaryField label="候选明细" value={sync?.candidates ? JSON.stringify(sync.candidates, null, 2) : ''} code />
    </div>
  )
}

type AccountDetailModalProps = {
  open: boolean
  onClose: () => void
  onSave: () => Promise<void> | void
  currentAccount: any
  detailForm: any
  token: {
    colorFillAlter: string
    colorBorder: string
    borderRadius: number
  }
  importingTeamAccountId: number | null
  onImportAccountToTeam: (record: any) => Promise<void> | void
  formatSyncTime: (value?: string) => string
  getRefreshToken: (record: any) => string
  canImportAccountToTeam: (record: any) => boolean
  authStateMeta: (state?: string) => { color: string; label: string }
  planMeta: (plan?: string) => { color: string; label: string }
  codexStateMeta: (state?: string) => { color: string; label: string }
}

export function AccountDetailModal({
  open,
  onClose,
  onSave,
  currentAccount,
  detailForm,
  token,
  importingTeamAccountId,
  onImportAccountToTeam,
  formatSyncTime,
  getRefreshToken,
  canImportAccountToTeam,
  authStateMeta,
  planMeta,
  codexStateMeta,
}: AccountDetailModalProps) {
  return (
    <Modal
      title="账号详情"
      open={open}
      onCancel={onClose}
      onOk={onSave}
      maskClosable={false}
      width={760}
      styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
    >
      {currentAccount && (
        <>
          <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
            <Form.Item name="status" label="状态">
              <Select
                options={[
                  { value: 'registered', label: '已注册' },
                  { value: 'trial', label: '试用中' },
                  { value: 'subscribed', label: '已订阅' },
                  { value: 'expired', label: '已过期' },
                  { value: 'invalid', label: '已失效' },
                ]}
              />
            </Form.Item>
            <Form.Item name="token" label="Access Token">
              <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
            </Form.Item>
          </Form>
          {(() => {
            const rt = getRefreshToken(currentAccount)
            if (!rt) return null
            return (
              <div style={{ marginTop: 8 }}>
                <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>Refresh Token</div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 8,
                    background: token.colorFillAlter,
                    border: `1px solid ${token.colorBorder}`,
                    borderRadius: token.borderRadius,
                    padding: '8px 10px',
                  }}
                >
                  <Text
                    style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                    copyable={{ text: rt, tooltips: ['复制 RT', '已复制'] }}
                  >
                    {rt}
                  </Text>
                </div>
              </div>
            )
          })()}
          {canImportAccountToTeam(currentAccount) ? (
            <div style={{ marginTop: 12 }}>
              <Button
                type="primary"
                loading={importingTeamAccountId === currentAccount.id}
                onClick={() => onImportAccountToTeam(currentAccount)}
              >
                设为 Team 母号
              </Button>
            </div>
          ) : null}
          {currentAccount.teamInviteSource ? (
            <DetailSection title="Business / Team Invite 来源">
              <SummaryField label="母号邮箱" value={currentAccount.teamInviteSource.team_email} />
              <SummaryField label="母号 Account ID" value={currentAccount.teamInviteSource.team_account_id || currentAccount.teamInviteSource.primary_account_id} />
              <SummaryField label="母号名称" value={currentAccount.teamInviteSource.primary_account_name} />
              <SummaryField label="Team 名称" value={currentAccount.teamInviteSource.team_name} />
              <SummaryField label="Team ID" value={currentAccount.teamInviteSource.team_id ? String(currentAccount.teamInviteSource.team_id) : ''} />
              <SummaryField label="Invite 状态" value={currentAccount.teamInviteSource.invite_status} />
              <SummaryField label="邀请时间" value={currentAccount.teamInviteSource.invited_at ? formatSyncTime(currentAccount.teamInviteSource.invited_at) : ''} />
              <SummaryField label="加入时间" value={currentAccount.teamInviteSource.joined_at ? formatSyncTime(currentAccount.teamInviteSource.joined_at) : ''} />
              <SummaryField label="移除时间" value={currentAccount.teamInviteSource.removed_from_team_at ? formatSyncTime(currentAccount.teamInviteSource.removed_from_team_at) : ''} />
            </DetailSection>
          ) : null}
          <DetailSection title="本地真实状态">
            {currentAccount.chatgptLocal && Object.keys(currentAccount.chatgptLocal).length > 0 ? (
              <LocalProbeSummary
                probe={currentAccount.chatgptLocal}
                authStateMeta={authStateMeta}
                planMeta={planMeta}
                codexStateMeta={codexStateMeta}
                formatSyncTime={formatSyncTime}
              />
            ) : (
              <Text type="secondary">尚未探测。可在操作菜单中点击“探测本地状态”。</Text>
            )}
          </DetailSection>
          <DetailSection title="CLIProxyAPI 状态">
            {currentAccount.cliproxySync && Object.keys(currentAccount.cliproxySync).length > 0 ? (
              <CliproxySyncSummary sync={currentAccount.cliproxySync} formatSyncTime={formatSyncTime} />
            ) : (
              <Text type="secondary">尚未同步。可在操作菜单中点击“同步 CLIProxyAPI 状态”。</Text>
            )}
          </DetailSection>
          <DetailSection title="Sub2API 状态">
            {currentAccount.sub2apiSync && Object.keys(currentAccount.sub2apiSync).length > 0 ? (
              <Sub2ApiSyncSummary sync={currentAccount.sub2apiSync} formatSyncTime={formatSyncTime} />
            ) : (
              <Text type="secondary">尚未同步。可在“状态同步”里先执行一次 Sub2API 探测，或直接走补传。</Text>
            )}
          </DetailSection>
        </>
      )}
    </Modal>
  )
}
