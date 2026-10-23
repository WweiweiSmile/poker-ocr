import {
  Alert, App, Button, Card, Collapse, Descriptions, Empty, Progress, Space,
  Statistic, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowLeftOutlined, DownloadOutlined, ReloadOutlined, StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, downloadExport, errorMessage } from '../api'
import type { JobDetail, JobFailure, JobHand } from '../types'
import { BoardCards, HandCards, RunStatusTag } from '../components/CardChips'
import { formatDate, scopeLabel, shortHash } from '../lib/format'

const HAND_STATUS: Record<string, { label: string; color: string }> = {
  pending: { label: '等待', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  done: { label: '完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '跳过', color: 'default' },
  invalid: { label: '不可重跑', color: 'error' },
}

function scopeText(job: JobDetail): string {
  return `${scopeLabel(job.scope)} · ${job.handCount} 手 · ${job.totalDecisions} 个决策点`
}

export default function JobDetailPage() {
  const { jobId } = useParams()
  const id = Number(jobId)
  const navigate = useNavigate()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()

  const job = useQuery<JobDetail>({
    queryKey: ['job', id],
    queryFn: () => api.get(`/runs/${id}`),
    enabled: Number.isFinite(id),
    refetchInterval: (query) => {
      const data = query.state.data
      return data && (data.status === 'running' || data.status === 'queued') ? 1000 : false
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.post(`/runs/${id}/cancel`),
    onSuccess: () => {
      message.success('已请求取消，当前调用完成后停止')
      queryClient.invalidateQueries({ queryKey: ['job', id] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const retry = useMutation({
    mutationFn: () => api.post<{ jobId: number }>(`/runs/${id}/retry-failed`),
    onSuccess: (data) => {
      message.success(`已创建重试任务 #${data.jobId}`)
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      navigate(`/jobs/${data.jobId}`)
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const exportData = async (format: 'csv' | 'markdown') => {
    try {
      await downloadExport({ jobId: id, format })
    } catch (error) {
      message.error(errorMessage(error))
    }
  }

  if (job.error) {
    return <Alert type="error" showIcon message="任务加载失败" description={errorMessage(job.error)} />
  }
  if (!job.data) {
    return <Card loading bordered={false} />
  }
  const data = job.data
  const active = data.status === 'running' || data.status === 'queued'

  const handColumns: ColumnsType<JobHand> = [
    {
      title: '手牌',
      dataIndex: 'handId',
      width: 220,
      render: (_: unknown, hand) => (
        <div className="hand-cell">
          <HandCards cards={hand.heroCards} small />
          <div className="hand-cell-copy">
            <div className="hand-cell-title">{hand.title}</div>
            <div className="hand-cell-sub mono">#{hand.handId} · {hand.heroPosition || '—'}</div>
          </div>
        </div>
      ),
    },
    { title: '公共牌', width: 200, render: (_: unknown, hand) => <BoardCards board={hand.board} /> },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => {
        const meta = HAND_STATUS[value] || HAND_STATUS.pending
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '进度',
      dataIndex: 'doneUnits',
      width: 150,
      render: (_: unknown, hand) => (
        <div>
          <Progress
            percent={hand.totalUnits ? Math.min(100, hand.doneUnits / hand.totalUnits * 100) : 0}
            size="small"
            showInfo={false}
            status={hand.status === 'failed' ? 'exception' : undefined}
          />
          <span className="muted">{hand.doneUnits}/{hand.totalUnits}</span>
        </div>
      ),
    },
    {
      title: '提示',
      dataIndex: 'errorMsg',
      render: (value: string | null, hand) => (
        <span className={value ? 'down' : 'muted'}>
          {value || (hand.modified ? '手牌在任务创建后被修改；本次仍使用创建时快照' : '—')}
        </span>
      ),
    },
    {
      title: '',
      width: 70,
      render: (_: unknown, hand) => (
        <Button type="link" size="small" onClick={() => navigate(`/hands/${hand.handId}`)}>查看</Button>
      ),
    },
  ]

  const failureColumns: ColumnsType<JobFailure> = [
    { title: '手牌', dataIndex: 'handId', width: 100, render: (value) => <span className="mono">#{value}</span> },
    { title: '街', dataIndex: 'street', width: 80 },
    { title: 'D#', dataIndex: 'seq', width: 60, render: (value: number) => `D${value + 1}` },
    { title: '版本', dataIndex: 'versionName', width: 180 },
    { title: '错误', dataIndex: 'errorMsg', ellipsis: true, render: (value) => <span className="down">{value || '—'}</span> },
    { title: '时间', dataIndex: 'createdAt', width: 130, render: (value) => <span className="muted">{formatDate(value)}</span> },
  ]

  return (
    <div>
      <div className="page-head job-head">
        <div>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/jobs')}>任务列表</Button>
          <div className="eyebrow" style={{ marginTop: 12 }}>REPLAY JOB</div>
          <h1>
            任务 #{data.id}
            <RunStatusTag status={data.status} />
            {data.parentJobId ? <Tag className="mini-tag">重试自 #{data.parentJobId}</Tag> : null}
          </h1>
          <p className="page-sub">{scopeText(data)} · {formatDate(data.createdAt)} 创建</p>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={() => job.refetch()} loading={job.isFetching}>刷新</Button>
          {data.canCancel ? (
            <Button danger icon={<StopOutlined />} loading={cancel.isPending}
              onClick={() => modal.confirm({
                title: '取消任务？',
                content: '协作式取消：当前模型调用会允许完成，已产出的结果全部保留。',
                okText: '取消任务',
                cancelText: '继续跑',
                onOk: () => cancel.mutate(),
              })}>
              取消任务
            </Button>
          ) : null}
          {data.canRetry ? (
            <Button icon={<ThunderboltOutlined />} loading={retry.isPending} onClick={() => retry.mutate()}>
              重试失败/未完成
            </Button>
          ) : null}
          <Button onClick={() => navigate(`/jobs/${data.id}/compare`)} disabled={!data.doneUnits}>查看对比</Button>
          <Button icon={<DownloadOutlined />} onClick={() => exportData('csv')} disabled={!data.doneUnits}>CSV</Button>
          <Button icon={<DownloadOutlined />} onClick={() => exportData('markdown')} disabled={!data.doneUnits}>Markdown</Button>
        </Space>
      </div>

      {data.errorMsg ? <Alert className="quality-alert" type="warning" showIcon message={data.errorMsg} /> : null}
      {active ? (
        <Alert
          className="quality-alert"
          type="info"
          showIcon
          message="任务运行中，进度每秒刷新"
          description={`心跳 ${formatDate(data.heartbeatAt)} · worker ${data.workerId || '—'}`}
        />
      ) : null}

      <div className="job-stat-grid">
        <Card className="job-progress-card" bordered={false}>
          <div className="progress-head">
            <div>
              <div className="card-title">总进度</div>
              <div className="progress-big">{data.doneUnits}<span>/{data.totalUnits}</span></div>
              <div className="muted">{data.totalUnits ? `${data.progress}%` : '无 unit'} · 已跳过 {data.skippedUnits}</div>
            </div>
            <Progress
              type="circle"
              percent={data.progress}
              size={92}
              strokeWidth={8}
              status={data.status === 'failed' ? 'exception' : active ? 'active' : 'normal'}
            />
          </div>
          <div className="progress-foot">
            <Statistic title="手牌" value={data.handCount} suffix="手" />
            <Statistic title="决策点" value={data.totalDecisions} suffix="个" />
            <Statistic title="失败" value={data.failedUnits} valueStyle={{ color: data.failedUnits ? '#b91c1c' : undefined }} />
            <Statistic title="耗时" value={data.durationSeconds ?? '—'} suffix={data.durationSeconds ? 's' : ''} />
          </div>
        </Card>

        <Card className="job-version-card" bordered={false}>
          <div className="card-title">分版本统计</div>
          <Table
            rowKey="versionId"
            size="small"
            pagination={false}
            dataSource={data.byVersion}
            columns={[
              { title: '版本', dataIndex: 'name', render: (name: string, row) => (
                <div><div>{name}</div><span className="muted mono">{row.model}</span></div>
              ) },
              { title: '完成', dataIndex: 'done', width: 70 },
              { title: '失败', dataIndex: 'failed', width: 70, render: (v: number) => <span className={v ? 'down' : ''}>{v}</span> },
              { title: '兜底', dataIndex: 'fallback', width: 70 },
              { title: 'p50', dataIndex: 'p50Ms', width: 80, render: (v: number | null) => v ? `${Math.round(v)}ms` : '—' },
            ]}
          />
        </Card>
      </div>

      <Card className="table-card job-hands-card" bordered={false}>
        <div className="card-title">逐手进度 <span className="muted">（{data.hands.length} 手）</span></div>
        <Table<JobHand>
          rowKey="handId"
          dataSource={data.hands}
          columns={handColumns}
          pagination={false}
          size="middle"
          scroll={{ x: 1060 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="任务没有包含手牌" /> }}
        />
      </Card>

      <Collapse
        className="job-extra-collapse"
        items={[
          {
            key: 'failures',
            label: `失败明细 (${data.failures.length})`,
            children: data.failures.length ? (
              <Table<JobFailure>
                rowKey="id"
                size="small"
                dataSource={data.failures}
                columns={failureColumns}
                pagination={{ pageSize: 20 }}
              />
            ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有失败单元" />,
          },
          {
            key: 'snapshot',
            label: '任务配置快照 / 可追溯信息',
            children: (
              <Descriptions column={2} size="small" bordered>
                <Descriptions.Item label="范围">{data.scope}</Descriptions.Item>
                <Descriptions.Item label="重复">{data.repeat} 次</Descriptions.Item>
                <Descriptions.Item label="版本">{data.versions.join('、')}</Descriptions.Item>
                <Descriptions.Item label="调用单元">{data.totalUnits}</Descriptions.Item>
                <Descriptions.Item label="创建">{formatDate(data.createdAt)}</Descriptions.Item>
                <Descriptions.Item label="开始">{formatDate(data.startedAt)}</Descriptions.Item>
                <Descriptions.Item label="结束">{formatDate(data.finishedAt)}</Descriptions.Item>
                <Descriptions.Item label="心跳">{formatDate(data.heartbeatAt)}</Descriptions.Item>
                <Descriptions.Item label="配置哈希" span={2}>
                  <span className="mono">{shortHash((data.configSnapshot as any)?.versions?.[0]?.configHash) || '—'}</span>
                </Descriptions.Item>
                <Descriptions.Item label="代码版本" span={2}>
                  <span className="mono">{shortHash((data.configSnapshot as any)?.engine?.commit) || '—'}</span>
                  {(data.configSnapshot as any)?.engine?.dirty ? <Tag className="mini-tag">dirty</Tag> : null}
                </Descriptions.Item>
                <Descriptions.Item label="备注" span={2}>{data.note || '—'}</Descriptions.Item>
              </Descriptions>
            ),
          },
        ]}
      />
    </div>
  )
}
