import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Empty, Form, Input, InputNumber, Modal, Select,
  Space, Statistic, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowLeftOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { CaseItem, CaseSet, Meta, SpotSpecList } from '../types'
import { formatDate } from '../lib/format'
import { CASE_SET_PURPOSE, p0RatioText } from './CaseSetsPage'

const STREET_NAMES: Record<string, string> = {
  preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌',
}

export default function CaseSetDetailPage() {
  const { setId } = useParams()
  const id = Number(setId)
  const navigate = useNavigate()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [sampleOpen, setSampleOpen] = useState(false)
  const [form] = Form.useForm()

  const set = useQuery<CaseSet>({
    queryKey: ['case-set', id],
    queryFn: () => api.get(`/case-sets/${id}`),
    enabled: Number.isFinite(id),
  })
  const meta = useQuery<Meta>({ queryKey: ['meta'], queryFn: () => api.get('/meta') })
  const specs = useQuery<SpotSpecList>({
    queryKey: ['specs', 'all'],
    queryFn: () => api.get('/specs?pageSize=200'),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['case-set', id] })
    queryClient.invalidateQueries({ queryKey: ['case-sets'] })
  }

  const freeze = useMutation({
    mutationFn: () => api.post<CaseSet>(`/case-sets/${id}/freeze`),
    onSuccess: (result) => {
      message.success(`v${result.revision} 已冻结，之后要改就建新版本`)
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const newRevision = useMutation({
    mutationFn: () => api.post<CaseSet>(`/case-sets/${id}/revisions`, {}),
    onSuccess: (result) => {
      message.success(`已复制成 v${result.revision}`)
      invalidate()
      navigate(`/case-sets/${result.id}`)
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const patchCase = useMutation({
    mutationFn: (payload: { caseId: number; body: Record<string, unknown> }) =>
      api.patch<CaseSet>(`/case-sets/${id}/cases/${payload.caseId}`, payload.body),
    onSuccess: () => {
      message.success('已更新案例')
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const removeCase = useMutation({
    mutationFn: (caseId: number) => api.delete<CaseSet>(`/case-sets/${id}/cases/${caseId}`),
    onSuccess: () => {
      message.success('已删除案例')
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const sample = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const bucket: Record<string, string[]> = {}
      for (const dimension of meta.data?.buckets || []) {
        const value = values[dimension.key]
        if (value) bucket[dimension.key] = [String(value)]
      }
      return api.post<CaseSet>(`/case-sets/${id}/sample`, {
        bucket, count: Number(values.count), priority: Number(values.priority ?? 1),
      })
    },
    onSuccess: (result) => {
      const created = result.created ?? 0
      const noSpec = result.noSpec || []
      message.success(`补样完成：新增 ${created} 条` + (noSpec.length ? `；${noSpec.length} 条没匹配到期望` : ''))
      setSampleOpen(false)
      form.resetFields()
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const specOptions = useMemo(
    () => (specs.data?.items || []).map((spec) => ({
      value: spec.id, label: `#${spec.id} ${spec.name}`,
    })),
    [specs.data],
  )

  const data = set.data
  const frozen = !!data?.frozen
  const columns: ColumnsType<CaseItem> = [
    {
      title: '决策点', key: 'decision',
      render: (_: unknown, row) => (
        <div>
          <Tag color="blue">{row.decisionKey}</Tag>
          <span className="mono">{STREET_NAMES[row.street || ''] || row.street || ''}</span>
          <div className="muted cell-sub mono">
            {row.hand?.heroPosition || '—'} · {row.hand?.heroCards || '—'} · {row.hand?.board || '—'}
          </div>
        </div>
      ),
    },
    {
      title: '期望', key: 'spec', width: 260,
      render: (_: unknown, row) => frozen ? (
        <span>{row.specName || '（spec 已删除）'}</span>
      ) : (
        <Select
          size="small"
          style={{ width: '100%' }}
          value={row.specId ?? undefined}
          options={specOptions}
          showSearch
          optionFilterProp="label"
          onChange={(value) => patchCase.mutate({ caseId: row.id, body: { specId: value } })}
        />
      ),
    },
    {
      title: '优先级', key: 'priority', width: 110,
      render: (_: unknown, row) => frozen ? (
        row.p0 ? <Tag color="red">P0</Tag> : <span className="mono">P{row.priority}</span>
      ) : (
        <Select
          size="small"
          value={row.priority}
          style={{ width: 90 }}
          options={[0, 1, 2, 3].map((value) => ({
            value, label: value === 0 ? 'P0' : `P${value}`,
          }))}
          onChange={(value) => patchCase.mutate({ caseId: row.id, body: { priority: value } })}
        />
      ),
    },
    {
      title: '桶', key: 'bucket', width: 240,
      render: (_: unknown, row) => row.bucketLabel
        ? <span className="muted">{row.bucketLabel}</span>
        : <span className="muted">桶未落库</span>,
    },
    {
      title: '备注', dataIndex: 'note', key: 'note',
      render: (value: string) => <span className="muted">{value || '—'}</span>,
    },
    {
      title: '', key: 'action', width: 70,
      render: (_: unknown, row) => (
        <Button
          size="small" danger icon={<DeleteOutlined />} disabled={frozen}
          onClick={() => modal.confirm({
            title: '删除这条案例？',
            content: `${row.decisionKey} · ${row.specName || 'spec 已删除'}`,
            okText: '删除', cancelText: '取消', okButtonProps: { danger: true },
            onOk: () => removeCase.mutate(row.id),
          })}
        />
      ),
    },
  ]

  if (set.error) return <Alert type="error" showIcon message={errorMessage(set.error)} />
  if (!data) return <Card loading bordered={false} />

  const stats = data.stats
  return (
    <div className="case-set-detail">
      <div className="detail-back">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/case-sets')}>案例集</Button>
        <span className="mono muted">#{data.id} · v{data.revision}</span>
        {data.frozen ? <Tag color="green">已冻结</Tag> : <Tag>未冻结</Tag>}
      </div>

      <Card className="hand-hero" bordered={false}>
        <div className="hand-hero-main">
          <div>
            <div className="hand-hero-title">{data.name}</div>
            <div className="hand-hero-sub">
              <span>{CASE_SET_PURPOSE[data.purpose] || data.purpose}</span>
              <span>{data.caseCount} 条案例</span>
              <span>P0 {data.p0Count} 条 · {p0RatioText(data.p0Ratio || 0)}</span>
              <span>{data.frozenAt ? `冻结于 ${formatDate(data.frozenAt, false)}` : `创建于 ${formatDate(data.createdAt, false)}`}</span>
            </div>
          </div>
          <Space>
            <Button
              icon={<PlusOutlined />} disabled={frozen}
              onClick={() => { form.resetFields(); setSampleOpen(true) }}
            >
              按桶补样
            </Button>
            <Button
              onClick={() => freeze.mutate()} loading={freeze.isPending} disabled={frozen}
            >
              冻结 v{data.revision}
            </Button>
            <Button type="primary" onClick={() => newRevision.mutate()} loading={newRevision.isPending}>
              建新版本
            </Button>
          </Space>
        </div>
      </Card>

      {frozen ? (
        <Alert
          type="info" showIcon
          message={`v${data.revision} 已冻结：案例不能再增删改。要调整就点「建新版本」复制一份 v${data.revision + 1}。`}
        />
      ) : null}

      <div className="set-stats">
        <Card bordered={false}><Statistic title="案例" value={stats?.total ?? data.caseCount} /></Card>
        <Card bordered={false}><Statistic title="P0 比例" value={p0RatioText(stats?.p0Ratio ?? data.p0Ratio ?? 0)} /></Card>
        <Card bordered={false}>
          <Statistic title="按街" value={stats?.byStreet?.length || 0} suffix="条街" />
          <div className="muted">
            {(stats?.byStreet || []).map((item) => (
              <Tag key={item.street} className="mini-tag">
                {STREET_NAMES[item.street] || item.street} {item.count}
              </Tag>
            ))}
          </div>
        </Card>
        <Card bordered={false}>
          <Statistic title="覆盖桶" value={stats?.byBucket?.length || 0} suffix="个桶" />
          <div className="muted">
            {(stats?.byBucket || []).slice(0, 6).map((item) => (
              <Tag key={item.label} className="mini-tag">{item.label} {item.count}</Tag>
            ))}
          </div>
        </Card>
      </div>

      <Card bordered={false}>
        <div className="card-title">
          案例 <Tag className="mini-tag">{data.items?.length || 0}</Tag>
          <span className="muted advice-hint">案例把决策点和写死的期望绑在一起；之后加期望不影响已有案例</span>
        </div>
        <Table<CaseItem>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={data.items || []}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有案例。去手牌详情建，或「按桶补样」。" /> }}
        />
      </Card>

      <Modal
        open={sampleOpen}
        title={`按桶补样 · v${data.revision}`}
        okText="补样" cancelText="取消"
        confirmLoading={sample.isPending}
        onCancel={() => setSampleOpen(false)}
        onOk={() => form.submit()}
      >
        <Form form={form} layout="vertical" initialValues={{ count: 10, priority: 1 }}
          onFinish={(values) => sample.mutate(values)}>
          {(meta.data?.buckets || []).map((dimension) => (
            <Form.Item key={dimension.key} name={dimension.key} label={dimension.label}>
              <Select
                allowClear placeholder="不限"
                options={dimension.options.map((option) => ({ value: option.value, label: option.label }))}
              />
            </Form.Item>
          ))}
          <Form.Item name="count" label="数量" rules={[{ required: true }]}>
            <InputNumber min={1} max={100} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="priority" label="优先级">
            <Select options={[0, 1, 2, 3].map((value) => ({ value, label: value === 0 ? 'P0' : `P${value}` }))} />
          </Form.Item>
          <div className="muted">
            从存档按时间倒序抽符合条件的决策点，自动挑最具体的 active 期望；已在本案例集里的决策点会跳过。
          </div>
        </Form>
      </Modal>
    </div>
  )
}
