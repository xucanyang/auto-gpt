import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

export type AccountsQueryParams = {
  email?: string
  status?: string
  manuallyUsed?: string
  authType?: string
  phoneBindingState?: string
  subscriptionType?: string
  accountValidity?: string
  sub2apiState?: string
  oaipayState?: string
  ideaSubmitState?: string
  revivalState?: string
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
  phoneBindingState = '',
  subscriptionType = '',
  accountValidity = '',
  sub2apiState = '',
  oaipayState = '',
  ideaSubmitState = '',
  revivalState = '',
  sortBy = '',
  sortOrder = '',
  page = 1,
  pageSize = 50,
}: AccountsQueryParams) {
  return useQuery<AccountsQueryResult>({
    queryKey: ['accounts', { email, status, manuallyUsed, authType, phoneBindingState, subscriptionType, accountValidity, sub2apiState, oaipayState, ideaSubmitState, revivalState, sortBy, sortOrder, page, pageSize }],
    queryFn: async ({ signal }) => {
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
      if (phoneBindingState) params.set('phone_binding_state', phoneBindingState)
      if (subscriptionType) params.set('subscription_type', subscriptionType)
      if (accountValidity) params.set('account_validity', accountValidity)
      if (sub2apiState) params.set('sub2api_state', sub2apiState)
      if (oaipayState) params.set('oaipay_state', oaipayState)
      if (ideaSubmitState) params.set('idea_submit_state', ideaSubmitState)
      if (revivalState) params.set('revival_state', revivalState)
      if (sortBy && sortOrder) {
        params.set('sort_by', sortBy)
        params.set('sort_order', sortOrder)
      }
      return apiFetch(`/accounts?${params}`, { signal }) as Promise<AccountsQueryResult>
    },
    placeholderData: (previousData) => previousData,
    staleTime: 60_000,
  })
}
