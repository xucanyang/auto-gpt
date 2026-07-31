import { lazy } from 'react'
import type { LazyExoticComponent, ComponentType } from 'react'

export type AppRouteItem = {
  path: string
  key: string
  label?: string
  loader: LazyExoticComponent<ComponentType<any>>
}

const DashboardPage = lazy(() => import('@/pages/Dashboard'))
const AccountsPage = lazy(() => import('@/pages/Accounts'))
const RegisterTaskPage = lazy(() => import('@/pages/RegisterTaskPage'))
const CustomEmailRecheckPage = lazy(() => import('@/pages/CustomEmailRecheckPage'))
const TaskHistoryPage = lazy(() => import('@/pages/TaskHistory'))
const ProxiesPage = lazy(() => import('@/pages/Proxies'))
const PhonePoolPage = lazy(() => import('@/pages/PhonePool'))
const BaxiGptCdkPoolPage = lazy(() => import('@/pages/BaxiGptCdkPool'))
const DeliveryCardsPage = lazy(() => import('@/pages/DeliveryCards'))
const CodexUsagePage = lazy(() => import('@/pages/CodexUsagePage'))
const SettingsPage = lazy(() => import('@/pages/Settings'))

export const APP_ROUTES: AppRouteItem[] = [
  { path: '/', key: '/', label: '仪表盘', loader: DashboardPage },
  { path: '/chatgpt', key: '/chatgpt', label: 'ChatGPT', loader: AccountsPage },
  { path: '/accounts', key: '/chatgpt', loader: AccountsPage },
  { path: '/codex-usage', key: '/codex-usage', label: 'Codex额度监控', loader: CodexUsagePage },
  { path: '/register', key: '/register', loader: RegisterTaskPage },
  { path: '/custom-email-recheck', key: '/custom-email-recheck', label: '邮箱登录测活', loader: CustomEmailRecheckPage },
  { path: '/history', key: '/history', label: '任务历史', loader: TaskHistoryPage },
  { path: '/proxies', key: '/proxies', label: '代理管理', loader: ProxiesPage },
  { path: '/phone-pool', key: '/phone-pool', label: '手机号池', loader: PhonePoolPage },
  { path: '/baxigpt-cdk-pool', key: '/baxigpt-cdk-pool', label: 'iDEAL 卡密池', loader: BaxiGptCdkPoolPage },
  { path: '/delivery-cards', key: '/delivery-cards', label: '交付卡密', loader: DeliveryCardsPage },
  { path: '/settings', key: '/settings', label: '全局配置', loader: SettingsPage },
]
