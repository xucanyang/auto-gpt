import {
  CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

type RegistrationExtra = Record<string, unknown>

export interface ChatGPTRegistrationRequestAdapter {
  readonly mode: ChatGPTRegistrationMode
  extendExtra(extra: RegistrationExtra): RegistrationExtra
}

class RefreshTokenChatGPTRegistrationRequestAdapter
  implements ChatGPTRegistrationRequestAdapter
{
  readonly mode = CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN

  extendExtra(extra: RegistrationExtra): RegistrationExtra {
    const saveRegistrationAccessToken =
      extra.chatgpt_save_registration_access_token_account
    return {
      ...extra,
      chatgpt_registration_mode: this.mode,
      chatgpt_has_refresh_token_solution: true,
      // RT 抓取失败时，注册阶段通常已经拿到了可用 AT。默认保底保存 AT-only，
      // 否则会出现“账号实际注册成功，但任务显示失败且库存没落地”的资源浪费。
      chatgpt_save_registration_access_token_account:
        saveRegistrationAccessToken === undefined ||
        saveRegistrationAccessToken === null
          ? true
          : Boolean(saveRegistrationAccessToken),
    }
  }
}

class AccessTokenOnlyChatGPTRegistrationRequestAdapter
  implements ChatGPTRegistrationRequestAdapter
{
  readonly mode = CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY

  extendExtra(extra: RegistrationExtra): RegistrationExtra {
    return {
      ...extra,
      chatgpt_registration_mode: this.mode,
      chatgpt_has_refresh_token_solution: false,
    }
  }
}

export function buildChatGPTRegistrationRequestAdapter(
  platform: string | undefined,
  mode: ChatGPTRegistrationMode,
): ChatGPTRegistrationRequestAdapter | null {
  if (platform !== 'chatgpt') return null

  if (mode === CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY) {
    return new AccessTokenOnlyChatGPTRegistrationRequestAdapter()
  }

  return new RefreshTokenChatGPTRegistrationRequestAdapter()
}
