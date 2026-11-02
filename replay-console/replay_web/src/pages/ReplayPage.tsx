import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Card, Drawer, Empty, Segmented, Skeleton, Space, Tag, Tabs,
} from 'antd'
import {
  ArrowLeftOutlined, CaretRightOutlined, LeftOutlined, PauseOutlined,
  RightOutlined, StepBackwardOutlined, StepForwardOutlined,
} from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, errorMessage } from '../api'
import type {
  HandReplay, PromptDetail, ReplayFrame,
} from '../types'
import { BoardCards, HandCards, QualityBadge } from '../components/CardChips'
import { formatAmount, formatBB, formatDate } from '../lib/format'

const STREET_NAMES: Record<string, string> = {
  preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌',
}
const ACTION_NAMES: Record<string, string> = {
  check: '过牌', call: '跟注', bet: '下注', raise: '加注',
  fold: '弃牌', allin: '全下',
}
const SEAT_POS: Record<string, string> = {
  hero: 'seat-hero', lowL: 'seat-low-l', lowR: 'seat-low-r',
  midL: 'seat-mid-l', midR: 'seat-mid-r', upL: 'seat-up-l',
  upR: 'seat-up-r', topL: 'seat-top-l', topR: 'seat-top-r',
}
// 旧记录只有 position、没有固定 seat_id：按位置给一个近似桌位。
const POS_CLASS: Record<string, string> = {
  UTG: 'seat-low-l', 'UTG+1': 'seat-mid-l', LJ: 'seat-up-l',
  HJ: 'seat-top-l', CO: 'seat-up-r', BTN: 'seat-mid-r',
  SB: 'seat-low-r', BB: 'seat-low-l',
}

function actionLabel(frame: ReplayFrame): string {
  if (!frame.action) return ''
  const base = ACTION_NAMES[frame.action] || frame.action
  if (frame.amount !== null && frame.amount !== undefined) {
    return `${base} ${formatAmount(frame.amount)}BB`
  }
  return base
}

function FrameRow({ frame, active, onClick, onPrompt }: {
  frame: ReplayFrame
  active: boolean
  onClick: () => void
  onPrompt: () => void
}) {
  if (frame.type === 'hand_start') {
    return (
      <div className={`wf-row wf-hand-start${active ? ' active' : ''}`} onClick={onClick}>
        <span className="wf-time">{formatDate(frame.ts || null, false)}</span>
        <span className="wf-title">手牌开始</span>
        <span className="wf-meta">
          {frame.table?.size || 0} 人桌 · 盲注 {frame.table?.blinds?.sb}/{frame.table?.blinds?.bb}
          {frame.table?.blinds?.ante ? ` · 前注 ${frame.table.blinds.ante}` : ''}
          {frame.table?.bombPot ? ' · 炸弹池' : ''}
        </span>
      </div>
    )
  }
  if (frame.type === 'street_start') {
    return (
      <div className={`wf-row wf-street${active ? ' active' : ''}`} onClick={onClick}>
        <div className="wf-street-head">
          <span className="wf-street-name">{STREET_NAMES[frame.street || ''] || frame.street}</span>
          <BoardCards board={frame.board || []} small />
          <span className="wf-meta">底池 {formatAmount(frame.potStart)}BB</span>
          {frame.derived ? <Tag className="mini-tag">推算</Tag> : null}
        </div>
      </div>
    )
  }
  if (frame.type === 'decision') {
    const advice = frame.advice
    return (
      <div className={`wf-row wf-decision${active ? ' active' : ''}`} onClick={onClick}>
        <div className="wf-decision-head">
          <Tag color="teal">D{frame.seq}</Tag>
          <span className="wf-actor">Hero 决策</span>
          <span className={`option-pill kind-${advice?.kind || 'none'}`}>
            {advice?.label || advice?.optionId || '无建议'}
          </span>
          {advice?.source ? <Tag className="mini-tag">{advice.source}</Tag> : null}
          {advice?.durationMs ? <span className="wf-meta">{Math.round(advice.durationMs)}ms</span> : null}
          {advice?.promptMissing ? <Tag color="orange" className="mini-tag">prompt 缺失</Tag> : null}
        </div>
        <div className="wf-reason">{advice?.reasonFull || advice?.reason || '（无理由）'}</div>
        <Space size={6} className="wf-decision-actions">
          <Button size="small" onClick={(event) => {
            event.stopPropagation()
            onPrompt()
          }}>
            看 Prompt
          </Button>
          <span className="wf-meta mono">{advice?.promptSha256?.slice(0, 12) || '—'}</span>
        </Space>
      </div>
    )
  }
  if (frame.type === 'action') {
    return (
      <div className={`wf-row wf-action${active ? ' active' : ''}`} onClick={onClick}>
        <span className={`wf-actor${frame.actor?.seatId === 'hero' ? ' hero' : ''}`}>
          {frame.actor?.name || frame.actor?.position || frame.actor?.seatId || '?'}
        </span>
        <span className={`wf-action-text action-${frame.action || ''}`}>{actionLabel(frame)}</span>
        <span className="wf-meta">底池 {formatAmount(frame.potAfter)}BB</span>
        {frame.inferred ? <Tag className="mini-tag">补全</Tag> : null}
        {frame.derived ? <Tag className="mini-tag">推算</Tag> : null}
      </div>
    )
  }
  if (frame.type === 'hand_end') {
    return (
      <div className={`wf-row wf-end${active ? ' active' : ''}`} onClick={onClick}>
        <span className="wf-title">手牌结束</span>
        <span className="wf-meta">
          {frame.result?.outcome || 'unknown'} · {formatBB(frame.result?.heroNetBb)}
        </span>
        {frame.quality ? <QualityBadge quality={frame.quality as never} /> : null}
      </div>
    )
  }
  return null
}

