import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Col, Empty, Form, Input, InputNumber, Modal,
  Popconfirm, Row, Select, Space, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { AimOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api, errorMessage, toQuery } from '../api'
import type {
  SpecBand, SpecChange, SpecDiff, SpecMeta, SpecOption, SpotSpec,
  SpotSpecHistory, SpotSpecList,
} from '../types'
import { formatDate } from '../lib/format'

const STATUS_LABEL: Record<string, string> = { draft: '草稿', active: '生效' }

const CHANGE_KIND: Record<string, { label: string; color: string }> = {
  added: { label: '新增', color: 'green' },
  removed: { label: '删除', color: 'red' },
  changed: { label: '修改', color: 'gold' },
}

function labelOf(options: SpecOption[], value: unknown): string {
  const found = options.find((option) => option.value === value)
  return found ? found.label : String(value ?? '—')
}

function bandText(band: SpecBand): string {
  const unit = band.unit === 'bb' ? 'BB' : '底池'
  return `${unit} ${band.min}–${band.max}`
}

function sizingText(sizing: SpotSpec['sizing']): string {
  const bands = Array.isArray(sizing) ? sizing : sizing ? [sizing] : []
  return bands.length ? bands.map(bandText).join(' / ') : '不限'
}

function freqText(freq: SpotSpec['freq'], actions: SpecOption[]): string {
  if (!freq) return '不限'
  return `${labelOf(actions, freq.action)} ${Math.round(freq.min * 100)}%–${Math.round(freq.max * 100)}%`
}

/** diff 的 before/after 是原始 JSON，按 section 翻译成人话。 */
function describeValue(change: SpecChange, value: unknown, meta: SpecMeta): string {
  if (value === null || value === undefined || value === '') return '—'
  const list = Array.isArray(value) ? value : [value]
  switch (change.section) {
    case 'bucket': {
      const dimension = meta.dimensions.find((item) => item.key === change.field)
      return list.map((item) => labelOf(dimension?.values || [], item)).join(' / ')
    }
    case 'actions':
      return list.map((item) => labelOf(meta.actions, item)).join('、')
    case 'sizing':
      return list
        .map((item) => (typeof item === 'object' && item ? bandText(item as SpecBand) : String(item)))
        .join(' / ')
    case 'freq': {
      const freq = value as { action?: string; min?: number; max?: number }
      return `${labelOf(meta.actions, freq.action)} ${Math.round((freq.min ?? 0) * 100)}%–${Math.round((freq.max ?? 0) * 100)}%`
    }
    case 'points':
      return list
        .map((item) => {
          if (typeof item === 'object' && item) {
            const point = item as { key?: string; text?: string }
            return `${point.key ?? ''}${point.text ? `：${point.text}` : ''}`
          }
          return String(item)
        })
        .join('；')
    default:
      return String(value)
  }
}

// ─────────────── 编辑器（新建 / 派生 revision） ───────────────

function blankForm() {
  return {
    name: '', status: 'draft', note: '', bucket: {},
    allowedActions: [], forbiddenActions: [],
    sizing: [], freq: undefined,
    requiredPoints: [], forbiddenPoints: [], rationale: '',
  }
}

function specToForm(spec: SpotSpec) {
  const sizing = Array.isArray(spec.sizing) ? spec.sizing : spec.sizing ? [spec.sizing] : []
  return {
    name: spec.name,
    status: spec.status,
    note: spec.note,
    bucket: spec.bucket,
    allowedActions: spec.allowedActions,
    forbiddenActions: spec.forbiddenActions,
    sizing,
    freq: spec.freq || undefined,
    requiredPoints: spec.requiredPoints,
    forbiddenPoints: spec.forbiddenPoints,
    rationale: spec.rationale,
  }
}

