import { Tag, Tooltip } from 'antd'
import type { Quality, RunStatus } from '../types'
import { QUALITY, RUN_STATUS, bucketColor, cardParts, cardsFromString, sourceLabel } from '../lib/format'

export function CardChip({ code, small = false }: { code: string; small?: boolean }) {
  const { rank, symbol, red } = cardParts(code)
  return (
    <span className={`card-chip${small ? ' card-chip-sm' : ''}${red ? ' card-red' : ''}`}>
      <span className="card-rank">{rank}</span>
      <span className="card-suit">{symbol}</span>
    </span>
  )
}

export function HandCards({ cards, small = false }: { cards: string | string[]; small?: boolean }) {
  const list = Array.isArray(cards) ? cards : cardsFromString(cards)
  if (!list.length) return <span className="muted">—</span>
  return (
    <span className="card-row">
      {list.map((card) => <CardChip key={card} code={card} small={small} />)}
    </span>
  )
}

export function BoardCards({ board, small = true }: { board: string | string[]; small?: boolean }) {
  const list = Array.isArray(board) ? board : cardsFromString(board)
  if (!list.length) return <span className="muted">—</span>
  const groups = [
    { label: 'F', cards: list.slice(0, 3) },
    { label: 'T', cards: list.slice(3, 4) },
    { label: 'R', cards: list.slice(4, 5) },
  ].filter((group) => group.cards.length)
  return (
    <span className="board-row">
      {groups.map((group, index) => (
        <span className="board-group" key={group.label}>
          {index > 0 && (small ? <span className="street-gap" /> : <span className="street-sep">|</span>)}
          {group.cards.map((card) => <CardChip key={card} code={card} small={small} />)}
        </span>
      ))}
    </span>
  )
}

export function QualityBadge({ quality, showText = true }: { quality?: Quality | null; showText?: boolean }) {
  if (!quality) return null
  const meta = QUALITY[quality] || QUALITY.approx
  return (
    <span className={`quality-badge quality-${quality}`}>
      <span className="quality-dot" />
      {showText ? meta.label : null}
    </span>
  )
}

export function RunStatusTag({ status }: { status: RunStatus }) {
  const meta = RUN_STATUS[status] || { label: status, color: 'default' }
  return <Tag color={meta.color} className="status-tag">{meta.label}</Tag>
}

export function BucketTag({ bucket }: { bucket?: string | null }) {
  if (!bucket) return null
  if (bucket === 'error') return <Tag className="mini-tag">失败</Tag>
  return <Tag color={bucketColor(bucket)} className="mini-tag">{bucket}</Tag>
}

export function SourceTag({ source }: { source?: string | null }) {
  if (!source || source === 'model') return null
  const meta = sourceLabel(source)
  return <Tag color={meta.color} className="mini-tag">{meta.label}</Tag>
}

export function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  )
}

export function MonoTooltip({ text }: { text: string }) {
  return (
    <Tooltip title={<span style={{ wordBreak: 'break-all', fontSize: 12 }}>{text}</span>}>
      <span className="mono truncate">{text}</span>
    </Tooltip>
  )
}
