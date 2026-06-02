import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

export type AccountsQueryParams = {
  email?: string
  status?: string
  manuallyUsed?: string
  authType?: string
  subscriptionType?: string
  accountValidity?: string
  sub2apiState?: string
  sortBy?: string
  sortOrder?: string
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
  manuallyUsed = '',
  authType = '',
  subscriptionType = '',
  accountValidity = '',
  sub2apiState = '',
  sortBy = '',
  sortOrder = '',
  page = 1,
  pageSize = 50,
}: AccountsQueryParams) {
  return useQuery<AccountsQueryResult>({
    queryKey: ['accounts', { email, status, manuallyUsed, authType, subscriptionType, accountValidity, sub2apiState, sortBy, sortOrder, page, pageSize }],
    queryFn: async () => {
      const params = new URLSearchParams({
        platform: 'chatgpt',
        page: String(page),
        page_size: String(pageSize),
        detail: 'false',
      })
      if (email) params.set('email', email)
      if (status) params.set('status', status)
      if (manuallyUsed) params.set('manually_used', manuallyUsed)
      if (authType) params.set('auth_type', authType)
      if (subscriptionType) params.set('subscription_type', subscriptionType)
      if (accountValidity) params.set('account_validity', accountValidity)
      if (sub2apiState) params.set('sub2api_state', sub2apiState)
      if (sortBy && sortOrder) {
        params.set('sort_by', sortBy)
        params.set('sort_order', sortOrder)
      }
      return apiFetch(`/accounts?${params}`) as Promise<AccountsQueryResult>
    },
  })
}
