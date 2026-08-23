import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '@/lib/utils'

const EMPTY_EXACT_EMAIL_FILTER: string[] = []

export type AccountsQueryParams = {
  filterPresetId?: string
  filterPresetRevision?: string
  primaryPresetId?: string
  secondaryScope?: '' | 'unassigned' | 'fixed'
  fixedGroupId?: string
  fixedGroupRevision?: number
  email?: string
  emails?: string[]
  status?: string
  manuallyUsed?: string
  authType?: string
  phoneBindingState?: string
  paymentLinkPlatform?: string
  paymentLinkGenerated?: string
  subscriptionType?: string
  accountValidity?: string
  sub2apiState?: string
  oaipayState?: string
  zeroAmountEligibilityState?: string
  gcashPaymentMethodState?: string
  checkoutLinkType?: string
  submitState?: string
  hasSubmitted?: string
  /** Legacy caller alias; migrated callers should use submitState. */
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
  fixed_preset?: {
    id: string
    parent_preset_id?: string
    revision?: number
    legacy?: boolean
    stored_account_count: number
    resolved_account_ids: number[]
    missing_account_ids: number[]
  }
}

export function useAccountsQuery({
  filterPresetId = '',
  filterPresetRevision = '',
  primaryPresetId = '',
  secondaryScope = '',
  fixedGroupId = '',
  fixedGroupRevision,
  email = '',
  emails = EMPTY_EXACT_EMAIL_FILTER,
  status = '',
  manuallyUsed = '',
  authType = '',
  phoneBindingState = '',
  paymentLinkPlatform = '',
  paymentLinkGenerated = '',
  subscriptionType = '',
  accountValidity = '',
  sub2apiState = '',
  oaipayState = '',
  zeroAmountEligibilityState = '',
  gcashPaymentMethodState = '',
  checkoutLinkType = '',
  submitState = '',
  hasSubmitted = '',
  ideaSubmitState = '',
  revivalState = '',
  sortBy = '',
  sortOrder = '',
  page = 1,
  pageSize = 50,
}: AccountsQueryParams) {
  const canonicalSubmitState = submitState || ''
  return useQuery<AccountsQueryResult>({
    queryKey: ['accounts', { filterPresetId, filterPresetRevision, primaryPresetId, secondaryScope, fixedGroupId, fixedGroupRevision, email, emails, status, manuallyUsed, authType, phoneBindingState, paymentLinkPlatform, paymentLinkGenerated, subscriptionType, accountValidity, sub2apiState, oaipayState, zeroAmountEligibilityState, gcashPaymentMethodState, checkoutLinkType, submitState: canonicalSubmitState, hasSubmitted, ideaSubmitState, revivalState, sortBy, sortOrder, page, pageSize }],
    queryFn: async ({ signal }) => {
      if (emails.length > 0) {
        return apiFetch('/accounts/query', {
          method: 'POST',
          signal,
          body: JSON.stringify({
            platform: 'chatgpt',
            filter_preset_id: filterPresetId,
            primary_preset_id: primaryPresetId,
            secondary_scope: secondaryScope,
            fixed_group_id: fixedGroupId,
            fixed_group_revision: fixedGroupRevision,
            email: '',
            emails,
            status,
            manually_used: manuallyUsed,
            auth_type: authType,
            phone_binding_state: phoneBindingState,
            payment_link_platform: paymentLinkPlatform,
            payment_link_generated: paymentLinkGenerated || null,
            subscription_type: subscriptionType,
            account_validity: accountValidity,
            sub2api_state: sub2apiState,
            oaipay_state: oaipayState,
            zero_amount_eligibility_state: zeroAmountEligibilityState,
            gcash_payment_method_state: gcashPaymentMethodState,
            checkout_link_type: checkoutLinkType,
            submit_state: canonicalSubmitState,
            has_submitted: hasSubmitted || null,
            idea_submit_state: canonicalSubmitState ? '' : ideaSubmitState,
            revival_state: revivalState,
            sort_by: sortBy,
            sort_order: sortOrder,
            page,
            page_size: pageSize,
            detail: false,
          }),
        }) as Promise<AccountsQueryResult>
      }
      const params = new URLSearchParams({
        platform: 'chatgpt',
        page: String(page),
        page_size: String(pageSize),
        detail: 'false',
      })
      if (filterPresetId) params.set('filter_preset_id', filterPresetId)
      if (primaryPresetId) params.set('primary_preset_id', primaryPresetId)
      if (secondaryScope) params.set('secondary_scope', secondaryScope)
      if (fixedGroupId) params.set('fixed_group_id', fixedGroupId)
      if (fixedGroupRevision) params.set('fixed_group_revision', String(fixedGroupRevision))
      if (email) params.set('email', email)
      if (status) params.set('status', status)
      if (manuallyUsed) params.set('manually_used', manuallyUsed)
      if (authType) params.set('auth_type', authType)
      if (phoneBindingState) params.set('phone_binding_state', phoneBindingState)
      if (paymentLinkPlatform) params.set('payment_link_platform', paymentLinkPlatform)
      if (paymentLinkGenerated) params.set('payment_link_generated', paymentLinkGenerated)
      if (subscriptionType) params.set('subscription_type', subscriptionType)
      if (accountValidity) params.set('account_validity', accountValidity)
      if (sub2apiState) params.set('sub2api_state', sub2apiState)
      if (oaipayState) params.set('oaipay_state', oaipayState)
      if (zeroAmountEligibilityState) params.set('zero_amount_eligibility_state', zeroAmountEligibilityState)
      if (gcashPaymentMethodState) params.set('gcash_payment_method_state', gcashPaymentMethodState)
      if (checkoutLinkType) params.set('checkout_link_type', checkoutLinkType)
      if (canonicalSubmitState) params.set('submit_state', canonicalSubmitState)
      if (hasSubmitted) params.set('has_submitted', hasSubmitted)
      // Compatibility for external callers that still explicitly pass the
      // legacy alias. Accounts.tsx uses canonical submitState exclusively.
      if (!canonicalSubmitState && ideaSubmitState) params.set('idea_submit_state', ideaSubmitState)
      if (revivalState) params.set('revival_state', revivalState)
      if (sortBy && sortOrder) {
        params.set('sort_by', sortBy)
        params.set('sort_order', sortOrder)
      }
      return apiFetch(`/accounts?${params}`, { signal }) as Promise<AccountsQueryResult>
    },
    placeholderData: (previousData) => previousData,
    staleTime: filterPresetId || secondaryScope ? 0 : 60_000,
  })
}