function buildPayload(values: Record<string, unknown>, meta: SpecMeta) {
  const rawBucket = (values.bucket || {}) as Record<string, Array<string | number> | undefined>
  const bucket: Record<string, Array<string | number>> = {}
  meta.dimensions.forEach((dimension) => {
    const picked = rawBucket[dimension.key]
    if (picked && picked.length) bucket[dimension.key] = picked
  })
  const sizing = ((values.sizing || []) as Array<Partial<SpecBand>>)
    .filter((band) => band && band.unit && band.min !== undefined && band.min !== null
      && band.max !== undefined && band.max !== null)
    .map((band) => ({ unit: band.unit, min: band.min, max: band.max }))
  const rawFreq = (values.freq || {}) as { action?: string; min?: number; max?: number }
  const freq = rawFreq.action
    ? { action: rawFreq.action, min: rawFreq.min ?? 0, max: rawFreq.max ?? 1 }
    : null
  const requiredPoints = ((values.requiredPoints || []) as Array<{ key?: string; text?: string; weight?: number }>)
    .filter((point) => point && point.key)
    .map((point) => ({ key: point.key, text: point.text || '', weight: point.weight ?? 1 }))
  const forbiddenPoints = ((values.forbiddenPoints || []) as Array<{ key?: string; text?: string }>)
    .filter((point) => point && point.key)
    .map((point) => ({ key: point.key, text: point.text || '' }))
  return {
    name: values.name,
    status: values.status || 'draft',
    note: values.note || '',
    bucket,
    allowedActions: values.allowedActions || [],
    forbiddenActions: values.forbiddenActions || [],
    sizing,
    freq,
    requiredPoints,
    forbiddenPoints,
    rationale: values.rationale || '',
  }
}

