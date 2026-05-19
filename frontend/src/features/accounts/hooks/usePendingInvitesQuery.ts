import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

export function usePendingInvitesQuery(enabled = false) {
  return useQuery<any[]>({
    queryKey: ['chatgpt-pending-business-invites'],
    enabled,
    queryFn: async () => {
      const data = await apiFetch('/chatgpt/pending-business-invites?limit=200')
      return data.items || []
    },
  })
}
