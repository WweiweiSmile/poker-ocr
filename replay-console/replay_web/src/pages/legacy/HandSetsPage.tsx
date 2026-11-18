import { Alert, Button, Card, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../../api'
import type { HandSet, HandSetList } from '../../types'
import { formatDate } from '../../lib/format'
import LegacyBanner from '../../components/LegacyBanner'

const PURPOSE: Record<string, string> = {
  regression: '回归', exploration: '探索', golden: '金标准', coverage: '覆盖',
}

export default function HandSetsPage() {
  const navigate = useNavigate()

  const sets = useQuery<HandSetList>({
    queryKey: ['hand-sets'],
    queryFn: () => api.get('/hand-sets'),
  })

  const columns: ColumnsType<HandSet> = [
    {
      title: '手牌集', dataIndex: 'name', key: 'name',
      render: (_: string, row) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => navigate(`/legacy/sets/${row.id}`)}>
            {row.name}
          </Button>
          <div className="muted cell-sub">{row.description || '—'}</div>
        </div>
      ),
    },
    {
      title: '用途', dataIndex: 'purpose', width: 90,
      render: (value: string) => <Tag>{PURPOSE[value] || value}</Tag>,
    },
    {
      title: '规模', width: 150,
      render: (_: unknown, row) => (
        <span className="mono">{row.handCount} 手 · {row.coverage?.decisions || 0} 决策</span>
      ),
    },
    {
      title: 'Prompt 覆盖', width: 130,
      render: (_: unknown, row) => (
        <span className="mono">{((row.coverage?.promptCoverage || 0) * 100).toFixed(0)}%</span>
      ),
    },
    {
      title: 'Revision', dataIndex: 'revision', width: 80,
      render: (value: number) => <span className="mono">v{value}</span>,
    },
    {
      title: '更新', dataIndex: 'updatedAt', width: 140,
      render: (value: string | null) => <span className="muted">{formatDate(value, false)}</span>,
    },
  ]

  return (
    <div className="hand-sets-page">
      <div className="section-head">
        <div>
          <h2>手牌集</h2>
          <p className="muted">
            旧链路按「手」挑的回归集；实验创建时冻结 revision。新链路的取样单位是决策点，见「案例集」。
          </p>
        </div>
      </div>

      <LegacyBanner scope="手牌集" />

      {sets.error ? <Alert type="error" showIcon message={errorMessage(sets.error)} /> : null}

      <Card bordered={false}>
        <Table<HandSet>
          rowKey="id"
          size="middle"
          loading={sets.isLoading}
          columns={columns}
          dataSource={sets.data?.items || []}
          pagination={false}
          locale={{ emptyText: '还没有旧手牌集。新链路请用「案例集」。' }}
        />
      </Card>
    </div>
  )
}
