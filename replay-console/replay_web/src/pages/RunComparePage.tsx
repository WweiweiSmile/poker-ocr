import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Col, Descriptions, Drawer, Empty, Row, Segmented,
  Space, Statistic, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowLeftOutlined, DownloadOutlined, SwapOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, downloadScoreRunComparison, errorMessage } from '../api'
import { JudgeCard } from '../components/ScoreJudgePanel'
import type {
  CompareDirection, CompareMean, CompareRow, CompareSide, ScoreRunComparison,
  ScoreRunItem,
} from '../types'
import { RUN_STATUS, adviceText, formatDate, scoreText } from '../lib/format'

const { Text } = Typography

const DIRECTION_META: Record<CompareDirection, { label: string; color: string }> = {
  worse: { label: '改坏', color: 'red' },
  improved: { label: '改好', color: 'green' },
  unchanged: { label: '不变', color: 'default' },
  incomparable: { label: '不可比', color: 'orange' },
}

const DIRECTION_ORDER: CompareDirection[] = ['worse', 'improved', 'unchanged', 'incomparable']

/** 分数差（0–100 口径）；不可比没有差。 */
function deltaText(delta: number | null) {
  if (delta === null || delta === undefined) return '—'
  const rounded = Math.round(delta * 100) / 100
  if (rounded === 0) return '0'
  return `${rounded > 0 ? '+' : '−'}${Math.abs(rounded).toFixed(2)}`
}

/** 均分的 95% 区间；样本不足时没有区间，显示「—」而不是编一个数。 */
function scoreCi(mean: CompareMean | undefined) {
  if (!mean || mean.lo === null || mean.lo === undefined
    || mean.hi === null || mean.hi === undefined) return '—'
  return `[${scoreText(mean.lo)}, ${scoreText(mean.hi)}]`
}

function DirectionTag({ direction }: { direction: CompareDirection }) {
  const meta = DIRECTION_META[direction]
  return <Tag color={meta.color}>{meta.label}</Tag>
}

function RunHeadline({ label, run }: { label: string; run: ScoreRunComparison['left'] }) {
  return (
    <Space direction="vertical" size={2}>
      <Text type="secondary">{label}</Text>
      <Text strong>#{run.id} · {run.version.name || '未命名版本'}</Text>
      <Text type="secondary">
        {run.caseSet.name || '—'} v{run.caseSet.revision ?? '—'} · 总分 {scoreText(run.score)}
      </Text>
    </Space>
  )
}

/** 一侧的实际建议与判定：抽屉里两列并排，直接看「当时到底建议了什么」。 */
function SideCard({ label, side, item }: {
  label: string
  side: CompareSide
  item?: ScoreRunItem
}) {
  return (
    <Card
      size="small"
      title={(
        <Space>
          <span>{label}</span>
          <Tag>#{side.caseId}</Tag>
          {side.overridden ? <Tag color="blue">人工改判</Tag> : null}
        </Space>
      )}
    >
      {item?.override ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message={`人工改判（${item.override.actor || '未知'}）：机器分 `
            + `${scoreText(item.override.machine?.score ?? null)} → 有效分 `
            + `${scoreText(side.score)}`}
          description={item.override.note || undefined}
        />
      ) : null}
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="得分">
          <Text strong>{scoreText(side.score)}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="五维">
          <Space size={4} wrap>
            {([
              ['动作', side.scores.action], ['尺寸', side.scores.size],
              ['依据', side.scores.reason], ['事实', side.scores.fact],
              ['稳定', side.scores.stable],
            ] as [string, number | null | undefined][]).map(([name, value]) => (
              <Tag key={name} color={value === null || value === undefined ? 'default' : (value >= 0.999 ? 'green' : (value > 0 ? 'gold' : 'red'))}>
                {name} {value === null || value === undefined ? '—' : value.toFixed(2)}
              </Tag>
            ))}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="硬失败">
          {side.hardFail ? <Tag color="red">{side.hardFail}</Tag> : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="建议">
          <Space direction="vertical" size={2}>
            <Text strong>{adviceText(side.advice.kind, side.advice.amount)}</Text>
            <Text type="secondary">{side.advice.reason || '（无理由）'}</Text>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="来源">{side.source || '—'}</Descriptions.Item>
        <Descriptions.Item label="spec">
          {side.specName || '—'}
          {side.specRevision === null || side.specRevision === undefined
            ? '' : ` (r${side.specRevision})`}
        </Descriptions.Item>
      </Descriptions>
      {item ? (
        <div style={{ marginTop: 12 }}>
          <JudgeCard judge={item.judge} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            以上是机器判定；翻转方向用的是叠加人工改判后的有效分。
          </Text>
        </div>
      ) : null}
    </Card>
  )
}

