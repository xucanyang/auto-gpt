export const EXECUTOR_OPTIONS = [
  { value: 'protocol', label: '纯协议' },
  { value: 'headless', label: '无头浏览器' },
  { value: 'headed', label: '有头浏览器' },
] as const

const CHATGPT_EXECUTORS = ['protocol', 'headless', 'headed']

export function getSupportedExecutors(platform?: string) {
  if (!platform || platform === 'chatgpt') return CHATGPT_EXECUTORS
  return ['protocol']
}

export function getExecutorOptions(platform?: string) {
  const supported = new Set(getSupportedExecutors(platform))
  return EXECUTOR_OPTIONS.filter((option) => supported.has(option.value))
}

export function normalizeExecutorForPlatform(platform: string | undefined, executor: string | undefined) {
  const supported = getSupportedExecutors(platform)
  if (executor && supported.includes(executor)) return executor
  return supported[0] || 'protocol'
}
