import { useMemo, useState } from 'react'
import {
  Alert, Card, Col, Row, Select, Space, Statistic, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useQuery } from '@tanstack/react-query'
import { api, errorMessage } from '../api'
import type {
  RangeABAction, RangeABAdvice, RangeABCase, RangeABResponse,
} from '../types'
import { formatAmount, formatDate } from '../lib/format'

const STATUS_COLORS: Record<string, string> = {
  ready: 'blue',
  blocked_cap: 'orange',
  blocked_data: 'red',
  data_broken: 'red',
  manual: 'purple',
  needs_reconstruction: 'default',
}
const STATUS_LABELS: Record<string, string> = {
  ready: '可跑',
  blocked_cap: '4 人上限',
  blocked_data: '数据矛盾',
  data_broken: '行动线断裂',
  manual: '手工',
  needs_reconstruction: '待重建',
}
const EXPECT_LABELS: Record<string, string> = {
  cross_down: '旧过 / 新不过',
  cross_up: '旧不过 / 新过',
  stay_above: '两侧都过',
  stay_below: '两侧都不过',
  'n/a': '无门槛',
}
const RESULT_COLORS: Record<string, string> = {
  PASS: 'green', FAIL: 'red', SKIP: 'default',
  MISSING: 'orange', MISSING_ADVICE: 'orange', NO_RANGE: 'orange',
}
const PRIORITY_COLORS: Record<string, string> = {
  P0: 'red', P1: 'orange', P2: 'blue', P3: 'default',
}

