import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'
import type { ActiveTaskSnapshot } from '@/lib/activeTaskControls'

export function useActiveTasksQuery(enabled = false) {
  return useQuery<ActiveTaskSnapshot[]>({
    queryKey: ['active-tasks'],
    enabled,
    queryFn: async () => {
      const data = await apiFetch('/tasks/active-summary')
      return Array.isArray(data) ? data : []
    },
    refetchInterval: enabled ? 3000 : false,
    refetchIntervalInBackground: false,
  })
}
