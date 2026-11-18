import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Col, Descriptions, Drawer, Empty, Form, Input,
  Modal, Row, Select, Space, Statistic, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, errorMessage, toQuery } from '../api'
import { JudgeCard } from '../components/ScoreJudgePanel'
import { scoreTags } from './RunDetailPage'
import type {
  CaseCandidate, CaseCandidates, CaseSetList, HotspotReport, HotspotRow,
  ScoreRun, ScoreRunItem, ScoreRunItems, ScoreRunList, SpotSpec,
  SpotSpecList, SpecMeta,
} from '../types'
import {
  RUN_STATUS, adviceText, ciText, formatDate, rateText, scoreText,
} from '../lib/format'

const { Text, Paragraph } = Typography

// 严重度分档与「与全局差」的分档只是显示阈值（不是为了卡点）：
// 3 档 ≈ 一个目标带宽度、6 档 ≈ 两个；±5 分是肉眼可感的偏差。
const SEVERE = 6
const HIGH = 3
const LOW = 1
const DELTA_BAND = 5

const FREQ_STATUS: Record<string, { label: string; color: string }> = {
  absent: { label: '无频次目标', color: 'default' },
  insufficient: { label: '样本不足', color: 'default' },
  met: { label: '带内', color: 'green' },
  missed: { label: '带外', color: 'red' },
}

function severityColor(row: HotspotRow) {
  if (row.severity === null) return 'default'
  if (row.severity >= SEVERE) return 'red'
  if (row.severity >= HIGH) return 'volcano'
  if (row.severity >= LOW) return 'gold'
  return 'default'
}

function signed(value: number | null) {
  if (value === null || value === undefined) return '—'
  const text = Math.abs(value).toFixed(1)
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : '0.0'
}

