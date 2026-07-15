import { Alert, Button, Collapse, Input, InputNumber, Modal, Popconfirm, Space, Tag, Typography } from 'antd'

type BatchGopayWorkbenchProps = {
  open: boolean
  onClose: () => void
  token: {
    colorBorder: string
    borderRadius: number
    colorBgContainer: string
  }
  items: any[]
  phones: any[]
  loading: boolean
  phoneSaving: boolean
  started: boolean
  stopMode: string
  roundInterval: number
  otpAutoResendDelay: number
  otpDelaySaving: boolean
  nextRoundAt: number | null
  phoneCountryCode: string
  phoneNumber: string
  onPhoneCountryCodeChange: (value: string) => void
  onPhoneNumberChange: (value: string) => void
  onSaveOtpDelay: () => Promise<unknown> | unknown
  onRefreshConfig: () => Promise<unknown> | unknown
  onStart: () => Promise<void> | void
  onStopAfterCurrent: () => Promise<void> | void
  onCancelAll: () => Promise<void> | void
  onAddPhone: () => Promise<void> | void
  onMovePhone: (phoneId: string, direction: 'up' | 'down' | 'top' | 'bottom') => Promise<void> | void
  onDeletePhone: (phoneId: string) => Promise<void> | void
  onRoundIntervalChange: (value: number) => void
  onOtpAutoResendDelayChange: (value: number) => void
  formatGopayPhoneLabel: (phone: any) => string
  formatGopayPhoneExpiryLabel: (phone: any) => string
  renderBatchGopayItem: (item: any) => React.ReactNode
  normalizeGopayOtpAutoResendDelay: (value: unknown) => number
  activePhaseMatcher: (item: any) => boolean
}

const { Text } = Typography

