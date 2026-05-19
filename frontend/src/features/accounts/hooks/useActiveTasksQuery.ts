import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

export function useActiveTasksQuery(enabled = false) {
  return useQuery<any[]>({
    queryKey: ['active-tasks'],
    enabled,
    queryFn: async () => {
      const data = await apiFetch('/tasks/active-summary')
      return Array.isArray(data) ? data : []
    },
  })
}
