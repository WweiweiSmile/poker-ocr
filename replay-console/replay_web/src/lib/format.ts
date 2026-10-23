import dayjs from 'dayjs'
import type { Quality, RunStatus } from '../types'

export function cardsFromString(text?: string | null): string[] {
  const value = (text || '').trim()
  const out: string[] = []
  for (let i = 0; i + 1 < value.length; i += 2) out.push(value.slice(i, i + 2))
  return out
}

const SUITS: Record<string, { symbol: string; red: boolean }> = {
  s: { symbol: '♠', red: false },
  h: { symbol: '♥', red: true },
  d: { symbol: '♦', red: true },
  c: { symbol: '♣', red: false },
}

export function cardParts(code: string) {
  const text = (code || '').trim()
  const suit = text.slice(-1).toLowerCase()
  const rank = text.slice(0, -1) || '?'
  const meta = SUITS[suit] || { symbol: suit || '?', red: false }
  return { rank, suit, ...meta }
}

export function formatBB(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  const abs = Math.abs(number)
  const text = abs >= 100 ? abs.toFixed(0) : abs.toFixed(digits).replace(/\.0$/, '')
  return `${number > 0 ? '+' : number < 0 ? '−' : ''}${text}`
}

export function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const number = Number(value)
  return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
}

export function formatDate(value?: string | null, withTime = true): string {
  if (!value) return '—'
  return dayjs(value).format(withTime ? 'MM-DD HH:mm' : 'YYYY-MM-DD')
}

export function relativeTime(value?: string | null): string {
  if (!value) return '—'
  const date = dayjs(value)
  const diff = dayjs().diff(date, 'minute')
  if (diff < 1) return '刚刚'
  if (diff < 60) return `${diff} 分钟前`
  if (diff < 60 * 24) return `${Math.floor(diff / 60)} 小时前`
  if (diff < 60 * 24 * 7) return `${Math.floor(diff / 60 / 24)} 天前`
  return date.format('MM-DD HH:mm')
}

export const QUALITY: Record<Quality, { label: string; color: string; dot: string }> = {
  exact: { label: '精确', color: 'green', dot: 'exact' },
  approx: { label: '重建可疑', color: 'gold', dot: 'approx' },
  invalid: { label: '不可重跑', color: 'red', dot: 'invalid' },
}

export const RUN_STATUS: Record<RunStatus, { label: string; color: string }> = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  succeeded: { label: '完成', color: 'success' },
  partial: { label: '部分完成', color: 'warning' },
  failed: { label: '失败', color: 'error' },
  canceled: { label: '已取消', color: 'default' },
}

export function scopeLabel(scope?: string | null): string {
  if (scope === 'favorites') return '收藏夹'
  if (scope === 'filter') return '筛选结果'
  if (scope === 'hands') return '选中手牌'
  return scope || '—'
}

export function bucketColor(bucket?: string | null): string {
  if (bucket === '一致') return 'green'
  if (bucket === '尺度差') return 'gold'
  if (bucket === '动作不同') return 'red'
  if (bucket === 'error') return 'default'
  return 'default'
}

export function sourceLabel(source?: string | null): { label: string; color: string } {
  if (source === 'model') return { label: '模型', color: 'green' }
  if (source === 'fallback') return { label: '兜底', color: 'gold' }
  if (source === 'error') return { label: '失败', color: 'red' }
  return { label: source || '—', color: 'default' }
}

export function shortHash(hash?: string | null): string {
  if (!hash) return '—'
  return hash.length > 12 ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : hash
}
