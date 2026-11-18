import { useMemo } from 'react'
import { Alert, Button, Card, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../../api'
import type { Experiment, Version } from '../../types'
import { formatDate } from '../../lib/format'
import LegacyBanner from '../../components/LegacyBanner'

const STATUS_COLORS: Record<string, string> = {
  draft: 'default', queued: 'blue', running: 'processing',
  done: 'green', failed: 'red', archived: 'default',
}
const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '排队', running: '运行中',
  done: '完成', failed: '失败', archived: '已归档',
}

export default function ExperimentsPage() {
  const navigate = useNavigate()

  const experiments = useQuery<Experiment[]>({
    queryKey: ['experiments'],
    queryFn: () => api.get('/experiments'),
  })
  const versions = useQuery<{ items: Version[] }>({
    queryKey: ['versions', 'all'],
    queryFn: () => api.get('/versions?includeArchived=1'),
  })
  const versionMap = useMemo(() => {
    const map: Record<number, Version> = {}
    for (const version of versions.data?.items || []) map[version.id] = version
    return map
  }, [versions.data])

  const columns: ColumnsType<Experiment> = [
    {
      title: '实验', dataIndex: 'name', key: 'name',
      render: (_: string, row: Experiment) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => navigate(`/legacy/experiments/${row.id}`)}>
            {row.name}
          </Button>
          {row.hypothesis ? <div className="muted cell-sub">{row.hypothesis}</div> : null}
        </div>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (status: string) => (
        <Tag color={STATUS_COLORS[status] || 'default'}>{STATUS_LABELS[status] || status}</Tag>
      ),
    },
    {
      title: '版本', key: 'versions', width: 260,
      render: (_: unknown, row: Experiment) => (
        <div className="version-pair">
          <div>A · {versionMap[row.baselineVersionId]?.name || `#${row.baselineVersionId}`}</div>
          <div>B · {versionMap[row.variantVersionId]?.name || `#${row.variantVersionId}`}</div>
        </div>
      ),
    },
    {
      title: '手牌集', key: 'hands', width: 160,
      render: (_: unknown, row: Experiment) => (
        <span className="mono">
          {row.hands.total} 手 · dev {row.hands.dev} / holdout {row.hands.holdout}
        </span>
      ),
    },
    {
      title: '任务', dataIndex: 'jobId', key: 'jobId', width: 90,
      render: (jobId: number | null) => jobId
        ? <Button type="link" size="small" onClick={() => navigate(`/jobs/${jobId}`)}>#{jobId}</Button>
        : <span className="muted">未跑</span>,
    },
    {
      title: '创建', dataIndex: 'createdAt', key: 'createdAt', width: 150,
      render: (value: string | null) => <span className="muted">{formatDate(value, false)}</span>,
    },
    {
      title: '', key: 'action', width: 100,
      render: (_: unknown, row: Experiment) => (
        <Button size="small" icon={<ThunderboltOutlined />} onClick={() => navigate(`/legacy/experiments/${row.id}`)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <div className="experiments-page">
      <div className="section-head">
        <div>
          <h2>A/B 实验</h2>
          <p className="muted">
            冻结手牌集 → baseline/variant 各跑一遍 → 按 dev/holdout 看一致率与复盘结论。
          </p>
        </div>
      </div>

      <LegacyBanner scope="A/B 实验" />

      {experiments.error ? (
        <Alert type="error" showIcon message={errorMessage(experiments.error)} />
      ) : null}

      <Card bordered={false}>
        <Table<Experiment>
          rowKey="id"
          size="middle"
          loading={experiments.isLoading}
          columns={columns}
          dataSource={experiments.data || []}
          pagination={false}
          locale={{ emptyText: '还没有旧实验。新链路的评测请走「案例集 → 运行」。' }}
        />
      </Card>
    </div>
  )
}
