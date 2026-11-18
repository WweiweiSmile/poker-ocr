import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Divider, Empty, Input, Modal, Popover, Segmented,
  Select, Skeleton, Space, Spin, Switch, Tag, Tooltip,
} from 'antd'
import {
  ArrowLeftOutlined, DeleteOutlined, PlayCircleOutlined, ReloadOutlined,
  StarFilled, StarOutlined, ThunderboltOutlined, WarningOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type {
  CaseSetList, ComparisonDecision, DecisionMeta, HandAdvice, HandComparison,
  HandDeleteResult, HandDetail, Meta, Version, VersionBlock,
} from '../types'
import {
  BoardCards, BucketTag, HandCards, QualityBadge, SourceTag,
} from '../components/CardChips'
import RunModal from '../components/RunModal'
import { formatAmount, formatBB, formatDate } from '../lib/format'

const STREET_ORDER: Record<string, number> = { preflop: 0, flop: 1, turn: 2, river: 3 }
const STREET_NAMES: Record<string, string> = {
  preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌',
}

function actionText(block?: VersionBlock | null): string {
  if (!block) return '未跑'
  const summary = block.summary
  if (!summary.optionId) return '—'
  const kind = summary.optionKind || summary.optionId || ''
  const labels: Record<string, string> = {
    fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注', allin: '全下',
  }
  const base = labels[kind] || summary.optionId
  return summary.amount !== null && summary.amount !== undefined
    ? `${base} ${formatAmount(summary.amount)}`
    : base
}

function actualText(decision: ComparisonDecision): string {
  const action = decision.actual?.action
  if (!action) return '—'
  const labels: Record<string, string> = {
    fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注', allin: '全下',
  }
  const base = labels[action] || action
  return decision.actual.amountBb !== null && decision.actual.amountBb !== undefined
    ? `${base} ${formatAmount(decision.actual.amountBb)}`
    : base
}

function FactStrip({ decision }: { decision: ComparisonDecision }) {
  const facts = decision.versions.find((v) => v.summary.facts)?.summary.facts
    || decision.previewFacts
  if (!facts) return null
  if (facts.error) return <div className="fact-error">事实计算失败：{facts.error}</div>
  const items: { label: string; value: string; color?: string }[] = []
  if (facts.handTier) items.push({ label: '牌力', value: String(facts.handTier), color: 'green' })
  if (facts.handLabelEn) items.push({ label: '牌型', value: String(facts.handLabelEn) })
  if (facts.equityPct !== null && facts.equityPct !== undefined) {
    items.push({ label: '胜率', value: `${facts.equityPct}%`, color: 'blue' })
  }
  if (facts.beatAtLeastPct !== null && facts.beatAtLeastPct !== undefined) {
    // 随机牌独立近似（coach.at_least_one_behind），不是手牌范围的联合重抽；
    // 与 handStrength.behindAny 量纲/算法都不同，展示时必须标出来。
    items.push({ label: '至少一家领先', value: `${facts.beatAtLeastPct}%（近似）` })
  }
  if (facts.textureEn) items.push({ label: '牌面', value: String(facts.textureEn) })
  if (facts.heroIsNuts !== null && facts.heroIsNuts !== undefined) {
    items.push({ label: '坚果', value: facts.heroIsNuts ? '是' : '否', color: facts.heroIsNuts ? 'gold' : undefined })
  }
  if (facts.tableDynamics) items.push({ label: '桌风', value: '已注入' })
  if (facts.explosivePot) items.push({ label: '爆炸底池', value: '是', color: 'volcano' })
  if (facts.actualViolationTags?.length) {
    items.push({ label: '实际违规', value: facts.actualViolationTags.join('、'), color: 'red' })
  }
  return (
    <div className="fact-strip">
      {items.map((item) => (
        <span className="fact-item" key={item.label}>
          <span className="fact-label">{item.label}</span>
          <Tag color={item.color} className="fact-tag">{item.value}</Tag>
        </span>
      ))}
      {!items.length ? <span className="muted">事实层暂无额外信息</span> : null}
      {facts.directive ? (
        <div className="fact-directive">
          <span className="fact-label">价值指令</span>
          <Tag color="purple" className="fact-tag directive-tag">{String(facts.directive)}</Tag>
        </div>
      ) : null}
    </div>
  )
}

function VersionRow({ block, decision }: { block: VersionBlock; decision: ComparisonDecision }) {
  const summary = block.summary
  const hasResults = block.results.length > 0
  const nText = summary.total ? `${summary.n}/${summary.total}` : ''
  const distribution = Object.entries(summary.distribution || {})
  const popover = (
    <div className="repeat-popover">
      {block.results.map((result) => (
        <div className="repeat-row" key={`${result.repeat}`}>
          <div className="repeat-head">
            <Tag className="mini-tag">第 {result.repeat + 1} 次</Tag>
            <span className="repeat-option">{result.optionId || '—'}</span>
            {result.amount !== null && result.amount !== undefined ? <span className="muted">{formatAmount(result.amount)}</span> : null}
            <SourceTag source={result.source} />
            {result.durationMs ? <span className="repeat-ms">{result.durationMs}ms</span> : null}
          </div>
          <div className="repeat-reason">{result.error || result.reason || '（无理由）'}</div>
        </div>
      ))}
    </div>
  )

  return (
    <div className={`version-result${summary.bucket === '动作不同' ? ' differ' : ''}${!hasResults ? ' pending' : ''}`}>
      <div className="version-result-head">
        <span className="version-name">{block.name}</span>
        <span className="version-model">{block.model}</span>
        {hasResults && nText ? (
          <Popover content={popover} title={`${block.name} · 每次采样`} trigger="click" placement="bottom">
            <button className="n-button">{nText}</button>
          </Popover>
        ) : <Tag className="mini-tag">未跑</Tag>}
        <SourceTag source={summary.source} />
        <BucketTag bucket={summary.bucket} />
        <span className="spacer" />
      </div>
      <div className="version-result-main">
        <span className={`option-pill kind-${summary.optionKind || 'none'}`}>
          {actionText(block)}
        </span>
        {distribution.length > 1 ? (
          <span className="distribution">
            {distribution.map(([option, count]) => (
              <span className="dist-chip" key={option}>{option} ×{count}</span>
            ))}
          </span>
        ) : null}
        {summary.violationTags?.length ? (
          <Tooltip title={summary.violationTags.join('、')}>
            <Tag color="red" className="mini-tag">违规 {summary.violationTags.length}</Tag>
          </Tooltip>
        ) : null}
      </div>
      {hasResults ? (
        <div className={`version-reason${summary.source === 'error' ? ' error' : ''}`}>
          {summary.source === 'error'
            ? (block.results[0]?.error || '模型调用失败')
            : (block.results.find((r) => r.optionId === summary.optionId)?.reason || block.results[0]?.reason || '（无理由）')}
        </div>
      ) : null}
    </div>
  )
}

const REVIEW_LABELS: Record<string, string> = {
  good: '好决策', bad: '坏决策', unsure: '待定',
}

function DecisionCard({ decision, onReview }: {
  decision: ComparisonDecision
  onReview: (decision: DecisionMeta) => void
}) {
  const d = decision.decision
  const facts = decision.versions.find((v) => v.summary.facts)?.summary.facts || decision.previewFacts
  const warnings = d.warnings || []
  return (
    <Card
      id={`decision-${d.seq}`}
      className={`decision-card${decision.diverged ? ' diverged' : ''}`}
      bordered={false}
    >
      <div className="decision-head">
        <div className="decision-title">
          <span className="decision-anchor">D{d.seq + 1}</span>
          <span className="decision-street">{d.street === 'preflop' ? '翻前' : d.street === 'flop' ? '翻牌' : d.street === 'turn' ? '转牌' : '河牌'}</span>
          {decision.diverged ? <Tag color="orange" className="mini-tag">版本分歧</Tag> : null}
          <QualityBadge quality={d.quality} />
        </div>
        <div className="decision-meta">
          <span>底池 <b>{formatAmount(d.pot)}</b> BB</span>
          <span>需跟 <b>{formatAmount(d.callAmount ?? d.toCall)}</b> BB</span>
          <span>筹码 <b>{formatAmount(d.heroStack)}</b> BB</span>
          <span>{d.opponents} 个对手</span>
        </div>
        <div className="decision-review">
          {d.review ? (
            <Tag color={d.review.verdict === 'good' ? 'green'
              : d.review.verdict === 'bad' ? 'red' : 'default'}
              className="mini-tag">
              复盘 {REVIEW_LABELS[d.review.verdict] || d.review.verdict}
            </Tag>
          ) : <Tag className="mini-tag">未复盘</Tag>}
          <Button size="small" disabled={!d.id} onClick={() => onReview(d)}>
            复盘
          </Button>
        </div>
      </div>

      <FactStrip decision={decision} />
      {warnings.length ? (
        <div className="decision-warning">
          <WarningOutlined /> {warnings.join('；')}
        </div>
      ) : null}

      <div className="actual-line">
        <span className="actual-label">实际动作</span>
        <span className="actual-pill">{actualText(decision)}</span>
      </div>

      <Divider className="thin-divider" />
      <div className="version-results">
        {decision.versions.length ? decision.versions.map((block) => (
          <VersionRow key={block.versionId} block={block} decision={decision} />
        )) : <span className="muted">还没有跑过这手牌。选择版本后点「重跑这手」。</span>}
      </div>
      {facts?.warnings?.length ? (
        <div className="fact-warnings">{facts.warnings.join('；')}</div>
      ) : null}
    </Card>
  )
}

function AdviceContext({ advice }: { advice: HandAdvice }) {
  const context = advice.context
  if (!context) return null
  const stats = context.table_stats || {}
  const reads = context.opponent_reads || []
  const statItems: [string, unknown][] = [
    ['口径', context.table_stats_scope],
    ['在局', stats.total_players],
    ['有画像', stats.players],
    ['样本', stats.hands],
    ['VPIP', stats.vpip],
    ['PFR', stats.pfr],
    ['3-bet', stats.threebet],
    ['对 c-bet 弃', stats.fold_to_cbet],
  ]
  return (
    <details className="advice-context">
      <summary>桌况 / 对手快照</summary>
      <div className="advice-context-body">
        <div className="advice-context-row">
          <span className="fact-label">桌况</span>
          {statItems.filter(([, value]) => value !== null && value !== undefined && value !== '')
            .map(([label, value]) => (
              <Tag className="mini-tag" key={label}>{label} {String(value)}</Tag>
            ))}
          {!Object.keys(stats).length ? <span className="muted">没有 table_stats</span> : null}
        </div>
        {reads.length ? (
          <div className="advice-context-row">
            <span className="fact-label">对手</span>
            {reads.map((read) => (
              <Tag className="mini-tag" key={`${read.seat_id}-${read.key}`}>
                {read.display_name || read.key || read.seat_id} {read.archetype || 'unknown'}
                {read.n !== undefined ? ` · n=${read.n}` : ''}
              </Tag>
            ))}
          </div>
        ) : null}
        {context.table_dynamics ? (
          <div className="advice-context-dynamics">{context.table_dynamics}</div>
        ) : null}
      </div>
    </details>
  )
}

export default function HandDetailPage() {
  const { handId } = useParams()
  const navigate = useNavigate()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const id = Number(handId)
  const [selectedVersions, setSelectedVersions] = useState<number[]>([])
  const [onlyDiverged, setOnlyDiverged] = useState(false)
  const [rerunOpen, setRerunOpen] = useState(false)
  const [markFavorite, setMarkFavorite] = useState(false)
  const [tags, setTags] = useState<string[]>([])
  const [status, setStatus] = useState<'todo' | 'confirmed' | 'disputed'>('todo')
  const [note, setNote] = useState('')
  const [reviewTarget, setReviewTarget] = useState<DecisionMeta | null>(null)
  const [caseTarget, setCaseTarget] = useState<DecisionMeta | null>(null)
  const [caseSpecId, setCaseSpecId] = useState<number | null>(null)
  const [casePriority, setCasePriority] = useState(1)
  const [caseNote, setCaseNote] = useState('')
  const [caseSetId, setCaseSetId] = useState<number | null>(null)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [reviewVerdict, setReviewVerdict] = useState<'good' | 'bad' | 'unsure'>('unsure')
  const [reviewTags, setReviewTags] = useState<string[]>([])
  const [reviewNote, setReviewNote] = useState('')

  const detail = useQuery<HandDetail>({
    queryKey: ['hand', id],
    queryFn: () => api.get(`/hands/${id}`),
    enabled: Number.isFinite(id),
  })
  const versions = useQuery<{ items: Version[] }>({
    queryKey: ['versions', 'all'],
    queryFn: () => api.get('/versions?includeArchived=1'),
  })
  const meta = useQuery<Meta>({ queryKey: ['meta'], queryFn: () => api.get('/meta') })

  const specs = useQuery<{ items: { id: number; name: string }[] }>({
    queryKey: ['specs', 'case-create'],
    queryFn: () => api.get('/specs?pageSize=100&status=active'),
    enabled: caseTarget !== null,
  })
  const caseSets = useQuery<CaseSetList>({
    queryKey: ['case-sets'],
    queryFn: () => api.get('/case-sets'),
    enabled: caseTarget !== null || bulkOpen,
  })

  const caseCanonicalId = detail.data?.hand.canonicalId
    ?? detail.data?.canonical?.id ?? null

  const createCase = useMutation({
    mutationFn: () => api.post<{ created: boolean }>('/cases', {
      canonicalId: caseCanonicalId,
      decisionKey: caseTarget?.decisionKey ?? `${caseTarget?.street}:${caseTarget?.seq}`,
      specId: caseSpecId,
      caseSetId: caseSetId ?? undefined,
      priority: casePriority,
      note: caseNote || undefined,
    }),
    onSuccess: (result: { created: boolean }) => {
      message.success(result.created ? '案例已创建' : '这条决策点已有案例，已跳到已有记录')
      setCaseTarget(null)
      setCaseNote('')
      queryClient.invalidateQueries({ queryKey: ['hand', id] })
      queryClient.invalidateQueries({ queryKey: ['hands'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const bulkCreateCases = useMutation({
    mutationFn: () => api.post<{ created: number; existing: number; noSpec: string[] }>(
      `/case-sets/${caseSetId}/hand-cases`,
      { canonicalId: caseCanonicalId, priority: casePriority },
    ),
    onSuccess: (result) => {
      const noSpec = result.noSpec || []
      message.success(`整手全建：新增 ${result.created} 条，已存在 ${result.existing} 条`
        + (noSpec.length ? `；${noSpec.length} 个决策点没匹配到期望` : ''))
      setBulkOpen(false)
      queryClient.invalidateQueries({ queryKey: ['hand', id] })
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['case-set', caseSetId] })
      queryClient.invalidateQueries({ queryKey: ['case-sets'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const comparison = useQuery<HandComparison>({
    queryKey: ['hand-comparison', id, selectedVersions],
    queryFn: () => api.get(`/hands/${id}/comparison?versionIds=${selectedVersions.join(',')}`),
    enabled: Number.isFinite(id) && selectedVersions.length > 0,
  })

  useEffect(() => {
    const hand = detail.data?.hand
    if (!hand) return
    setMarkFavorite(hand.favorite)
    setTags(hand.markTags || [])
    setStatus((hand.markStatus as typeof status) || 'todo')
    setNote(hand.markNote || '')
  }, [detail.data?.hand.id])

  useEffect(() => {
    if (!detail.data || !versions.data) return
    const runIds = detail.data.hand.lastRun?.versionIds || []
    if (runIds.length) {
      setSelectedVersions(runIds)
      return
    }
    const active = versions.data.items.filter((v) => !v.archived && v.runnable).map((v) => v.id)
    setSelectedVersions(active.slice(0, 4))
  }, [detail.data?.hand.id, versions.data])

  const reviewMutation = useMutation({
    mutationFn: () => api.patch(`/decisions/${reviewTarget?.id}/mark`, {
      verdict: reviewVerdict, tags: reviewTags, note: reviewNote,
    }),
    onSuccess: () => {
      message.success('决策复盘已保存')
      setReviewTarget(null)
      queryClient.invalidateQueries({ queryKey: ['hand', id] })
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const openReview = (decision: DecisionMeta) => {
    setReviewTarget(decision)
    setReviewVerdict((decision.review?.verdict as typeof reviewVerdict) || 'unsure')
    setReviewTags(decision.review?.tags || [])
    setReviewNote(decision.review?.note || '')
  }

  const saveMark = useMutation({
    mutationFn: (handId: number) => api.patch(`/hands/${handId}/mark`, {
      favorite: markFavorite, tags, status, note,
    }),
    onSuccess: () => {
      message.success('收藏与结论已保存')
      queryClient.invalidateQueries({ queryKey: ['hand', id] })
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const toggleFavorite = useMutation({
    mutationFn: ({ handId, favorite }: { handId: number; favorite: boolean }) =>
      api.patch(`/hands/${handId}/mark`, { favorite }),
    onSuccess: (_data, variables) => {
      setMarkFavorite(variables.favorite)
      queryClient.invalidateQueries({ queryKey: ['hand', id] })
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const deleteMutation = useMutation({
    mutationFn: (handId: number) => api.delete<HandDeleteResult>(`/hands/${handId}`),
    onSuccess: () => {
      message.success(`手牌 #${hand.id} 已删除`)
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
      navigate('/hands')
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const confirmDelete = () => {
    modal.confirm({
      title: `删除手牌 #${hand.id}`,
      content: '导入手牌会软删除，已跑结果保留；复盘手牌只会从控制台隐藏，不会修改 review_hands。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          await deleteMutation.mutateAsync(hand.id)
        } catch {
          throw new Error('delete failed')
        }
      },
    })
  }

  const blocks: ComparisonDecision[] = useMemo(() => {
    if (comparison.data) return comparison.data.decisions
    return (detail.data?.decisions || []).map((d) => ({
      decision: d,
      actual: d.actual,
      versions: [],
      diverged: false,
    }))
  }, [comparison.data, detail.data])

  const filteredBlocks = onlyDiverged ? blocks.filter((block) => block.diverged) : blocks
  const activeStreet = useMemo(() => {
    const first = blocks[0]?.decision.street
    return first || 'preflop'
  }, [blocks])

  if (detail.isLoading) {
    return <div className="page-loading"><Skeleton active paragraph={{ rows: 8 }} /></div>
  }
  if (detail.error || !detail.data) {
    return <Alert type="error" showIcon message="手牌加载失败" description={errorMessage(detail.error)} />
  }
  const data = detail.data
  const hand = data.hand
  const detailHandId = hand.id
  const canonicalId = hand.canonicalId ?? data.canonical?.id ?? null
  const sourceItems = data.sources || []
  const bucketLabels = new Map<string, string>()
  for (const dimension of meta.data?.buckets || []) {
    for (const option of dimension.options) {
      bucketLabels.set(`${dimension.label}:${option.value}`, option.label)
    }
  }
  const versionItems = versions.data?.items || []
  const blocked = data.reconstruction.quality === 'invalid'
  const dimensionLabels = new Map<string, string>(
    (meta.data?.buckets || []).map((dimension) => [dimension.key, dimension.label]),
  )
  const unfrozenSets = (caseSets.data?.items || []).filter((item) => !item.frozen)
  const defaultSet = unfrozenSets.find((item) => item.name === '待整理') || unfrozenSets[0]
  const openCase = (decision: DecisionMeta) => {
    setCaseTarget(decision)
    setCaseNote('')
    setCasePriority(1)
    if (caseSetId === null && defaultSet) setCaseSetId(defaultSet.id)
    if (caseSpecId === null && specs.data?.items.length) setCaseSpecId(specs.data.items[0].id)
  }

  const scrollToDecision = (seq: number) => {
    document.getElementById(`decision-${seq}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="hand-detail-page">
      <div className="detail-back">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/hands')}>手牌库</Button>
        <span className="mono muted">#{hand.id}</span>
        {hand.source !== 'db' ? (
          <Tag color={hand.source === 'live' ? 'geekblue' : 'cyan'}>
            {hand.sourceLabel}{hand.externalId ? ` · ${hand.externalId}` : ''}
          </Tag>
        ) : null}
        {hand.duplicate ? <Tag color="purple">内容重复</Tag> : null}
        {data.reconstruction.bombPot ? (
          <Tooltip title="爆炸底池：翻前没有行动，每人强制 5BB 前注">
            <Tag color="volcano">爆炸底池</Tag>
          </Tooltip>
        ) : null}
      </div>

      <Card className="hand-hero" bordered={false}>
        <div className="hand-hero-main">
          <div className="hand-hero-left">
            <HandCards cards={hand.heroCards} />
            <div>
              <div className="hand-hero-title">{hand.title}</div>
              <div className="hand-hero-sub">
                <span className="position-tag">{hand.heroPosition || '—'}</span>
                <span>{formatAmount(hand.heroStackBb)} BB</span>
                <span>{hand.tableSize} 人桌</span>
                <span>任务 {hand.lastRun ? `#${hand.lastRun.jobId}` : '未跑'}</span>
              </div>
            </div>
          </div>
          <div className="hand-hero-center">
            <div className="hero-label">公共牌</div>
            <BoardCards board={hand.board} small={false} />
          </div>
          <div className="hand-hero-right">
            <div className={`hero-result ${hand.resultAmount && hand.resultAmount > 0 ? 'up' : hand.resultAmount && hand.resultAmount < 0 ? 'down' : ''}`}>
              {hand.resultAmount !== null && hand.resultAmount !== undefined
                ? <>{formatBB(hand.resultAmount)}<span className="hero-result-unit">BB</span></>
                : <span className="hero-result-text">{hand.result === 'lose' ? '输' : hand.result === 'win' ? '赢' : hand.result === 'fold' ? '弃牌' : '未知'}</span>}
            </div>
            <div className="hero-created">创建于 {formatDate(hand.createdAt, false)}</div>
            <Space>
              <button
                className={`star-btn big${markFavorite ? ' on' : ''}`}
                onClick={() => toggleFavorite.mutate({ handId: detailHandId, favorite: !markFavorite })}
                aria-label="收藏"
              >
                {markFavorite ? <StarFilled /> : <StarOutlined />}
              </button>
              <Button icon={<PlayCircleOutlined />} onClick={() => navigate(`/hands/${detailHandId}/replay`)}>
                瀑布流回放
              </Button>
              <Button icon={<PlayCircleOutlined />} onClick={() => navigate(`/hands/${detailHandId}/replay?mode=table`)}>
                牌桌回放
              </Button>
              <Button type="primary" icon={<ThunderboltOutlined />} disabled={blocked} onClick={() => setRerunOpen(true)}>
                重跑这手
              </Button>
              <Button danger icon={<DeleteOutlined />} onClick={confirmDelete}>
                删除这手
              </Button>
            </Space>
          </div>
        </div>
      </Card>

      {data.reconstruction.quality !== 'exact' ? (
        <Alert
          className="quality-alert"
          type={blocked ? 'error' : 'warning'}
          showIcon
          message={blocked ? '这手牌不可重跑' : '重建质量：approx（底池/行动线为推算值）'}
          description={
            <div>
              <div>{data.reconstruction.invalidReason || data.reconstruction.warnings.join('；') || '缺少精确的起始底池记录。'}</div>
              {!blocked ? <div className="muted">重跑仍可用，但对比结论需谨慎；批量任务可只选 exact 决策点。</div> : null}
            </div>
          }
        />
      ) : null}

      {sourceItems.length ? (
        <Card className="sources-card" bordered={false}>
          <div className="card-title">
            来源并列 <Tag className="mini-tag">{sourceItems.length}</Tag>
            {canonicalId ? <span className="mono muted">canonical #{canonicalId}</span> : null}
            <span className="muted advice-hint">同一手牌内容在不同来源各存一份；复盘结论只读，控制台不改写 review_hands</span>
          </div>
          <div className="source-grid">
            {sourceItems.map((source) => (
              <div key={`${source.source}-${source.handId}`} className={`source-item${source.primary ? ' primary' : ''}`}>
                <div className="source-item-head">
                  <Tag color={source.source === 'review' ? 'cyan' : source.source === 'live' ? 'geekblue' : 'purple'}>
                    {source.sourceLabel}
                  </Tag>
                  {source.primary ? <Tag color="gold">主来源</Tag> : null}
                  <span className="mono muted">#{source.handId}</span>
                  {source.mark.favorite ? <StarFilled className="star-inline" /> : null}
                </div>
                <div className="source-item-title">{source.title}</div>
                <div className="source-item-meta">
                  <span>{source.resultAmount !== null && source.resultAmount !== undefined
                    ? `${formatBB(source.resultAmount)} BB` : '结果未知'}</span>
                  <span>{formatDate(source.createdAt, false)}</span>
                </div>
                <div className="source-item-readonly">
                  <div className="readonly-label">复盘结论（只读）</div>
                  <div className="muted">
                    {source.analyzeStatus ? `解析：${source.analyzeStatus}` : '无解析状态'}
                    {source.reviewScore ? ` · 深挖分 ${source.reviewScore}` : ''}
                  </div>
                  {source.heroThought ? <div>想法：{source.heroThought}</div> : null}
                  {source.advices?.length ? (
                    <div className="source-advices">
                      <div className="readonly-label">
                        实时 payload · AI 建议 {source.advices.length} 条
                      </div>
                      {source.advices.slice(0, 3).map((advice, index) => (
                        <div className="source-advice" key={advice.id ?? index}>
                          <span className="mono">{advice.street || '—'}</span>
                          <span>{advice.option?.label || advice.option?.id || '—'}</span>
                          {advice.option?.amount !== null && advice.option?.amount !== undefined
                            ? <span className="mono">{advice.option.amount}BB</span> : null}
                          {advice.reason
                            ? <span className="muted source-advice-reason">{advice.reason}</span>
                            : null}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
                {(source.mark.note || source.mark.tags?.length) ? (
                  <div className="source-item-readonly">
                    <div className="readonly-label">控制台标记（可编辑）</div>
                    {source.mark.note ? <div>{source.mark.note}</div> : null}
                    {source.mark.tags?.length ? (
                      <Space size={4} wrap>{source.mark.tags.map((tag) => <Tag key={tag} className="mini-tag">{tag}</Tag>)}</Space>
                    ) : null}
                  </div>
                ) : null}
                <Button size="small" type="link" style={{ padding: 0 }}
                  onClick={() => navigate(`/hands/${source.handId}`)}>
                  查看该来源
                </Button>
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {canonicalId ? (
        <Card className="cases-card" bordered={false}>
          <div className="card-title">
            案例 <Tag className="mini-tag">{data.cases?.length || 0}</Tag>
            <span className="muted advice-hint">案例挂在 canonical 手牌上，按「街道:序号」指向具体决策点</span>
            <Button
              size="small" style={{ marginLeft: 'auto' }} disabled={!canonicalId}
              onClick={() => setBulkOpen(true)}
            >
              整手全建
            </Button>
          </div>
          {data.cases?.length ? (
            <div className="case-list">
              {data.cases.map((item) => (
                <div className="case-row" key={item.id}>
                  <Tag color="blue">{item.decisionKey}</Tag>
                  <Tag color={item.p0 ? 'red' : 'default'}>{item.p0 ? 'P0' : `P${item.priority}`}</Tag>
                  <span>{item.specName || '（spec 已删除）'}</span>
                  <span className="muted">{item.caseSetName || '未命名案例集'} rev.{item.caseSetRevision}</span>
                  {item.frozen ? <Tag className="mini-tag">已冻结</Tag> : <Tag className="mini-tag">草稿</Tag>}
                  {item.note ? <span className="muted">{item.note}</span> : null}
                </div>
              ))}
            </div>
          ) : (
            <div className="muted">还没有案例。在下面的决策点时间线里点「建案例」。</div>
          )}
        </Card>
      ) : null}

      <Card className="timeline-card" bordered={false}>
        <div className="card-title">
          决策点时间线 <Tag className="mini-tag">{data.decisions.length}</Tag>
          <span className="muted advice-hint">每个决策点都能建案例；「看对比」跳到下方逐街对比</span>
        </div>
        {data.decisions.length ? (
          <div className="timeline-list">
            {data.decisions.map((decision, index) => (
              <div className="timeline-row" key={decision.id ?? index}>
                <div className="timeline-head">
                  <Tag color="blue">{STREET_NAMES[decision.street] || decision.street} D{decision.seq + 1}</Tag>
                  <span className="mono muted">{decision.decisionKey || `${decision.street}:${decision.seq}`}</span>
                  <QualityBadge quality={decision.quality} />
                  {decision.actual?.label ? <span>实际 {decision.actual.label}</span> : null}
                  <span className="muted">底池 {formatAmount(decision.pot)} · 需跟 {formatAmount(decision.toCall)}</span>
                </div>
                <Space size={4} wrap className="timeline-buckets">
                  {Object.entries(decision.buckets || {}).map(([dimension, value]) => (
                    <Tag key={dimension} className="mini-tag">
                      {dimensionLabels.get(dimension) || dimension}：{bucketLabels.get(`${dimensionLabels.get(dimension) || dimension}:${value}`) || value}
                    </Tag>
                  ))}
                  {(hand.bucketChips || []).length && !Object.keys(decision.buckets || {}).length ? (
                    <span className="muted">桶标签未落库（旧决策点）</span>
                  ) : null}
                </Space>
                <Space size={4}>
                  <Button size="small" onClick={() => scrollToDecision(decision.seq)}>看对比</Button>
                  <Button size="small" type="primary" ghost
                    disabled={!canonicalId || !decision.decisionKey}
                    onClick={() => openCase(decision)}>
                    建案例
                  </Button>
                  <Button size="small" type="link" style={{ padding: 0 }}
                    onClick={() => navigate(`/hands/${detailHandId}/replay`)}>
                    旧重跑/回放
                  </Button>
                </Space>
              </div>
            ))}
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可重建的决策点" />
        )}
      </Card>

      <div className="detail-grid">
        <Card className="mark-card" bordered={false}>
          <div className="card-title">收藏与结论</div>
          <div className="mark-row">
            <label>状态</label>
            <Select
              value={status}
              onChange={(value) => setStatus(value)}
              options={[
                { value: 'todo', label: '待复核' },
                { value: 'confirmed', label: '已确认' },
                { value: 'disputed', label: '有争议' },
              ]}
              style={{ width: '100%' }}
            />
          </div>
          <div className="mark-row">
            <label>标签</label>
            <Select
              mode="tags"
              value={tags}
              onChange={setTags}
              placeholder="典型 / 边界 / 漏点 / 坚果"
              options={(meta.data?.tags || ['典型', '边界', '漏点', '坚果']).map((tag) => ({ value: tag, label: tag }))}
              style={{ width: '100%' }}
            />
          </div>
          <div className="mark-row">
            <label>一句话结论</label>
            <Input.TextArea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              autoSize={{ minRows: 3, maxRows: 6 }}
              placeholder="例如：转牌应该弃牌，理由…"
            />
          </div>
          <Button type="primary" block onClick={() => saveMark.mutate(detailHandId)} loading={saveMark.isPending}>
            保存
          </Button>
        </Card>

        <Card className="street-card" bordered={false}>
          <div className="card-title">行动街</div>
          <div className="street-list">
            {data.streets.map((street) => (
              <div key={street.street} className={`street-row${street.street === activeStreet ? ' current' : ''}`}>
                <div className="street-side">
                  <div className="street-name">{street.label}</div>
                  <div className="street-pot">起始 {street.potStartBb !== null && street.potStartBb !== undefined
                    ? `${formatAmount(street.potStartBb)} BB` : '—'}</div>
                </div>
                <div className="street-actions">
                  {street.actions.length ? street.actions.map((action) => (
                    <button
                      key={`${street.street}-${action.index}`}
                      className={`action-chip${action.isHero ? ' hero' : ''}${action.decisionSeq !== null ? ' clickable' : ''}`}
                      onClick={() => action.decisionSeq !== null && scrollToDecision(action.decisionSeq)}
                      disabled={action.decisionSeq === null}
                    >
                      {action.label}
                      {action.decisionSeq !== null ? <span className="action-d">D{action.decisionSeq + 1}</span> : null}
                    </button>
                  )) : <span className="muted">这条街没有记录</span>}
                </div>
              </div>
            ))}
          </div>
          {data.duplicates.length ? (
            <div className="duplicates">
              内容重复：
              {data.duplicates.map((dup) => (
                <Button key={dup.id} type="link" size="small" onClick={() => navigate(`/hands/${dup.id}`)}>
                  #{dup.id}
                </Button>
              ))}
            </div>
          ) : null}
        </Card>
      </div>

      {data.advices?.length ? (
        <Card className="advice-card" bordered={false}>
          <div className="card-title">
            导入时的 AI 建议 <Tag className="mini-tag">{data.advices.length}</Tag>
            <span className="muted advice-hint">来自日志里的 advice 记录，按街顺序挂在手牌下</span>
          </div>
          <div className="advice-list">
            {data.advices.map((advice, index) => (
              <div className="advice-row" key={`${advice.street}-${advice.ts || index}`}>
                <div className="advice-head">
                  <span className="advice-street">
                    {STREET_NAMES[advice.street || ''] || advice.street || '—'}
                  </span>
                  <span className={`option-pill kind-${advice.option?.kind || 'none'}`}>
                    {advice.option?.label || advice.option?.id || '—'}
                  </span>
                  {advice.option?.amount !== null && advice.option?.amount !== undefined ? (
                    <span className="muted">{formatAmount(advice.option.amount)} BB</span>
                  ) : null}
                  {advice.confidence !== null && advice.confidence !== undefined ? (
                    <Tag className="mini-tag">置信度 {advice.confidence}</Tag>
                  ) : null}
                  {advice.model ? <Tag className="mini-tag">{advice.model}</Tag> : null}
                  {advice.source ? <Tag className="mini-tag">{advice.source}</Tag> : null}
                  {advice.promptVersion ? <Tag className="mini-tag">{advice.promptVersion}</Tag> : null}
                  {advice.actionIndex !== null && advice.actionIndex !== undefined ? (
                    <Tag className="mini-tag">动作 #{advice.actionIndex}</Tag>
                  ) : null}
                  {advice.promptMissing ? <Tag color="orange" className="mini-tag">prompt 缺失</Tag> : null}
                  {advice.bucket ? <BucketTag bucket={advice.bucket} /> : null}
                  <Button size="small" type="link" style={{ padding: 0 }}
                    onClick={() => navigate(`/hands/${detailHandId}/replay`)}>
                    看回放 / Prompt
                  </Button>
                </div>
                {advice.reason ? <div className="advice-reason">{advice.reason}</div> : null}
                {advice.plan ? <div className="advice-plan">计划：{advice.plan}</div> : null}
                <AdviceContext advice={advice} />
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      <div className="section-head">
        <div>
          <h2>逐街决策对比</h2>
          <p className="muted">{blocks.length} 个决策点 · 实际动作 + 多版本建议 + 重复分布</p>
        </div>
        <Space wrap>
          <Select
            mode="multiple"
            value={selectedVersions}
            onChange={setSelectedVersions}
            placeholder="选择版本"
            style={{ minWidth: 380, maxWidth: 620 }}
            options={versionItems.map((version) => ({
              value: version.id,
              label: `${version.name}${version.archived ? '（已归档）' : ''}`,
            }))}
          />
          <label className="inline-switch">
            <Switch size="small" checked={onlyDiverged} onChange={setOnlyDiverged} />
            仅看分歧
          </label>
          <Button icon={<ReloadOutlined />} onClick={() => comparison.refetch()} loading={comparison.isFetching}>
            刷新结果
          </Button>
        </Space>
      </div>

      {comparison.isFetching ? <div className="inline-loading"><Spin size="small" /> 加载对比…</div> : null}
      {comparison.error ? <Alert type="error" showIcon message={errorMessage(comparison.error)} /> : null}

      <div className="decision-list">
        {filteredBlocks.length ? filteredBlocks.map((decision) => (
          <DecisionCard
            key={`${decision.decision.street}-${decision.decision.seq}`}
            decision={decision}
            onReview={openReview}
          />
        )) : (
          <Empty
            description={onlyDiverged ? '当前版本之间没有分歧' : '还没有可展示的决策点'}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        )}
      </div>

      <Modal
        open={reviewTarget !== null}
        title={reviewTarget ? `复盘 D${reviewTarget.seq + 1} · ${STREET_NAMES[reviewTarget.street] || reviewTarget.street}` : '复盘'}
        okText="保存"
        cancelText="取消"
        onCancel={() => setReviewTarget(null)}
        onOk={() => reviewMutation.mutate()}
        confirmLoading={reviewMutation.isPending}
      >
        <div className="mark-row">
          <label>结论</label>
          <Select
            value={reviewVerdict}
            onChange={(value) => setReviewVerdict(value)}
            options={[
              { value: 'good', label: '好决策' },
              { value: 'bad', label: '坏决策' },
              { value: 'unsure', label: '待定' },
            ]}
            style={{ width: '100%' }}
          />
        </div>
        <div className="mark-row">
          <label>标签</label>
          <Select
            mode="tags"
            value={reviewTags}
            onChange={setReviewTags}
            placeholder="例如：河牌薄价值 / 弃牌过紧 / 尺寸"
            options={['河牌薄价值', '弃牌过紧', '尺寸', '读人', '范围'].map((tag) => ({ value: tag, label: tag }))}
            style={{ width: '100%' }}
          />
        </div>
        <div className="mark-row">
          <label>备注</label>
          <Input.TextArea
            value={reviewNote}
            onChange={(event) => setReviewNote(event.target.value)}
            autoSize={{ minRows: 3, maxRows: 6 }}
            placeholder="当时应该怎么打，为什么"
          />
        </div>
      </Modal>

      <Modal
        open={caseTarget !== null}
        title={caseTarget
          ? `建案例 · ${STREET_NAMES[caseTarget.street] || caseTarget.street} D${caseTarget.seq + 1}`
          : '建案例'}
        okText="建案例"
        cancelText="取消"
        confirmLoading={createCase.isPending}
        okButtonProps={{ disabled: !canonicalId || !caseSpecId }}
        onOk={() => createCase.mutate()}
        onCancel={() => setCaseTarget(null)}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          {!canonicalId ? (
            <Alert type="warning" showIcon message="这手牌还没有 canonical 映射，先跑 013 回填再建案例" />
          ) : null}
          <div>
            <div className="muted">案例集</div>
            <Select
              style={{ width: '100%' }}
              placeholder="默认放进「待整理」"
              value={caseSetId ?? undefined}
              onChange={(value) => setCaseSetId(value)}
              options={(caseSets.data?.items || [])
                .filter((item) => !item.frozen)
                .map((item) => ({
                  value: item.id,
                  label: `${item.name} · v${item.revision}（${item.caseCount} 条）`,
                }))}
              notFoundContent="没有未冻结的案例集；先建一个"
            />
          </div>
          {specs.isLoading ? <Spin /> : specs.data?.items.length ? (
            <div>
              <div className="muted">期望（spec）</div>
              <Select
                style={{ width: '100%' }}
                placeholder="选择这条案例要校验的期望"
                value={caseSpecId ?? undefined}
                onChange={(value) => setCaseSpecId(value)}
                options={specs.data.items.map((spec) => ({ value: spec.id, label: `#${spec.id} ${spec.name}` }))}
              />
            </div>
          ) : (
            <Alert
              type="warning"
              showIcon
              message="还没有 active 状态的期望"
              description="案例必须绑定 spec；先去「规格」建一条并激活。"
              action={<Button size="small" onClick={() => navigate('/specs')}>去规格</Button>}
            />
          )}
          <div>
            <div className="muted">优先级</div>
            <Segmented
              value={casePriority}
              onChange={(value) => setCasePriority(Number(value))}
              options={[{ label: 'P0 必过', value: 0 }, { label: 'P1', value: 1 }, { label: 'P2', value: 2 }]}
            />
          </div>
          <div>
            <div className="muted">备注</div>
            <Input.TextArea value={caseNote} onChange={(event) => setCaseNote(event.target.value)}
              autoSize={{ minRows: 2, maxRows: 4 }} placeholder="这条案例为什么值得留（可留空）" />
          </div>
        </Space>
      </Modal>

      <Modal
        open={bulkOpen}
        title="整手全建"
        okText="建案例"
        cancelText="取消"
        confirmLoading={bulkCreateCases.isPending}
        okButtonProps={{ disabled: !canonicalId || caseSetId === null }}
        onOk={() => bulkCreateCases.mutate()}
        onCancel={() => setBulkOpen(false)}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div className="muted">
            把当前这手牌的每个决策点都建成案例：自动挑最具体的 active 期望，写死 spec_id；
            每个决策点用「优先级」统一设置。
          </div>
          <div>
            <div className="muted">案例集</div>
            <Select
              style={{ width: '100%' }}
              value={caseSetId ?? undefined}
              placeholder="选择未冻结的案例集"
              onChange={(value) => setCaseSetId(value)}
              options={(caseSets.data?.items || [])
                .filter((item) => !item.frozen)
                .map((item) => ({
                  value: item.id,
                  label: `${item.name} · v${item.revision}`,
                }))}
            />
          </div>
          <div>
            <div className="muted">优先级</div>
            <Segmented
              value={casePriority}
              onChange={(value) => setCasePriority(Number(value))}
              options={[{ label: 'P0 必过', value: 0 }, { label: 'P1', value: 1 }, { label: 'P2', value: 2 }]}
            />
          </div>
        </Space>
      </Modal>

      <RunModal
        open={rerunOpen}
        scope="hands"
        handIds={[detailHandId]}
        onClose={() => setRerunOpen(false)}
        title={`重跑 #${id} ${hand.heroCards}`}
      />
    </div>
  )
}
