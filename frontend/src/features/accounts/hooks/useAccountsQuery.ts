import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

export type AccountsQueryParams = {
  email?: string
  status?: string
  page?: number
  pageSize?: number
}

export type AccountsQueryResult = {
  total: number
  page: number
  items: any[]
}

export function useAccountsQuery({
  email = '',
  status = '',
  page = 1,
  pageSize = 50,
}: AccountsQueryParams) {
  return useQuery<AccountsQueryResult>({
    queryKey: ['accounts', { email, status, page, pageSize }],
    queryFn: async () => {
      const params = new URLSearchParams({
        platform: 'chatgpt',
        page: String(page),
        page_size: String(pageSize),
        detail: 'false',
      })
      if (email) params.set('email', email)
      if (status) params.set('status', status)
      return apiFetch(`/accounts?${params}`) as Promise<AccountsQueryResult>
    },
  })
}
