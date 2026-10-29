import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, AutoComplete, Button, Card, Collapse, Descriptions, Divider,
  Form, Input, InputNumber, Modal, Radio, Select, Space, Switch, Table, Tag,
  Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CopyOutlined, DownloadOutlined, InboxOutlined, PlusOutlined,
  ReloadOutlined, StopOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, errorMessage } from '../api'
import type { Version } from '../types'
import { formatDate, shortHash } from '../lib/format'

const PROVIDERS = ['deepseek', 'openrouter', 'openai', 'anthropic', 'custom']
const KEY_REFS = ['AI_SERVER_API_KEY', 'AI_KEY_FLASH', 'AI_KEY_PRO', 'OPENROUTER_API_KEY']

interface FormValues {
  name: string
  provider: string
  baseUrl: string
  model: string
  apiKeyRef: string
  temperature: number | null
  topP: number | null
  maxTokens: number | null
  thinking: 'enabled' | 'disabled' | null
  reasoningEffort: 'none' | 'low' | 'high' | 'max' | null
  promptStyle: 'lean'
  tableDynamicsMode: 'current' | 'text' | 'none' | 'keep'
  tableDynamicsText?: string
  notes?: string
}

export default function VersionsPage() {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [includeArchived, setIncludeArchived] = useState(true)
  const [formOpen, setFormOpen] = useState(false)
  const [cloneSource, setCloneSource] = useState<Version | null>(null)
  const [form] = Form.useForm<FormValues>()
  const mode = Form.useWatch('tableDynamicsMode', form)

  const versions = useQuery<{ items: Version[] }>({
    queryKey: ['versions', includeArchived],
    queryFn: () => api.get(`/versions?includeArchived=${includeArchived ? 1 : 0}`),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['versions'] })
    queryClient.invalidateQueries({ queryKey: ['meta'] })
  }

  const importEnv = useMutation({
    mutationFn: () => api.post<{ version: Version; reused: boolean }>('/versions/import-env'),
    onSuccess: (data) => {
      message.success(data.reused ? `已有相同配置：${data.version.name}` : `已导入版本 ${data.version.name}`)
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const archive = useMutation({
    mutationFn: ({ id, archived }: { id: number; archived: boolean }) =>
      api.patch(`/versions/${id}`, { archived }),
    onSuccess: (_data, vars) => {
      message.success(vars.archived ? '版本已归档' : '版本已取消归档')
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const create = useMutation({
    mutationFn: (values: FormValues) => {
      if (cloneSource) {
        return api.post<Version>(`/versions/${cloneSource.id}/clone`, values)
      }
      return api.post<Version>('/versions', values)
    },
    onSuccess: (version) => {
      message.success(cloneSource ? `已克隆为 ${version.name}` : `已创建版本 ${version.name}`)
      setFormOpen(false)
      setCloneSource(null)
      form.resetFields()
      invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const openCreate = () => {
    setCloneSource(null)
    form.setFieldsValue({
      name: '',
      provider: 'deepseek',
      baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-flash',
      apiKeyRef: 'AI_SERVER_API_KEY',
      temperature: 0.2,
      topP: null,
      maxTokens: null,
      thinking: 'disabled',
      reasoningEffort: null,
      promptStyle: 'lean',
      tableDynamicsMode: 'current',
      tableDynamicsText: '',
      notes: '',
    })
    setFormOpen(true)
  }

  const openClone = (version: Version) => {
    setCloneSource(version)
    form.setFieldsValue({
      name: `${version.name}-copy`,
      provider: version.provider,
      baseUrl: version.baseUrl,
      model: version.model,
      apiKeyRef: version.apiKeyRef,
      temperature: version.temperature,
      topP: version.topP,
      maxTokens: version.maxTokens,
      thinking: version.thinking as FormValues['thinking'],
      reasoningEffort: version.reasoningEffort as FormValues['reasoningEffort'],
      promptStyle: 'lean',
      tableDynamicsMode: 'keep',
      tableDynamicsText: version.tableDynamics,
      notes: version.notes,
    })
    setFormOpen(true)
  }

  useEffect(() => {
    if (!formOpen) setCloneSource(null)
  }, [formOpen])

  const columns: ColumnsType<Version> = useMemo(() => [
    {
      title: '名称',
      dataIndex: 'name',
      width: 200,
      render: (name: string, version) => (
        <div className="version-name-cell">
          <div className="version-name-row">
            <span className="version-title">{name}</span>
            {version.archived ? <Tag className="mini-tag">归档</Tag> : null}
          </div>
          <div className="muted mono">{version.model}</div>
        </div>
      ),
    },
    {
      title: '配置',
      width: 190,
      render: (_: unknown, version) => (
        <div className="version-config-cell">
          <div>
            <Tag color="blue" className="mini-tag">{version.promptStyle}</Tag>
            <Tag className="mini-tag">{version.provider}</Tag>
            {version.thinking ? <Tag className="mini-tag">{version.thinking}</Tag> : null}
          </div>
          <div className="muted mono">T={version.temperature ?? '—'} · max={version.maxTokens ?? '—'}</div>
        </div>
      ),
    },
    {
      title: '桌风快照',
      dataIndex: 'tableDynamicsLength',
      width: 120,
      render: (length: number, version) => version.tableDynamicsLength ? (
        <Tooltip title={version.tableDynamicsSha || ''}>
          <span>{version.tableDynamicsLength} 字</span>
          <div className="muted mono">{shortHash(version.tableDynamicsSha)}</div>
        </Tooltip>
      ) : <span className="muted">无</span>,
    },
    {
      title: 'Key',
      dataIndex: 'hasKey',
      width: 70,
      render: (hasKey: boolean, version) => (
        <Tooltip title={version.apiKeyRef}>
          <Tag color={hasKey ? 'green' : 'red'}>{hasKey ? '已配置' : '缺失'}</Tag>
        </Tooltip>
      ),
    },
    {
      title: '可重跑',
      dataIndex: 'runnable',
      width: 80,
      render: (runnable: boolean, version) => runnable
        ? <Tag color="green">可重跑</Tag>
        : <Tooltip title={version.blockers.map((b) => b.message).join('；')}><Tag color="orange">不可用</Tag></Tooltip>,
    },
    { title: '任务', dataIndex: 'taskCount', width: 50 },
    {
      title: '最近使用',
      dataIndex: 'lastUsedAt',
      width: 100,
      render: (value: string | null) => <span className="muted">{value ? formatDate(value) : '—'}</span>,
    },
    {
      title: '操作',
      width: 150,
      render: (_: unknown, version) => (
        <Space size={2}>
          <Button type="link" size="small" icon={<CopyOutlined />} onClick={() => openClone(version)}>克隆</Button>
          <Button
            type="link"
            size="small"
            danger={!version.archived}
            icon={version.archived ? <DownloadOutlined /> : <StopOutlined />}
            onClick={() => {
              if (version.archived) {
                archive.mutate({ id: version.id, archived: false })
              } else {
                modal.confirm({
                  title: `归档 ${version.name}？`,
                  content: '归档后不再出现在重跑选择器，但历史对比仍可显示。配置本身不可修改。',
                  okText: '归档',
                  onOk: () => archive.mutate({ id: version.id, archived: true }),
                })
              }
            }}
          >
            {version.archived ? '取消归档' : '归档'}
          </Button>
        </Space>
      ),
    },
  ], [archive, modal])

  const expandedRow = (version: Version) => (
    <div className="version-expand">
      {version.blockers.length ? (
        <Alert
          type="warning"
          showIcon
          message="这个版本不能创建新任务"
          description={version.blockers.map((b) => b.message).join('；')}
        />
      ) : null}
      <Descriptions column={3} size="small" bordered>
        <Descriptions.Item label="provider">{version.provider}</Descriptions.Item>
        <Descriptions.Item label="baseUrl" span={2}><span className="mono">{version.baseUrl}</span></Descriptions.Item>
        <Descriptions.Item label="model"><span className="mono">{version.model}</span></Descriptions.Item>
        <Descriptions.Item label="API Key 变量"><span className="mono">{version.apiKeyRef}</span></Descriptions.Item>
        <Descriptions.Item label="Key 状态"><Tag color={version.hasKey ? 'green' : 'red'}>{version.hasKey ? '已配置' : '未配置'}</Tag></Descriptions.Item>
        <Descriptions.Item label="temperature">{version.temperature ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="top_p">{version.topP ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="max_tokens">{version.maxTokens ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="thinking">{version.thinking ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="reasoning_effort">{version.reasoningEffort ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="prompt_style">{version.promptStyle}</Descriptions.Item>
        <Descriptions.Item label="advice_version"><span className="mono">{version.adviceVersion}</span></Descriptions.Item>
        <Descriptions.Item label="reconstruct_version"><span className="mono">{version.reconstructVersion}</span></Descriptions.Item>
        <Descriptions.Item label="engine_commit">
          <span className="mono">{shortHash(version.engineCommit)}</span>
          {version.engineDirty ? <Tag className="mini-tag">dirty</Tag> : null}
        </Descriptions.Item>
        <Descriptions.Item label="config_hash" span={2}><span className="mono">{version.configHash}</span></Descriptions.Item>
        <Descriptions.Item label="table_dynamics_sha" span={3}>
          <span className="mono">{version.tableDynamicsSha || '—'}</span>
        </Descriptions.Item>
        <Descriptions.Item label="备注" span={3}>{version.notes || '—'}</Descriptions.Item>
      </Descriptions>
      {version.tableDynamicsLength ? (
        <Collapse
          ghost
          size="small"
          items={[{
            key: 'dyn',
            label: `桌风快照全文（${version.tableDynamicsLength} 字）`,
            children: <pre className="dynamics-pre">{version.tableDynamics}</pre>,
          }]}
        />
      ) : null}
    </div>
  )

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="eyebrow">AI VERSIONS</div>
          <h1>版本管理</h1>
          <p className="page-sub">
            版本是「模型 + 采样参数 + 提示词风格 + 语义版本 + 桌风快照」的不可变快照。
            创建后配置不可修改，要改只能克隆。
          </p>
        </div>
        <Space wrap>
          <label className="inline-switch">
            <Switch size="small" checked={includeArchived} onChange={setIncludeArchived} />
            显示归档
          </label>
          <Button icon={<ReloadOutlined />} onClick={() => versions.refetch()} loading={versions.isFetching}>刷新</Button>
          <Button icon={<InboxOutlined />} loading={importEnv.isPending} onClick={() => importEnv.mutate()}>
            从 .env 导入
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建版本</Button>
        </Space>
      </div>

      <Card className="table-card" bordered={false}>
        <Table<Version>
          rowKey="id"
          dataSource={versions.data?.items || []}
          columns={columns}
          loading={versions.isLoading}
          size="middle"
          scroll={{ x: 1010 }}
          pagination={false}
          expandable={{ expandedRowRender: expandedRow }}
          locale={{ emptyText: '还没有版本。点「从 .env 导入」用当前配置创建第一个。' }}
        />
      </Card>

      <Modal
        open={formOpen}
        onCancel={() => setFormOpen(false)}
        width={760}
        title={cloneSource ? `克隆版本：${cloneSource.name}` : '新建 AI 版本'}
        okText={cloneSource ? '创建克隆' : '创建版本'}
        confirmLoading={create.isPending}
        onOk={() => form.submit()}
        forceRender
      >
        <Form<FormValues>
          form={form}
          layout="vertical"
          onFinish={(values) => create.mutate(values)}
          requiredMark="optional"
        >
          <div className="form-grid">
            <Form.Item name="name" label="名称" rules={[{ required: true, message: '给版本一个名字' }]}>
              <Input placeholder="flash-lean-1023" />
            </Form.Item>
            <Form.Item name="provider" label="provider">
              <Select options={PROVIDERS.map((item) => ({ value: item, label: item }))} />
            </Form.Item>
          </div>
          <Form.Item name="baseUrl" label="Base URL" rules={[{ required: true, message: '需要接口地址' }]}>
            <Input placeholder="https://api.deepseek.com" />
          </Form.Item>
          <div className="form-grid">
            <Form.Item name="model" label="模型" rules={[{ required: true, message: '需要模型名' }]}>
              <Input placeholder="deepseek-flash" />
            </Form.Item>
            <Form.Item name="apiKeyRef" label="API Key 环境变量名" rules={[{ required: true }]}
              extra="只保存变量名；Key 从环境变量读取，不会写入数据库。">
              <AutoComplete options={KEY_REFS.map((item) => ({ value: item }))} />
            </Form.Item>
          </div>
          <Divider className="thin-divider" />
          <div className="form-grid-three">
            <Form.Item name="temperature" label="temperature">
              <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} placeholder="0.2" />
            </Form.Item>
            <Form.Item name="topP" label="top_p">
              <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} placeholder="留空=服务端默认" />
            </Form.Item>
            <Form.Item name="maxTokens" label="max_tokens">
              <InputNumber min={1} style={{ width: '100%' }} placeholder="留空=服务端默认" />
            </Form.Item>
          </div>
          <div className="form-grid-three">
            <Form.Item name="thinking" label="thinking">
              <Select allowClear options={[{ value: 'enabled' }, { value: 'disabled' }]} placeholder="留空" />
            </Form.Item>
            <Form.Item name="reasoningEffort" label="reasoning_effort">
              <Select allowClear options={['none', 'low', 'high', 'max'].map((value) => ({ value }))} placeholder="留空" />
            </Form.Item>
            <Form.Item name="promptStyle" label="提示词风格">
              <Radio.Group optionType="button" buttonStyle="solid">
                <Radio.Button value="lean">中文 lean（唯一）</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </div>
          <Divider className="thin-divider" />
          <Form.Item name="tableDynamicsMode" label="桌风快照">
            <Radio.Group>
              {!cloneSource ? <Radio value="current">使用当前 .table_dy</Radio> : null}
              {cloneSource ? <Radio value="keep">保持原版本快照</Radio> : null}
              <Radio value="text">粘贴文本</Radio>
              <Radio value="none">无桌风（对照）</Radio>
            </Radio.Group>
          </Form.Item>
          {mode === 'text' ? (
            <Form.Item name="tableDynamicsText" label="桌风文本">
              <Input.TextArea autoSize={{ minRows: 5, maxRows: 10 }} placeholder="当前是新手局，桌上基本都是松弱玩家…" />
            </Form.Item>
          ) : null}
          <Form.Item name="notes" label="备注">
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} placeholder="为什么建这个版本 / 想验证什么" />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            message="版本一旦创建，配置字段不可修改；要改请克隆一个新版本。"
          />
        </Form>
      </Modal>
    </div>
  )
}
