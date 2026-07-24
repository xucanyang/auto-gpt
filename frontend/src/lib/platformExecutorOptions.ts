export const EXECUTOR_OPTIONS = [
  { value: 'protocol', label: '纯协议（不启动注册浏览器）' },
  { value: 'headless', label: '无头浏览器' },
  { value: 'headed', label: '有头浏览器' },
] as const

export const EXECUTOR_SELECTION_HELP = '三种注册执行器互斥：每个任务仅运行所选模式，执行中不会在纯协议、无头和有头浏览器之间自动切换。'

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
