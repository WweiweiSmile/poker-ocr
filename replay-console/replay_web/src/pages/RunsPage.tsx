import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Form, Modal, Select, Space,
  Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type {
  ScoreRun, ScoreRunEstimate, ScoreRunList, ScoreRunMeta,
} from '../types'
import { RUN_STATUS, formatDate } from '../lib/format'

export function scoreText(value: number | null | undefined) {
  if (value === null || value === undefined) return '—'
  return (Math.round(value * 10) / 10).toFixed(1)
}

export function rateText(value: number | null | undefined) {
  if (value === null || value === undefined) return '—'
  return `${(Math.round(value * 1000) / 10).toFixed(1)}%`
}

export function costText(estimate: ScoreRunEstimate) {
  if (estimate.cost === null || estimate.cost === undefined) {
    return estimate.priceMissing ? '未知单价（未配 REPLAY_PRICE_TABLE）' : '—'
  }
  return `¥${estimate.cost.toFixed(4)}`
}

export default function RunsPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [caseSetId, setCaseSetId] = useState<number | null>(null)
  const [versionId, setVersionId] = useState<number | null>(null)
  const [form] = Form.useForm()

  const meta = useQuery<ScoreRunMeta>({
    queryKey: ['score-runs', 'meta'],
    queryFn: () => api.get('/score-runs/meta'),
  })
  const runs = useQuery<ScoreRunList>({
    queryKey: ['score-runs'],
    queryFn: () => api.get('/score-runs?limit=50'),
    refetchInterval: 5_000,
  })
  const estimate = useQuery<ScoreRunEstimate>({
    queryKey: ['score-runs', 'estimate', caseSetId, versionId],
    queryFn: () => api.post('/score-runs/estimate', { caseSetId, versionId }),
    enabled: open && !!caseSetId && !!versionId,
    retry: false,
  })
  const create = useMutation({
    mutationFn: () => api.post<ScoreRun>('/score-runs', { caseSetId, versionId }),
    onSuccess: (run) => {
      message.success(`评分运行 #${run.id} 已创建`)
      setOpen(false)
      setCaseSetId(null)
      setVersionId(null)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['score-runs'] })
      navigate(`/runs/${run.id}`)
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const setOptions = useMemo(
    () => (meta.data?.caseSets || []).map((item) => ({
      value: item.id,
      disabled: !item.frozen,
      label: `${item.name} v${item.revision} · ${item.caseCount} 案例 / P0 ${item.p0Count}${item.frozen ? '' : '（未冻结）'}`,
    })), [meta.data])

  const versionOptions = useMemo(
    () => (meta.data?.versions || []).map((item) => ({
      value: item.id,
      disabled: !item.runnable,
      label: `${item.name} · ${item.model || '—'}${item.runnable ? '' : '（不可运行）'}`,
    })), [meta.data])

  const columns: ColumnsType<ScoreRun> = [
    {
      title: '运行', key: 'id', width: 90,
      render: (_: unknown, row) => (
        <Button type="link" size="small" onClick={() => navigate(`/runs/${row.id}`)}>
          #{row.id}
        </Button>
      ),
    },
    {
      title: '状态', dataIndex: 'status', width: 110,
      render: (status: ScoreRun['status'], row) => {
        const meta = RUN_STATUS[status] || { label: status, color: 'default' }
        return (
          <Space size={4}>
            <Tag color={meta.color}>{meta.label}</Tag>
            {row.cancelRequested && status !== 'partial' && status !== 'succeeded'
              ? <Tag color="default">已请求中断</Tag> : null}
          </Space>
        )
      },
    },
    {
      title: '案例集', key: 'caseSet',
      render: (_: unknown, row) => (
        <span>{row.caseSet.name || '—'} <Tag>v{row.caseSet.revision}</Tag></span>
      ),
    },
    {
      title: '版本', key: 'version',
      render: (_: unknown, row) => (
        <Tooltip title={row.version.model || ''}>
          <span>{row.version.name || '—'}</span>
        </Tooltip>
      ),
    },
    {
      title: '进度', key: 'progress', width: 100,
      render: (_: unknown, row) => `${row.doneCases}/${row.totalCases}`,
    },
    {
      title: '总分', dataIndex: 'score', width: 80,
      render: (value: number | null) => scoreText(value),
    },
    {
      title: 'P0 通过率', dataIndex: 'p0PassRate', width: 110,
      render: (value: number | null) => rateText(value),
    },
    {
      title: '覆盖率', dataIndex: 'coverageRate', width: 90,
      render: (value: number | null) => rateText(value),
    },
    {
      title: '创建', dataIndex: 'createdAt', width: 130,
      render: (value: string | null) => formatDate(value),
    },
  ]

  return (
    <div className="page">
      <Card
        title="评分运行"
        extra={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => runs.refetch()}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              新建运行
            </Button>
          </Space>
        )}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="一次运行 = 一个冻结案例集 × 一个版本 × 一套 spec。P0 案例额外再跑 2 次用于稳定性维，其余案例跑 1 次。"
        />
        <Table<ScoreRun>
          rowKey="id"
          size="small"
          loading={runs.isLoading}
          dataSource={runs.data?.items || []}
          columns={columns}
          pagination={false}
        />
      </Card>

      <Modal
        title="新建评分运行"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => create.mutate()}
        okText="创建并运行"
        confirmLoading={create.isPending}
        okButtonProps={{ disabled: !estimate.data?.runnable }}
        width={720}
      >
        <Form form={form} layout="vertical">
          <Form.Item label="案例集（只能选已冻结的）" required>
            <Select
              value={caseSetId}
              options={setOptions}
              placeholder="选择冻结案例集"
              onChange={(value: number) => setCaseSetId(value)}
            />
          </Form.Item>
          <Form.Item label="版本" required>
            <Select
              value={versionId}
              options={versionOptions}
              placeholder="选择版本"
              onChange={(value: number) => setVersionId(value)}
            />
          </Form.Item>
        </Form>
        {estimate.isError ? (
          <Alert type="error" showIcon message={errorMessage(estimate.error)} />
        ) : null}
        {estimate.data ? (
          <Descriptions size="small" bordered column={2} style={{ marginTop: 8 }}>
            <Descriptions.Item label="案例数">{estimate.data.cases}</Descriptions.Item>
            <Descriptions.Item label="P0 案例">{estimate.data.p0}</Descriptions.Item>
            <Descriptions.Item label="预计调用次数">{estimate.data.calls}</Descriptions.Item>
            <Descriptions.Item label="预计 token">
              {Math.round(estimate.data.tokensIn)} in / {Math.round(estimate.data.tokensOut)} out
              <Tag style={{ marginLeft: 6 }}>
                {estimate.data.tokensSource === 'history' ? '按历史均值' : '按默认值'}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="预计成本">{costText(estimate.data)}</Descriptions.Item>
            <Descriptions.Item label="判定版本">
              {meta.data?.judgeVersion || '—'} / {meta.data?.reconstructVersion || '—'}
            </Descriptions.Item>
          </Descriptions>
        ) : null}
        {estimate.data && !estimate.data.runnable ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 8 }}
            message={!estimate.data.frozen
              ? '案例集还没冻结：先到案例集详情冻结成 revision 再建运行。'
              : `${estimate.data.casesWithoutSpec.length} 个案例没有匹配的 active spec，先补期望库。`}
          />
        ) : null}
      </Modal>
    </div>
  )
}
