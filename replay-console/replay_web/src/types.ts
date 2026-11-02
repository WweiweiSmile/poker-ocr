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
  source: 'db' | 'review' | 'live' | string
  sourceLabel: string
  externalId: string | null
  importBatchId: number | null
  title: string
  heroCards: string
  board: string
  boardByStreet: { flop: string[]; turn: string[]; river: string[] }
  heroPosition: string | null
  tableSize: number
  heroStackBb: number | null
  bombPotBb: number
  bombPot: boolean
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
  flagMask?: number
  flags?: Record<string, { reason?: string }>
  reviewScore?: number
  createdAt: string | null
  updatedAt: string | null
  userId: number
}

export interface ActualAction {
  action: string | null
  amountBb: number | null
  label: string
}

export interface DecisionReview {
  verdict: 'good' | 'bad' | 'unsure' | string
  tags: string[]
  note: string
}

export interface DecisionMeta {
  id: number | null
  street: string
  seq: number
  actionIndex?: number
  pot: number
  toCall: number
  // 实际跟注额：后手不足时封顶到 heroStack，旧接口没有该字段
  callAmount?: number
  heroStack: number | null
  heroPosition: string | null
  heroCards: string[]
  board: string[]
  players: number
  opponents: number
  quality: Quality
  warnings: string[]
  actual: ActualAction
  review?: DecisionReview | null
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

export interface HandAdvice {
  id?: number | null
  ts?: string | null
  street?: string | null
  pot?: number | null
  toCall?: number | null
  option?: {
    id?: string | null
    label?: string | null
    kind?: string | null
    amount?: number | null
  }
  reason?: string | null
  reasonFull?: string | null
  plan?: string | null
  confidence?: number | null
  source?: string | null
  model?: string | null
  durationMs?: number | null
  promptVersion?: string | null
  promptSha256?: string | null
  promptMissing?: boolean
  promptMissingReason?: string | null
  hasPrompt?: boolean
  actionIndex?: number | null
  decisionKey?: unknown
  actualAction?: string | null
  actualAmount?: number | null
  bucket?: string | null
  context?: {
    table_dynamics?: string | null
    table_dynamics_sha?: string | null
    table_stats_scope?: string | null
    table_stats?: Record<string, unknown> | null
    opponent_reads?: {
      seat_id?: string | null
      key?: string
      display_name?: string | null
      archetype?: string
      confidence?: number
      n?: number
      source?: string
      gate?: string
    }[]
  } | null
}

export interface HandDetail {
  hand: HandSummary
  streets: StreetBlock[]
  decisions: DecisionMeta[]
  // 导入实时日志时并入手牌的「当时 AI 建议」；旧记录没有该字段。
  advices?: HandAdvice[]
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
  promptVersion?: string | null
  adviceVersion: string
  reconstructVersion: string
  engineCommit: string | null
  engineDirty: boolean
  tableDynamics: string
  tableDynamicsSha: string | null
  tableDynamicsLength: number
  contextMode?: string
  tableStatsMode?: string
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
  madeNameCn?: string | null
  handStrength?: {
    kind: string
    size: string
    behindAny: number
    equity: number
    n: number
  } | null
  options?: { id: string; label: string; kind: string; amount?: number | null }[]
  warnings?: string[]
  opponentsInHand?: number | null
  tableDynamics?: boolean
  explosivePot?: boolean | null
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
  imports?: { ok: boolean; detail: string | null; count: number | null }
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
  handSources?: { value: string; label: string; count: number }[]
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

export type ImportFormat = 'auto' | 'review' | 'live'
export type ImportStatus = 'imported' | 'updated' | 'duplicate' | 'invalid' | 'approx' | 'error'

export interface ImportIssue {
  index?: number
  line?: number | null
  reason: string
}

export interface ImportSample {
  index: number
  source: string
  externalId: string | null
  title: string | null
  heroCards: string | null
  board: string | null
  heroPosition: string | null
  status: ImportStatus
  decisionCount: number
  quality: Quality | null
  reason: string | null
}

export interface ImportResult {
  dryRun: boolean
  batchId: number | null
  source: 'review' | 'live' | string
  sourceLabel: string
  container: 'json' | 'jsonl' | string
  filename: string | null
  total: number
  hands: number
  // 并进手牌的实时建议条数（type=advice 行不单独成手）
  advices: number
  imported: number
  updated: number
  duplicates: number
  invalid: number
  // 因 onlyExact 跳过的 approx 手牌数
  skippedNotExact: number
  errors: number
  skippedNonHand: number
  skippedPartial: number
  skippedMidHand: number
  sessionCount: number
  dedupe: boolean
  onlyExact: boolean
  allowInvalid: boolean
  samples: ImportSample[]
  issues: ImportIssue[]
}

export interface ImportBatch {
  id: number
  user_id: number
  source: string
  sourceLabel: string
  container: string
  filename: string | null
  total_records: number
  imported_count: number
  updated_count: number
  duplicate_count: number
  invalid_count: number
  error_count: number
  status: string
  error_msg: string | null
  summary: Record<string, unknown>
  createdAt: string | null
  finishedAt: string | null
}

export interface HandDeleteResult {
  requested: number
  deleted: number
  imported: number
  reviewHidden: number
  missing: number[]
  handId?: number
}

export interface Experiment {
  id: number
  name: string
  hypothesis: string
  baselineVersionId: number
  variantVersionId: number
  filter: Record<string, unknown>
  devRatio: number
  repeat: number
  status: string
  jobId: number | null
  hands: { total: number; dev: number; holdout: number }
  handSetId?: number | null
  handSetRevision?: number | null
  kind?: string
  changeScope?: string | null
  baselinePromptMode?: string
  decision?: string | null
  conclusion?: string
  metrics?: Record<string, unknown>
  createdAt: string | null
  startedAt: string | null
  finishedAt: string | null
}

export interface ExperimentVersionResult {
  versionId: number
  name: string
  contextMode: string
  tableStatsMode: string
  n: number
  bucket: Record<string, number>
  source: Record<string, number>
  violations: Record<string, number>
  splits: Record<string, number>
  agreement: number
}

export interface ExperimentResults {
  experiment: Experiment
  jobId: number
  split: string | null
  rows: number
  versions: Record<string, ExperimentVersionResult>
  review: Record<string, number>
}

export interface RangeABModelRun {
  old?: string
  new?: string
  old_label?: string
  new_label?: string
  old_kind?: string
  new_kind?: string
  old_amount?: number | null
  new_amount?: number | null
  old_reason?: string
  new_reason?: string
  old_ms?: number
  new_ms?: number
}

export interface RangeABAction {
  position: string | null
  action: string
  amount: number | null
  allIn: boolean
  inferred: boolean
  isHero: boolean
}

export interface RangeABStreet {
  street: string
  potStart: number | null
  actions: RangeABAction[]
}

export interface RangeABAdvice {
  street: string
  actionIndex: number | null
  optionId: string
  optionLabel: string
  amount: number | null
  reason: string
  pot: number | null
  toCall: number | null
  model: string | null
  promptVersion: string | null
}

export interface RangeABHandDetail {
  heroCards: string
  board: string
  heroPosition: string | null
  pot: number | null
  bombPot: boolean
  streets: RangeABStreet[]
  advices: RangeABAdvice[]
}

export interface RangeABResult {
  result: string
  old: number | null
  new: number | null
  threshold: number | null
  problems: string[]
  adviceDiff: string | null
  model: RangeABModelRun | null
}

export interface RangeABCase {
  id: string
  priority: string
  hand: string
  street: string
  action_index: number | null
  category: string
  source: string
  status: string
  expect: string
  pot?: number | null
  to_call?: number | null
  threshold?: number | null
  old_equity?: number | null
  new_equity?: number | null
  old_advice?: string
  new_advice?: string
  note?: string
  handDetail?: RangeABHandDetail | null
  result?: RangeABResult
}

export interface RangeABSummary {
  total: number
  ready: number
  blocked: number
  pass: number
  fail: number
  skip: number
  missing: number
  byStatus: Record<string, number>
  byPriority: Record<string, { total: number; pass: number; fail: number; skip: number }>
}

export interface RangeABResponse {
  available: boolean
  error?: string
  hint?: string
  generatedAt?: string
  setPath?: string
  resultsPath?: string
  resultsAvailable?: boolean
  resultsUpdatedAt?: string | null
  summary?: RangeABSummary
  cases?: RangeABCase[]
  skipped?: { hand: string; reason: string }[]
  run?: string
  baseline?: string
  candidate?: string
}

// ── 手牌回放 / 手牌集 / 成对 A/B ─────────────────────────────────────

export interface ReplayActor {
  seatId?: string | null
  name?: string | null
  position?: string | null
}

export interface ReplayAdvice {
  adviceId?: number | null
  optionId?: string | null
  kind?: string | null
  amount?: number | null
  label?: string | null
  reason?: string | null
  reasonFull?: string | null
  source?: string | null
  model?: string | null
  promptSha256?: string | null
  promptMissing?: boolean
  durationMs?: number | null
  confidence?: number | null
}

export interface ReplayFrame {
  seq: number
  type: 'hand_start' | 'street_start' | 'action' | 'decision' | 'hand_end' | string
  ts?: string | null
  street?: string
  label?: string
  actor?: ReplayActor
  action?: string
  amount?: number | null
  amountMode?: string
  potAfter?: number | null
  potStart?: number | null
  betsAfter?: Record<string, number>
  stacksAfter?: Record<string, number>
  board?: string[]
  decisionId?: number | null
  decisionKey?: unknown
  snapshot?: Record<string, unknown>
  actual?: { action?: string | null; amountBb?: number | null }
  advice?: ReplayAdvice | null
  promptSha256?: string | null
  promptMissing?: boolean
  allIn?: boolean
  inferred?: boolean
  derived?: boolean
  warnings?: string[]
  quality?: string
  result?: {
    heroNetBb?: number | null
    outcome?: string | null
    source?: string | null
  }
  hero?: {
    seatId?: string
    cards?: string[]
    position?: string | null
    stack?: number | null
  }
  roster?: {
    seatId?: string | null
    name?: string | null
    position?: string | null
    stack?: number | null
    status?: string | null
    inHand?: boolean
  }[]
  table?: {
    size?: number
    blinds?: { sb?: number | null; bb?: number | null; ante?: number | null }
    bombPot?: boolean
  }
}

export interface ReplayDecision {
  id: number | null
  street: string
  seq: number
  actionIndex: number
  pot: number
  toCall: number
  heroStack: number | null
  actual: ActualAction
  adviceId?: number | null
  promptSha256?: string | null
  promptMissing?: boolean
  advice?: ReplayAdvice | null
}

export interface HandReplay {
  hand: {
    id: number
    uid: string | null
    heroCards: string[]
    heroPosition: string | null
    tableSize: number
    board: string[]
    result: { heroNetBb: number | null; outcome: string; source: string }
    quality: Quality
    warnings: string[]
    partial: boolean
    createdAt: string | null
  }
  timeline: { version?: string; frames?: ReplayFrame[] }
  frames: ReplayFrame[]
  decisions: ReplayDecision[]
}

export interface PromptDetail {
  adviceId: number
  handId: number
  street: string
  actionIndex: number
  promptVersion: string | null
  promptSha256: string | null
  promptMissing: boolean
  promptMissingReason: string | null
  promptBytes: number
  system: string
  user: string
  rawResponse: string | null
  reasonFull: string | null
  facts: Record<string, unknown>
  context: Record<string, unknown>
  modelParams: Record<string, unknown>
  capturedAt: string | null
  tokensIn: number | null
  tokensOut: number | null
  durationMs: number | null
}

export interface HandSetItem {
  handId: number
  fingerprint: string
  role: string
  priority: number
  stratum: string | null
  note: string
  expect: Record<string, unknown>
  quality: Quality | 'missing'
  stale: boolean
  flags: Record<string, unknown>
  hand: {
    id: number
    title: string
    heroCards: string
    board: string
    heroPosition: string | null
    result: string
    resultAmount: number | null
    reviewScore: number
  } | null
}

export interface HandSetCoverage {
  roles: Record<string, number>
  streets: Record<string, number>
  positions: Record<string, number>
  quality: Record<string, number>
  results: Record<string, number>
  decisions: number
  promptReady: number
  promptCoverage: number
}

export interface HandSet {
  id: number
  name: string
  description: string
  purpose: string
  revision: number
  frozenAt: string | null
  createdAt: string | null
  updatedAt: string | null
  handCount: number
  items: HandSetItem[]
  coverage: HandSetCoverage
}

export interface HandSetList {
  items: HandSet[]
}

export interface PairSide {
  optionId: string | null
  kind: string | null
  amount: number | null
  reason: string | null
  source: string | null
  promptSha256: string | null
  promptSystem?: string | null
  promptUser?: string | null
  rawResponse?: string | null
  reasonFull?: string | null
  stable: boolean
  repeatCount: number
  distribution: Record<string, number>
}

export interface PairRow {
  decisionId: number
  handId: number
  street: string
  seq: number
  split: string | null
  quality: string | null
  actual: { action: string | null; amountBb: number | null }
  A: PairSide | null
  B: PairSide | null
  auto: {
    sameKind: boolean
    sameSize: boolean
    changed: boolean
    p0: boolean
    p0Pass: boolean | null
    p0Regression?: boolean
    assertions: { type: string; ok: boolean; value?: unknown }[]
    baselineAssertions?: { type: string; ok: boolean; value?: unknown }[]
  }
  review: { preferred: string; tags: string[]; note: string } | null
}

export interface ExperimentPairs {
  experimentId: number
  jobId: number
  rows: PairRow[]
  summary: {
    n: number
    changed: number
    aWins: number
    bWins: number
    same: number
    unsure: number
    p0Regressions: number
  }
}
