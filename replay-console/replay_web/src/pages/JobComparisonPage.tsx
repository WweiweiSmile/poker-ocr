import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Collapse, Empty, Popover, Progress, Segmented,
  Space, Tag, Table, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowLeftOutlined, DownloadOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, downloadExport, errorMessage } from '../api'
import type { ComparisonDecision, HandComparison, JobResults, VersionBlock } from '../types'
import { BoardCards, BucketTag, HandCards, QualityBadge } from '../components/CardChips'
import { formatAmount, formatDate, scopeLabel } from '../lib/format'

const FILTERS = [
  { label: '全部', value: '' },
  { label: '动作不同', value: 'diff' },
  { label: '尺度差', value: 'size' },
  { label: '分歧', value: 'diverged' },
  { label: '违规', value: 'violation' },
  { label: '失败/兜底', value: 'error' },
]

function optionLabel(block?: VersionBlock): string {
  if (!block || !block.summary.optionId) return '—'
  const s = block.summary
  const labels: Record<string, string> = {
    fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注', allin: '全下',
  }
  const kind = s.optionKind || s.optionId || ''
  const base = labels[kind] || kind
  return s.amount !== null && s.amount !== undefined ? `${base} ${formatAmount(s.amount)}` : base
}

function actualText(decision: ComparisonDecision): string {
  const action = decision.actual.action
  if (!action) return '—'
  const labels: Record<string, string> = {
    fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注', allin: '全下',
  }
  const base = labels[action] || action
  return decision.actual.amountBb !== null && decision.actual.amountBb !== undefined
    ? `${base} ${formatAmount(decision.actual.amountBb)}` : base
}

