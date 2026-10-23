export type Quality = 'exact' | 'approx' | 'invalid'
export type RunStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'canceled'
export type Bucket = '一致' | '尺度差' | '动作不同' | 'error' | string

export interface Mark {
  handId?: number
  favorite: boolean
  tags: string[]
  status: 'todo' | 'confirmed' | 'disputed'
  note: string
  updatedAt?: string | null
}

export interface LastRun {
  jobId: number
  at: string
  versions: string[]
  versionIds: number[]
  repeat: number
}

export interface HandSummary {
  id: number
  title: string
  heroCards: string
  board: string
  boardByStreet: { flop: string[]; turn: string[]; river: string[] }
  heroPosition: string | null
  tableSize: number
  heroStackBb: number | null
  bombPotBb: number
  result: string
  resultAmount: number | null
  potType: string | null
  heroTags: string[]
  favorite: boolean
  markTags: string[]
  markStatus: string
  markNote: string
  decisionCount: number
  approxCount: number
  quality: Quality
  warnings: string[]
  invalidReason: string | null
  duplicate: boolean
  duplicateIds: number[]
  fingerprint: string
  hasRun: boolean
  lastRun: LastRun | null
  createdAt: string | null
  updatedAt: string | null
  userId: number
}

export interface ActualAction {
  action: string | null
  amountBb: number | null
  label: string
}

export interface DecisionMeta {
  id: number | null
  street: string
  seq: number
  actionIndex?: number
  pot: number
  toCall: number
  heroStack: number | null
  heroPosition: string | null
  heroCards: string[]
  board: string[]
  players: number
  opponents: number
  quality: Quality
  warnings: string[]
  actual: ActualAction
  snapshot?: Record<string, unknown>
}

export interface StreetAction {
  index: number
  actor: string
  actorLabel: string
  action: string
  amountBb: number | null
  isHero: boolean
  decisionSeq: number | null
  label: string
}

export interface StreetBlock {
  street: string
  label: string
  potStartBb: number | null
  actions: StreetAction[]
  decisionSeqs: number[]
}

export interface HandDetail {
  hand: HandSummary
  streets: StreetBlock[]
  decisions: DecisionMeta[]
  duplicates: { id: number; title: string; createdAt: string | null }[]
  mark: Mark
  reconstruction: {
    quality: Quality
    warnings: string[]
    invalidReason: string | null
    bombPot: boolean
    players: number
  }
  current: { adviceVersion: string; reconstructVersion: string }
}

export interface Version {
  id: number
  name: string
  provider: string
  baseUrl: string
  model: string
  apiKeyRef: string
  hasKey: boolean
  temperature: number | null
  topP: number | null
  maxTokens: number | null
  thinking: string | null
  reasoningEffort: string | null
  promptStyle: string
  adviceVersion: string
  reconstructVersion: string
  engineCommit: string | null
  engineDirty: boolean
  tableDynamics: string
  tableDynamicsSha: string | null
  tableDynamicsLength: number
  configHash: string
  notes: string
  archived: boolean
  createdAt: string | null
  taskCount: number
  lastUsedAt: string | null
  runnable: boolean
  blockers: { code: string; message: string }[]
  config?: Record<string, unknown>
}

export interface OptionFacts {
  handTier?: string | null
  handLabelEn?: string | null
  textureEn?: string | null
  equityPct?: number | null
  beatAtLeastPct?: number | null
  heroIsNuts?: boolean | null
  directive?: string | null
  options?: { id: string; label: string; kind: string; amount?: number | null }[]
  warnings?: string[]
  opponentsInHand?: number | null
  tableDynamics?: boolean
  actualViolationTags?: string[]
  error?: string
  [key: string]: unknown
}

export interface VersionSummary {
  optionId: string | null
  optionKind: string | null
  amount: number | null
  optionLabel: string | null
  n: number
  total: number
  distribution: Record<string, number>
  allSame: boolean
  source: string
  bucket: Bucket | null
  violationTags: string[]
  facts: OptionFacts | null
  label: string
}

export interface ResultRow {
  id: number | null
  repeat: number
  optionId: string | null
  optionKind: string | null
  amount: number | null
  optionLabel: string | null
  reason: string
  source: string
  model: string | null
  durationMs: number | null
  tokensIn: number | null
  tokensOut: number | null
  bucket: Bucket | null
  violationTags: string[]
  error: string | null
  facts: OptionFacts | null
}

export interface VersionBlock {
  versionId: number
  name: string
  model: string | null
  summary: VersionSummary
  results: ResultRow[]
}