function Waterfall({ data, cursor, onSelect, onPrompt }: {
  data: HandReplay
  cursor: number
  onSelect: (index: number) => void
  onPrompt: (frame: ReplayFrame) => void
}) {
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const row = listRef.current?.querySelector(`[data-frame="${cursor}"]`)
    row?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [cursor])
  return (
    <div className="replay-waterfall" ref={listRef}>
      {data.frames.map((frame, index) => (
        <div key={frame.seq} data-frame={index} className={index === cursor ? 'current' : ''}>
          <FrameRow
            frame={frame}
            active={index === cursor}
            onClick={() => onSelect(index)}
            onPrompt={() => onPrompt(frame)}
          />
        </div>
      ))}
      {!data.frames.length ? <Empty description="没有可回放的事件" /> : null}
    </div>
  )
}

interface TableState {
  roster: NonNullable<ReplayFrame['roster']>
  stacks: Record<string, number | null>
  bets: Record<string, number>
  pot: number
  board: string[]
  street: string
  lastAction: ReplayFrame | null
  advice: ReplayFrame | null
}

function tableState(frames: ReplayFrame[], cursor: number): TableState {
  const state: TableState = {
    roster: [], stacks: {}, bets: {}, pot: 0, board: [],
    street: 'preflop', lastAction: null, advice: null,
  }
  for (let i = 0; i <= Math.min(cursor, frames.length - 1); i += 1) {
    const frame = frames[i]
    if (frame.type === 'hand_start') {
      state.roster = frame.roster || []
      for (const seat of state.roster) {
        if (seat.seatId) state.stacks[seat.seatId] = seat.stack ?? null
      }
    } else if (frame.type === 'street_start') {
      state.street = frame.street || state.street
      state.board = frame.board || state.board
      state.bets = {}
      state.pot = Number(frame.potStart || 0)
    } else if (frame.type === 'action') {
      state.pot = Number(frame.potAfter ?? state.pot)
      state.bets = { ...(frame.betsAfter || {}) }
      for (const [seat, stack] of Object.entries(frame.stacksAfter || {})) {
        state.stacks[seat] = stack
      }
      state.lastAction = frame
    } else if (frame.type === 'decision') {
      state.advice = frame
      state.pot = Number((frame.snapshot as Record<string, unknown>)?.pot ?? state.pot)
    }
  }
  return state
}

