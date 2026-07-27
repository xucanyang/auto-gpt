import { useEffect, useState } from 'react'

import {
  CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
  loadChatGPTRegistrationMode,
  saveChatGPTRegistrationMode,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

export function usePersistentChatGPTRegistrationMode() {
  const [mode, setModeState] = useState<ChatGPTRegistrationMode>(() =>
    loadChatGPTRegistrationMode(),
  )

  const setMode = (_nextMode: ChatGPTRegistrationMode) => {
    setModeState(CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY)
  }

  useEffect(() => {
    saveChatGPTRegistrationMode(mode)
  }, [mode])

  return {
    mode,
    setMode,
    hasRefreshTokenSolution: false,
  }
}
