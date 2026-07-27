import { Space, Tag, Typography } from 'antd'

import {
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

const { Text } = Typography

type ChatGPTRegistrationModeSwitchProps = {
  mode: ChatGPTRegistrationMode
  onChange: (mode: ChatGPTRegistrationMode) => void
}

export function ChatGPTRegistrationModeSwitch({
  mode: _mode,
  onChange: _onChange,
}: ChatGPTRegistrationModeSwitchProps) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space align="center" wrap>
        <Tag color="success">AccessToken-only 注册</Tag>
      </Space>
      <Text type="secondary">
        注册完成后保存 AccessToken、Web Session 和 Cookie 即结束；完整 Auth/refresh_token 请使用独立的补抓 Auth 任务。
      </Text>
    </Space>
  )
}
