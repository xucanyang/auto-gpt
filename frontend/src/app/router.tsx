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
const TeamsPage = lazy(() => import('@/pages/Teams'))
const GoPayOtpPage = lazy(() => import('@/pages/GoPayOtpAdapter'))
const PipelinePage = lazy(() => import('@/pages/Pipeline'))
const TaskHistoryPage = lazy(() => import('@/pages/TaskHistory'))
const ProxiesPage = lazy(() => import('@/pages/Proxies'))
const SettingsPage = lazy(() => import('@/pages/Settings'))

export const APP_ROUTES: AppRouteItem[] = [
  { path: '/', key: '/', label: '仪表盘', loader: DashboardPage },
  { path: '/chatgpt', key: '/chatgpt', label: 'ChatGPT', loader: AccountsPage },
  { path: '/accounts', key: '/chatgpt', loader: AccountsPage },
  { path: '/register', key: '/register', loader: RegisterTaskPage },
  { path: '/custom-email-recheck', key: '/custom-email-recheck', label: '自定义邮箱测活', loader: CustomEmailRecheckPage },
  { path: '/teams', key: '/teams', label: 'Team', loader: TeamsPage },
  { path: '/gopay-otp', key: '/gopay-otp', label: 'GoPay OTP', loader: GoPayOtpPage },
  { path: '/pipeline', key: '/pipeline', label: '自动流水线', loader: PipelinePage },
  { path: '/history', key: '/history', label: '任务历史', loader: TaskHistoryPage },
  { path: '/proxies', key: '/proxies', label: '代理管理', loader: ProxiesPage },
  { path: '/settings', key: '/settings', label: '全局配置', loader: SettingsPage },
]