function TableView({ data, cursor, onPrompt }: {
  data: HandReplay
  cursor: number
  onPrompt: (frame: ReplayFrame) => void
}) {
  const state = useMemo(() => tableState(data.frames, cursor), [data.frames, cursor])
  const seats = state.roster.filter((seat) => seat.seatId)
  const hero = seats.find((seat) => seat.seatId === 'hero' || seat.seatId === 'hero')
  const adviceFrame = state.advice
  return (
    <div className="replay-table-wrap">
      <div className="replay-table">
        <div className="table-oval" />
        {seats.map((seat) => {
          const id = String(seat.seatId)
          const cls = SEAT_POS[id] || POS_CLASS[String(seat.position || '')] || ''
          const stack = state.stacks[id] ?? seat.stack
          const bet = state.bets[id] || 0
          const isHero = id === 'hero'
          return (
            <div className={`replay-seat ${cls}${isHero ? ' hero' : ''}`} key={id}>
              <div className="replay-seat-head">
                <span className="replay-seat-pos">{seat.position || id}</span>
                <span className="replay-seat-name">{seat.name || '空座'}</span>
              </div>
              <div className="replay-seat-stack">{formatAmount(stack)} BB</div>
              {isHero ? <HandCards cards={data.hand.heroCards} small /> : <div className="seat-card-back">🂠</div>}
              {bet > 0 ? <div className="replay-seat-bet">{formatAmount(bet)}</div> : null}
              {state.lastAction?.actor?.seatId === id ? (
                <div className="replay-seat-action">{actionLabel(state.lastAction)}</div>
              ) : null}
            </div>
          )
        })}
        <div className="table-center">
          <div className="table-street">{STREET_NAMES[state.street] || state.street}</div>
          <BoardCards board={state.board} small={false} />
          <div className="table-pot">底池 {formatAmount(state.pot)} BB</div>
        </div>
        {adviceFrame?.advice ? (
          <div className="table-advice-bubble">
            <div className="table-advice-head">
              <Tag color="teal">AI</Tag>
              <span className={`option-pill kind-${adviceFrame.advice.kind || 'none'}`}>
                {adviceFrame.advice.label || adviceFrame.advice.optionId}
              </span>
              <span className="muted">{adviceFrame.advice.source}</span>
              <span className="spacer" />
              <Button size="small" onClick={() => onPrompt(adviceFrame)}>Prompt</Button>
            </div>
            <div className="table-advice-reason">{adviceFrame.advice.reason || '—'}</div>
          </div>
        ) : null}
        <div className="table-hero-label">
          {hero?.name || 'Hero'} · {data.hand.heroPosition || '—'}
        </div>
      </div>
    </div>
  )
}

function PromptDrawer({ handId, frame, onClose }: {
  handId: number
  frame: ReplayFrame | null
  onClose: () => void
}) {
  const adviceId = frame?.advice?.adviceId || null
  const prompt = useQuery<PromptDetail>({
    queryKey: ['prompt', handId, adviceId],
    queryFn: () => api.get(`/hands/${handId}/advices/${adviceId}/prompt`),
    enabled: !!adviceId,
  })
  const [tab, setTab] = useState('system')
  useEffect(() => { setTab('system') }, [adviceId])
  return (
    <Drawer
      title={frame ? `D${frame.seq} · ${STREET_NAMES[frame.street || ''] || frame.street} · Prompt` : 'Prompt'}
      width={760}
      open={!!frame}
      onClose={onClose}
      destroyOnClose
    >
      {!adviceId ? <Alert type="warning" showIcon message="这个决策点没有 adviceId（旧记录）" /> : null}
      {prompt.isLoading ? <Skeleton active /> : null}
      {prompt.error ? <Alert type="error" showIcon message={errorMessage(prompt.error)} /> : null}
      {prompt.data ? (
        <div className="prompt-inspector">
          <Space wrap size={6} className="prompt-meta">
            <Tag>{prompt.data.promptVersion || '—'}</Tag>
            <Tag className="mono">{prompt.data.promptSha256?.slice(0, 12) || 'no-sha'}</Tag>
            <Tag>{(prompt.data.promptBytes / 1024).toFixed(1)} KB</Tag>
            <Tag>{prompt.data.tokensIn ?? '—'} in / {prompt.data.tokensOut ?? '—'} out</Tag>
            <Tag>{prompt.data.durationMs ?? '—'}ms</Tag>
            {prompt.data.promptMissing ? <Tag color="orange">prompt 缺失</Tag> : null}
          </Space>
          {prompt.data.promptMissing ? (
            <Alert type="warning" showIcon className="prompt-alert"
              message={`没有保存 prompt：${prompt.data.promptMissingReason || '旧记录'}`}
              description="这条建议仍保留了 option/reason/context；prompt 只在有模型调用且版本 >= 本次改动时才有。" />
          ) : null}
          <Tabs
            activeKey={tab}
            onChange={setTab}
            items={[
              { key: 'system', label: 'System', children: <pre className="prompt-pre">{prompt.data.system || '—'}</pre> },
              { key: 'user', label: 'User', children: <pre className="prompt-pre">{prompt.data.user || '—'}</pre> },
              { key: 'raw', label: 'Raw', children: <pre className="prompt-pre">{prompt.data.rawResponse || '—'}</pre> },
              {
                key: 'reason', label: 'Reason',
                children: <pre className="prompt-pre">{prompt.data.reasonFull || frame?.advice?.reason || '—'}</pre>,
              },
              {
                key: 'facts', label: 'Facts',
                children: <pre className="prompt-pre">{JSON.stringify(prompt.data.facts, null, 2)}</pre>,
              },
              {
                key: 'context', label: 'Context',
                children: <pre className="prompt-pre">{JSON.stringify(prompt.data.context, null, 2)}</pre>,
              },
            ]}
          />
        </div>
      ) : null}
    </Drawer>
  )
}

