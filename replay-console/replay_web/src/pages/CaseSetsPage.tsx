import { useState } from 'react'
import {
  Alert, App, Button, Card, Form, Input, Modal, Select, Space, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { CaseSeedReport, CaseSet, CaseSetList } from '../types'
import { formatDate } from '../lib/format'

export const CASE_SET_PURPOSE: Record<string, string> = {
  golden: '金标准', regression: '回归', exploration: '探索', coverage: '覆盖',
}

export function p0RatioText(ratio: number) {
  return `${(Math.round(ratio * 1000) / 10).toFixed(1)}%`
}

export default function CaseSetsPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [seedOpen, setSeedOpen] = useState(false)
  const [seedReport, setSeedReport] = useState<CaseSeedReport | null>(null)
  const [form] = Form.useForm()

  const sets = useQuery<CaseSetList>({
    queryKey: ['case-sets'],
    queryFn: () => api.get('/case-sets'),
  })
  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.post<CaseSet>('/case-sets', values),
    onSuccess: (set) => {
      message.success(`案例集 #${set.id} 已创建`)
      setOpen(false)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['case-sets'] })
      navigate(`/case-sets/${set.id}`)
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const seed = useMutation({
    mutationFn: (dryRun: boolean) => api.post<CaseSeedReport>('/case-sets/seed', { dryRun }),
    onSuccess: (report) => {
      setSeedReport(report)
      setSeedOpen(true)
      queryClient.invalidateQueries({ queryKey: ['case-sets'] })
      queryClient.invalidateQueries({ queryKey: ['specs'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<CaseSet> = [
    {
      title: '案例集', key: 'name',
      render: (_: unknown, row) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => navigate(`/case-sets/${row.id}`)}>
            {row.name}
          </Button>
          <div className="muted cell-sub mono">
            #{row.id} · v{row.revision} · {formatDate(row.createdAt, false)}
          </div>
        </div>
      ),
    },
    {
      title: '用途', dataIndex: 'purpose', width: 90,
      render: (value: string) => <Tag>{CASE_SET_PURPOSE[value] || value}</Tag>,
    },
    {
      title: '规模', key: 'size', width: 120,
      render: (_: unknown, row) => <span className="mono">{row.caseCount} 条案例</span>,
    },
    {
      title: 'P0', key: 'p0', width: 150,
      render: (_: unknown, row) => (
        <span className="mono">
          {row.p0Count} 条 · {p0RatioText(row.p0Ratio || 0)}
        </span>
      ),
    },
    {
      title: '状态', key: 'frozen', width: 110,
      render: (_: unknown, row) => row.frozen
        ? <Tag color="green">已冻结</Tag>
        : <Tag>未冻结</Tag>,
    },
    {
      title: '', key: 'action', width: 90,
      render: (_: unknown, row) => (
        <Button size="small" onClick={() => navigate(`/case-sets/${row.id}`)}>打开</Button>
      ),
    },
  ]

  const report = seedReport
  return (
    <div className="case-sets-page">
      <div className="section-head">
        <div>
          <h2>案例集</h2>
          <p className="muted">
            评分输入单元：案例 = 一个决策点 + 写死的期望。冻结后案例不可改，要改就建新版本。
          </p>
        </div>
        <Space>
          <Button onClick={() => seed.mutate(true)} loading={seed.isPending && seed.variables === true}>
            导入 seed 预演
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            新建案例集
          </Button>
        </Space>
      </div>

      {sets.error ? <Alert type="error" showIcon message={errorMessage(sets.error)} /> : null}

      <Card bordered={false}>
        <Table<CaseSet>
          rowKey="id"
          size="middle"
          loading={sets.isLoading}
          columns={columns}
          dataSource={sets.data?.items || []}
          pagination={false}
          locale={{
            emptyText: '还没有案例集。新建一个，或把 tools/ab_set.json 导入成 seed-2026-11。',
          }}
        />
      </Card>

      <Modal
        open={open}
        title="新建案例集"
        okText="创建"
        cancelText="取消"
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
      >
        <Form form={form} layout="vertical" initialValues={{ purpose: 'exploration' }}
          onFinish={(values) => create.mutate(values)}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '给案例集起个名字' }]}>
            <Input placeholder="例如：P0 回归 2026-11" maxLength={100} />
          </Form.Item>
          <Form.Item name="purpose" label="用途">
            <Select options={Object.entries(CASE_SET_PURPOSE).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={seedOpen}
        width={720}
        title={report?.dryRun ? 'seed 预演（未写库）' : 'seed 导入结果'}
        okText={report?.dryRun ? '正式导入' : '关闭'}
        cancelText="关闭"
        confirmLoading={seed.isPending}
        onOk={() => {
          if (!report?.dryRun) { setSeedOpen(false); return }
          seed.mutate(false)
        }}
        onCancel={() => setSeedOpen(false)}
      >
        {report ? (
          <Space direction="vertical" style={{ width: '100%' }} size={10}>
            <div className="mono muted">{report.source} → {report.setName}</div>
            <div>
              <Tag color="blue">spec 新建 {report.specs.created.length + report.specs.wouldCreate.length}</Tag>
              <Tag>spec 复用 {report.specs.reused.length}</Tag>
              <Tag color="green">案例新建 {report.cases.created.length + report.cases.wouldCreate.length}</Tag>
              <Tag>案例已存在 {report.cases.existing.length}</Tag>
              <Tag color="orange">案例未建 {report.cases.skipped.length + report.cases.skippedPlan.length}</Tag>
            </div>
            {report.cases.skipped.length ? (
              <div>
                <div className="muted">缺数据未能建案例：</div>
                {report.cases.skipped.map((item) => (
                  <div key={item.seedId} className="mono muted">
                    {item.seedId} · {item.code || '—'} · {item.reason}
                  </div>
                ))}
              </div>
            ) : null}
            {report.cases.skippedPlan.length ? (
              <div>
                <div className="muted">按设计只建 spec、不建案例：</div>
                {report.cases.skippedPlan.map((item) => (
                  <div key={item.seedId} className="mono muted">{item.seedId} · {item.reason}</div>
                ))}
              </div>
            ) : null}
            {report.dryRun ? null : (
              <Button onClick={() => seed.mutate(true)}>再预演一次（验证幂等）</Button>
            )}
          </Space>
        ) : <div className="muted">没有结果</div>}
      </Modal>
    </div>
  )
}
