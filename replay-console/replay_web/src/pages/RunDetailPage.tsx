import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Col, Collapse, Descriptions, Drawer, Empty,
  Popconfirm, Row, Space, Statistic, Table, Tabs, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowLeftOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { ScoreRun, ScoreRunItem, ScoreRunItems } from '../types'
import { RUN_STATUS, formatDate } from '../lib/format'
import { rateText, scoreText } from './RunsPage'

const { Text } = Typography

function kindText(kind: string | null | undefined) {
  return kind || '—'
}

function amountText(kind: string | null | undefined, amount: number | null | undefined) {
  if (kind === 'fold' || kind === 'check' || !kind) return kindText(kind)
  if (amount === null || amount === undefined) return kindText(kind)
  return `${kind} ${amount}`
}

export function scoreTags(scores: ScoreRunItem['scores']) {
  const order: [string, number | null | undefined][] = [
    ['动作', scores.action], ['尺寸', scores.size], ['依据', scores.reason],
    ['事实', scores.fact], ['稳定', scores.stable],
  ]
  return (
    <Space size={4} wrap>
      {order.map(([label, value]) => (
        <Tag key={label} color={value === null || value === undefined ? 'default' : (value >= 0.999 ? 'green' : (value > 0 ? 'gold' : 'red'))}>
          {label} {value === null || value === undefined ? '—' : value.toFixed(2)}
        </Tag>
      ))}
    </Space>
  )
}

