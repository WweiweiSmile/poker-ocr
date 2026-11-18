import { useMemo, useState } from 'react'
import {
  Alert, Button, Card, Empty, Modal, Segmented, Space, Statistic, Table, Tabs, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, errorMessage } from '../../api'
import type { Experiment, ExperimentPairs, PairRow, Version } from '../../types'
import { formatAmount } from '../../lib/format'
import LegacyBanner from '../../components/LegacyBanner'

const STREETS: Record<string, string> = {
  preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌',
}

function Side({ side, version, onPrompt, preferred }: {
  side: PairRow['A']
  version?: Version
  onPrompt: () => void
  preferred?: string
}) {
  return (
    <div className={`pair-side${preferred === 'A' ? ' preferred' : ''}`}>
      <div className="pair-side-head">
        <Tag color="blue">A</Tag>
        <span className="muted mono">{version?.name || 'baseline'}</span>
        <span className="spacer" />
        <Button size="small" onClick={onPrompt}>Prompt</Button>
      </div>
      <div className="pair-option">
        <span className={`option-pill kind-${side?.kind || 'none'}`}>
          {side?.kind || '无结果'} {side?.amount !== null && side?.amount !== undefined ? formatAmount(side.amount) : ''}
        </span>
        {side?.stable ? <Tag className="mini-tag">稳定</Tag> : <Tag color="orange" className="mini-tag">波动</Tag>}
      </div>
      <div className="pair-reason">{side?.reason || side?.optionId || '—'}</div>
    </div>
  )
}

function BSide({ side, version, onPrompt, preferred }: {
  side: PairRow['B']
  version?: Version
  onPrompt: () => void
  preferred?: string
}) {
  return (
    <div className={`pair-side${preferred === 'B' ? ' preferred' : ''}`}>
      <div className="pair-side-head">
        <Tag color="purple">B</Tag>
        <span className="muted mono">{version?.name || 'variant'}</span>
        <span className="spacer" />
        <Button size="small" onClick={onPrompt}>Prompt</Button>
      </div>
      <div className="pair-option">
        <span className={`option-pill kind-${side?.kind || 'none'}`}>
          {side?.kind || '无结果'} {side?.amount !== null && side?.amount !== undefined ? formatAmount(side.amount) : ''}
        </span>
        {side?.stable ? <Tag className="mini-tag">稳定</Tag> : <Tag color="orange" className="mini-tag">波动</Tag>}
      </div>
      <div className="pair-reason">{side?.reason || side?.optionId || '—'}</div>
    </div>
  )
}

