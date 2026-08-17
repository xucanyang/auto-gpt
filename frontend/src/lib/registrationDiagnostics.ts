export type RegistrationDiagnosticsMode = 'off' | 'smart' | 'full'

export const REGISTRATION_DIAGNOSTICS_OPTIONS = [
  { label: '关闭', value: 'off' },
  { label: '智能诊断', value: 'smart' },
  { label: '全量留存', value: 'full' },
] as const

export function normalizeRegistrationDiagnosticsMode(
  value: unknown,
  executorType: unknown,
  platform: unknown = 'chatgpt',
): RegistrationDiagnosticsMode {
  if (String(platform || '').trim().toLowerCase() !== 'chatgpt') return 'off'
  const executor = String(executorType || '').trim().toLowerCase()
  if (!['protocol', 'headless', 'headed'].includes(executor)) return 'off'
  const normalized = String(value || '').trim().toLowerCase()
  return normalized === 'full' ? 'full' : normalized === 'smart' ? 'smart' : 'off'
}

export function registrationDiagnosticsModeLabel(value: unknown): string {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'full') return '全量留存'
  if (normalized === 'smart') return '智能诊断'
  return '关闭'
}
