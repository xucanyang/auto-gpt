export type BrowserFamily = 'random' | 'chrome' | 'firefox' | 'safari'

export const BROWSER_FAMILY_OPTIONS: Array<{ value: BrowserFamily; label: string }> = [
  { value: 'random', label: '随机（Chrome / Firefox / Safari）' },
  { value: 'chrome', label: 'Chrome（curl_cffi 协议画像）' },
  { value: 'firefox', label: 'Firefox（curl_cffi / Camoufox）' },
  { value: 'safari', label: 'Safari（curl_cffi 协议画像）' },
]

const FIREFOX_ONLY_OPTION = BROWSER_FAMILY_OPTIONS.filter((option) => option.value === 'firefox')
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
  return isDeepBrowserExecutor(executor) ? FIREFOX_ONLY_OPTION : BROWSER_FAMILY_OPTIONS
}

export function normalizeBrowserFamilyForExecutor(
  platform: string | undefined,
  executor: string | undefined,
  browserFamily: string | undefined,
): BrowserFamily {
  if (platform && platform !== 'chatgpt') return 'random'
  if (isDeepBrowserExecutor(executor)) return 'firefox'
  const normalized = String(browserFamily || '').trim().toLowerCase() as BrowserFamily
  return SUPPORTED_BROWSER_FAMILIES.has(normalized) ? normalized : 'random'
}

export function getBrowserFamilySelectionHelp(platform?: string, executor?: string) {
  if (platform && platform !== 'chatgpt') return '当前平台不使用 ChatGPT 浏览器指纹。'
  if (isDeepBrowserExecutor(executor)) {
    return '当前无头/有头执行器使用 Camoufox 深浏览器环境，只支持 Firefox；Chrome 和 Safari 仅在纯协议执行器中可用。'
  }
  return '纯协议执行器按所选浏览器族生成完整 curl_cffi 画像；选择随机时每个注册尝试独立选择一次。'
}
