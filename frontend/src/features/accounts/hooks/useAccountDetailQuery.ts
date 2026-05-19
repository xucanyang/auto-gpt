import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

export function useAccountDetailQuery(accountId?: number | null, enabled = false) {
  return useQuery<any>({
    queryKey: ['account-detail', accountId],
    enabled: enabled && Boolean(accountId),
    queryFn: async () => {
      if (!accountId) return null
      return apiFetch(`/accounts/${accountId}`)
    },
  })
}