export default function ExperimentPairsPage() {
  const { experimentId } = useParams()
  const id = Number(experimentId)
  const navigate = useNavigate()
  const [only, setOnly] = useState('all')
  const [promptRow, setPromptRow] = useState<PairRow | null>(null)

  const experiment = useQuery<Experiment>({
    queryKey: ['experiment', id],
    queryFn: () => api.get(`/experiments/${id}`),
    enabled: Number.isFinite(id),
  })
  const versions = useQuery<{ items: Version[] }>({
    queryKey: ['versions', 'all'],
    queryFn: () => api.get('/versions?includeArchived=1'),
  })
  const pairs = useQuery<ExperimentPairs>({
    queryKey: ['experiment-pairs', id, only],
    queryFn: () => api.get(`/experiments/${id}/pairs${only === 'all' ? '' : `?only=${only}`}`),
    enabled: Number.isFinite(id) && !!experiment.data?.jobId,
  })

  const versionMap = useMemo(() => {
    const map: Record<number, Version> = {}
    for (const version of versions.data?.items || []) map[version.id] = version
    return map
  }, [versions.data])

  const columns: ColumnsType<PairRow> = [
    {
      title: '决策点', key: 'decision', width: 220,
      render: (_: unknown, row) => (
        <div>
          <Space size={4}>
            <Tag color="teal">{STREETS[row.street] || row.street}</Tag>
            <span className="mono">D{row.seq + 1}</span>
            {row.split ? <Tag className="mini-tag">{row.split}</Tag> : null}
            {row.quality === 'approx' ? <Tag color="orange" className="mini-tag">approx</Tag> : null}
          </Space>
          <div className="muted cell-sub">
            实际 {row.actual.action || '—'}
            {row.actual.amountBb !== null && row.actual.amountBb !== undefined ? ` ${formatAmount(row.actual.amountBb)}` : ''}
          </div>
          <Button type="link" size="small" style={{ padding: 0 }}
            onClick={() => navigate(`/hands/${row.handId}/replay`)}>
            手牌 #{row.handId}
          </Button>
        </div>
      ),
    },
    {
      title: 'A（baseline）', key: 'a', width: 300,
      render: (_: unknown, row) => (
        <Side side={row.A} version={versionMap[experiment.data?.baselineVersionId || 0]}
          preferred={row.review?.preferred}
          onPrompt={() => setPromptRow(row)} />
      ),
    },
    {
      title: 'B（variant）', key: 'b', width: 300,
      render: (_: unknown, row) => (
        <BSide side={row.B} version={versionMap[experiment.data?.variantVersionId || 0]}
          preferred={row.review?.preferred}
          onPrompt={() => setPromptRow(row)} />
      ),
    },
    {
      title: '自动 / 人工', key: 'auto', width: 220,
      render: (_: unknown, row) => (
        <div>
          <Space wrap size={4}>
            {row.auto.changed ? <Tag color="orange" className="mini-tag">有差异</Tag> : <Tag color="green" className="mini-tag">一致</Tag>}
            {row.auto.p0 ? (row.auto.p0Pass ? <Tag color="green" className="mini-tag">P0 过</Tag> : <Tag color="red" className="mini-tag">P0 失败</Tag>) : null}
          </Space>
          {row.review ? <Tag color="blue" className="mini-tag">已复盘 {row.review.preferred}</Tag> : <Tag className="mini-tag">未复盘</Tag>}
          {row.auto.assertions?.length ? (
            <div className="pair-assertions">
              {row.auto.assertions.map((assertion, index) => (
                <Tag key={`${assertion.type}-${index}`} color={assertion.ok ? 'green' : 'red'} className="mini-tag">
                  {assertion.type}
                </Tag>
              ))}
            </div>
          ) : null}
        </div>
      ),
    },
  ]

  if (!experiment.data?.jobId) {
    return <Alert type="info" showIcon message="实验还没跑" description="这张实验没有关联任务，所以没有成对结果。" />
  }
  if (pairs.error) return <Alert type="error" showIcon message={errorMessage(pairs.error)} />
  const summary = pairs.data?.summary

  return (
    <div className="experiment-pairs-page">
      <div className="detail-back">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate(`/legacy/experiments/${id}`)}>实验详情</Button>
        <span className="mono muted">#{id}</span>
        <Tag>{experiment.data?.name}</Tag>
      </div>

      <LegacyBanner scope="成对复盘" />

      <Card className="hand-hero" bordered={false}>
        <div className="hand-hero-main">
          <div>
            <div className="hand-hero-title">成对复盘</div>
            <div className="hand-hero-sub">
              <span>看同一决策点的 A/B 建议与理由；当时记录的偏好以「已复盘」标签显示</span>
              <span>agreement 只是诊断，不是正确率</span>
            </div>
          </div>
          <Space>
            <Segmented
              value={only}
              onChange={(value) => setOnly(String(value))}
              options={[
                { label: '全部', value: 'all' }, { label: '有差异', value: 'changed' },
                { label: '未复盘', value: 'unreviewed' }, { label: 'P0', value: 'p0' },
              ]}
            />
          </Space>
        </div>
      </Card>

      <div className="set-stats">
        <Card bordered={false}><Statistic title="配对决策" value={summary?.n || 0} /></Card>
        <Card bordered={false}><Statistic title="有差异" value={summary?.changed || 0} /></Card>
        <Card bordered={false}><Statistic title="B 更好" value={summary?.bWins || 0} /></Card>
        <Card bordered={false}><Statistic title="A 更好" value={summary?.aWins || 0} /></Card>
        <Card bordered={false}><Statistic title="P0 回归" value={summary?.p0Regressions || 0} valueStyle={{ color: summary?.p0Regressions ? '#b91c1c' : undefined }} /></Card>
      </div>

      <Card bordered={false} className="experiment-outcome-card">
        <div className="outcome-row">
          <div>
            <div className="card-title">实验结论（已记录，只读）</div>
            <div className="muted">
              结论：{experiment.data?.decision || '未记录'}
              {experiment.data?.conclusion ? ` · ${experiment.data.conclusion}` : ''}
            </div>
          </div>
        </div>
      </Card>

      <Card bordered={false}>
        <Table<PairRow>
          rowKey="decisionId"
          size="middle"
          loading={pairs.isLoading}
          dataSource={pairs.data?.rows || []}
          columns={columns}
          pagination={{ pageSize: 30, showSizeChanger: true }}
          locale={{ emptyText: <Empty description="没有配对结果。" /> }}
        />
      </Card>

      <Modal
        open={!!promptRow}
        title={promptRow ? `Prompt 对比 · 手牌 #${promptRow.handId} · D${promptRow.seq + 1}` : 'Prompt'}
        width={1040}
        footer={null}
        onCancel={() => setPromptRow(null)}
        destroyOnClose
      >
        {promptRow ? (
          <Tabs
            items={[
              {
                key: 'a', label: 'A / baseline',
                children: (
                  <div className="pair-prompt-grid">
                    <div>
                      <div className="card-title">System</div>
                      <pre className="prompt-pre small">{promptRow.A?.promptSystem || '—'}</pre>
                    </div>
                    <div>
                      <div className="card-title">User</div>
                      <pre className="prompt-pre small">{promptRow.A?.promptUser || '—'}</pre>
                    </div>
                    <div>
                      <div className="card-title">Raw</div>
                      <pre className="prompt-pre small">{promptRow.A?.rawResponse || '—'}</pre>
                    </div>
                  </div>
                ),
              },
              {
                key: 'b', label: 'B / variant',
                children: (
                  <div className="pair-prompt-grid">
                    <div>
                      <div className="card-title">System</div>
                      <pre className="prompt-pre small">{promptRow.B?.promptSystem || '—'}</pre>
                    </div>
                    <div>
                      <div className="card-title">User</div>
                      <pre className="prompt-pre small">{promptRow.B?.promptUser || '—'}</pre>
                    </div>
                    <div>
                      <div className="card-title">Raw</div>
                      <pre className="prompt-pre small">{promptRow.B?.rawResponse || '—'}</pre>
                    </div>
                  </div>
                ),
              },
              {
                key: 'diff', label: 'A/B 对照',
                children: (
                  <div className="pair-prompt-grid">
                    <div>
                      <div className="card-title">A user</div>
                      <pre className="prompt-pre small">{promptRow.A?.promptUser || '—'}</pre>
                    </div>
                    <div>
                      <div className="card-title">B user</div>
                      <pre className="prompt-pre small">{promptRow.B?.promptUser || '—'}</pre>
                    </div>
                  </div>
                ),
              },
            ]}
          />
        ) : null}
      </Modal>
    </div>
  )
}
