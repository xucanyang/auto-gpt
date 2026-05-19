import { Suspense, useEffect, useState } from 'react'
import { Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { App as AntdApp, ConfigProvider, Layout, Menu, Button, Spin } from 'antd'
import {
  DashboardOutlined,
  UserOutlined,
  GlobalOutlined,
  HistoryOutlined,
  SettingOutlined,
  SunOutlined,
  MoonOutlined,
  LogoutOutlined,
  TeamOutlined,
  MobileOutlined,
  RocketOutlined,
} from '@ant-design/icons'
import zhCN from 'antd/locale/zh_CN'
import { APP_ROUTES } from './router'
import { darkTheme, lightTheme } from '@/theme'
import { clearToken, getToken } from '@/lib/utils'

const { Sider, Content } = Layout

function RouteFallback() {
  return (
    <div style={{ minHeight: '40vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <Spin size="large" />
    </div>
  )
}

function ProtectedLayout() {
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)
  const [hasPassword, setHasPassword] = useState(false)

  useEffect(() => {
    fetch('/api/auth/status')
      .then((r) => r.json())
      .then((s) => {
        const token = getToken()
        setHasPassword(Boolean(s?.has_password))
        if (s?.has_password && !token) {
          navigate('/login', { replace: true })
        } else {
          setReady(true)
        }
      })
      .catch(() => setReady(true))
  }, [navigate])

  if (!ready) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    )
  }

  return <AppContent hasPassword={hasPassword} />
}

function AppContent({ hasPassword }: { hasPassword: boolean }) {
  const [themeMode, setThemeMode] = useState<'dark' | 'light'>(() =>
    (localStorage.getItem('theme') as 'dark' | 'light') || 'dark',
  )
  const [collapsed, setCollapsed] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    document.documentElement.classList.toggle('light', themeMode === 'light')
    document.documentElement.style.setProperty(
      '--sider-trigger-border',
      themeMode === 'light' ? 'rgba(0,0,0,0.1)' : 'rgba(255,255,255,0.15)',
    )
    localStorage.setItem('theme', themeMode)
  }, [themeMode])

  const isLight = themeMode === 'light'
  const currentTheme = isLight ? lightTheme : darkTheme

  const getSelectedKey = () => {
    const path = location.pathname
    if (path === '/') return ['/']
    if (path.startsWith('/chatgpt') || path.startsWith('/accounts')) return ['/chatgpt']
    if (path.startsWith('/teams')) return ['/teams']
    if (path === '/pipeline') return ['/pipeline']
    if (path === '/gopay-otp') return ['/gopay-otp']
    if (path === '/history') return ['/history']
    if (path === '/proxies') return ['/proxies']
    if (path === '/settings') return ['/settings']
    return ['/']
  }

  const menuItems = [
    { key: '/', icon: <DashboardOutlined />, label: '仪表盘' },
    { key: '/chatgpt', icon: <UserOutlined />, label: 'ChatGPT' },
    { key: '/teams', icon: <TeamOutlined />, label: 'Team' },
    { key: '/gopay-otp', icon: <MobileOutlined />, label: 'GoPay OTP' },
    { key: '/pipeline', icon: <RocketOutlined />, label: '自动流水线' },
    { key: '/history', icon: <HistoryOutlined />, label: '任务历史' },
    { key: '/proxies', icon: <GlobalOutlined />, label: '代理管理' },
    { key: '/settings', icon: <SettingOutlined />, label: '全局配置' },
  ]

  return (
    <ConfigProvider theme={currentTheme} locale={zhCN}>
      <AntdApp>
        <Layout style={{ minHeight: '100vh' }}>
          <Sider
            collapsible
            collapsed={collapsed}
            onCollapse={setCollapsed}
            style={{
              background: currentTheme.token?.colorBgContainer,
              borderRight: `1px solid ${currentTheme.token?.colorBorder}`,
            }}
            width={220}
          >
            <div
              style={{
                height: 64,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderBottom: `1px solid ${currentTheme.token?.colorBorder}`,
              }}
            >
              <DashboardOutlined style={{ fontSize: 20, color: currentTheme.token?.colorPrimary }} />
              {!collapsed && (
                <span
                  style={{
                    marginLeft: 8,
                    fontWeight: 600,
                    fontSize: 14,
                    color: currentTheme.token?.colorText,
                  }}
                >
                  Auto ChatGPT
                </span>
              )}
            </div>
            <Menu
              mode="inline"
              selectedKeys={getSelectedKey()}
              defaultOpenKeys={['/accounts']}
              items={menuItems}
              onClick={({ key }) => navigate(key)}
              style={{
                borderRight: 0,
                background: 'transparent',
              }}
            />
            <div
              style={{
                position: 'absolute',
                bottom: 56,
                left: 0,
                right: 0,
                padding: '0 16px',
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
              }}
            >
              <Button
                block
                icon={isLight ? <SunOutlined /> : <MoonOutlined />}
                onClick={() => setThemeMode(isLight ? 'dark' : 'light')}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: collapsed ? 'center' : 'space-between',
                }}
              >
                {!collapsed && (isLight ? '亮色模式' : '暗色模式')}
              </Button>
              {hasPassword && (
                <Button
                  block
                  danger
                  icon={<LogoutOutlined />}
                  onClick={() => {
                    clearToken()
                    navigate('/login')
                  }}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: collapsed ? 'center' : 'space-between',
                  }}
                >
                  {!collapsed && '退出登录'}
                </Button>
              )}
            </div>
          </Sider>
          <Content
            style={{
              padding: 24,
              overflow: 'hidden',
              background: currentTheme.token?.colorBgLayout,
            }}
          >
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                {APP_ROUTES.map((route) => (
                  <Route key={route.path} path={route.path} element={<route.loader />} />
                ))}
              </Routes>
            </Suspense>
          </Content>
        </Layout>
      </AntdApp>
    </ConfigProvider>
  )
}

export function AppShell() {
  return (
    <Routes>
      <Route path="/*" element={<ProtectedLayout />} />
    </Routes>
  )
}