function ComparisonCell({ block }: { block?: VersionBlock }) {
  if (!block) return <span className="muted">—</span>
  const s = block.summary
  if (!block.results.length) {
    return <span className="muted">未跑</span>
  }
  const reason = s.source === 'error'
    ? (block.results[0]?.error || '调用失败')
    : (block.results.find((r) => r.optionId === s.optionId)?.reason || block.results[0]?.reason || '（无理由）')
  const detail = (
    <div className="cmp-popover">
      <div className="cmp-popover-head">
        <strong>{block.name}</strong>
        <span className="muted">{s.n}/{s.total} 次模型返回</span>
      </div>
      <div className="cmp-reason">{reason}</div>
      {Object.keys(s.distribution || {}).length > 1 ? (
        <div className="cmp-dist">
          {Object.entries(s.distribution).map(([option, count]) => (
            <Tag key={option} className="mini-tag">{option} ×{count}</Tag>
          ))}
        </div>
      ) : null}
      {s.violationTags?.length ? (
        <div className="down">违规：{s.violationTags.join('、')}</div>
      ) : null}
      {block.results.length > 1 ? (
        <div className="cmp-repeat-list">
          {block.results.map((result) => (
            <div key={result.repeat} className="cmp-repeat">
              <span className="muted">#{result.repeat + 1}</span>
              <span>{result.optionId || '—'}</span>
              <span className="muted">{result.durationMs ? `${result.durationMs}ms` : ''}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
  return (
    <Popover content={detail} trigger="click" placement="bottom" title={null}>
      <button className={`cmp-cell kind-${s.optionKind || 'none'}`}>
        <span className={`option-pill kind-${s.optionKind || 'none'}`}>{optionLabel(block)}</span>
        <span className="cmp-cell-meta">
          {s.total ? `${s.n}/${s.total}` : ''}
          {s.source !== 'model' ? <Tag color={s.source === 'error' ? 'red' : 'gold'} className="mini-tag">{s.source === 'error' ? '失败' : '兜底'}</Tag> : null}
        </span>
        <BucketTag bucket={s.bucket} />
        {s.violationTags?.length ? <Tag color="red" className="mini-tag">违规</Tag> : null}
      </button>
    </Popover>
  )
}

export default function JobComparisonPage() {
  const { jobId } = useParams()
  const id = Number(jobId)
  const navigate = useNavigate()
  const { message } = App.useApp()
  const [only, setOnly] = useState('')
  const [page, setPage] = useState(1)

  const data = useQuery<JobResults>({
    queryKey: ['job-results', id, only, page],
    queryFn: () => api.get(`/runs/${id}/results?only=${only}&page=${page}&pageSize=50`),
    enabled: Number.isFinite(id),
  })

  const stats = useMemo(() => {
    const hands = data.data?.hands || []
    const decisions = hands.flatMap((hand) => hand.decisions)
    return {
      hands: data.data?.totalHands ?? hands.length,
      decisions: data.data?.totalDecisions ?? decisions.length,
      diverged: decisions.filter((d) => d.diverged).length,
      violated: decisions.filter((d) => d.versions.some((v) => v.summary.violationTags?.length)).length,
      errors: decisions.filter((d) => d.versions.some((v) => v.summary.source === 'error' || v.summary.source === 'fallback')).length,
    }
  }, [data.data])

  const exportData = async (format: 'csv' | 'markdown') => {
    try {
      await downloadExport({ jobId: id, format, only })
    } catch (error) {
      message.error(errorMessage(error))
    }
  }

  const columns: ColumnsType<ComparisonDecision> = useMemo(() => {
    const versions = data.data?.versions || []
    return [
      {
        title: '决策点',
        width: 100,
        fixed: 'left',
        render: (_: unknown, row) => (
          <div className="cmp-decision">
            <span className="decision-anchor">D{row.decision.seq + 1}</span>
            <span className="muted">{row.decision.street}</span>
            <QualityBadge quality={row.decision.quality} showText={false} />
          </div>
        ),
      },
      {
        title: '底池 / 需跟',
        width: 110,
        render: (_: unknown, row) => (
          <div className="cmp-spot">
            <span>{formatAmount(row.decision.pot)}</span>
            <span className="muted">/ {formatAmount(row.decision.callAmount ?? row.decision.toCall)}</span>
          </div>
        ),
      },
      {
        title: '实际',
        width: 110,
        render: (_: unknown, row) => <span className="actual-pill small">{actualText(row)}</span>,
      },
      ...versions.map((version) => ({
        title: (
          <div className="cmp-version-head">
            <span>{version.name}</span>
            <span className="muted mono">{version.model}</span>
          </div>
        ),
        key: String(version.id),
        width: 190,
        render: (_: unknown, row: ComparisonDecision) => (
          <ComparisonCell block={row.versions.find((v) => v.versionId === version.id)} />
        ),
      })),
      {
        title: '',
        width: 60,
        render: (_: unknown, row) => row.diverged ? <Tag color="orange" className="mini-tag">分歧</Tag> : null,
      },
    ]
  }, [data.data])

  if (data.error) {
    return <Alert type="error" showIcon message="对比加载失败" description={errorMessage(data.error)} />
  }
  const job = data.data?.job

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate(`/jobs/${id}`)}>任务 #{id}</Button>
          <div className="eyebrow" style={{ marginTop: 12 }}>MULTI-VERSION COMPARISON</div>
          <h1>多版本对比</h1>
          <p className="page-sub">
            {job ? `${scopeLabel(job.scope)} · ${job.handCount} 手 · ${job.totalDecisions} 决策点 · ${job.versions.join(' + ')} ×${job.repeat}` : '加载中…'}
          </p>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={() => data.refetch()} loading={data.isFetching}>刷新</Button>
          <Button icon={<DownloadOutlined />} onClick={() => exportData('csv')} disabled={!data.data?.hands.length}>CSV</Button>
          <Button icon={<DownloadOutlined />} onClick={() => exportData('markdown')} disabled={!data.data?.hands.length}>Markdown</Button>
        </Space>
      </div>

      <div className="cmp-stat-grid">
        <Card bordered={false}><div className="stat-value">{stats.hands}</div><div className="stat-label">手牌</div></Card>
        <Card bordered={false}><div className="stat-value">{stats.decisions}</div><div className="stat-label">决策点</div></Card>
        <Card bordered={false}><div className="stat-value warn">{stats.diverged}</div><div className="stat-label">版本分歧</div></Card>
        <Card bordered={false}><div className="stat-value down">{stats.violated}</div><div className="stat-label">有硬规则违规</div></Card>
        <Card bordered={false}><div className="stat-value down">{stats.errors}</div><div className="stat-label">失败/兜底</div></Card>
      </div>

      <Card className="cmp-toolbar-card" bordered={false}>
        <div className="jobs-toolbar">
          <Segmented value={only} onChange={(value) => { setOnly(String(value)); setPage(1) }} options={FILTERS} />
          <span className="muted">点击任一建议单元格查看理由与每次采样</span>
        </div>
      </Card>

      {data.isLoading ? <Card loading bordered={false} /> : null}
      {!data.isLoading && !data.data?.hands.length ? (
        <Card bordered={false}><Empty description="这个筛选下没有结果" /></Card>
      ) : null}

      <div className="cmp-hands">
        {(data.data?.hands || []).map((hand) => {
          const diverged = hand.decisions.filter((d) => d.diverged).length
          const errors = hand.decisions.filter((d) => d.versions.some((v) => v.summary.source !== 'model' && v.summary.total > 0)).length
          return (
            <Collapse
              key={hand.hand.id}
              className="cmp-hand-collapse"
              defaultActiveKey={['body']}
              items={[{
                key: 'body',
                label: (
                  <div className="cmp-hand-head">
                    <HandCards cards={hand.hand.heroCards} small />
                    <div className="cmp-hand-copy">
                      <div className="cmp-hand-title">{hand.hand.title}</div>
                      <div className="muted">#{hand.hand.id} · {hand.hand.heroPosition || '—'} · {hand.hand.result || '—'}</div>
                    </div>
                    <BoardCards board={hand.hand.board} />
                    <span className="spacer" />
                    <Tag className="mini-tag">{hand.decisions.length} 决策点</Tag>
                    {diverged ? <Tag color="orange" className="mini-tag">分歧 {diverged}</Tag> : null}
                    {errors ? <Tag color="red" className="mini-tag">失败 {errors}</Tag> : null}
                  </div>
                ),
                children: (
                  <Table<ComparisonDecision>
                    rowKey={(row) => `${row.decision.street}-${row.decision.seq}`}
                    dataSource={hand.decisions}
                    columns={columns}
                    pagination={false}
                    size="small"
                    scroll={{ x: 'max-content' }}
                    rowClassName={(row) => row.diverged ? 'cmp-row-diverged' : ''}
                  />
                ),
              }]}
            />
          )
        })}
      </div>

      {data.data && data.data.totalHands > (data.data.pageSize || 50) ? (
        <div className="cmp-pagination">
          <Button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button>
          <span className="muted">第 {page} 页 · 共 {data.data.totalHands} 手</span>
          <Button disabled={page * (data.data.pageSize || 50) >= data.data.totalHands} onClick={() => setPage(page + 1)}>下一页</Button>
        </div>
      ) : null}
    </div>
  )
}