export default function ReplayPage() {
  const { handId } = useParams()
  const id = Number(handId)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const mode = searchParams.get('mode') === 'table' ? 'table' : 'waterfall'
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [pauseOnDecision, setPauseOnDecision] = useState(true)
  const [promptFrame, setPromptFrame] = useState<ReplayFrame | null>(null)
  const replay = useQuery<HandReplay>({
    queryKey: ['replay', id],
    queryFn: () => api.get(`/hands/${id}/replay`),
    enabled: Number.isFinite(id),
  })

  useEffect(() => {
    const raw = Number(searchParams.get('frame') || 0)
    setCursor(Number.isFinite(raw) ? raw : 0)
  }, [searchParams])

  const frames = replay.data?.frames || []
  useEffect(() => {
    if (!replay.data) return
    if (cursor >= frames.length) setCursor(Math.max(0, frames.length - 1))
  }, [replay.data, frames.length, cursor])

  const update = (next: number, replace = true) => {
    const bounded = Math.max(0, Math.min(next, Math.max(0, frames.length - 1)))
    setCursor(bounded)
    const params = new URLSearchParams(searchParams)
    params.set('mode', mode)
    params.set('frame', String(bounded))
    setSearchParams(params, { replace })
  }

  useEffect(() => {
    if (!playing || !frames.length) return
    const timer = window.setTimeout(() => {
      const next = cursor + 1
      if (next >= frames.length) {
        setPlaying(false)
        return
      }
      const target = frames[next]
      if (pauseOnDecision && target.type === 'decision') {
        update(next)
        setPlaying(false)
        return
      }
      update(next)
    }, Math.max(120, 900 / speed))
    return () => window.clearTimeout(timer)
  }, [playing, cursor, frames, speed, pauseOnDecision])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return
      if (event.code === 'Space') {
        event.preventDefault()
        setPlaying((value) => !value)
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault()
        update(cursor - 1)
      } else if (event.key === 'ArrowRight') {
        event.preventDefault()
        update(cursor + 1)
      } else if (event.key.toLowerCase() === 'p') {
        const frame = frames[cursor]
        if (frame?.advice?.adviceId) setPromptFrame(frame)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cursor, frames])

  if (replay.isLoading) return <div className="page-loading"><Skeleton active paragraph={{ rows: 10 }} /></div>
  if (replay.error || !replay.data) {
    return <Alert type="error" showIcon message="回放加载失败" description={errorMessage(replay.error)} />
  }
  const data = replay.data
  const stepDecision = (direction: 1 | -1) => {
    for (let i = cursor + direction; i >= 0 && i < frames.length; i += direction) {
      if (frames[i]?.type === 'decision') {
        update(i)
        return
      }
    }
  }

  return (
    <div className="replay-page">
      <div className="detail-back">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate(`/hands/${id}`)}>手牌详情</Button>
        <span className="mono muted">#{id}</span>
        <Tag color={data.hand.partial ? 'orange' : 'geekblue'}>{data.hand.partial ? '未完成' : '完整'}</Tag>
        <QualityBadge quality={data.hand.quality} />
      </div>

      <Card className="replay-hero" bordered={false}>
        <div className="replay-hero-main">
          <div className="replay-hero-left">
            <HandCards cards={data.hand.heroCards} />
            <div>
              <div className="hand-hero-title">{data.hand.heroPosition || '—'} · {data.hand.heroCards.join(' ')}</div>
              <div className="hand-hero-sub">
                <span>{data.hand.tableSize} 人桌</span>
                <span>{data.hand.uid || `#${data.hand.id}`}</span>
                <span>{formatDate(data.hand.createdAt, false)}</span>
              </div>
            </div>
          </div>
          <div className="replay-hero-center">
            <div className="hero-label">公共牌</div>
            <BoardCards board={data.hand.board} />
          </div>
          <div className="replay-hero-right">
            <div className={`hero-result ${data.hand.result.heroNetBb && data.hand.result.heroNetBb > 0 ? 'up' : data.hand.result.heroNetBb && data.hand.result.heroNetBb < 0 ? 'down' : ''}`}>
              {data.hand.result.heroNetBb !== null && data.hand.result.heroNetBb !== undefined
                ? <>{formatBB(data.hand.result.heroNetBb)}<span className="hero-result-unit">BB</span></>
                : <span className="hero-result-text">{data.hand.result.outcome}</span>}
            </div>
            <div className="muted">帧 {Math.min(cursor + 1, frames.length)} / {frames.length}</div>
            <Segmented
              value={mode}
              onChange={(value) => {
                const params = new URLSearchParams(searchParams)
                params.set('mode', String(value))
                setSearchParams(params, { replace: true })
              }}
              options={[{ label: '瀑布流', value: 'waterfall' }, { label: '牌桌', value: 'table' }]}
            />
          </div>
        </div>
      </Card>

      <Card className="replay-player" bordered={false}>
        <div className="replay-controls">
          <Space size={8}>
            <Button icon={<StepBackwardOutlined />} onClick={() => stepDecision(-1)}>上一决策</Button>
            <Button icon={<LeftOutlined />} onClick={() => update(cursor - 1)}>上一帧</Button>
            <Button type="primary" icon={playing ? <PauseOutlined /> : <CaretRightOutlined />}
              onClick={() => setPlaying((value) => !value)}>
              {playing ? '暂停' : '播放'}
            </Button>
            <Button icon={<RightOutlined />} onClick={() => update(cursor + 1)}>下一帧</Button>
            <Button icon={<StepForwardOutlined />} onClick={() => stepDecision(1)}>下一决策</Button>
          </Space>
          <Space size={8}>
            <Segmented
              value={speed}
              onChange={(value) => setSpeed(Number(value))}
              options={[0.5, 1, 2, 4].map((value) => ({ label: `${value}x`, value }))}
            />
            <label className="replay-check">
              <input type="checkbox" checked={pauseOnDecision}
                onChange={(event) => setPauseOnDecision(event.target.checked)} />
              决策点暂停
            </label>
            <span className="muted mono">{frames[cursor] ? `#${frames[cursor].seq} ${frames[cursor].type}` : '—'}</span>
          </Space>
        </div>

        {mode === 'waterfall' ? (
          <Waterfall data={data} cursor={cursor} onSelect={update}
            onPrompt={(frame) => setPromptFrame(frame)} />
        ) : (
          <TableView data={data} cursor={cursor} onPrompt={(frame) => setPromptFrame(frame)} />
        )}
      </Card>

      <div className="replay-footnote">
        <Space wrap size={8}>
          <Tag>空格 播放/暂停</Tag>
          <Tag>←/→ 上一帧/下一帧</Tag>
          <Tag>P 打开 prompt</Tag>
          <Tag>点击任意行跳帧</Tag>
        </Space>
        {data.hand.warnings?.length ? (
          <Alert type="warning" showIcon message={data.hand.warnings.join('；')} />
        ) : null}
      </div>

      <PromptDrawer handId={id} frame={promptFrame} onClose={() => setPromptFrame(null)} />
    </div>
  )
}
