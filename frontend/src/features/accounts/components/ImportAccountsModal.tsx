import { Input, Modal } from 'antd'

type ImportAccountsModalProps = {
  open: boolean
  importLoading: boolean
  importText: string
  onClose: () => void
  onSubmit: () => Promise<void> | void
  onImportTextChange: (value: string) => void
}

export function ImportAccountsModal({
  open,
  importLoading,
  importText,
  onClose,
  onSubmit,
  onImportTextChange,
}: ImportAccountsModalProps) {
  return (
    <Modal
      title="批量导入"
      open={open}
      onCancel={onClose}
      onOk={onSubmit}
      confirmLoading={importLoading}
      maskClosable={false}
    >
      <p style={{ marginBottom: 8, fontSize: 12, color: '#7a8ba3' }}>
        每行格式: <code style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 4px', borderRadius: 4 }}>email password [cashier_url]</code>
      </p>
      <Input.TextArea
        value={importText}
        onChange={(event) => onImportTextChange(event.target.value)}
        rows={8}
        style={{ fontFamily: 'monospace' }}
      />
    </Modal>
  )
}
