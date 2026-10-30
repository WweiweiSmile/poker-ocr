import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Form, Input, InputNumber, Modal, Select, Space,
  Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { Experiment, Version } from '../types'
import { formatDate } from '../lib/format'

const STATUS_COLORS: Record<string, string> = {
  draft: 'default', queued: 'blue', running: 'processing',
  done: 'green', failed: 'red', archived: 'default',
}
const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '排队', running: '运行中',
  done: '完成', failed: '失败', archived: '已归档',
}

export default function ExperimentsPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const experiments = useQuery<Experiment[]>({
    queryKey: ['experiments'],
    queryFn: () => api.get('/experiments'),
    refetchInterval: 5_000,
  })
  const versions = useQuery<{ items: Version[] }>({
    queryKey: ['versions', 'all'],
    queryFn: () => api.get('/versions?includeArchived=1'),
  })
  const versionMap = useMemo(() => {
    const map: Record<number, Version> = {}
    for (const version of versions.data?.items || []) map[version.id] = version
    return map
  }, [versions.data])

  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.post<Experiment>('/experiments', values),
    onSuccess: (experiment) => {
      message.success(`实验 #${experiment.id} 已创建，手牌集已冻结`)
      setOpen(false)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
      navigate(`/experiments/${experiment.id}`)
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<Experiment> = [
    {
      title: '实验', dataIndex: 'name', key: 'name',
      render: (_: string, row: Experiment) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => navigate(`/experiments/${row.id}`)}>
            {row.name}
          </Button>
          {row.hypothesis ? <div className="muted cell-sub">{row.hypothesis}</div> : null}
        </div>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (status: string) => (
        <Tag color={STATUS_COLORS[status] || 'default'}>{STATUS_LABELS[status] || status}</Tag>
      ),
    },
    {
      title: '版本', key: 'versions', width: 260,
      render: (_: unknown, row: Experiment) => (
        <div className="version-pair">
          <div>A · {versionMap[row.baselineVersionId]?.name || `#${row.baselineVersionId}`}</div>
          <div>B · {versionMap[row.variantVersionId]?.name || `#${row.variantVersionId}`}</div>
        </div>
      ),
    },
    {
      title: '手牌集', key: 'hands', width: 160,
      render: (_: unknown, row: Experiment) => (
        <span className="mono">
          {row.hands.total} 手 · dev {row.hands.dev} / holdout {row.hands.holdout}
        </span>
      ),
    },
    {
      title: '任务', dataIndex: 'jobId', key: 'jobId', width: 90,
      render: (jobId: number | null) => jobId
        ? <Button type="link" size="small" onClick={() => navigate(`/jobs/${jobId}`)}>#{jobId}</Button>
        : <span className="muted">未跑</span>,
    },
    {
      title: '创建', dataIndex: 'createdAt', key: 'createdAt', width: 150,
      render: (value: string | null) => <span className="muted">{formatDate(value, false)}</span>,
    },
    {
      title: '', key: 'action', width: 100,
      render: (_: unknown, row: Experiment) => (
        <Button size="small" icon={<ThunderboltOutlined />} onClick={() => navigate(`/experiments/${row.id}`)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <div className="experiments-page">
      <div className="section-head">
        <div>
          <h2>A/B 实验</h2>
          <p className="muted">
            冻结手牌集 → baseline/variant 各跑一遍 → 按 dev/holdout 看一致率与复盘结论
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          新建实验
        </Button>
      </div>

      {experiments.error ? (
        <Alert type="error" showIcon message={errorMessage(experiments.error)} />
      ) : null}

      <Card bordered={false}>
        <Table<Experiment>
          rowKey="id"
          size="middle"
          loading={experiments.isLoading}
          columns={columns}
          dataSource={experiments.data || []}
          pagination={false}
          locale={{ emptyText: '还没有实验。新建一个，把「当前桌统计」版本和「无统计」版本放在同一批手牌上对比。' }}
        />
      </Card>

      <Modal
        open={open}
        title="新建离线 A/B 实验"
        okText="创建并冻结手牌集"
        cancelText="取消"
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={create.isPending}
        width={620}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ scope: 'filter', source: 'live', devRatio: 0.7, repeat: 3 }}
          onFinish={(values) => {
            const source = values.source as string
            create.mutate({
              name: values.name,
              hypothesis: values.hypothesis || null,
              baselineVersionId: values.baselineVersionId,
              variantVersionId: values.variantVersionId,
              scope: 'filter',
              filter: source === 'all' ? {} : { source },
              devRatio: values.devRatio,
              repeat: values.repeat,
            })
          }}
        >
          <Form.Item name="name" label="实验名" rules={[{ required: true, message: '给实验起个名字' }]}>
            <Input placeholder="例如：当前桌统计 vs 无统计" maxLength={100} />
          </Form.Item>
          <Form.Item name="hypothesis" label="假设（可选）">
            <Input.TextArea rows={2} placeholder="例如：注入本手在局对手的统计后，河牌薄价值更少" />
          </Form.Item>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item name="baselineVersionId" label="A / baseline" rules={[{ required: true }]} style={{ flex: 1 }}>
              <Select
                placeholder="选择版本"
                options={(versions.data?.items || []).filter((v) => !v.archived).map((v) => ({
                  value: v.id,
                  label: `${v.name} · ${v.model} · ${v.contextMode || 'none'}`,
                }))}
              />
            </Form.Item>
            <Form.Item name="variantVersionId" label="B / variant" rules={[{ required: true }]} style={{ flex: 1 }}>
              <Select
                placeholder="选择版本"
                options={(versions.data?.items || []).filter((v) => !v.archived).map((v) => ({
                  value: v.id,
                  label: `${v.name} · ${v.model} · ${v.contextMode || 'none'}`,
                }))}
              />
            </Form.Item>
          </Space>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item name="source" label="手牌来源" style={{ flex: 1 }}>
              <Select options={[
                { value: 'live', label: '导入·实时（推荐）' },
                { value: 'review', label: '导入·复盘' },
                { value: 'db', label: '复盘库' },
                { value: 'all', label: '全部' },
              ]} />
            </Form.Item>
            <Form.Item name="devRatio" label="dev 比例" style={{ flex: 1 }}>
              <InputNumber min={0.1} max={0.9} step={0.05} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="repeat" label="每点重复" style={{ flex: 1 }}>
              <InputNumber min={1} max={5} style={{ width: '100%' }} />
            </Form.Item>
          </Space>
          <Alert
            type="info"
            showIcon
            message="切分按手牌指纹，不按决策点；创建后手牌集冻结，跑完不漂移。"
          />
        </Form>
      </Modal>
    </div>
  )
}