export interface ComparisonDecision {
  decision: DecisionMeta
  actual: ActualAction
  versions: VersionBlock[]
  diverged: boolean
  previewFacts?: OptionFacts
}

export interface HandComparison {
  handId: number
  hand: HandSummary
  jobId: number | null
  versions: VersionSnapshot[]
  decisions: ComparisonDecision[]
  diverged: boolean
  quality: Quality
}

export interface VersionSnapshot {
  id: number
  name: string
  provider?: string
  baseUrl?: string
  model: string
  apiKeyRef?: string
  temperature?: number | null
  topP?: number | null
  maxTokens?: number | null
  thinking?: string | null
  reasoningEffort?: string | null
  promptStyle?: string
  adviceVersion?: string
  reconstructVersion?: string
  engineCommit?: string | null
  engineDirty?: boolean
  tableDynamicsSha?: string | null
  configHash?: string
}

export interface RunSkipped {
  duplicates: number
  invalid: number
  lowQuality: number
  duplicateHands?: { handId: number; reason: string }[]
  invalidHands?: { handId: number; reason: string }[]
  lowQualityHands?: { handId: number; reason: string }[]
  units: number
}

export interface RunPreview {
  scope: string
  versions: VersionSnapshot[]
  handIds: number[]
  handCount: number
  decisionCount: number
  totalUnits: number
  repeat: number
  dedupe: boolean
  includeQuality: string[]
  estimatedSeconds: number | null
  skipped: RunSkipped
}

export interface JobSummary {
  id: number
  parentJobId: number | null
  scope: string
  status: RunStatus
  versionIds: number[]
  versions: string[]
  handCount: number
  totalHands: number
  totalDecisions: number
  totalUnits: number
  doneUnits: number
  failedUnits: number
  skippedUnits: number
  repeat: number
  progress: number
  cancelRequested: boolean
  note: string
  errorMsg: string | null
  createdAt: string | null
  startedAt: string | null
  finishedAt: string | null
  heartbeatAt: string | null
  workerId: string | null
  configSnapshot?: Record<string, unknown>
}

export interface JobHand {
  handId: number
  status: string
  decisionCount: number
  doneUnits: number
  totalUnits: number
  errorMsg: string | null
  title: string
  heroCards: string
  board: string
  heroPosition: string | null
  result: string | null
  resultAmount: number | null
  fingerprint: string
  modified?: boolean
}

export interface JobFailure {
  id: number
  versionId: number
  versionName?: string
  errorMsg: string | null
  handId: number
  street: string
  seq: number
  createdAt: string | null
}

export interface JobVersionStat {
  versionId: number
  name: string
  model: string | null
  done: number
  failed: number
  fallback: number
  p50Ms: number | null
}

export interface JobDetail extends JobSummary {
  byVersion: JobVersionStat[]
  hands: JobHand[]
  failures: JobFailure[]
  canCancel: boolean
  canRetry: boolean
  durationSeconds: number | null
}

export interface JobHandResult {
  hand: {
    id: number
    title: string
    heroCards: string
    board: string
    heroPosition: string | null
    result: string | null
    resultAmount: number | null
    quality: Quality | null
  }
  decisions: ComparisonDecision[]
}

export interface JobResults {
  job: JobSummary
  versions: VersionSnapshot[]
  hands: JobHandResult[]
  totalHands: number
  totalDecisions: number
  page: number
  pageSize: number
}

export interface Health {
  ok: boolean
  db: { ok: boolean; detail: string; host: string; name: string }
  reviewHands: { ok: boolean; detail: string | null }
  aiKey: { ok: boolean; detail: string }
  worker: {
    alive: boolean
    enabled: boolean
    workerId?: string
    currentJobId?: number | null
    lastHeartbeat?: number
    workers?: number
  }
  queueDepth: number | null
  schema: { version: string; appliedAt: string | null }[]
  serverTime: string
}

export interface Meta {
  handCount: number
  favoriteCount: number
  versionCount: number
  lastJob: JobSummary | null
  defaults: { versionIds: number[]; repeat: number }
  current: {
    adviceVersion: string
    reconstructVersion: string
    promptStyles: string[]
    engine: { commit: string | null; dirty: boolean }
  }
  tags: string[]
  markStatuses: string[]
  runStatuses: RunStatus[]
  limits: {
    maxCallsPerJob: number
    maxQueueDepth: number
    repeatMax: number
    pageSize: number
  }
  settings: Record<string, unknown>
  health: Health
}

export interface HandsPage {
  total: number
  page: number
  pageSize: number
  items: HandSummary[]
  filters: Record<string, unknown>
  tagOptions: string[]
}