export default function RunDetailPage() {
  const { runId } = useParams()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState('all')
  const [active, setActive] = useState<ScoreRunItem | null>(null)

  const id = Number(runId)
  const run = useQuery<ScoreRun>({
    queryKey: ['score-run', id],
    queryFn: () => api.get(`/score-runs/${id}`),
    enabled: Number.isFinite(id),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'queued' || status === 'running' ? 2_000 : false
    },
  })

  const items = useQuery<ScoreRunItems>({
    queryKey: ['score-run', id, 'items', tab],
    queryFn: () => api.get(`/score-runs/${id}/items?limit=500${tab === 'hardfail' ? '&only=hardfail' : ''}`),
    enabled: Number.isFinite(id),
    refetchInterval: run.data?.status === 'running' || run.data?.status === 'queued' ? 5_000 : false,
  })

  const detail = useQuery<ScoreRunItem>({
    queryKey: ['score-run', id, 'item', active?.caseId, active?.repeatIndex],
    queryFn: () => api.get(`/score-runs/${id}/items/${active?.caseId}/${active?.repeatIndex}`),
    enabled: !!active,
  })

  const cancel = useMutation({
    mutationFn: () => api.post(`/score-runs/${id}/cancel`, {}),
    onSuccess: () => {
      message.success('已请求中断，当前单元跑完就停')
      queryClient.invalidateQueries({ queryKey: ['score-run', id] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<ScoreRunItem> = useMemo(() => [
    {
      title: '案例', key: 'case', width: 150,
      render: (_: unknown, row) => (
        <Space size={4}>
          <span>#{row.caseId}</span>
          {row.p0 ? <Tag color="volcano">P0</Tag> : null}
          {row.repeatIndex > 0 ? <Tag>第 {row.repeatIndex + 1} 次</Tag> : null}
        </Space>
      ),
    },
    {
      title: '决策点', key: 'decision', width: 140,
      render: (_: unknown, row) => (
        <Tooltip title={row.decisionKey || ''}>
          <span>{row.street} ai={row.actionIndex ?? '—'}</span>
        </Tooltip>
      ),
    },
    {
      title: '桶', dataIndex: 'bucketLabel', ellipsis: true,
    },
    {
      title: '建议', key: 'advice', width: 120,
      render: (_: unknown, row) => amountText(row.advice.kind, row.advice.amount),
    },
    {
      title: '来源', dataIndex: 'source', width: 90,
      render: (value: string | null) => value || '—',
    },
    {
      title: '五维', key: 'scores', width: 330,
      render: (_: unknown, row) => scoreTags(row.scores),
    },
    {
      title: '得分', dataIndex: 'score', width: 80,
      render: (value: number | null) => <Text strong>{scoreText(value)}</Text>,
    },
    {
      title: '硬失败', dataIndex: 'hardFail', width: 140,
      render: (value: string | null, row) => (value
        ? <Tooltip title={row.errorMsg || ''}><Tag color="red">{value}</Tag></Tooltip>
        : null),
    },
  ], [])

  const data = run.data
  const metrics = data?.metrics || {}
  const running = data?.status === 'queued' || data?.status === 'running'
  const counts = items.data?.counts
  const options = (detail.data?.options || []) as { id?: string; label?: string; kind?: string; amount?: number | null }[]

  return (
    <div className="page">
      <Card
        title={(
          <Space>
            <Link to="/runs"><Button size="small" icon={<ArrowLeftOutlined />}>返回</Button></Link>
            <span>评分运行 #{id}</span>
            {data ? (
              <Tag color={(RUN_STATUS[data.status] || { color: 'default' }).color}>
                {(RUN_STATUS[data.status] || { label: data.status }).label}
              </Tag>
            ) : null}
            {data?.cancelRequested && running ? <Tag>已请求中断</Tag> : null}
          </Space>
        )}
        extra={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => { run.refetch(); items.refetch() }}>刷新</Button>
            <Popconfirm
              title="中断这次运行？"
              description="已完成的案例照常计分，未完成的标成「部分完成」。"
              okText="中断"
              disabled={!running}
              onConfirm={() => cancel.mutate()}
            >
              <Button danger icon={<StopOutlined />} disabled={!running} loading={cancel.isPending}>
                中断
              </Button>
            </Popconfirm>
          </Space>
        )}
      >
        {data?.error ? (
          <Alert
            type={data.status === 'partial' ? 'warning' : 'error'}
            showIcon
            style={{ marginBottom: 12 }}
            message={data.error}
          />
        ) : null}
        <Row gutter={16}>
          <Col span={4}>
            <Statistic
              title="总分"
              value={scoreText(metrics.score?.mean ?? data?.score ?? null)}
              suffix={metrics.score?.lo !== null && metrics.score?.lo !== undefined
                ? `[${scoreText(metrics.score.lo)}, ${scoreText(metrics.score.hi)}]`
                : undefined}
            />
          </Col>
          <Col span={4}>
            <Statistic title="P0 通过率" value={rateText(metrics.p0?.rate ?? data?.p0PassRate ?? null)} />
          </Col>
          <Col span={4}>
            <Statistic title="覆盖率" value={rateText(metrics.coverage?.rate ?? data?.coverageRate ?? null)} />
          </Col>
          <Col span={4}>
            <Statistic
              title="硬失败"
              value={counts ? counts.fails : (metrics.hardFail?.count ?? 0)}
              suffix={metrics.hardFail?.rate !== null && metrics.hardFail?.rate !== undefined
                ? `(${rateText(metrics.hardFail.rate)})`
                : undefined}
            />
          </Col>
          <Col span={4}>
            <Statistic title="进度" value={`${data?.doneCases ?? 0}/${data?.totalCases ?? 0}`} />
          </Col>
          <Col span={4}>
            <Statistic title="计分案例" value={metrics.scored ?? data?.scoredCases ?? 0} />
          </Col>
        </Row>
        <Descriptions size="small" column={4} style={{ marginTop: 12 }}>
          <Descriptions.Item label="案例集">
            {data?.caseSet.name || '—'} <Tag>v{data?.caseSet.revision ?? '—'}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="版本">{data?.version.name || '—'}</Descriptions.Item>
          <Descriptions.Item label="判定/重建">
            {data?.judgeVersion || '—'} / {data?.reconstructVersion || '—'}
          </Descriptions.Item>
          <Descriptions.Item label="创建">{formatDate(data?.createdAt || null)}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card style={{ marginTop: 12 }}>
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'all',
              label: `逐条结果${counts ? `（${counts.items}）` : ''}`,
            },
            {
              key: 'hardfail',
              label: `硬失败${counts ? `（${counts.fails}）` : ''}`,
            },
          ]}
        />
        {items.data && !items.data.items.length ? (
          <Empty description={tab === 'hardfail' ? '没有硬失败，干净' : '还没有结果'} />
        ) : (
          <Table<ScoreRunItem>
            rowKey={(row) => `${row.caseId}-${row.repeatIndex}`}
            size="small"
            loading={items.isLoading}
            dataSource={items.data?.items || []}
            columns={columns}
            pagination={{ pageSize: 50, showSizeChanger: false }}
            onRow={(row) => ({ onClick: () => setActive(row) })}
            rowClassName={() => 'clickable-row'}
          />
        )}
      </Card>

      <Drawer
        open={!!active}
        onClose={() => setActive(null)}
        width={760}
        title={active ? `案例 #${active.caseId} · ${active.street} ai=${active.actionIndex ?? '—'}` : ''}
      >
        {detail.isLoading ? <Empty description="加载中" /> : null}
        {detail.data ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert
              type={detail.data.hardFail ? 'error' : 'success'}
              showIcon
              message={detail.data.hardFail
                ? `硬失败：${detail.data.hardFail}`
                : `得分 ${scoreText(detail.data.score)}`}
              description={detail.data.errorMsg || undefined}
            />
            <Descriptions size="small" bordered column={1}>
              <Descriptions.Item label="桶">{detail.data.bucketLabel}</Descriptions.Item>
              <Descriptions.Item label="决策点">
                {detail.data.decisionKey || '—'}（{detail.data.street} ai={detail.data.actionIndex ?? '—'}）
              </Descriptions.Item>
              <Descriptions.Item label="手牌">
                {detail.data.hand.heroCards} / {detail.data.hand.board} · {detail.data.hand.heroPosition || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="原始建议">
                <Space direction="vertical" size={2}>
                  <Text strong>{amountText(detail.data.advice.kind, detail.data.advice.amount)}</Text>
                  <Text type="secondary">{detail.data.advice.reason || '（无理由）'}</Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="五维">{scoreTags(detail.data.scores)}</Descriptions.Item>
              <Descriptions.Item label="来源">{detail.data.source || '—'}</Descriptions.Item>
            </Descriptions>

            <Card size="small" title="判据证据（judge）">
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                {JSON.stringify(detail.data.judge || {}, null, 2)}
              </pre>
            </Card>

            <Card size="small" title={`当时的选项（${options.length}）`}>
              <Table
                size="small"
                rowKey={(row, index) => row.id || String(index)}
                dataSource={options}
                pagination={false}
                columns={[
                  { title: 'id', dataIndex: 'id', width: 160 },
                  { title: '标签', dataIndex: 'label' },
                  {
                    title: '动作', key: 'kind', width: 140,
                    render: (_: unknown, row) => amountText(row.kind, row.amount),
                  },
                  {
                    title: '', key: 'chosen', width: 80,
                    render: (_: unknown, row) => (row.id && row.id === detail.data?.advice.optionId
                      ? <Tag color="green">选中</Tag> : null),
                  },
                ]}
              />
            </Card>

            <Card size="small" title="事实（facts）">
              <pre style={{ margin: 0, maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                {JSON.stringify(detail.data.facts || {}, null, 2)}
              </pre>
            </Card>

            {detail.data.prompt?.system || detail.data.prompt?.user ? (
              <Collapse
                items={[
                  {
                    key: 'prompt',
                    label: `Prompt（sha ${detail.data.prompt?.sha || '—'}）`,
                    children: (
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                        {`--- system ---\n${detail.data.prompt?.system || ''}\n--- user ---\n${detail.data.prompt?.user || ''}`}
                      </pre>
                    ),
                  },
                ]}
              />
            ) : null}
          </Space>
        ) : null}
      </Drawer>
    </div>
  )
}
