import { useState } from 'react'
import {
  Alert, App, Button, Card, Form, Input, Modal, Select, Space, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type { HandSet, HandSetList } from '../types'
import { formatDate } from '../lib/format'

const PURPOSE: Record<string, string> = {
  regression: '回归', exploration: '探索', golden: '金标准', coverage: '覆盖',
}

export default function HandSetsPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const sets = useQuery<HandSetList>({
    queryKey: ['hand-sets'],
    queryFn: () => api.get('/hand-sets'),
  })
  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.post<HandSet>('/hand-sets', values),
    onSuccess: (set) => {
      message.success(`手牌集 #${set.id} 已创建`)
      setOpen(false)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['hand-sets'] })
      navigate(`/sets/${set.id}`)
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: ColumnsType<HandSet> = [
    {
      title: '手牌集', dataIndex: 'name', key: 'name',
      render: (_: string, row) => (
        <div>
          <Button type="link" className="cell-link" onClick={() => navigate(`/sets/${row.id}`)}>
            {row.name}
          </Button>
          <div className="muted cell-sub">{row.description || '—'}</div>
        </div>
      ),
    },
    {
      title: '用途', dataIndex: 'purpose', width: 90,
      render: (value: string) => <Tag>{PURPOSE[value] || value}</Tag>,
    },
    {
      title: '规模', width: 150,
      render: (_: unknown, row) => (
        <span className="mono">{row.handCount} 手 · {row.coverage?.decisions || 0} 决策</span>
      ),
    },
    {
      title: 'Prompt 覆盖', width: 130,
      render: (_: unknown, row) => (
        <span className="mono">{((row.coverage?.promptCoverage || 0) * 100).toFixed(0)}%</span>
      ),
    },
    {
      title: 'Revision', dataIndex: 'revision', width: 80,
      render: (value: number) => <span className="mono">v{value}</span>,
    },
    {
      title: '更新', dataIndex: 'updatedAt', width: 140,
      render: (value: string | null) => <span className="muted">{formatDate(value, false)}</span>,
    },
  ]

  return (
    <div className="hand-sets-page">
      <div className="section-head">
        <div>
          <h2>手牌集</h2>
          <p className="muted">
            人工挑选的回归集。实验创建时冻结 revision；之后改集合不影响旧实验。
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          新建手牌集
        </Button>
      </div>

      {sets.error ? <Alert type="error" showIcon message={errorMessage(sets.error)} /> : null}

      <Card bordered={false}>
        <Table<HandSet>
          rowKey="id"
          size="middle"
          loading={sets.isLoading}
          columns={columns}
          dataSource={sets.data?.items || []}
          pagination={false}
          locale={{
            emptyText: '还没有手牌集。新建一个，然后从手牌库把「典型/边界/回归」手牌加进来。',
          }}
        />
      </Card>

      <Modal
        open={open}
        title="新建手牌集"
        okText="创建"
        cancelText="取消"
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
      >
        <Form form={form} layout="vertical" initialValues={{ purpose: 'regression' }}
          onFinish={(values) => create.mutate(values)}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '给手牌集起个名字' }]}>
            <Input placeholder="例如：我的回归集 2026-11" maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={3} placeholder="这组手牌覆盖什么场景、为什么选它们" />
          </Form.Item>
          <Form.Item name="purpose" label="用途">
            <Select options={Object.entries(PURPOSE).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
