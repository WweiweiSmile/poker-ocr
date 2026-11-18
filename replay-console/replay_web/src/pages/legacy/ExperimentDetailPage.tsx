import { useMemo, useState } from 'react'
import {
  Alert, Button, Card, Descriptions, Empty, Progress, Segmented, Space,
  Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowLeftOutlined, BranchesOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, errorMessage } from '../../api'
import type {
  Experiment, ExperimentResults, ExperimentVersionResult, Version,
} from '../../types'
import { formatDate } from '../../lib/format'
import LegacyBanner from '../../components/LegacyBanner'

const STATUS_COLORS: Record<string, string> = {
  draft: 'default', queued: 'blue', running: 'processing',
  done: 'green', failed: 'red', archived: 'default',
}
const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '排队', running: '运行中',
  done: '完成', failed: '失败', archived: '已归档',
}

function pct(value: number | undefined): string {
  if (value === undefined || value === null || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

export default function ExperimentDetailPage() {
  const { experimentId } = useParams()
  const id = Number(experimentId)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [split, setSplit] = useState<string>('all')

  const experiment = useQuery<Experiment>({
    queryKey: ['experiment', id],
    queryFn: () => api.get(`/experiments/${id}`),
    enabled: Number.isFinite(id),
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
  const results = useQuery<ExperimentResults>({
    queryKey: ['experiment-results', id, split],
    queryFn: () => api.get(`/experiments/${id}/results${split === 'all' ? '' : `?split=${split}`}`),
    enabled: Number.isFinite(id) && !!experiment.data?.jobId,
  })

  const rows = useMemo(() => {
    const data = results.data?.versions
    if (!data) return []
    return Object.values(data).sort((a, b) => a.versionId - b.versionId)
  }, [results.data])

  const columns: ColumnsType<ExperimentVersionResult> = [
    {
      title: '版本', key: 'name',
      render: (_: unknown, row) => (
        <div>
          <div className="version-name">{row.name}</div>
          <div className="muted mono">
            {row.contextMode} · stats {row.tableStatsMode}
          </div>
        </div>
      ),
    },
    {
      title: 'n', dataIndex: 'n', key: 'n', width: 70,
      render: (value: number) => <span className="mono">{value}</span>,
    },
    {
      title: '一致率', dataIndex: 'agreement', key: 'agreement', width: 110,
      render: (value: number | null, row) => (
        <Tooltip title={`一致 ${row.bucket['一致'] || 0} / 可判定 ${row.scored || 0}` +
          `（未知动作 ${row.unknown || 0} 个不计入）`}>
          <div>
            {value == null ? <span className="muted">—</span> : (
              <>
                <Progress percent={Math.round(value * 100)} size="small" showInfo={false} />
                <span className="mono">{pct(value)}</span>
              </>
            )}
          </div>
        </Tooltip>
      ),
    },
    {
      title: 'bucket', key: 'bucket', width: 240,
      render: (_: unknown, row) => (
        <Space wrap size={4}>
          {Object.entries(row.bucket).map(([key, value]) => (
            <Tag key={key} color={key === '一致' ? 'green' : key === 'error' ? 'red' : 'orange'}
              className="mini-tag">
              {key} {value}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '违规', key: 'violations', width: 210,
      render: (_: unknown, row) => {
        const entries = Object.entries(row.violations)
        if (!entries.length) return <span className="muted">—</span>
        return (
          <Space wrap size={4}>
            {entries.map(([key, value]) => (
              <Tag key={key} color="red" className="mini-tag">{key} {value}</Tag>
            ))}
          </Space>
        )
      },
    },
    {
      title: 'split', key: 'splits', width: 150,
      render: (_: unknown, row) => (
        <span className="mono muted">
          {Object.entries(row.splits).map(([key, value]) => `${key} ${value}`).join(' · ') || '—'}
        </span>
      ),
    },
  ]

  if (experiment.isLoading) return <Card loading bordered={false} />
  if (experiment.error || !experiment.data) {
    return <Alert type="error" showIcon message="实验加载失败" description={errorMessage(experiment.error)} />
  }
  const exp = experiment.data
  const result = results.data

  return (
    <div className="experiment-detail-page">
      <div className="detail-back">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/legacy/experiments')}>
          A/B 实验
        </Button>
        <span className="mono muted">#{exp.id}</span>
        <Tag color={STATUS_COLORS[exp.status] || 'default'}>
          {STATUS_LABELS[exp.status] || exp.status}
        </Tag>
      </div>

      <LegacyBanner scope="A/B 实验" />

      <Card className="hand-hero" bordered={false}>
        <div className="hand-hero-main">
          <div>
            <div className="hand-hero-title">{exp.name}</div>
            <div className="hand-hero-sub">
              <span>{exp.hypothesis || '没有写假设'}</span>
            </div>
          </div>
          <Space>
            {exp.jobId ? (
              <Button onClick={() => navigate(`/jobs/${exp.jobId}`)}>任务 #{exp.jobId}</Button>
            ) : null}
            <Button icon={<BranchesOutlined />} disabled={!exp.jobId}
              onClick={() => navigate(`/legacy/experiments/${exp.id}/pairs`)}>
              成对复盘
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => {
              queryClient.invalidateQueries({ queryKey: ['experiment', exp.id] })
              queryClient.invalidateQueries({ queryKey: ['experiment-results', exp.id] })
            }}>
              刷新
            </Button>
          </Space>
        </div>
      </Card>

      <Card bordered={false} className="experiment-meta-card">
        <Descriptions size="small" column={4}>
          <Descriptions.Item label="A / baseline">
            {versionMap[exp.baselineVersionId]?.name || `#${exp.baselineVersionId}`}
          </Descriptions.Item>
          <Descriptions.Item label="B / variant">
            {versionMap[exp.variantVersionId]?.name || `#${exp.variantVersionId}`}
          </Descriptions.Item>
          <Descriptions.Item label="手牌集">
            {exp.hands.total} 手（dev {exp.hands.dev} / holdout {exp.hands.holdout}）
          </Descriptions.Item>
          <Descriptions.Item label="重复 / 创建">
            {exp.repeat} 次 · {formatDate(exp.createdAt, false)}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {!exp.jobId ? (
        <Alert type="info" showIcon className="quality-alert"
          message="实验还没跑"
          description="这张实验没有关联任务，所以没有结果可看。旧链路已停止受理新的运行请求。"
        />
      ) : null}

      <div className="section-head">
        <div>
          <h2>结果</h2>
          <p className="muted">
            一致率 = 「一致」/ 可判定决策点（实际动作缺录的记「未知动作」，不计入分母）；
            bucket 和违规来自硬规则审计。n&lt;30 手不要写显著。
          </p>
        </div>
        <Segmented
          value={split}
          onChange={(value) => setSplit(String(value))}
          options={[
            { value: 'all', label: '全部' },
            { value: 'dev', label: 'dev' },
            { value: 'holdout', label: 'holdout' },
          ]}
        />
      </div>

      {results.error ? <Alert type="error" showIcon message={errorMessage(results.error)} /> : null}
      {result ? (
        <Card bordered={false}>
          <div className="experiment-summary">
            <span>决策结果行 <b>{result.rows}</b></span>
            <span>复盘 <b>{Object.entries(result.review).map(([k, v]) => `${k} ${v}`).join(' · ') || '—'}</b></span>
          </div>
          <Table<ExperimentVersionResult>
            rowKey="versionId"
            size="middle"
            columns={columns}
            dataSource={rows}
            pagination={false}
            locale={{ emptyText: '还没有结果；确认任务已完成或稍后刷新。' }}
          />
        </Card>
      ) : (
        <Empty description={exp.jobId ? '加载结果…' : '这张实验没有关联任务'} image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
    </div>
  )
}