export function BatchGopayWorkbench({
  open,
  onClose,
  token,
  items,
  phones,
  loading,
  phoneSaving,
  started,
  stopMode,
  roundInterval,
  otpAutoResendDelay,
  otpDelaySaving,
  nextRoundAt,
  phoneCountryCode,
  phoneNumber,
  onPhoneCountryCodeChange,
  onPhoneNumberChange,
  onSaveOtpDelay,
  onRefreshConfig,
  onStart,
  onStopAfterCurrent,
  onCancelAll,
  onAddPhone,
  onMovePhone,
  onDeletePhone,
  onRoundIntervalChange,
  onOtpAutoResendDelayChange,
  formatGopayPhoneLabel,
  formatGopayPhoneExpiryLabel,
  renderBatchGopayItem,
  normalizeGopayOtpAutoResendDelay,
  activePhaseMatcher,
}: BatchGopayWorkbenchProps) {
  const hasCancellableItems = items.some((item) => ['queued', 'starting', 'running'].includes(item.status) || activePhaseMatcher(item))
  const drainingAfterCurrent = stopMode === 'after_current'

  return (
    <Modal
      title="GoPay 批量支付工作台"
      open={open}
      onCancel={onClose}
      width={980}
      footer={[
        <Button key="close" onClick={onClose}>
          关闭
        </Button>,
      ]}
      maskClosable={false}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
          <Space wrap>
            <Tag color="processing">已选 {items.length}</Tag>
            <Tag color="success">手机号池 {phones.length}</Tag>
            <Tag color="blue">最大并发 {phones.length}</Tag>
            {nextRoundAt ? (
              <Tag color="warning">下一轮等待 {Math.max(0, Math.ceil((nextRoundAt - Date.now()) / 1000))} 秒</Tag>
            ) : null}
          </Space>
          <Space wrap>
            <Text type="secondary">轮次间隔</Text>
            <InputNumber
              min={0}
              max={600}
              value={roundInterval}
              disabled={started}
              onChange={(value) => onRoundIntervalChange(Number(value || 0))}
              addonAfter="秒"
            />
            <Text type="secondary">OTP 自动重发</Text>
            <InputNumber
              min={0}
              max={3600}
              precision={0}
              value={otpAutoResendDelay}
              disabled={started}
              onChange={(value) => onOtpAutoResendDelayChange(normalizeGopayOtpAutoResendDelay(value))}
              addonAfter="秒"
            />
            <Button
              loading={otpDelaySaving}
              disabled={started}
              onClick={onSaveOtpDelay}
            >
              保存延迟
            </Button>
          </Space>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <Space wrap>
            <Button key="refresh-config" loading={loading} onClick={onRefreshConfig}>
              刷新手机号池
            </Button>
            <Button
              key="start"
              type="primary"
              loading={loading || otpDelaySaving}
              disabled={started || items.length === 0 || phones.length === 0}
              onClick={onStart}
            >
              开始批量 GoPay
            </Button>
            <Button
              key="stop-after-current"
              disabled={!started || !hasCancellableItems || drainingAfterCurrent}
              onClick={onStopAfterCurrent}
            >
              完成当前后停止
            </Button>
            <Button
              key="cancel-all"
              danger
              disabled={!hasCancellableItems}
              onClick={onCancelAll}
            >
              立即停止
            </Button>
          </Space>
        </div>
        <Alert
          type="info"
          showIcon
          message="批量模式按手机号池可用数量自动限制并发，并按轮次依次启动。批量启动不会写入全局 GoPay 默认手机号。"
        />
        {drainingAfterCurrent ? (
          <Alert
            type="warning"
            showIcon
            message="当前已启动会话收尾中，后续账号不会启动"
          />
        ) : null}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
          <Text type="secondary">手机号池录入</Text>
          <Input
            style={{ width: 110 }}
            addonBefore="+"
            value={phoneCountryCode}
            onChange={(e) => onPhoneCountryCodeChange(e.target.value)}
            placeholder="区号"
          />
          <Input
            style={{ flex: '1 1 220px', minWidth: 220 }}
            value={phoneNumber}
            onChange={(e) => onPhoneNumberChange(e.target.value)}
            placeholder="手机号"
            inputMode="numeric"
            maxLength={20}
          />
          <Button loading={phoneSaving} onClick={onAddPhone}>
            添加手机号
          </Button>
        </div>
        {phones.length > 0 ? (
          <Collapse
            size="small"
            defaultActiveKey={[]}
            items={[
              {
                key: 'batch-gopay-phone-pool',
                label: `手机号池（${phones.length}）`,
                children: (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {phones.map((phone, index) => (
                      <div
                        key={phone.id}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          gap: 8,
                          alignItems: 'center',
                          padding: '8px 10px',
                          border: `1px solid ${token.colorBorder}`,
                          borderRadius: token.borderRadius,
                          background: token.colorBgContainer,
                        }}
                      >
                        <Space wrap>
                          <Tag>{index + 1}</Tag>
                          <Text>{formatGopayPhoneLabel(phone)}</Text>
                          {formatGopayPhoneExpiryLabel(phone) ? <Tag color="processing">有效期 {formatGopayPhoneExpiryLabel(phone)}</Tag> : <Tag>有效期 -</Tag>}
                        </Space>
                        <Space size={4} wrap>
                          <Button size="small" disabled={started || index === 0} onClick={() => onMovePhone(phone.id, 'up')}>上移</Button>
                          <Button size="small" disabled={started || index === phones.length - 1} onClick={() => onMovePhone(phone.id, 'down')}>下移</Button>
                          <Button size="small" disabled={started || index === 0} onClick={() => onMovePhone(phone.id, 'top')}>置顶</Button>
                          <Button size="small" disabled={started || index === phones.length - 1} onClick={() => onMovePhone(phone.id, 'bottom')}>置底</Button>
                          <Popconfirm title="确认删除该手机号？" onConfirm={() => onDeletePhone(phone.id)} disabled={started}>
                            <Button size="small" danger disabled={started}>删除</Button>
                          </Popconfirm>
                        </Space>
                      </div>
                    ))}
                  </div>
                ),
              },
            ]}
          />
        ) : null}
        {loading ? (
          <Alert
            type="info"
            showIcon
            message="正在加载批量支付配置..."
            description="会先读取手机号池和默认参数，再生成批量任务列表。"
          />
        ) : phones.length === 0 ? (
          <Alert type="warning" showIcon message="手机号池为空，请先在单账号 GoPay 弹窗中保存手机号候选。" />
        ) : items.length === 0 ? (
          <Alert type="warning" showIcon message="当前没有可启动的 ChatGPT 账号，请先勾选需要批量支付的账号。" />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {items.map(renderBatchGopayItem)}
          </div>
        )}
      </Space>
    </Modal>
  )
}
