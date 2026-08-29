export type BrowserFamily = 'random' | 'chrome' | 'firefox' | 'safari'
export type WebSessionBrowserFamily = 'account' | 'chrome' | 'firefox'
export type DeepBrowserFamily = 'chrome' | 'firefox'

export const BROWSER_FAMILY_OPTIONS: Array<{ value: BrowserFamily; label: string }> = [
  { value: 'random', label: '随机（Chrome / Firefox / Safari）' },
  { value: 'chrome', label: 'Chrome（curl_cffi 协议画像）' },
  { value: 'firefox', label: 'Firefox（curl_cffi 协议画像）' },
  { value: 'safari', label: 'Safari（curl_cffi 协议画像）' },
]

const FIREFOX_ONLY_OPTION = BROWSER_FAMILY_OPTIONS.filter((option) => option.value === 'firefox')
const DEEP_BROWSER_FAMILY_OPTIONS: Record<
  DeepBrowserFamily,
  Array<{ value: BrowserFamily; label: string }>
> = {
  chrome: [
    { value: 'chrome', label: 'Chromium 151 on Linux（Patchright 原生）' },
  ],
  firefox: [
    { value: 'firefox', label: 'Firefox 147 on macOS（Camoufox 深画像）' },
  ],
}
const SUPPORTED_BROWSER_FAMILIES = new Set<BrowserFamily>([
  'random',
  'chrome',
  'firefox',
  'safari',
])

export function isDeepBrowserExecutor(executor: string | undefined) {
  return executor === 'headless' || executor === 'headed'
}

export function normalizeEffectiveDeepBrowserFamily(value: unknown): DeepBrowserFamily {
  return String(value || '').trim().toLowerCase() === 'firefox' ? 'firefox' : 'chrome'
}

export function getBrowserFamilyOptions(
  platform?: string,
  executor?: string,
  effectiveDeepBrowserFamily?: unknown,
) {
  if (platform && platform !== 'chatgpt') return FIREFOX_ONLY_OPTION
  return isDeepBrowserExecutor(executor)
    ? DEEP_BROWSER_FAMILY_OPTIONS[normalizeEffectiveDeepBrowserFamily(effectiveDeepBrowserFamily)]
    : BROWSER_FAMILY_OPTIONS
}

export function normalizeBrowserFamilyForExecutor(
  platform: string | undefined,
  executor: string | undefined,
  browserFamily: string | undefined,
  effectiveDeepBrowserFamily?: unknown,
): BrowserFamily {
  if (platform && platform !== 'chatgpt') return 'random'
  const normalized = String(browserFamily || '').trim().toLowerCase() as BrowserFamily
  if (isDeepBrowserExecutor(executor)) {
    return normalizeEffectiveDeepBrowserFamily(effectiveDeepBrowserFamily)
  }
  return SUPPORTED_BROWSER_FAMILIES.has(normalized) ? normalized : 'random'
}

export function getBrowserFamilySelectionHelp(
  platform?: string,
  executor?: string,
  effectiveDeepBrowserFamily?: unknown,
) {
  if (platform && platform !== 'chatgpt') return '当前平台不使用 ChatGPT 浏览器指纹。'
  if (isDeepBrowserExecutor(executor)) {
    if (normalizeEffectiveDeepBrowserFamily(effectiveDeepBrowserFamily) === 'firefox') {
      return '浏览器执行器固定使用 Camoufox Firefox 147 的 macOS 深画像；运行在 Xvfb 有头模式，失败不会回退到 Patchright。'
    }
    return '浏览器执行器固定使用 Patchright Chromium 151 的 Linux 原生表面；运行在 Xvfb 有头模式，失败不会回退到 Camoufox。'
  }
  return '纯协议执行器按所选浏览器族生成完整 curl_cffi 画像；选择随机时每个注册尝试独立选择一次。'
}

export function getWebSessionBrowserFamilyOptions(
  effectiveDeepBrowserFamily?: unknown,
): Array<{ value: WebSessionBrowserFamily; label: string }> {
  if (normalizeEffectiveDeepBrowserFamily(effectiveDeepBrowserFamily) === 'firefox') {
    return [
      { value: 'account', label: '复用账号身份并迁移到 Firefox' },
      { value: 'firefox', label: '使用 Firefox 147 on macOS' },
    ]
  }
  return [
    { value: 'account', label: '复用账号身份并迁移到 Chromium' },
    { value: 'chrome', label: '使用 Chromium 151 on Linux' },
  ]
}

export function getWebSessionBrowserFamilySelectionHelp(
  effectiveDeepBrowserFamily?: unknown,
) {
  if (normalizeEffectiveDeepBrowserFamily(effectiveDeepBrowserFamily) === 'firefox') {
    return '旧账号画像会在登录成功后迁移为 Camoufox Firefox 147 的 macOS 深画像；失败时保留原画像，不回退到 Patchright。'
  }
  return '旧账号画像会在登录成功后迁移为 Patchright Chromium 151 的 Linux 原生画像；失败时保留原画像，不回退到 Camoufox。'
}
