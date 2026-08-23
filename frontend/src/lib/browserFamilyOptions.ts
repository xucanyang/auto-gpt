export type BrowserFamily = 'random' | 'chrome' | 'firefox' | 'safari'
export type WebSessionBrowserFamily = 'account' | 'chrome' | 'firefox'

export const BROWSER_FAMILY_OPTIONS: Array<{ value: BrowserFamily; label: string }> = [
  { value: 'random', label: '随机（Chrome / Firefox / Safari）' },
  { value: 'chrome', label: 'Chrome（curl_cffi 协议画像）' },
  { value: 'firefox', label: 'Firefox（curl_cffi / Camoufox）' },
  { value: 'safari', label: 'Safari（curl_cffi 协议画像）' },
]

const FIREFOX_ONLY_OPTION = BROWSER_FAMILY_OPTIONS.filter((option) => option.value === 'firefox')
const DEEP_BROWSER_FAMILY_OPTIONS: Array<{ value: BrowserFamily; label: string }> = [
  { value: 'firefox', label: 'Firefox on Mac（Camoufox）' },
  { value: 'chrome', label: 'Chrome on Mac（Patchright）' },
]

export const WEB_SESSION_BROWSER_FAMILY_OPTIONS: Array<{
  value: WebSessionBrowserFamily
  label: string
}> = [
  { value: 'account', label: '复用账号现有画像' },
  { value: 'firefox', label: '切换为 Firefox on Mac' },
  { value: 'chrome', label: '切换为 Chrome on Mac' },
]
const SUPPORTED_BROWSER_FAMILIES = new Set<BrowserFamily>([
  'random',
  'chrome',
  'firefox',
  'safari',
])

export function isDeepBrowserExecutor(executor: string | undefined) {
  return executor === 'headless' || executor === 'headed'
}

export function getBrowserFamilyOptions(platform?: string, executor?: string) {
  if (platform && platform !== 'chatgpt') return FIREFOX_ONLY_OPTION
  return isDeepBrowserExecutor(executor) ? DEEP_BROWSER_FAMILY_OPTIONS : BROWSER_FAMILY_OPTIONS
}

export function normalizeBrowserFamilyForExecutor(
  platform: string | undefined,
  executor: string | undefined,
  browserFamily: string | undefined,
): BrowserFamily {
  if (platform && platform !== 'chatgpt') return 'random'
  const normalized = String(browserFamily || '').trim().toLowerCase() as BrowserFamily
  if (isDeepBrowserExecutor(executor)) {
    return normalized === 'chrome' || normalized === 'firefox' ? normalized : 'firefox'
  }
  return SUPPORTED_BROWSER_FAMILIES.has(normalized) ? normalized : 'random'
}

export function getBrowserFamilySelectionHelp(platform?: string, executor?: string) {
  if (platform && platform !== 'chatgpt') return '当前平台不使用 ChatGPT 浏览器指纹。'
  if (isDeepBrowserExecutor(executor)) {
    return 'Firefox 使用 Camoufox，Chrome 使用 Patchright Chromium；两者均固定为完整 macOS 画像，执行失败不会跨内核回退。'
  }
  return '纯协议执行器按所选浏览器族生成完整 curl_cffi 画像；选择随机时每个注册尝试独立选择一次。'
}