function pct(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(1)}%`
}

function EquityPair({ row }: { row: RangeABCase }) {
  const old = row.result?.old ?? row.old_equity
  const next = row.result?.new ?? row.new_equity
  const color = row.expect === 'cross_down' ? '#b91c1c'
    : row.expect === 'cross_up' ? '#b45309' : undefined
  return (
    <span className="mono" style={{ color }}>
      {pct(old)} → {pct(next)}
    </span>
  )
}

const ACTION_LABELS: Record<string, string> = {
  fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注', allin: '全下',
}
const STREET_LABELS: Record<string, string> = {
  preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌',
}

function actionText(action: RangeABAction): string {
  const label = ACTION_LABELS[action.action] || action.action
  return action.amount === null || action.amount === undefined
    ? label : `${label} ${formatAmount(action.amount)}`
}

function AdviceCard({ title, option, amount, reason, meta, highlight }: {
  title: string
  option?: string | null
  amount?: number | null
  reason?: string | null
  meta?: string | null
  highlight?: boolean
}) {
  return (
    <Card
      size="small"
      style={highlight
        ? { borderColor: '#0f766e', background: '#f5faf8' }
        : undefined}
    >
      <div className="card-title" style={{ marginBottom: 8 }}>{title}</div>
      <Space wrap style={{ marginBottom: 6 }}>
        <span className="option-pill">{option || '—'}</span>
        {amount !== null && amount !== undefined
          ? <span className="mono">{formatAmount(amount)} BB</span> : null}
        {meta ? <span className="muted">{meta}</span> : null}
      </Space>
      <div className="advice-reason">{reason || '—'}</div>
    </Card>
  )
}

function historicalAdvice(row: RangeABCase): RangeABAdvice | undefined {
  const advices = row.handDetail?.advices || []
  return advices.find((advice) => (
    advice.street === row.street
    && (row.action_index === null || row.action_index === undefined
      || advice.actionIndex === row.action_index)
  )) || advices.find((advice) => advice.street === row.street)
}

function CaseDetail({ row }: { row: RangeABCase }) {
  const hand = row.handDetail
  const model = row.result?.model
  const hist = historicalAdvice(row)
  const old = row.result?.old ?? row.old_equity
  const next = row.result?.new ?? row.new_equity
  return (
    <div style={{ padding: '2px 8px 10px' }}>
      <div className="muted" style={{ marginBottom: 10 }}>
        {hand
          ? `${hand.heroCards} · ${hand.board || '—'} · ${hand.heroPosition || '—'} · 底池 ${hand.pot !== null && hand.pot !== undefined ? formatAmount(hand.pot) : '—'}BB${hand.bombPot ? ' · 炸弹池' : ''}`
          : '没有手牌行动线'}
        {' · '}
        决策点：{STREET_LABELS[row.street] || row.street}
        {row.to_call ? ` · 需跟 ${formatAmount(row.to_call)}BB` : ''}
        {row.pot ? ` · 底池 ${formatAmount(row.pot)}BB` : ''}
        {' · '}旧 {pct(old)} → 新 {pct(next)}（门槛 {pct(row.threshold)}）
        <span style={{ marginLeft: 8 }}>
          模型建议为单次采样，可能波动；「线上原建议」来自当时落库的 advice。
        </span>
      </div>
      {hand ? (
        <div className="street-list" style={{ marginBottom: 12 }}>
          {hand.streets.map((street) => (
            <div className="street-row" key={street.street}>
              <div className="street-side" style={{ minWidth: 92 }}>
                <div className="street-name">
                  {STREET_LABELS[street.street] || street.street}
                </div>
                <div className="street-pot">
                  {street.potStart !== null && street.potStart !== undefined
                    ? `起始 ${formatAmount(street.potStart)} BB` : '—'}
                </div>
              </div>
              <div className="street-actions">
                {street.actions.length ? street.actions.map((action, index) => (
                  <span
                    className={`action-chip${action.isHero ? ' hero' : ''}`}
                    key={`${street.street}-${index}`}
                  >
                    {action.position || '?'} {actionText(action)}
                    {action.allIn
                      ? <Tag color="red" className="mini-tag">ALLIN</Tag> : null}
                    {action.inferred
                      ? <span className="muted">（推断）</span> : null}
                  </span>
                )) : <span className="muted">没有记录</span>}
              </div>
            </div>
          ))}
        </div>
      ) : null}
      <Row gutter={[12, 12]}>
        <Col span={8}>
          <AdviceCard
            title="线上原建议"
            option={hist?.optionLabel}
            amount={hist?.amount}
            reason={hist?.reason}
            meta={hist
              ? [hist.model, hist.promptVersion].filter(Boolean).join(' · ')
              : null}
          />
        </Col>
        <Col span={8}>
          <AdviceCard
            title="旧引擎重跑"
            option={model?.old_label || model?.old}
            amount={model?.old_amount}
            reason={model?.old_reason || (model ? '' : '未跑模型（--model）')}
            meta={model?.old_ms ? `${model.old_ms}ms` : null}
          />
        </Col>
        <Col span={8}>
          <AdviceCard
            title="新引擎重跑"
            option={model?.new_label || model?.new}
            amount={model?.new_amount}
            reason={model?.new_reason || (model ? '' : '未跑模型（--model）')}
            meta={model?.new_ms ? `${model.new_ms}ms` : null}
            highlight
          />
        </Col>
      </Row>
    </div>
  )
}

export default function RangeABPage() {
  const [priority, setPriority] = useState<string>('all')
  const [status, setStatus] = useState<string>('all')

  const query = useQuery<RangeABResponse>({
    queryKey: ['range-ab'],
    queryFn: () => api.get('/range-ab'),
    refetchInterval: 30_000,
  })

  const data = query.data
  const cases = useMemo(() => data?.cases || [], [data])
  const statuses = useMemo(
    () => Array.from(new Set(cases.map((row) => row.status))),
    [cases],
  )
  const shown = useMemo(() => cases.filter((row) => (
    (priority === 'all' || row.priority === priority)
    && (status === 'all' || row.status === status)
  )), [cases, priority, status])

  const columns: ColumnsType<RangeABCase> = [
    {
      title: '用例', key: 'id', width: 118, fixed: 'left',
      render: (_: unknown, row: RangeABCase) => (
        <div>
          <Tag color={PRIORITY_COLORS[row.priority] || 'default'}>{row.priority}</Tag>
          <span className="mono">{row.id}</span>
        </div>
      ),
    },
    {
      title: '手牌 / 决策点', key: 'hand', width: 220,
      render: (_: unknown, row: RangeABCase) => (
        <div>
          <div className="mono">{row.hand}</div>
          <div className="muted">
            {row.street}
            {row.action_index === null || row.action_index === undefined
              ? '' : ` idx=${row.action_index}`}
          </div>
        </div>
      ),
    },
    {
      title: '类别', dataIndex: 'category', key: 'category', width: 210,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: '旧 → 新权益', key: 'equity', width: 150,
      render: (_: unknown, row: RangeABCase) => <EquityPair row={row} />,
    },
    {
      title: '门槛', key: 'threshold', width: 78,
      render: (_: unknown, row: RangeABCase) => (
        <span className="mono">{pct(row.threshold)}</span>
      ),
    },
    {
      title: '预期', dataIndex: 'expect', key: 'expect', width: 130,
      render: (value: string) => (
        <Tag color={value === 'cross_down' ? 'red'
          : value === 'cross_up' ? 'orange' : 'default'}>
          {EXPECT_LABELS[value] || value}
        </Tag>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 110,
      render: (value: string) => (
        <Tag color={STATUS_COLORS[value] || 'default'}>
          {STATUS_LABELS[value] || value}
        </Tag>
      ),
    },
    {
      title: '结果', key: 'result', width: 92,
      render: (_: unknown, row: RangeABCase) => {
        const result = row.result?.result
        const problems = row.result?.problems || []
        const diff = row.result?.adviceDiff
        const tag = (
          <Tag color={RESULT_COLORS[result || ''] || 'default'}>
            {result || '未跑'}
          </Tag>
        )
        if (!problems.length && !diff) return tag
        return (
          <Tooltip title={[...problems, diff].filter(Boolean).join('；')}>
            {tag}
          </Tooltip>
        )
      },
    },
    {
      title: '备注', dataIndex: 'note', key: 'note',
      render: (value: string, row: RangeABCase) => {
        const diff = row.result?.adviceDiff
        return (
          <div>
            <div className="muted">{value || '—'}</div>
            {diff ? <div className="muted">模型差异：{diff}</div> : null}
          </div>
        )
      },
    },
  ]

  if (query.isLoading) {
    return <Card loading bordered={false} />
  }
  if (query.error) {
    return (
      <Alert type="error" showIcon message="范围 A/B 加载失败"
        description={errorMessage(query.error)} />
    )
  }
  if (!data?.available) {
    return (
      <Alert type="warning" showIcon message={data?.error || '没有 A/B 清单'}
        description={data?.hint || '确认 tools/ab_set.json 存在。'} />
    )
  }

  const summary = data.summary
  return (
    <div>
      <div className="page-head">
        <div>
          <div className="eyebrow">RANGE MODEL A/B</div>
          <h1>范围模型 A/B</h1>
          <p className="page-sub">
            只读视图：{data.baseline || 'RANGE_EVENT_MODEL=0'} vs{' '}
            {data.candidate || 'RANGE_EVENT_MODEL=1'}。
            清单 {data.setPath}；结果 {data.resultsPath}。
          </p>
        </div>
        <Space>
          <Tag>{data.cases?.length || 0} 条</Tag>
          {data.resultsAvailable
            ? <Tag color="green">结果 {formatDate(data.resultsUpdatedAt, false)}</Tag>
            : <Tag color="orange">未生成结果</Tag>}
        </Space>
      </div>

      {!data.resultsAvailable ? (
        <Alert
          type="info" showIcon style={{ marginBottom: 16 }}
          message="还没有结果文件，当前只显示清单里记录的旧/新权益"
          description={(
            <span>
              在父项目跑{' '}
              <span className="mono">
                .venv/bin/python tools/ab_range_set.py --json tools/ab_results.json
              </span>
              ，刷新本页即可看到 PASS/FAIL、权益漂移和模型建议差异。
            </span>
          )}
        />
      ) : null}

      <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
        <Col span={4}><Card bordered={false}><Statistic title="用例" value={summary?.total || 0} suffix="条" /></Card></Col>
        <Col span={5}><Card bordered={false}><Statistic title="可跑" value={summary?.ready || 0} suffix="条" /></Card></Col>
        <Col span={5}><Card bordered={false}><Statistic title="PASS" value={summary?.pass || 0} valueStyle={{ color: '#15803d' }} /></Card></Col>
        <Col span={5}><Card bordered={false}><Statistic title="FAIL" value={summary?.fail || 0} valueStyle={{ color: '#b91c1c' }} /></Card></Col>
        <Col span={5}><Card bordered={false}><Statistic title="SKIP" value={summary?.skip || 0} /></Card></Col>
      </Row>

      <div className="section-head" style={{ marginBottom: 12 }}>
        <div>
          <h2>用例</h2>
          <p className="muted">
            旧/新权益来自清单；结果列来自最近一次 ab_range_set.py --json。
          </p>
        </div>
        <Space>
          <Select
            value={priority} style={{ width: 120 }}
            onChange={setPriority}
            options={[
              { value: 'all', label: '全部优先级' },
              { value: 'P0', label: 'P0' },
              { value: 'P1', label: 'P1' },
              { value: 'P2', label: 'P2' },
              { value: 'P3', label: 'P3' },
            ]}
          />
          <Select
            value={status} style={{ width: 160 }}
            onChange={setStatus}
            options={[
              { value: 'all', label: '全部状态' },
              ...statuses.map((value) => ({
                value, label: STATUS_LABELS[value] || value,
              })),
            ]}
          />
        </Space>
      </div>

      <Card bordered={false} className="table-card">
        <Table
          rowKey="id"
          size="middle"
          dataSource={shown}
          columns={columns}
          pagination={false}
          scroll={{ x: 1300 }}
          expandable={{
            expandedRowRender: (row) => <CaseDetail row={row} />,
            rowExpandable: (row) => Boolean(row.handDetail || row.result?.model),
          }}
        />
      </Card>

      {data.skipped?.length ? (
        <Card bordered={false} style={{ marginTop: 16 }}>
          <div className="card-title">未入集 / 已跳过（{data.skipped.length}）</div>
          <Table
            rowKey="hand"
            size="small"
            pagination={false}
            dataSource={data.skipped}
            columns={[
              { title: '手牌', dataIndex: 'hand', width: 220,
                render: (value: string) => <span className="mono">{value}</span> },
              { title: '原因', dataIndex: 'reason' },
            ]}
          />
        </Card>
      ) : null}
    </div>
  )
}
