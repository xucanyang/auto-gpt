export type BrowserFamily = 'random' | 'chrome' | 'firefox' | 'safari'
export type WebSessionBrowserFamily = 'account' | 'chrome' | 'firefox'

export const BROWSER_FAMILY_OPTIONS: Array<{ value: BrowserFamily; label: string }> = [
  { value: 'random', label: '随机（Chrome / Firefox / Safari）' },
  { value: 'chrome', label: 'Chrome（curl_cffi 协议画像）' },
  { value: 'firefox', label: 'Firefox（curl_cffi 协议画像）' },
  { value: 'safari', label: 'Safari（curl_cffi 协议画像）' },
]

const FIREFOX_ONLY_OPTION = BROWSER_FAMILY_OPTIONS.filter((option) => option.value === 'firefox')
const DEEP_BROWSER_FAMILY_OPTIONS: Array<{ value: BrowserFamily; label: string }> = [
  { value: 'chrome', label: 'Chromium 151 on Linux（Patchright 原生）' },
]

export const WEB_SESSION_BROWSER_FAMILY_OPTIONS: Array<{
  value: WebSessionBrowserFamily
  label: string
}> = [
  { value: 'account', label: '复用账号身份并迁移到 Chromium' },
  { value: 'chrome', label: '使用 Chromium 151 on Linux' },
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
    return 'chrome'
  }
  return SUPPORTED_BROWSER_FAMILIES.has(normalized) ? normalized : 'random'
}

export function getBrowserFamilySelectionHelp(platform?: string, executor?: string) {
  if (platform && platform !== 'chatgpt') return '当前平台不使用 ChatGPT 浏览器指纹。'
  if (isDeepBrowserExecutor(executor)) {
    return '浏览器执行器固定使用 Patchright Chromium 151 的 Linux 原生表面；运行在 Xvfb 有头模式，失败不会回退到 Camoufox。'
  }
  return '纯协议执行器按所选浏览器族生成完整 curl_cffi 画像；选择随机时每个注册尝试独立选择一次。'
}
