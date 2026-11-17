import { useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Divider, Empty, Input, InputNumber,
  List, Popconfirm, Select, Space, Table, Tag, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, errorMessage } from '../api'
import type {
  ScoreRunItem, ScoreRunJudge, ScoreRunJudgeEvidence, ScoreRunJudgePoint,
  ScoreRunScoreMap,
} from '../types'
import { formatDate, scoreText } from '../lib/format'

const { Text } = Typography

const HARD_FAIL_OPTIONS = [
  'HF_ERROR', 'HF_OPTION', 'HF_FALLBACK', 'HF_FORBIDDEN_ACTION',
  'HF_FORBIDDEN_POINT', 'HF_HALLUCINATION',
].map((code) => ({ value: code, label: code }))

const DIMENSIONS: { key: keyof ScoreRunScoreMap; label: string }[] = [
  { key: 'action', label: '动作' },
  { key: 'size', label: '尺寸' },
  { key: 'reason', label: '依据' },
  { key: 'fact', label: '事实' },
  { key: 'stable', label: '稳定' },
]

function statusTag(status?: string) {
  if (status === 'hit' || status === 'supported') return <Tag color="green">{status}</Tag>
  if (status === 'miss' || status === 'missing') return <Tag color="red">{status}</Tag>
  return <Tag>{status || '—'}</Tag>
}

