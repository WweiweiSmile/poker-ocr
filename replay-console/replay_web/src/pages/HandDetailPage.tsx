import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Divider, Empty, Input, Popover, Select,
  Skeleton, Space, Spin, Switch, Tag, Tooltip,
} from 'antd'
import {
  ArrowLeftOutlined, DeleteOutlined, ReloadOutlined, StarFilled,
  StarOutlined, ThunderboltOutlined, WarningOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type {
  ComparisonDecision, HandComparison, HandDeleteResult, HandDetail, Meta,
  Version, VersionBlock,
} from '../types'
import {
  BoardCards, BucketTag, HandCards, QualityBadge, SourceTag,
} from '../components/CardChips'
import RunModal from '../components/RunModal'
import { formatAmount, formatBB, formatDate } from '../lib/format'

const STREET_ORDER: Record<string, number> = { preflop: 0, flop: 1, turn: 2, river: 3 }

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
    items.push({ label: '至少一家领先', value: `${facts.beatAtLeastPct}%` })
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

function DecisionCard({ decision }: { decision: ComparisonDecision }) {
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

  const saveMark = useMutation({
    mutationFn: () => api.patch(`/hands/${id}/mark`, {
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
    mutationFn: (favorite: boolean) => api.patch(`/hands/${id}/mark`, { favorite }),
    onSuccess: (_data, favorite) => {
      setMarkFavorite(favorite)
      queryClient.invalidateQueries({ queryKey: ['hand', id] })
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const deleteMutation = useMutation({
    mutationFn: () => api.delete<HandDeleteResult>(`/hands/${id}`),
    onSuccess: () => {
      message.success(`手牌 #${id} 已删除`)
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
      navigate('/hands')
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const confirmDelete = () => {
    modal.confirm({
      title: `删除手牌 #${id}`,
      content: '导入手牌会软删除，已跑结果保留；复盘手牌只会从控制台隐藏，不会修改 review_hands。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          await deleteMutation.mutateAsync()
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
  const versionItems = versions.data?.items || []
  const blocked = data.reconstruction.quality === 'invalid'

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
                onClick={() => toggleFavorite.mutate(!markFavorite)}
                aria-label="收藏"
              >
                {markFavorite ? <StarFilled /> : <StarOutlined />}
              </button>
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
          <Button type="primary" block onClick={() => saveMark.mutate()} loading={saveMark.isPending}>
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
          <DecisionCard key={`${decision.decision.street}-${decision.decision.seq}`} decision={decision} />
        )) : (
          <Empty
            description={onlyDiverged ? '当前版本之间没有分歧' : '还没有可展示的决策点'}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        )}
      </div>

      <RunModal
        open={rerunOpen}
        scope="hands"
        handIds={[id]}
        onClose={() => setRerunOpen(false)}
        title={`重跑 #${id} ${hand.heroCards}`}
      />
    </div>
  )
}
