import { Form, Input, Modal, Select } from 'antd'

type AddAccountModalProps = {
  open: boolean
  addForm: any
  onClose: () => void
  onSubmit: () => Promise<void> | void
}

export function AddAccountModal({
  open,
  addForm,
  onClose,
  onSubmit,
}: AddAccountModalProps) {
  return (
    <Modal
      title="手动新增账号"
      open={open}
      onCancel={onClose}
      onOk={onSubmit}
      maskClosable={false}
    >
      <Form form={addForm} layout="vertical">
        <Form.Item name="email" label="邮箱" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="password" label="密码" rules={[{ required: true }]}>
          <Input.Password />
        </Form.Item>
        <Form.Item name="token" label="Token">
          <Input />
        </Form.Item>
        <Form.Item name="cashier_url" label="试用链接">
          <Input />
        </Form.Item>
        <Form.Item name="status" label="状态" initialValue="registered">
          <Select
            options={[
              { value: 'registered', label: '已注册' },
              { value: 'trial', label: '试用中' },
              { value: 'subscribed', label: '已订阅' },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