export default function RunComparePage() {
  const { runId, otherId } = useParams()
  const { message } = App.useApp()
  const id = Number(runId)
  const other = Number(otherId)
  const [only, setOnly] = useState<CompareDirection | 'all'>('all')
  const [active, setActive] = useState<CompareRow | null>(null)

  const query = useQuery<ScoreRunComparison>({
    queryKey: ['score-run-compare', id, other],
    queryFn: () => api.get(`/score-runs/${id}/compare/${other}?limit=1000`),
    enabled: Number.isFinite(id) && Number.isFinite(other) && id !== other,
  })

  // 抽屉里的两次判定：不算在对照响应里，点开才取。
  const leftItem = useQuery<ScoreRunItem>({
    queryKey: ['score-run', id, 'item', active?.left.caseId, 0],
    queryFn: () => api.get(`/score-runs/${id}/items/${active?.left.caseId}/0`),
    enabled: !!active,
  })
  const rightItem = useQuery<ScoreRunItem>({
    queryKey: ['score-run', other, 'item', active?.right.caseId, 0],
    queryFn: () => api.get(`/score-runs/${other}/items/${active?.right.caseId}/0`),
    enabled: !!active,
  })

  const data = query.data
  const rows = useMemo(() => (data?.rows || []).filter(
    (row) => only === 'all' || row.direction === only), [data, only])

  const columns: ColumnsType<CompareRow> = useMemo(() => [
    {
      title: '案例', key: 'case', width: 150,
      render: (_: unknown, row) => (
        <Space size={4} direction="vertical">
          <Space size={4}>
            <span>#{row.left.caseId}</span>
            {row.p0 ? <Tag color="volcano">P0</Tag> : null}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.hand.heroCards} / {row.hand.board}
          </Text>
        </Space>
      ),
    },
    {
      title: '决策点', key: 'decision', width: 170,
      render: (_: unknown, row) => (
        <Space size={4} direction="vertical">
          <Tooltip title={row.decisionKey || ''}>
            <span>{row.street || '—'} ai={row.actionIndex ?? '—'}</span>
          </Tooltip>
          <Text type="secondary" style={{ fontSize: 12 }}>
            case #{row.right.caseId}
          </Text>
        </Space>
      ),
    },
    { title: '桶', dataIndex: 'bucketLabel', ellipsis: true },
    {
      title: '基准建议', key: 'leftAdvice', width: 130,
      render: (_: unknown, row) => (
        <Space size={4} direction="vertical">
          <span>{adviceText(row.left.advice.kind, row.left.advice.amount)}</span>
          {row.left.hardFail ? <Tag color="red">{row.left.hardFail}</Tag> : null}
        </Space>
      ),
    },
    {
      title: '对照建议', key: 'rightAdvice', width: 130,
      render: (_: unknown, row) => (
        <Space size={4} direction="vertical">
          <span>{adviceText(row.right.advice.kind, row.right.advice.amount)}</span>
          {row.right.hardFail ? <Tag color="red">{row.right.hardFail}</Tag> : null}
        </Space>
      ),
    },
    {
      title: '基准分', dataIndex: ['left', 'score'], width: 90,
      render: (value: number | null) => scoreText(value),
    },
    {
      title: '对照分', dataIndex: ['right', 'score'], width: 90,
      render: (value: number | null) => scoreText(value),
    },
    {
      title: '差值', dataIndex: 'delta', width: 90,
      render: (value: number | null) => <Text strong>{deltaText(value)}</Text>,
    },
    {
      title: '方向', dataIndex: 'direction', width: 90,
      render: (value: CompareDirection) => <DirectionTag direction={value} />,
    },
  ], [])

  if (!Number.isFinite(id) || !Number.isFinite(other)) {
    return <Card><Empty description="缺运行编号" /></Card>
  }
  if (id === other) {
    return <Card><Empty description="同一个运行不能和自己对照" /></Card>
  }

  const cases = data?.cases
  const summary = data?.summary
  const warnings = data?.warnings || []

  return (
    <div className="page">
      <Card
        title={(
          <Space>
            <Link to={`/runs/${id}`}>
              <Button size="small" icon={<ArrowLeftOutlined />}>返回 #{id}</Button>
            </Link>
            <span>版本对照</span>
            <Tag icon={<SwapOutlined />}>#{id} → #{other}</Tag>
          </Space>
        )}
        extra={(
          <Button
            icon={<DownloadOutlined />}
            onClick={() => {
              downloadScoreRunComparison(id, other, only === 'all' ? {} : { only })
                .catch((error) => message.error(errorMessage(error)))
            }}
          >
            导出 CSV{only === 'all' ? '' : `（${DIRECTION_META[only].label}）`}
          </Button>
        )}
      >
        {data ? (
          <Row gutter={16} align="middle">
            <Col span={7}><RunHeadline label="基准运行" run={data.left} /></Col>
            <Col span={2} style={{ textAlign: 'center' }}><SwapOutlined /></Col>
            <Col span={7}><RunHeadline label="对照运行" run={data.right} /></Col>
            <Col span={8}>
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="共同案例">
                  <Text strong>{cases?.common ?? 0}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="交集外（不计入）">
                  基准 {cases?.excludedLeft ?? 0} · 对照 {cases?.excludedRight ?? 0}
                </Descriptions.Item>
                <Descriptions.Item label="参与配对（已出建议）">
                  基准 {cases?.left ?? 0} · 对照 {cases?.right ?? 0}
                </Descriptions.Item>
                <Descriptions.Item label="运行案例数">
                  基准 {data.left.totalCases} · 对照 {data.right.totalCases}
                </Descriptions.Item>
              </Descriptions>
            </Col>
          </Row>
        ) : <Empty description={query.isLoading ? '加载中' : '没有对照数据'} />}
      </Card>

      {warnings.length ? (
        <div style={{ marginTop: 12 }}>
          <Alert
            type="warning"
            showIcon
            message={data?.versions.crossVersion
              ? '两边口径不同：只比共同案例，分差里混着口径变化'
              : '对比只覆盖共同案例'}
            description={(
              <Space direction="vertical" size={4}>
                {warnings.map((warning) => (
                  <span key={warning.code}>
                    <Tag>{warning.code}</Tag>{warning.message}
                  </span>
                ))}
              </Space>
            )}
          />
        </div>
      ) : null}

      {data ? (
        <Card style={{ marginTop: 12 }}>
          <Row gutter={16} style={{ marginBottom: 12 }}>
            <Col span={8}>
              <Statistic
                title={`共同案例均分（n=${summary?.scored ?? 0}）`}
                value={summary?.left.mean === null || summary?.left.mean === undefined
                  ? '—' : `${scoreText(summary.left.mean)} → ${scoreText(summary.right.mean)}`}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                95% 区间 {scoreCi(summary?.left)} → {scoreCi(summary?.right)}
              </Text>
            </Col>
            <Col span={4}>
              <Statistic
                title="均分差"
                value={deltaText(summary?.delta ?? null)}
              />
            </Col>
            {DIRECTION_ORDER.map((direction) => (
              <Col span={3} key={direction}>
                <Statistic
                  title={<DirectionTag direction={direction} />}
                  value={summary?.directions[direction] ?? 0}
                />
              </Col>
            ))}
          </Row>
          <Segmented
            value={only}
            onChange={(value) => setOnly(value as CompareDirection | 'all')}
            options={[
              { label: `全部（${summary?.n ?? 0}）`, value: 'all' },
              ...DIRECTION_ORDER.map((direction) => ({
                label: `${DIRECTION_META[direction].label}（${summary?.directions[direction] ?? 0}）`,
                value: direction,
              })),
            ]}
          />
          {data.truncated ? (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 8 }}
              message={`表里只有前 ${data.limit} 条（共 ${data.total} 条），导出 CSV 是全量`}
            />
          ) : null}
          {cases && !cases.common ? (
            <Alert
              type="warning"
              showIcon
              style={{ margin: '8px 0' }}
              message="没有共同案例：两次运行没有跑过同一个决策点，无法对照"
              description="交集外的案例数见上方；空的翻转表不代表「没有变化」。"
            />
          ) : null}
          {!rows.length ? (
            <Empty
              description={cases && !cases.common
                ? '两次运行没有交集，没有可比的行'
                : '这个方向没有案例'}
              style={{ marginTop: 24 }}
            />
          ) : (
            <Table<CompareRow>
              rowKey={(row) => `${row.canonicalId}:${row.decisionKey}:${row.left.caseId}`}
              size="small"
              loading={query.isLoading}
              dataSource={rows}
              columns={columns}
              style={{ marginTop: 8 }}
              pagination={{ pageSize: 50, showSizeChanger: false }}
              onRow={(row) => ({ onClick: () => setActive(row) })}
              rowClassName={() => 'clickable-row'}
            />
          )}
        </Card>
      ) : null}

      <Drawer
        open={!!active}
        onClose={() => setActive(null)}
        width={980}
        title={active ? (
          <Space>
            <span>{active.street || '—'} ai={active.actionIndex ?? '—'}</span>
            <DirectionTag direction={active.direction} />
            <span>{adviceText(active.left.advice.kind, active.left.advice.amount)}</span>
            <span>→</span>
            <span>{adviceText(active.right.advice.kind, active.right.advice.amount)}</span>
            <Tag>{deltaText(active.delta)}</Tag>
          </Space>
        ) : ''}
      >
        {active ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions size="small" column={2}>
              <Descriptions.Item label="决策点">
                {active.decisionKey || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="桶">{active.bucketLabel}</Descriptions.Item>
              <Descriptions.Item label="手牌">
                {active.hand.heroCards} / {active.hand.board} · {active.hand.heroPosition || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="配对键">
                canonical {active.canonicalId ?? '—'} · {active.decisionKey || '—'}
              </Descriptions.Item>
            </Descriptions>
            <Row gutter={12}>
              <Col span={12}>
                <SideCard label="基准运行" side={active.left} item={leftItem.data} />
              </Col>
              <Col span={12}>
                <SideCard label="对照运行" side={active.right} item={rightItem.data} />
              </Col>
            </Row>
            {data ? (
              <Text type="secondary">
                基准 #{data.left.id}：{data.left.version.name || '—'}，案例集{' '}
                {data.left.caseSet.name || '—'} v{data.left.caseSet.revision}，创建{' '}
                {formatDate(data.left.createdAt)}；对照 #{data.right.id}：
                {data.right.version.name || '—'}，案例集 {data.right.caseSet.name || '—'} v
                {data.right.caseSet.revision}，创建 {formatDate(data.right.createdAt)}。状态{' '}
                {(RUN_STATUS[data.left.status] || { label: data.left.status }).label} /{' '}
                {(RUN_STATUS[data.right.status] || { label: data.right.status }).label}
              </Text>
            ) : null}
            {active.left.source === 'error' || active.right.source === 'error' ? (
              <Alert
                type="warning"
                showIcon
                message="有一侧没跑出建议（source=error），所以记为不可比，而不是「不变」"
              />
            ) : null}
          </Space>
        ) : null}
      </Drawer>
    </div>
  )
}