export function JudgeCard({ judge }: { judge?: ScoreRunJudge }) {
  const evidence: ScoreRunJudgeEvidence | undefined = judge?.evidence
  const required = judge?.requiredPoints || []
  const forbidden = judge?.forbiddenPoints || []
  const cited = judge?.citedFacts || []
  if (!judge || !Object.keys(judge).length) {
    return <Card size="small" title="判据证据（judge）"><Empty description="没有 judge 记录" /></Card>
  }
  const pointColumns: ColumnsType<ScoreRunJudgePoint> = [
    { title: '要点', dataIndex: 'text', ellipsis: true },
    { title: '权重', dataIndex: 'weight', width: 70 },
    {
      title: '判定', dataIndex: 'status', width: 90,
      render: (value: string) => statusTag(value),
    },
    {
      title: '引用原句', dataIndex: 'quote', width: 220, ellipsis: true,
      render: (value?: string) => value || '—',
    },
  ]
  return (
    <Card
      size="small"
      title="判据证据（judge）"
      extra={(
        <Space size={4} wrap>
          <Tag color={judge.mode === 'llm' ? 'blue' : 'default'}>{judge.mode || '—'}</Tag>
          <Tag color={judge.status === 'ok' ? 'green' : 'orange'}>{judge.status || '—'}</Tag>
          {judge.judgeVersion ? <Tag>{judge.judgeVersion}</Tag> : null}
        </Space>
      )}
    >
      <Descriptions size="small" column={2}>
        <Descriptions.Item label="模型">{judge.model || '—'}</Descriptions.Item>
        <Descriptions.Item label="温度">{judge.temperature ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="prompt">{judge.promptVersion || '—'}</Descriptions.Item>
        <Descriptions.Item label="输入指纹">{judge.inputDigest || '—'}</Descriptions.Item>
      </Descriptions>
      {judge.error ? (
        <Alert
          type="warning" showIcon style={{ marginBottom: 8 }}
          message="judge 调用失败，已退回规则判定"
          description={judge.error}
        />
      ) : null}
      {evidence?.code ? (
        <Alert
          type="error" showIcon style={{ marginBottom: 8 }}
          message={evidence.code + (evidence.hint ? '：' + evidence.hint : '')}
          description={(
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
              {evidence.quotes?.length ? <Text>引用原句：{evidence.quotes.join(' / ')}</Text> : null}
              {evidence.missingFacts?.length ? <Text>facts 缺失：{evidence.missingFacts.join('；')}</Text> : null}
              {evidence.forbiddenPoints?.length ? <Text>命中禁令：{evidence.forbiddenPoints.join('；')}</Text> : null}
              {evidence.forbiddenActions?.length ? <Text>禁用动作：{evidence.forbiddenActions.join(' / ')}</Text> : null}
              {evidence.error ? <Text type="danger">{evidence.error}</Text> : null}
            </Space>
          )}
        />
      ) : null}
      <Divider orientation="left" plain style={{ margin: '8px 0' }}>必需要点</Divider>
      {required.length ? (
        <Table
          size="small" pagination={false} rowKey="key"
          dataSource={required} columns={pointColumns}
        />
      ) : <Text type="secondary">无要点（退化为动作是否被理由支持）</Text>}
      {forbidden.length ? (
        <>
          <Divider orientation="left" plain style={{ margin: '8px 0' }}>禁止要点</Divider>
          <Table
            size="small" pagination={false} rowKey="key"
            dataSource={forbidden} columns={pointColumns}
          />
        </>
      ) : null}
      <Divider orientation="left" plain style={{ margin: '8px 0' }}>引用的事实</Divider>
      {cited.length ? (
        <Table
          size="small" pagination={false} rowKey="quote"
          dataSource={cited}
          columns={[
            { title: '引用', dataIndex: 'quote', ellipsis: true },
            { title: '判定', dataIndex: 'status', width: 100, render: (value: string) => statusTag(value) },
            { title: '对应事实', dataIndex: 'fact', width: 220, ellipsis: true, render: (value?: string) => value || '—' },
          ]}
        />
      ) : <Text type="secondary">没有引用事实</Text>}
      <Space size={8} wrap style={{ marginTop: 8 }}>
        <Tag color={judge.hallucinated ? 'red' : 'default'}>幻觉 {judge.hallucinated ? '是' : '否'}</Tag>
        {judge.missingPoints?.length ? <Tag color="orange">漏判 {judge.missingPoints.length}</Tag> : null}
        {judge.forbiddenHits?.length ? <Tag color="volcano">命中禁令 {judge.forbiddenHits.length}</Tag> : null}
        {judge.wrongFacts?.length ? <Tag color="red">错误事实 {judge.wrongFacts.length}</Tag> : null}
        {judge.reasonMode ? <Tag>依据模式 {judge.reasonMode}</Tag> : null}
      </Space>
    </Card>
  )
}

function emptyScores(): Record<string, number | null> {
  return { action: null, size: null, reason: null, fact: null, stable: null }
}

export function OverrideCard({ runId, item }: { runId: number; item: ScoreRunItem }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [scores, setScores] = useState<Record<string, number | null>>(emptyScores())
  const [hardFail, setHardFail] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [actor, setActor] = useState('console')
  const [rejudge, setRejudge] = useState<Record<string, unknown> | null>(null)
  const effective = item.effective
  const machine = item.scores || {}

  useEffect(() => {
    const base = effective?.scores || item.scores || {}
    const next: Record<string, number | null> = {}
    DIMENSIONS.forEach(({ key }) => { next[key as string] = base[key] ?? null })
    setScores(next)
    setHardFail(effective ? effective.hardFail : item.hardFail)
    setNote('')
    setActor(item.override?.actor || 'console')
    setRejudge(null)
  }, [item.caseId, item.repeatIndex, item.override?.updatedAt, effective?.score])

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['score-run', runId, 'item', item.caseId, item.repeatIndex] })
    queryClient.invalidateQueries({ queryKey: ['score-run', runId] })
    queryClient.invalidateQueries({ queryKey: ['score-run', runId, 'items'] })
  }

  const save = useMutation({
    mutationFn: () => api.post('/score-runs/' + runId + '/items/' + item.caseId + '/override', {
      scores, hardFail, note, actor,
    }),
    onSuccess: () => { message.success('已保存改判'); refresh() },
    onError: (error) => message.error(errorMessage(error)),
  })
  const clear = useMutation({
    mutationFn: () => api.delete('/score-runs/' + runId + '/items/' + item.caseId + '/override', {
      note, actor,
    }),
    onSuccess: () => { message.success('已撤回改判'); refresh() },
    onError: (error) => message.error(errorMessage(error)),
  })
  const runRejudge = useMutation({
    mutationFn: () => api.post('/score-runs/' + runId + '/items/' + item.caseId + '/' + item.repeatIndex + '/rejudge', {}),
    onSuccess: (data) => { setRejudge(data as Record<string, unknown>); message.success('重判完成（未写库）') },
    onError: (error) => message.error(errorMessage(error)),
  })

  return (
    <Card size="small" title="人工改判">
      <Descriptions size="small" column={3}>
        <Descriptions.Item label="机器分">{scoreText(item.score)}</Descriptions.Item>
        <Descriptions.Item label="生效分">
          {scoreText(effective ? effective.score : item.score)}
          {effective?.overridden ? <Tag color="gold" style={{ marginLeft: 6 }}>已改判</Tag> : null}
        </Descriptions.Item>
        <Descriptions.Item label="机器硬失败">{item.hardFail || '—'}</Descriptions.Item>
      </Descriptions>
      <Space size={8} wrap style={{ marginBottom: 8 }}>
        {DIMENSIONS.map(({ key, label }) => (
          <Space key={key} size={4}>
            <Text type="secondary">{label}</Text>
            <InputNumber
              size="small" min={0} max={1} step={0.05} style={{ width: 78 }}
              value={scores[key as string] ?? undefined}
              placeholder={String(machine[key] ?? '—')}
              onChange={(value) => setScores((prev) => ({ ...prev, [key]: value ?? null }))}
            />
          </Space>
        ))}
      </Space>
      <Space size={8} wrap style={{ marginBottom: 8 }}>
        <Text type="secondary">硬失败</Text>
        <Select
          size="small" allowClear style={{ width: 220 }}
          placeholder="保持原值"
          value={hardFail ?? undefined}
          options={HARD_FAIL_OPTIONS}
          onChange={(value) => setHardFail(value ?? null)}
        />
        <Text type="secondary">改判人</Text>
        <Input size="small" style={{ width: 140 }} value={actor} onChange={(e) => setActor(e.target.value)} />
      </Space>
      <Input.TextArea
        rows={2} value={note} placeholder="改判理由（留痕，最长 2000 字）"
        onChange={(e) => setNote(e.target.value)}
      />
      <Space size={8} wrap style={{ marginTop: 8 }}>
        <Button type="primary" size="small" loading={save.isPending} onClick={() => save.mutate()}>保存改判</Button>
        <Popconfirm
          title="撤回后聚合分回到机器判定，确定？"
          onConfirm={() => clear.mutate()}
          disabled={!item.override}
        >
          <Button size="small" danger disabled={!item.override} loading={clear.isPending}>撤回改判</Button>
        </Popconfirm>
        <Button size="small" loading={runRejudge.isPending} onClick={() => runRejudge.mutate()}>重判（不写库）</Button>
      </Space>
      {rejudge ? (
        <Alert
          style={{ marginTop: 8 }}
          type={rejudge.consistent ? 'success' : 'warning'}
          showIcon
          message={rejudge.consistent ? '重判结果与原始判定一致（temp 0 可复现）' : '重判结果与原始判定不一致，见差异'}
          description={(
            <Text style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
              {JSON.stringify(rejudge.diffs ?? {}, null, 2)}
            </Text>
          )}
        />
      ) : null}
      {item.overrideHistory?.length ? (
        <>
          <Divider orientation="left" plain style={{ margin: '10px 0 4px' }}>改判留痕</Divider>
          <List
            size="small" dataSource={item.overrideHistory}
            renderItem={(row) => (
              <List.Item>
                <Space direction="vertical" size={0} style={{ width: '100%' }}>
                  <Text>
                    <Tag>{row.actor || '—'}</Tag>
                    {formatDate(row.createdAt)}
                    {row.note ? <Text type="secondary"> · {row.note}</Text> : null}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
                    {JSON.stringify(row.before)} → {JSON.stringify(row.after)}
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        </>
      ) : null}
    </Card>
  )
}