export default function HotspotsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [active, setActive] = useState<HotspotRow | null>(null)
  const [item, setItem] = useState<ScoreRunItem | null>(null)
  const [specOpen, setSpecOpen] = useState(false)
  const [sampleOpen, setSampleOpen] = useState(false)
  const [sampleSetId, setSampleSetId] = useState<number | null>(null)
  const [picked, setPicked] = useState<React.Key[]>([])
  const [specForm] = Form.useForm()

  const runs = useQuery<ScoreRunList>({
    queryKey: ['score-runs', 'hotspot-picker'],
    queryFn: () => api.get('/score-runs?limit=50'),
  })
  const runId = Number(params.get('runId') || 0) || undefined
  const chosen = useMemo(() => {
    const list = runs.data?.items || []
    if (runId) return list.find((row) => row.id === runId) || null
    return list.find((row) => row.status === 'succeeded')
      || list.find((row) => row.metrics?.scored) || list[0] || null
  }, [runs.data, runId])

  const report = useQuery<HotspotReport>({
    queryKey: ['score-run', chosen?.id, 'hotspots'],
    queryFn: () => api.get(`/score-runs/${chosen?.id}/hotspots`),
    enabled: !!chosen?.id,
  })

  const meta = useQuery<SpecMeta>({
    queryKey: ['spec-meta'],
    queryFn: () => api.get('/specs/meta'),
    enabled: specOpen,
  })
  const spec = useQuery<SpotSpec>({
    queryKey: ['spec', active?.specId],
    queryFn: () => api.get(`/specs/${active?.specId}`),
    enabled: !!active?.specId,
  })
  const caseSets = useQuery<CaseSetList>({
    queryKey: ['case-sets', 'sample-picker'],
    queryFn: () => api.get('/case-sets?limit=200'),
    enabled: sampleOpen,
  })
  const candidates = useQuery<CaseCandidates>({
    queryKey: ['case-candidates', sampleSetId, active?.specId],
    queryFn: () => api.get(
      `/case-sets/${sampleSetId}/candidates${toQuery({ ...(active?.bucket || {}), limit: 100 })}`),
    enabled: sampleOpen && !!sampleSetId && !!active,
  })
  const items = useQuery<ScoreRunItems>({
    queryKey: ['score-run', chosen?.id, 'hotspot-items', active?.specId],
    queryFn: () => api.get(
      `/score-runs/${chosen?.id}/items?specId=${active?.specId}&repeat=0&sort=score&limit=500`),
    enabled: !!active,
  })
  const detail = useQuery<ScoreRunItem>({
    queryKey: ['score-run', chosen?.id, 'item', item?.caseId, item?.repeatIndex],
    queryFn: () => api.get(
      `/score-runs/${chosen?.id}/items/${item?.caseId}/${item?.repeatIndex}`),
    enabled: !!item,
  })

  const createSpec = useMutation({
    mutationFn: (values: {
      name: string
      status: 'draft' | 'active'
      bucket: Record<string, Array<string | number>>
    }) => api.post<SpotSpec>('/specs', {
      name: values.name,
      status: values.status,
      bucket: values.bucket,
      allowedActions: [],
      forbiddenActions: [],
      sizing: null,
      freq: null,
      requiredPoints: [],
      forbiddenPoints: [],
      rationale: `偏差热点 #${chosen?.id ?? '?'} 桶：${active?.bucketLabel || ''}`.slice(0, 500),
    }),
    onSuccess: (created) => {
      message.success(
        `期望「${created.name}」已建（${created.status === 'active' ? '生效中' : '草稿'}）；`
        + '点「从存档补样」会把该桶新案例绑到它')
      setSpecOpen(false)
      queryClient.invalidateQueries({ queryKey: ['specs'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const sample = useMutation({
    mutationFn: () => {
      const rows = (candidates.data?.items || []).filter(
        (row) => picked.includes(`${row.canonicalId}-${row.decisionKey}`))
      return api.post(`/case-sets/${sampleSetId}/cases`, {
        cases: rows.map((row) => ({
          canonicalId: row.canonicalId,
          decisionKey: row.decisionKey,
          specId: row.specId,
          priority: 1,
          note: '热点补样',
        })),
      })
    },
    onSuccess: (result: unknown) => {
      const body = result as { created?: number; existing?: number; skipped?: unknown[] }
      message.success(
        `补样完成：新增 ${body.created ?? 0}，已在集内 ${body.existing ?? 0}` +
        `${body.skipped?.length ? `，跳过 ${body.skipped.length}` : ''}`)
      setSampleOpen(false)
      setPicked([])
      queryClient.invalidateQueries({ queryKey: ['case-candidates'] })
      queryClient.invalidateQueries({ queryKey: ['case-set'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<HotspotRow> = useMemo(() => [
    {
      title: '偏差', key: 'severity', width: 130,
      sorter: (a, b) => (a.severity ?? -1) - (b.severity ?? -1),
      render: (_: unknown, row) => (
        <Space size={4}>
          <Tag color={severityColor(row)}>{row.severity === null ? '—' : row.severity.toFixed(2)}</Tag>
          {row.basis ? (
            <Tag color="default">{row.basis === 'freq' ? '频次口径' : '均分口径'}</Tag>
          ) : null}
          {row.insufficient ? <Tag>样本不足</Tag> : null}
        </Space>
      ),
    },
    {
      title: '桶 / 期望', key: 'bucket',
      render: (_: unknown, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.bucketLabel || '（空桶）'}</Text>
          <Text type="secondary">{row.specName || `spec #${row.specId}`}</Text>
        </Space>
      ),
    },
    {
      title: '样本 n', dataIndex: 'n', width: 90,
      sorter: (a, b) => a.n - b.n,
      render: (value: number, row) => (
        <Tooltip title={`计分 ${row.scored} 条；门槛 ${report.data?.summary.freqMinN ?? 15}`}>
          <span>{value}</span>
        </Tooltip>
      ),
    },
    {
      title: '均分', key: 'mean', width: 150,
      render: (_: unknown, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{scoreText(row.mean)}</Text>
          <Text type="secondary">
            {row.scoreCi.lo === null ? '区间不可算' : `[${scoreText(row.scoreCi.lo)}, ${scoreText(row.scoreCi.hi)}]`}
          </Text>
        </Space>
      ),
    },
    {
      title: '硬失败', key: 'hardFail', width: 140,
      render: (_: unknown, row) => (
        <Tooltip title={row.hardFail.ci
          ? `95% Wilson ${ciText(row.hardFail.ci)}` : '没有计分样本'}>
          <span>{row.hardFail.count} · {rateText(row.hardFail.rate)}</span>
        </Tooltip>
      ),
    },
    {
      title: '频率（实际 vs 目标带）', key: 'freq', width: 210,
      render: (_: unknown, row) => {
        if (!row.freq) return <Text type="secondary">—</Text>
        const band = row.freq.min === null
          ? '—' : `${rateText(row.freq.min)}–${rateText(row.freq.max)}`
        const state = FREQ_STATUS[row.freq.status] || { label: row.freq.status, color: 'default' }
        return (
          <Tooltip title={row.freq.ci ? `95% Wilson ${ciText(row.freq.ci)}` : '没有可比样本'}>
            <Space direction="vertical" size={0}>
              <Space size={4}>
                <Text>{row.freq.action} {rateText(row.freq.observed)}</Text>
                <Tag color={state.color}>{state.label}</Tag>
              </Space>
              <Text type="secondary">
                目标 {band}（n={row.freq.n}{row.freq.ci ? ` · ${ciText(row.freq.ci)}` : ''}）
              </Text>
            </Space>
          </Tooltip>
        )
      },
    },
    {
      title: '与全局差', dataIndex: 'delta', width: 110,
      sorter: (a, b) => (a.delta ?? 0) - (b.delta ?? 0),
      render: (value: number | null) => (
        <Tag color={value === null ? 'default' : value <= -DELTA_BAND ? 'red' : value >= DELTA_BAND ? 'green' : 'default'}>
          {signed(value)}
        </Tag>
      ),
    },
  ], [report.data])

  const summary = report.data?.summary

  const openSpec = () => {
    if (!active) return
    // 换一行打开时必须先清空：antd 会保留未挂载字段的值，深合并会把上一行
    // 的桶维度带进这条新 spec，建出匹配不到任何案例的约束。
    specForm.resetFields()
    specForm.setFieldsValue({
      name: `${active.bucketLabel || '热点'} 期望`.slice(0, 120),
      // 默认生效：draft 不会被案例按桶匹配到，「保存后与该桶绑定」就落空。
      status: 'active',
      bucket: active.bucket,
    })
    setSpecOpen(true)
  }

  return (
    <div className="page">
      <Card
        title={(
          <Space>
            <span>偏差热点</span>
            {summary ? (
              <Tag>运行 #{report.data?.run.id}</Tag>
            ) : null}
          </Space>
        )}
        extra={(
          <Space>
            <Select
              style={{ width: 320 }}
              placeholder="选择一次评分运行"
              loading={runs.isLoading}
              value={chosen?.id}
              onChange={(value) => {
                setActive(null)
                setItem(null)
                setParams({ runId: String(value) })
              }}
              options={(runs.data?.items || []).map((run) => ({
                value: run.id,
                label: `#${run.id} ${RUN_STATUS[run.status]?.label || run.status} · ${run.caseSet.name || '—'} v${run.caseSet.revision} · ${formatDate(run.createdAt)}`,
              }))}
            />
            <Button onClick={() => report.refetch()}>刷新</Button>
          </Space>
        )}
      >
        {!runs.isLoading && !(runs.data?.items || []).length ? (
          <Empty description="还没有评分运行，先去 /runs 跑一次" />
        ) : null}
        {report.isError ? (
          <Alert type="error" showIcon message={errorMessage(report.error)} />
        ) : null}
        {summary ? (
          <>
            <Row gutter={16}>
              <Col span={5}>
                <Statistic
                  title={`总均分（n=${summary.score.n}）`}
                  value={scoreText(summary.score.mean)}
                  suffix={summary.score.lo === null ? undefined
                    : `[${scoreText(summary.score.lo)}, ${scoreText(summary.score.hi)}]`}
                />
              </Col>
              <Col span={5}>
                <Statistic
                  title="频率达成率"
                  value={rateText(summary.attainment.rate)}
                  suffix={summary.attainment.rate === null ? undefined
                    : ciText(summary.attainment.ci)
                      ?? `${summary.attainment.met}/${summary.attainment.judged}`}
                />
              </Col>
              <Col span={5}>
                <Statistic title="桶数（按 spec）" value={summary.specs} />
              </Col>
              <Col span={5}>
                <Statistic title="样本不足的桶" value={summary.insufficient}
                  suffix={`（n<${summary.freqMinN}）`} />
              </Col>
              <Col span={4}>
                <Statistic title="有硬失败的桶" value={summary.bucketsWithHardFail} />
              </Col>
            </Row>
            <Alert
              style={{ marginTop: 12 }}
              type="info"
              showIcon
              message="怎么看这张表"
              description={`偏差 = 频次离目标带中点的距离（半带宽为 1 档，最多 3 档）；无频次目标时按 (100−均分)/50。严重度 = 偏差 × √n，所以「差得多」和「差得稳」都能排上来。样本 n<${summary.freqMinN} 的桶只标「样本不足」，不进达成率。`}
            />
          </>
        ) : null}
      </Card>

      <Card style={{ marginTop: 12 }} title="按桶排的偏差">
        {report.data && !report.data.rows.length ? (
          <Empty description="这次运行没有可聚合的结果" />
        ) : (
          <Table<HotspotRow>
            rowKey={(row) => `${row.specId}`}
            size="small"
            loading={report.isLoading}
            dataSource={report.data?.rows || []}
            columns={columns}
            pagination={{ pageSize: 20, showSizeChanger: false }}
            onRow={(row) => ({ onClick: () => setActive(row) })}
            rowClassName={() => 'clickable-row'}
          />
        )}
      </Card>

      <Drawer
        open={!!active}
        onClose={() => { setActive(null); setItem(null) }}
        width={880}
        title={active ? `${active.bucketLabel || '空桶'} · ${active.specName || `spec #${active.specId}`}` : ''}
        extra={active ? (
          <Space>
            {active.specId ? (
              <Link to={`/specs?revise=${active.specId}`}>
                <Button>改 spec（预填桶，期望库）</Button>
              </Link>
            ) : null}
            <Button type="primary" onClick={openSpec}>建 spec（预填桶）</Button>
            <Button onClick={() => { setPicked([]); setSampleOpen(true) }}>从存档补样</Button>
          </Space>
        ) : null}
      >
        {active ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {active.insufficient ? (
              <Alert
                type="warning"
                showIcon
                message={`样本不足：n=${active.freq?.n ?? active.n} < ${report.data?.summary.freqMinN ?? 15}`}
                description="只做展示，不计入频率达成率的分子/分母；需要更多案例时用「从存档补样」。"
              />
            ) : null}
            <Descriptions size="small" bordered column={4}>
              <Descriptions.Item label="偏差">{active.dev === null ? '—' : active.dev.toFixed(2)}</Descriptions.Item>
              <Descriptions.Item label="严重度">{active.severity === null ? '—' : active.severity.toFixed(2)}</Descriptions.Item>
              <Descriptions.Item label="均分（95% CI）">
                {scoreText(active.mean)}
                {active.scoreCi.lo === null ? ' —'
                  : ` [${scoreText(active.scoreCi.lo)}, ${scoreText(active.scoreCi.hi)}]`}
              </Descriptions.Item>
              <Descriptions.Item label="硬失败">
                {active.hardFail.count} · {rateText(active.hardFail.rate)}
                {active.hardFail.ci ? ` ${ciText(active.hardFail.ci)}` : ''}
              </Descriptions.Item>
              <Descriptions.Item label="频率" span={2}>
                {active.freq
                  ? `${active.freq.action} 实际 ${rateText(active.freq.observed)}${active.freq.ci ? ` ${ciText(active.freq.ci)}` : ''} / 目标 ${rateText(active.freq.min)}–${rateText(active.freq.max)}（n=${active.freq.n}）`
                  : '这个 spec 没有频次目标'}
              </Descriptions.Item>
              <Descriptions.Item label="样本" span={2}>
                n={active.n}，计分 {active.scored}
              </Descriptions.Item>
            </Descriptions>

            <Card size="small" title="期望（spec）" loading={spec.isLoading}>
              {spec.data ? (
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Space wrap>
                    <Tag color={spec.data.status === 'active' ? 'green' : 'default'}>
                      {spec.data.status === 'active' ? '生效中' : '草稿'}
                    </Tag>
                    <Text type="secondary">{spec.data.name} · v{spec.data.revision}</Text>
                  </Space>
                  <Text>允许动作：{spec.data.allowedActions.length
                    ? spec.data.allowedActions.join(' / ') : '（未限制）'}</Text>
                  <Text>频次目标：{spec.data.freq
                    ? `${spec.data.freq.action} ${rateText(spec.data.freq.min)}–${rateText(spec.data.freq.max)}`
                    : '（无）'}</Text>
                  {spec.data.requiredPoints.length ? (
                    <Space direction="vertical" size={2}>
                      {spec.data.requiredPoints.map((point) => (
                        <Text key={point.key} type="secondary">
                          · {point.text}（权重 {point.weight ?? 0}）
                        </Text>
                      ))}
                    </Space>
                  ) : null}
                </Space>
              ) : (
                <Text type="secondary">这个桶还没有绑定期望；点右上角「建 spec（预填桶）」。</Text>
              )}
            </Card>

            <Card
              size="small"
              title={`案例（${active.n}）`}
              extra={items.data?.truncated ? (
                <Text type="warning">
                  条目已到 {items.data.limit} 条上限，排序只覆盖这批；更全的看运行详情
                </Text>
              ) : null}
            >
              {items.data && !items.data.items.length ? (
                <Empty description="没有条目" />
              ) : (
                <Table<ScoreRunItem>
                  rowKey={(row) => `${row.caseId}-${row.repeatIndex}`}
                  size="small"
                  loading={items.isLoading}
                  dataSource={items.data?.items || []}
                  pagination={{ pageSize: 50, showSizeChanger: false }}
                  onRow={(row) => ({ onClick: () => setItem(row) })}
                  rowClassName={() => 'clickable-row'}
                  columns={[
                    {
                      title: '案例', key: 'case', width: 110,
                      render: (_: unknown, row) => (
                        <Space size={4}>
                          <span>#{row.caseId}</span>
                          {row.p0 ? <Tag color="volcano">P0</Tag> : null}
                        </Space>
                      ),
                    },
                    {
                      title: '决策点 / 手牌', key: 'hand',
                      render: (_: unknown, row) => (
                        <Space direction="vertical" size={0}>
                          <Text>{row.decisionKey} · ai={row.actionIndex ?? '—'}</Text>
                          <Text type="secondary">{row.hand.heroCards} / {row.hand.board} · {row.hand.heroPosition || '—'}</Text>
                        </Space>
                      ),
                    },
                    {
                      title: '实际建议', key: 'advice', width: 120,
                      render: (_: unknown, row) => adviceText(row.advice.kind, row.advice.amount),
                    },
                    {
                      title: '得分', dataIndex: 'score', width: 80,
                      render: (value: number | null) => <Text strong>{scoreText(value)}</Text>,
                    },
                    {
                      title: '硬失败', dataIndex: 'hardFail', width: 140,
                      render: (value: string | null) => value ? <Tag color="red">{value}</Tag> : null,
                    },
                  ]}
                />
              )}
            </Card>
          </Space>
        ) : null}
      </Drawer>

      <Drawer
        open={!!item}
        onClose={() => setItem(null)}
        width={760}
        title={item ? `案例 #${item.caseId} · ${item.decisionKey}` : ''}
      >
        {detail.data ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert
              type={detail.data.hardFail ? 'error' : 'success'}
              showIcon
              message={detail.data.hardFail
                ? `硬失败：${detail.data.hardFail}`
                : `得分 ${scoreText(detail.data.effective?.score ?? detail.data.score)}`}
              description={detail.data.errorMsg || undefined}
            />
            <Descriptions size="small" bordered column={1}>
              <Descriptions.Item label="桶">{detail.data.bucketLabel}</Descriptions.Item>
              <Descriptions.Item label="手牌">
                {detail.data.hand.heroCards} / {detail.data.hand.board} · {detail.data.hand.heroPosition || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="实际建议">
                <Space direction="vertical" size={2}>
                  <Text strong>{adviceText(detail.data.advice.kind, detail.data.advice.amount)}</Text>
                  <Text type="secondary">{detail.data.advice.reason || '（无理由）'}</Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="五维">{scoreTags(detail.data.scores)}</Descriptions.Item>
            </Descriptions>
            <JudgeCard judge={detail.data.judge} />
            <Link to={`/runs/${chosen?.id}`}>
              <Button size="small">去运行详情改判</Button>
            </Link>
          </Space>
        ) : <Empty description={detail.isLoading ? '加载中' : '没有详情'} />}
      </Drawer>

      <Modal
        open={specOpen}
        title={active?.specId ? '新建期望（预填当前桶）' : '建期望（预填当前桶）'}
        okText="保存"
        confirmLoading={createSpec.isPending}
        onCancel={() => setSpecOpen(false)}
        onOk={() => specForm.submit()}
        width={720}
        destroyOnClose
      >
        {active?.specId ? (
          <Alert
            style={{ marginBottom: 12 }}
            type="info"
            showIcon
            message="这个桶已经有期望"
            description="在这里新建会另起一条同名之外的期望；要改现有期望请去「期望库」，从最新 revision 派生。新期望保存后，新建的案例按桶匹配到它；已建案例保持原绑定。"
          />
        ) : null}
        <Form
          form={specForm}
          layout="vertical"
          onFinish={(values) => createSpec.mutate(values)}
        >
          <Form.Item
            name="name"
            label="期望名称"
            rules={[{ required: true, message: '给这条期望起个名字' }, { max: 120 }]}
          >
            <Input placeholder="例如：翻牌干燥面 IP 持续下注" />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue="active">
            <Select
              options={[
                { value: 'active', label: '生效中（立刻参与匹配）' },
                { value: 'draft', label: '草稿（先不参与匹配）' },
              ]}
            />
          </Form.Item>
          <Form.Item label="桶（预填自热点行；缺省维度 = 通配）">
            {(meta.data?.dimensions || []).map((dimension) => (
              <Form.Item
                key={dimension.key}
                name={['bucket', dimension.key]}
                label={dimension.label}
                style={{ marginBottom: 8 }}
              >
                <Select
                  mode="multiple"
                  allowClear
                  placeholder="通配"
                  options={dimension.values}
                />
              </Form.Item>
            ))}
          </Form.Item>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            先只预填桶；动作 / 尺寸 / 要点 / 频次等细节保存后到「期望库」补。
          </Paragraph>
        </Form>
      </Modal>

      <Modal
        open={sampleOpen}
        title={`从存档补样 · ${active?.bucketLabel || ''}`}
        okText={`加入所选（${picked.length}）`}
        okButtonProps={{ disabled: !picked.length || !sampleSetId }}
        confirmLoading={sample.isPending}
        onCancel={() => setSampleOpen(false)}
        onOk={() => sample.mutate()}
        width={900}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Select
            style={{ width: 360 }}
            placeholder="选一个未冻结的案例集"
            loading={caseSets.isLoading}
            value={sampleSetId ?? undefined}
            onChange={(value) => { setSampleSetId(value); setPicked([]) }}
            options={(caseSets.data?.items || [])
              .filter((set) => !set.frozen)
              .map((set) => ({
                value: set.id,
                label: `${set.name} v${set.revision}（${set.caseCount} 条）`,
              }))}
          />
          {!sampleSetId ? (
            <Empty description="先选案例集，再勾选要补进的手牌决策点" />
          ) : (
            <Table<CaseCandidate>
              rowKey={(row) => `${row.canonicalId}-${row.decisionKey}`}
              size="small"
              loading={candidates.isLoading}
              dataSource={candidates.data?.items || []}
              pagination={{ pageSize: 8, showSizeChanger: false }}
              rowSelection={{
                selectedRowKeys: picked,
                onChange: setPicked,
                getCheckboxProps: (row) => ({ disabled: !row.specId || row.inSet }),
              }}
              columns={[
                {
                  title: '手牌', key: 'hand', width: 190,
                  render: (_: unknown, row) => (
                    <Space direction="vertical" size={0}>
                      <Text>{row.hand.heroCards} / {row.hand.board}</Text>
                      <Text type="secondary">{row.hand.heroPosition || '—'} · {row.decisionKey}</Text>
                    </Space>
                  ),
                },
                { title: '桶', dataIndex: 'bucketLabel', ellipsis: true },
                {
                  title: '期望', key: 'spec', width: 200,
                  render: (_: unknown, row) => (
                    row.inSet ? <Tag color="green">已在集内</Tag>
                      : row.specId ? <span>{row.specName || `spec #${row.specId}`}</span>
                        : <Tag>匹配不到 active spec</Tag>
                  ),
                },
              ]}
            />
          )}
          <Text type="secondary">
            只列当前桶的存档决策点；已在集内的会跳过（同一集 + 同一手牌 + 同一决策点幂等）。
          </Text>
        </Space>
      </Modal>
    </div>
  )
}