function SpecEditorModal({
  open, meta, spec, onClose,
}: {
  open: boolean
  meta?: SpecMeta
  spec: SpotSpec | null
  onClose: () => void
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()

  useEffect(() => {
    if (!open) return
    form.resetFields()
    form.setFieldsValue(spec ? specToForm(spec) : blankForm())
  }, [open, spec, form])

  const save = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      spec
        ? api.post<SpotSpec>(`/specs/${spec.id}/revisions`, payload)
        : api.post<SpotSpec>('/specs', payload),
    onSuccess: (saved) => {
      message.success(`已保存 v${saved.revision}`)
      queryClient.invalidateQueries({ queryKey: ['specs'] })
      queryClient.invalidateQueries({ queryKey: ['spec-history'] })
      onClose()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  if (!meta) return null
  const isRevision = !!spec

  return (
    <Modal
      open={open}
      width={960}
      title={isRevision ? `派生 ${spec.name} v${spec.revision + 1}` : '新建期望'}
      okText={isRevision ? `保存为 v${spec.revision + 1}` : '保存'}
      cancelText="取消"
      confirmLoading={save.isPending}
      onCancel={onClose}
      onOk={() => form.submit()}
    >
      {isRevision ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={`v${spec.revision} 保存后只读；这次修改会写成 v${spec.revision + 1}。`}
        />
      ) : null}
      <Form form={form} layout="vertical" onFinish={(values) => save.mutate(buildPayload(values, meta))}>
        <Row gutter={12}>
          <Col span={15}>
            <Form.Item name="name" label="名称" rules={[{ required: true, message: '给期望起个名字' }]}>
              <Input maxLength={120} disabled={isRevision} placeholder="例如：翻牌 EP 顶对弱踢 · 松弱对手" />
            </Form.Item>
          </Col>
          <Col span={9}>
            <Form.Item name="status" label="状态" tooltip="生效 = 这条 revision 会被案例引用；同一期望同时只有一个生效版">
              <Select options={meta.statuses} />
            </Form.Item>
          </Col>
        </Row>

        <Typography.Text strong>桶</Typography.Text>
        <div className="muted" style={{ marginBottom: 8 }}>
          留空 = 通配；维度之间 AND，同一维度的多个取值之间 OR。只选你真正要约束的维度。
        </div>
        <Row gutter={12}>
          {meta.dimensions.map((dimension) => (
            <Col span={8} key={dimension.key}>
              <Form.Item name={['bucket', dimension.key]} label={dimension.label}>
                <Select
                  mode="multiple"
                  allowClear
                  options={dimension.values}
                  placeholder="不限（通配）"
                />
              </Form.Item>
            </Col>
          ))}
        </Row>

        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="allowedActions" label="允许动作" tooltip="只允许这些动作；留空表示不限制">
              <Select mode="multiple" allowClear options={meta.actions} placeholder="不限" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="forbiddenActions" label="禁止动作" tooltip="这些动作一律不允许">
              <Select mode="multiple" allowClear options={meta.actions} placeholder="无" />
            </Form.Item>
          </Col>
        </Row>

        <Typography.Text strong>尺度</Typography.Text>
        <div className="muted" style={{ marginBottom: 8 }}>
          多条带同时约束，尺度需同时满足所有带；底池为 0 时池比例带自动跳过。
        </div>
        <Form.List name="sizing">
          {(fields, { add, remove }) => (
            <>
              {fields.map(({ key, name, ...rest }) => (
                <Space key={key} align="baseline" style={{ display: 'flex' }}>
                  <Form.Item {...rest} name={[name, 'unit']} rules={[{ required: true, message: '选单位' }]}>
                    <Select style={{ width: 130 }} options={meta.sizingUnits} />
                  </Form.Item>
                  <Form.Item {...rest} name={[name, 'min']} rules={[{ required: true, message: '填下限' }]}>
                    <InputNumber min={0} step={0.05} placeholder="下限" style={{ width: 120 }} />
                  </Form.Item>
                  <Form.Item {...rest} name={[name, 'max']} rules={[{ required: true, message: '填上限' }]}>
                    <InputNumber min={0} step={0.05} placeholder="上限" style={{ width: 120 }} />
                  </Form.Item>
                  <Button type="text" danger onClick={() => remove(name)}>删除</Button>
                </Space>
              ))}
              <Button type="dashed" block onClick={() => add({ unit: 'pot_fraction', min: 0.33, max: 0.75 })}>
                + 加一条尺度带
              </Button>
            </>
          )}
        </Form.List>

        <Typography.Text strong style={{ display: 'block', marginTop: 16 }}>频率（可选）</Typography.Text>
        <Row gutter={12} style={{ marginTop: 8 }}>
          <Col span={8}>
            <Form.Item name={['freq', 'action']} label="动作">
              <Select allowClear options={meta.actions} placeholder="不限" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name={['freq', 'min']} label="下限（0–1）">
              <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name={['freq', 'max']} label="上限（0–1）">
              <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>
        <div className="muted" style={{ marginBottom: 12 }}>
          频率是建议区间的统计口径；样本量门槛由评分代码统一处理，不写进期望。
        </div>

        <Typography.Text strong>必须出现的要点</Typography.Text>
        <Form.List name="requiredPoints">
          {(fields, { add, remove }) => (
            <>
              {fields.map(({ key, name, ...rest }) => (
                <Space key={key} align="baseline" style={{ display: 'flex' }}>
                  <Form.Item {...rest} name={[name, 'key']} rules={[{ required: true, message: 'key' }]}>
                    <Input placeholder="key（英文）" style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item {...rest} name={[name, 'text']} rules={[{ required: true, message: '要点文案' }]}>
                    <Input placeholder="要点文案" style={{ width: 420 }} />
                  </Form.Item>
                  <Form.Item {...rest} name={[name, 'weight']}>
                    <InputNumber min={1} max={9} placeholder="权重" style={{ width: 90 }} />
                  </Form.Item>
                  <Button type="text" danger onClick={() => remove(name)}>删除</Button>
                </Space>
              ))}
              <Button type="dashed" block onClick={() => add({ weight: 1 })}>+ 加一个必需要点</Button>
            </>
          )}
        </Form.List>

        <Typography.Text strong style={{ display: 'block', marginTop: 16 }}>禁止出现的要点</Typography.Text>
        <Form.List name="forbiddenPoints">
          {(fields, { add, remove }) => (
            <>
              {fields.map(({ key, name, ...rest }) => (
                <Space key={key} align="baseline" style={{ display: 'flex' }}>
                  <Form.Item {...rest} name={[name, 'key']} rules={[{ required: true, message: 'key' }]}>
                    <Input placeholder="key（英文）" style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item {...rest} name={[name, 'text']} rules={[{ required: true, message: '要点文案' }]}>
                    <Input placeholder="要点文案" style={{ width: 420 }} />
                  </Form.Item>
                  <Button type="text" danger onClick={() => remove(name)}>删除</Button>
                </Space>
              ))}
              <Button type="dashed" block onClick={() => add({})}>+ 加一个禁止要点</Button>
            </>
          )}
        </Form.List>

        <Form.Item name="rationale" label="依据（rationale）" style={{ marginTop: 16 }}>
          <Input.TextArea rows={3} maxLength={4000} placeholder="为什么这样定？引用哪次复盘 / 哪条策略依据" />
        </Form.Item>
        <Form.Item name="note" label="备注">
          <Input.TextArea rows={2} maxLength={2000} placeholder="可选：给以后看 revision 历史的人留一句话" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

// ─────────────── 历史与 diff ───────────────

function SpecHistoryModal({
  spec, meta, onClose,
}: {
  spec: SpotSpec | null
  meta?: SpecMeta
  onClose: () => void
}) {
  const [diffRow, setDiffRow] = useState<SpotSpec | null>(null)

  const history = useQuery<SpotSpecHistory>({
    queryKey: ['spec-history', spec?.id],
    queryFn: () => api.get(`/specs/${spec!.id}/history`),
    enabled: !!spec,
  })

  const columns: ColumnsType<SpotSpec> = [
    {
      title: 'Revision', width: 110,
      render: (_: unknown, row) => (
        <Space size={4}>
          <span className="mono">v{row.revision}</span>
          <Tag color={row.status === 'active' ? 'green' : 'default'}>{STATUS_LABEL[row.status] || row.status}</Tag>
        </Space>
      ),
    },
    {
      title: '时间', dataIndex: 'createdAt', width: 130,
      render: (value: string | null) => <span className="muted">{formatDate(value, false)}</span>,
    },
    {
      title: '尺度 / 频率',
      render: (_: unknown, row) => (
        <span className="mono">
          {sizingText(row.sizing)} · {freqText(row.freq, meta?.actions || [])}
        </span>
      ),
    },
    { title: '备注', dataIndex: 'note', render: (value: string) => <span className="muted">{value || '—'}</span> },
    {
      title: '操作', width: 130,
      render: (_: unknown, row) => (
        <Button type="link" size="small" disabled={!row.supersedes} onClick={() => setDiffRow(row)}>
          与上一版对比
        </Button>
      ),
    },
  ]

  return (
    <>
      <Modal
        open={!!spec}
        width={900}
        title={spec ? `${spec.name} · 历史` : '历史'}
        footer={null}
        onCancel={onClose}
      >
        <div className="muted" style={{ marginBottom: 12 }}>
          旧 revision 只读。要改内容，从最新 revision 派生新版本。
        </div>
        {history.error ? <Alert type="error" showIcon message={errorMessage(history.error)} /> : null}
        <Table<SpotSpec>
          rowKey="id"
          size="middle"
          loading={history.isLoading}
          columns={columns}
          dataSource={history.data?.items || []}
          pagination={false}
        />
      </Modal>
      <SpecDiffModal spec={diffRow} meta={meta} onClose={() => setDiffRow(null)} />
    </>
  )
}

function SpecDiffModal({
  spec, meta, onClose,
}: {
  spec: SpotSpec | null
  meta?: SpecMeta
  onClose: () => void
}) {
  const diff = useQuery<SpecDiff>({
    queryKey: ['spec-diff', spec?.id],
    queryFn: () => api.get(`/specs/${spec!.id}/diff`),
    enabled: !!spec,
  })

  const columns: ColumnsType<SpecChange> = [
    { title: '部分', dataIndex: 'label', width: 110 },
    {
      title: '变更', dataIndex: 'kind', width: 90,
      render: (kind: string) => (
        <Tag color={CHANGE_KIND[kind]?.color}>{CHANGE_KIND[kind]?.label || kind}</Tag>
      ),
    },
    {
      title: '之前',
      render: (_: unknown, change) => (
        <span className="mono">{meta ? describeValue(change, change.before, meta) : String(change.before ?? '—')}</span>
      ),
    },
    {
      title: '之后',
      render: (_: unknown, change) => (
        <span className="mono">{meta ? describeValue(change, change.after, meta) : String(change.after ?? '—')}</span>
      ),
    },
  ]

  return (
    <Modal
      open={!!spec}
      width={860}
      title={spec ? `v${spec.revision} 与上一版对比` : 'diff'}
      footer={null}
      onCancel={onClose}
    >
      {diff.error ? <Alert type="error" showIcon message={errorMessage(diff.error)} /> : null}
      <Table<SpecChange>
        rowKey={(change) => `${change.section}-${change.field || change.key || change.label}-${change.kind}`}
        size="small"
        loading={diff.isLoading}
        columns={columns}
        dataSource={diff.data?.changes || []}
        pagination={false}
        locale={{ emptyText: '两版内容相同。' }}
      />
    </Modal>
  )
}

// ─────────────── 列表页 ───────────────

export default function SpecsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<'all' | 'active' | 'draft'>('all')
  const [q, setQ] = useState('')
  const [editor, setEditor] = useState<{ open: boolean; spec: SpotSpec | null }>({ open: false, spec: null })
  const [historySpec, setHistorySpec] = useState<SpotSpec | null>(null)
  // 偏差热点「改 spec」带着 ?revise=<id> 跳进来：直接打开派生表单，
  // 内容与桶都预填成当前这一版，省掉一次手动查找。
  const [searchParams, setSearchParams] = useSearchParams()
  const reviseId = Number(searchParams.get('revise') || 0)

  const metaQuery = useQuery<SpecMeta>({
    queryKey: ['spec-meta'],
    queryFn: () => api.get('/specs/meta'),
    staleTime: 60_000,
  })
  const listQuery = useQuery<SpotSpecList>({
    queryKey: ['specs', status, q],
    queryFn: () => api.get(`/specs${toQuery({ status: status === 'all' ? '' : status, q })}`),
  })
  const meta = metaQuery.data
  const items = listQuery.data?.items || []
  // 列表只给最新 revision；热点可能冻结在旧 revision 上，按 id 单独取一次。
  const reviseQuery = useQuery<SpotSpec>({
    queryKey: ['spec', reviseId],
    queryFn: () => api.get(`/specs/${reviseId}`),
    enabled: !!reviseId && !!listQuery.data,
    retry: false,
  })

  useEffect(() => {
    if (!reviseId) return
    const target = items.find((item) => item.id === reviseId) || reviseQuery.data
    if (target) {
      setEditor({ open: true, spec: target })
      setSearchParams({}, { replace: true })
      return
    }
    // 找不到（已删除）就清掉参数，别把一次静默无操作留在地址栏里。
    if (reviseQuery.isError) setSearchParams({}, { replace: true })
  }, [reviseId, items, reviseQuery.data, reviseQuery.isError, setSearchParams])

  const groups = useMemo(() => {
    const map = new Map<string, SpotSpec[]>()
    items.forEach((item) => {
      const key = item.bucketLabel || '全局'
      const bucket = map.get(key) || []
      bucket.push(item)
      map.set(key, bucket)
    })
    return Array.from(map.entries())
  }, [items])

  const patch = useMutation({
    mutationFn: ({ id, next }: { id: number; next: 'active' | 'draft' }) =>
      api.patch<SpotSpec>(`/specs/${id}`, { status: next }),
    onSuccess: (saved) => {
      message.success(saved.status === 'active'
        ? `v${saved.revision} 已生效（同名旧生效版已退回草稿）`
        : `v${saved.revision} 已停用`)
      queryClient.invalidateQueries({ queryKey: ['specs'] })
      queryClient.invalidateQueries({ queryKey: ['spec-history'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const remove = useMutation({
    mutationFn: (spec: SpotSpec) => api.delete(`/specs/${spec.id}`),
    onSuccess: () => {
      message.success('已删除该 revision')
      queryClient.invalidateQueries({ queryKey: ['specs'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<SpotSpec> = [
    {
      title: '期望', dataIndex: 'name', key: 'name',
      render: (_: string, row) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => setHistorySpec(row)}>
            {row.name}
          </Button>
          <div className="muted cell-sub">{row.note || row.rationale || '—'}</div>
        </div>
      ),
    },
    {
      title: '版本', width: 120,
      render: (_: unknown, row) => (
        <Space size={4}>
          <span className="mono">v{row.revision}</span>
          <Tag color={row.status === 'active' ? 'green' : 'default'}>{STATUS_LABEL[row.status] || row.status}</Tag>
          {row.activeRevisionId !== null && row.activeRevisionId !== row.id ? (
            <Tooltip title="该期望当前生效的是另一版">
              <Tag>另有生效版</Tag>
            </Tooltip>
          ) : null}
        </Space>
      ),
    },
    {
      title: '允许动作', width: 190,
      render: (_: unknown, row) => (row.allowedActions.length
        ? row.allowedActions.map((action) => <Tag key={action}>{labelOf(meta?.actions || [], action)}</Tag>)
        : <span className="muted">不限</span>),
    },
    {
      title: '禁止动作', width: 160,
      render: (_: unknown, row) => (row.forbiddenActions.length
        ? row.forbiddenActions.map((action) => <Tag color="red" key={action}>{labelOf(meta?.actions || [], action)}</Tag>)
        : <span className="muted">—</span>),
    },
    {
      title: '尺度', width: 200,
      render: (_: unknown, row) => <span className="mono">{sizingText(row.sizing)}</span>,
    },
    {
      title: '频率', width: 150,
      render: (_: unknown, row) => <span className="mono">{freqText(row.freq, meta?.actions || [])}</span>,
    },
    {
      title: '要点', width: 130,
      render: (_: unknown, row) => (
        <span className="mono">{row.requiredPoints.length} 必需 · {row.forbiddenPoints.length} 禁止</span>
      ),
    },
    {
      title: '时间', dataIndex: 'createdAt', width: 120,
      render: (value: string | null) => <span className="muted">{formatDate(value, false)}</span>,
    },
    {
      title: '操作', width: 230,
      render: (_: unknown, row) => {
        const isLatest = row.revisionCount === null || row.revision === row.revisionCount
        const hasActive = row.activeRevisionId !== null
        return (
          <Space size={2}>
            <Tooltip title={isLatest ? '从这一版派生新 revision' : '旧 revision 只读；请从最新版派生'}>
              <Button type="link" size="small" disabled={!isLatest}
                onClick={() => setEditor({ open: true, spec: row })}>
                派生
              </Button>
            </Tooltip>
            <Button type="link" size="small"
              onClick={() => patch.mutate({ id: row.id, next: row.status === 'active' ? 'draft' : 'active' })}>
              {row.status === 'active' ? '停用' : '生效'}
            </Button>
            <Button type="link" size="small" onClick={() => setHistorySpec(row)}>历史</Button>
            <Popconfirm
              title="删除这个 revision？"
              description="只删最新 revision；旧 revision 只读，生效中的需先停用。"
              disabled={!isLatest || row.status === 'active'}
              onConfirm={() => remove.mutate(row)}
            >
              <Tooltip title={row.status === 'active' ? '先停用生效中的 revision'
                : (!isLatest ? '旧 revision 只读' : '')}>
                <Button type="link" size="small" danger disabled={!isLatest || row.status === 'active'}>删除</Button>
              </Tooltip>
            </Popconfirm>
          </Space>
        )
      },
    },
  ]

  return (
    <div className="specs-page">
      <div className="section-head">
        <div>
          <h2>期望库</h2>
          <p className="muted">
            挂在桶上的场景期望：桶维度留空即通配。每次保存都是新 revision，旧版只读；同一条期望同时只有一个生效版。
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />}
          onClick={() => setEditor({ open: true, spec: null })}>
          新建期望
        </Button>
      </div>

      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          value={status}
          onChange={setStatus}
          style={{ width: 140 }}
          options={[
            { value: 'all', label: '全部状态' },
            { value: 'active', label: '生效' },
            { value: 'draft', label: '草稿' },
          ]}
        />
        <Input.Search placeholder="按名称 / 桶标签搜索" allowClear style={{ width: 260 }} onSearch={setQ} />
        {meta ? <Tag>{meta.schema}</Tag> : null}
      </Space>

      {listQuery.error ? <Alert type="error" showIcon message={errorMessage(listQuery.error)} style={{ marginBottom: 16 }} /> : null}

      {!listQuery.isLoading && groups.length === 0 ? (
        <Card bordered={false}>
          <Empty description="还没有期望。新建一条，把「这个桶下该怎么打」写下来。" />
        </Card>
      ) : null}

      {groups.map(([label, rows]) => (
        <Card
          key={label}
          bordered={false}
          className="spec-group"
          style={{ marginBottom: 16 }}
          title={<Space><AimOutlined /><span>{label}</span><Tag>{rows.length}</Tag></Space>}
        >
          <Table<SpotSpec>
            rowKey="id"
            size="middle"
            loading={listQuery.isLoading}
            columns={columns}
            dataSource={rows}
            pagination={false}
            scroll={{ x: 1400 }}
          />
        </Card>
      ))}

      <SpecEditorModal
        open={editor.open}
        meta={meta}
        spec={editor.spec}
        onClose={() => setEditor({ open: false, spec: null })}
      />
      <SpecHistoryModal spec={historySpec} meta={meta} onClose={() => setHistorySpec(null)} />
    </div>
  )
}
