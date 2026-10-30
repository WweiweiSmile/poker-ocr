import { App as AntdApp, ConfigProvider, Tag, Tooltip } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import {
  DatabaseOutlined,
  DeploymentUnitOutlined,
  ExperimentOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { NavLink, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { BrowserRouter } from 'react-router-dom'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import { api } from './api'
import type { Health, Meta } from './types'
import { shortHash } from './lib/format'
import HandsPage from './pages/HandsPage'
import HandDetailPage from './pages/HandDetailPage'
import JobsPage from './pages/JobsPage'
import JobDetailPage from './pages/JobDetailPage'
import JobComparisonPage from './pages/JobComparisonPage'
import VersionsPage from './pages/VersionsPage'
import SettingsPage from './pages/SettingsPage'
import ExperimentsPage from './pages/ExperimentsPage'
import ExperimentDetailPage from './pages/ExperimentDetailPage'

dayjs.locale('zh-cn')

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

const NAV = [
  { to: '/hands', label: '手牌库', icon: <DatabaseOutlined />, hint: '筛选、收藏、逐街对比' },
  { to: '/experiments', label: 'A/B 实验', icon: <ExperimentOutlined />, hint: '离线冻结数据集、dev/holdout' },
  { to: '/jobs', label: '复跑任务', icon: <ThunderboltOutlined />, hint: '进度、取消、重试' },
  { to: '/versions', label: '版本管理', icon: <DeploymentUnitOutlined />, hint: '不可变 AI 配置快照' },
  { to: '/settings', label: '设置与健康', icon: <SettingOutlined />, hint: '连接、队列、上限' },
]

function StatusDot({ ok, label, detail }: { ok: boolean; label: string; detail?: string }) {
  return (
    <Tooltip title={detail || label}>
      <span className={`health-chip ${ok ? 'ok' : 'bad'}`}>
        <span className="health-dot" />
        {label}
      </span>
    </Tooltip>
  )
}

function Shell() {
  const location = useLocation()
  const meta = useQuery<Meta>({
    queryKey: ['meta'],
    queryFn: () => api.get('/meta'),
    refetchInterval: 30_000,
  })
  const health = useQuery<Health>({
    queryKey: ['health'],
    queryFn: () => api.get('/health'),
    refetchInterval: 15_000,
  })
  const h = health.data || meta.data?.health
  const active = NAV.find((item) => location.pathname.startsWith(item.to)) || NAV[0]
  const engine = meta.data?.current.engine

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <span>♠</span><span>♥</span>
          </div>
          <div>
            <div className="brand-name">Poker Replay</div>
            <div className="brand-sub">HAND REVIEW CONSOLE</div>
          </div>
        </div>

        <nav className="nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-copy">
                <span>{item.label}</span>
                <span className="nav-hint">{item.hint}</span>
              </span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-stats">
          <div>
            <div className="side-stat-value">{meta.data?.handCount ?? '—'}</div>
            <div className="side-stat-label">可复盘手牌</div>
          </div>
          <div>
            <div className="side-stat-value">{meta.data?.favoriteCount ?? '—'}</div>
            <div className="side-stat-label">已收藏</div>
          </div>
          <div>
            <div className="side-stat-value">{meta.data?.versionCount ?? '—'}</div>
            <div className="side-stat-label">AI 版本</div>
          </div>
        </div>

        <div className="sidebar-foot">
          <div className="engine-line">
            <span className={`engine-dot ${engine?.dirty ? 'dirty' : ''}`} />
            <span className="mono">{engine?.commit ? shortHash(engine.commit) : 'no git'}</span>
            {engine?.dirty ? <Tag className="tiny-tag">dirty</Tag> : null}
          </div>
          <div className="engine-meta">{meta.data?.current.adviceVersion || '—'} · {meta.data?.current.reconstructVersion || '—'}</div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="breadcrumb">
            <span className="crumb-root">Console</span>
            <span className="crumb-sep">/</span>
            <span className="crumb-current">{active.label}</span>
          </div>
          <div className="topbar-right">
            <StatusDot ok={!!h?.db.ok} label="MySQL" detail={h ? `${h.db.host}/${h.db.name} · ${h.db.detail}` : '检查中'} />
            <StatusDot ok={!!h?.aiKey.ok} label="AI Key" detail={h?.aiKey.detail} />
            <StatusDot
              ok={h?.worker.enabled === false ? true : !!h?.worker.alive}
              label="Worker"
              detail={h?.worker.enabled === false ? 'worker 已禁用（独立进程模式）' : h?.worker.currentJobId ? `正在跑任务 #${h.worker.currentJobId}` : '空闲'}
            />
            <span className="queue-chip">队列 {h?.queueDepth ?? '—'}</span>
          </div>
        </header>
        <div className="page">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#0f766e',
          colorInfo: '#0f766e',
          colorSuccess: '#15803d',
          colorWarning: '#b45309',
          colorError: '#b91c1c',
          borderRadius: 10,
          borderRadiusLG: 14,
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", sans-serif',
          colorBgLayout: '#f3f5f4',
          colorText: '#16211f',
          colorTextSecondary: '#5f6f6b',
        },
        components: {
          Table: { headerBg: '#f7f9f8', headerColor: '#53615e', rowHoverBg: '#f5faf8' },
          Card: { paddingLG: 18 },
          Button: { controlHeight: 34 },
          Input: { controlHeight: 34 },
          Select: { controlHeight: 34 },
        },
      }}
    >
      <AntdApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <Routes>
              <Route element={<Shell />}>
                <Route path="/" element={<Navigate to="/hands" replace />} />
                <Route path="/hands" element={<HandsPage />} />
                <Route path="/hands/:handId" element={<HandDetailPage />} />
                <Route path="/experiments" element={<ExperimentsPage />} />
                <Route path="/experiments/:experimentId" element={<ExperimentDetailPage />} />
                <Route path="/jobs" element={<JobsPage />} />
                <Route path="/jobs/:jobId" element={<JobDetailPage />} />
                <Route path="/jobs/:jobId/compare" element={<JobComparisonPage />} />
                <Route path="/versions" element={<VersionsPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="*" element={<Navigate to="/hands" replace />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </QueryClientProvider>
      </AntdApp>
    </ConfigProvider>
  )
}
