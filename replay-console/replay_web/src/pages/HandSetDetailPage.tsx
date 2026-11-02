import { useMemo } from 'react'
import {
  Alert, App, Button, Card, Empty, Space, Statistic, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowLeftOutlined, ExperimentOutlined, MinusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { HandSet, HandSetItem } from '../types'
import { QualityBadge } from '../components/CardChips'
import { formatAmount, formatDate } from '../lib/format'

const ROLE: Record<string, string> = {
  golden: '金标准', regression: '回归', exploration: '探索', negative: '对照',
}

export default function HandSetDetailPage() {
  const { setId } = useParams()
  const id = Number(setId)
  const navigate = useNavigate()
  const { message } = App.useApp()
  const queryClient = useQueryClient()

  const set = useQuery<HandSet>({
    queryKey: ['hand-set', id],
    queryFn: () => api.get(`/hand-sets/${id}`),
    enabled: Number.isFinite(id),
  })
  const remove = useMutation({
    mutationFn: (handIds: number[]) => api.delete<HandSet>(`/hand-sets/${id}/items`, { handIds }),
    onSuccess: () => {
      message.success('已从手牌集移除')
      queryClient.invalidateQueries({ queryKey: ['hand-set', id] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const freeze = useMutation({
    mutationFn: () => api.post<HandSet>(`/hand-sets/${id}/freeze`),
    onSuccess: () => {
      message.success('已冻结当前 revision')
      queryClient.invalidateQueries({ queryKey: ['hand-set', id] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<HandSetItem> = [
    {
      title: '手牌', key: 'hand',
      render: (_: unknown, row) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => navigate(`/hands/${row.handId}`)}>
            {row.hand?.title || `#${row.handId}`}
          </Button>
          <div className="muted cell-sub mono">
            {row.hand?.heroPosition || '—'} · {row.hand?.heroCards || '—'} · {row.hand?.board || '—'}
          </div>
        </div>
      ),
    },
    {
      title: '角色', dataIndex: 'role', width: 90,
      render: (value: string) => <Tag>{ROLE[value] || value}</Tag>,
    },
    {
      title: 'P', dataIndex: 'priority', width: 60,
      render: (value: number) => value === 0 ? <Tag color="red">P0</Tag> : <span className="mono">P{value}</span>,
    },
    {
      title: '质量', dataIndex: 'quality', width: 100,
      render: (value: string) => value === 'missing'
        ? <Tag color="red">missing</Tag>
        : <QualityBadge quality={value as never} />,
    },
    {
      title: '深挖分', key: 'score', width: 80,
      render: (_: unknown, row) => <span className="mono">{row.hand?.reviewScore ?? 0}</span>,
    },
    {
      title: '结果', key: 'result', width: 110,
      render: (_: unknown, row) => (
        <span className={row.hand?.resultAmount && row.hand.resultAmount > 0 ? 'money up' : row.hand?.resultAmount && row.hand.resultAmount < 0 ? 'money down' : ''}>
          {row.hand?.resultAmount !== null && row.hand?.resultAmount !== undefined
            ? `${formatAmount(row.hand.resultAmount)}BB` : row.hand?.result || '—'}
        </span>
      ),
    },
    {
      title: '断言', key: 'expect', width: 90,
      render: (_: unknown, row) => {
        const n = Array.isArray((row.expect as { assertions?: unknown[] })?.assertions)
          ? (row.expect as { assertions: unknown[] }).assertions.length : 0
        return n ? <Tag color="blue">{n} 条</Tag> : <span className="muted">—</span>
      },
    },
    {
      title: '', key: 'action', width: 70,
      render: (_: unknown, row) => (
        <Button size="small" danger icon={<MinusOutlined />} onClick={() => remove.mutate([row.handId])} />
      ),
    },
  ]

  const coverage = set.data?.coverage
  const totals = useMemo(() => ({
    p0: (set.data?.items || []).filter((item) => item.priority === 0).length,
    stale: (set.data?.items || []).filter((item) => item.stale).length,
    approx: (set.data?.items || []).filter((item) => item.quality === 'approx').length,
  }), [set.data])

  if (set.error) return <Alert type="error" showIcon message={errorMessage(set.error)} />
  const data = set.data
  if (!data) return <Card loading bordered={false} />

  return (
    <div className="hand-set-detail">
      <div className="detail-back">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/sets')}>手牌集</Button>
        <span className="mono muted">#{data.id} · v{data.revision}</span>
        {data.frozenAt ? <Tag color="green">已冻结</Tag> : <Tag>未冻结</Tag>}
      </div>

      <Card className="hand-hero" bordered={false}>
        <div className="hand-hero-main">
          <div>
            <div className="hand-hero-title">{data.name}</div>
            <div className="hand-hero-sub">
              <span>{data.description || '没有说明'}</span>
              <span>{data.handCount} 手</span>
              <span>更新 {formatDate(data.updatedAt, false)}</span>
            </div>
          </div>
          <Space>
            <Button onClick={() => freeze.mutate()} loading={freeze.isPending}>冻结 v{data.revision}</Button>
            <Button type="primary" icon={<ExperimentOutlined />}
              onClick={() => navigate(`/experiments?handSetId=${data.id}`)}>
              用这个集合跑 A/B
            </Button>
          </Space>
        </div>
      </Card>

      <div className="set-stats">
        <Card bordered={false}><Statistic title="决策点" value={coverage?.decisions || 0} /></Card>
        <Card bordered={false}><Statistic title="Prompt 覆盖" value={((coverage?.promptCoverage || 0) * 100)} suffix="%" /></Card>
        <Card bordered={false}><Statistic title="P0" value={totals.p0} /></Card>
        <Card bordered={false}><Statistic title="stale / approx" value={`${totals.stale} / ${totals.approx}`} /></Card>
      </div>

      <Card bordered={false}>
        <Table<HandSetItem>
          rowKey="handId"
          size="middle"
          columns={columns}
          dataSource={data.items}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有手牌。去手牌库勾选后「加入手牌集」。" /> }}
        />
      </Card>
    </div>
  )
}
