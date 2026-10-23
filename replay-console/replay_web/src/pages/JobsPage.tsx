import { useMemo } from 'react'
import {
  App, Button, Card, Empty, Progress, Segmented, Space, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { JobSummary } from '../types'
import { RunStatusTag } from '../components/CardChips'
import { formatDate, scopeLabel } from '../lib/format'

export default function JobsPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const status = searchParams.get('status') || 'all'
  const page = Number(searchParams.get('page') || 1)
  const pageSize = Number(searchParams.get('pageSize') || 20)

  const jobs = useQuery<{ total: number; items: JobSummary[]; page: number; pageSize: number }>({
    queryKey: ['jobs', status, page, pageSize],
    queryFn: () => api.get(`/runs?status=${status === 'all' ? '' : status}&page=${page}&pageSize=${pageSize}`),
    refetchInterval: (query) => {
      const items = query.state.data?.items || []
      return items.some((item) => item.status === 'running' || item.status === 'queued') ? 1500 : false
    },
  })

  const columns: ColumnsType<JobSummary> = useMemo(() => [
    {
      title: '任务',
      dataIndex: 'id',
      width: 90,
      render: (id: number) => <span className="job-id mono">#{id}</span>,
    },
    {
      title: '创建时间',
      dataIndex: 'createdAt',
      width: 130,
      render: (value: string) => <span className="muted">{formatDate(value)}</span>,
    },
    {
      title: '范围',
      dataIndex: 'scope',
      width: 150,
      render: (scope: string, job) => (
        <div className="job-scope">
          <Tag className="scope-tag">{scopeLabel(scope)}</Tag>
          <span className="muted">{job.handCount} 手 · {job.totalDecisions} 决策点</span>
        </div>
      ),
    },
    {
      title: '版本',
      dataIndex: 'versions',
      width: 220,
      render: (versions: string[], job) => (
        <div className="job-versions">
          <div>{versions.join(' + ') || '—'}</div>
          <span className="muted">×{job.repeat} 次 · {job.totalUnits} 次调用</span>
        </div>
      ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 210,
      render: (_: unknown, job) => (
        <div className="job-progress">
          <Progress
            percent={job.progress}
            size="small"
            status={job.status === 'failed' ? 'exception' : job.status === 'succeeded' ? 'success' : 'active'}
            strokeColor={job.status === 'canceled' ? '#94a3b8' : undefined}
          />
          <div className="job-progress-meta">
            {job.doneUnits}/{job.totalUnits}
            {job.failedUnits ? <span className="down"> · 失败 {job.failedUnits}</span> : null}
            {job.skippedUnits ? <span className="muted"> · 跳过 {job.skippedUnits}</span> : null}
          </div>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value) => <RunStatusTag status={value} />,
    },
    {
      title: '备注',
      dataIndex: 'note',
      ellipsis: true,
      render: (note: string, job) => (
        <Tooltip title={job.errorMsg || note || ''}>
          <span className={job.errorMsg ? 'down' : 'muted'}>{job.errorMsg || note || '—'}</span>
        </Tooltip>
      ),
    },
    {
      title: '操作',
      width: 120,
      render: (_: unknown, job) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={(event) => { event.stopPropagation(); navigate(`/jobs/${job.id}`) }}>
            详情
          </Button>
          {job.doneUnits > 0 ? (
            <Button type="link" size="small" onClick={(event) => { event.stopPropagation(); navigate(`/jobs/${job.id}/compare`) }}>
              对比
            </Button>
          ) : null}
        </Space>
      ),
    },
  ], [navigate])

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="eyebrow">REPLAY JOBS</div>
          <h1>复跑任务</h1>
          <p className="page-sub">DB 队列 + 进程内 worker · 逐 unit 落库 · 可取消、可重试、重启可恢复。</p>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => jobs.refetch()} loading={jobs.isFetching}>刷新</Button>
          <Button type="primary" icon={<ThunderboltOutlined />} onClick={() => navigate('/hands')}>新建重跑</Button>
        </Space>
      </div>

      <Card className="table-card" bordered={false}>
        <div className="jobs-toolbar">
          <Segmented
            value={status}
            onChange={(value) => {
              const next = new URLSearchParams(searchParams)
              if (value === 'all') next.delete('status')
              else next.set('status', String(value))
              next.delete('page')
              setSearchParams(next, { replace: true })
            }}
            options={[
              { label: '全部', value: 'all' },
              { label: '运行中', value: 'running' },
              { label: '排队', value: 'queued' },
              { label: '完成', value: 'succeeded' },
              { label: '部分', value: 'partial' },
              { label: '失败', value: 'failed' },
              { label: '已取消', value: 'canceled' },
            ]}
          />
          <span className="muted">共 {jobs.data?.total ?? 0} 个任务</span>
        </div>
        {jobs.error ? <Empty description={errorMessage(jobs.error)} /> : (
          <Table<JobSummary>
            rowKey="id"
            dataSource={jobs.data?.items || []}
            columns={columns}
            loading={jobs.isLoading}
            size="middle"
            scroll={{ x: 1100 }}
            pagination={{
              current: jobs.data?.page || page,
              pageSize: jobs.data?.pageSize || pageSize,
              total: jobs.data?.total || 0,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50],
              onChange: (nextPage, nextSize) => {
                const next = new URLSearchParams(searchParams)
                next.set('page', String(nextPage))
                next.set('pageSize', String(nextSize))
                setSearchParams(next, { replace: true })
              },
            }}
            onRow={(job) => ({
              onClick: () => navigate(`/jobs/${job.id}`),
              className: 'hand-row',
            })}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有复跑任务" /> }}
          />
        )}
      </Card>
    </div>
  )
}
