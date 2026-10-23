import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, App, Button, Checkbox, Divider, Empty, Modal, Radio, Segmented,
  Space, Spin, Switch, Tag, Tooltip,
} from 'antd'
import { ExperimentOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, ApiClientError, errorMessage } from '../api'
import type { Meta, RunPreview, Version } from '../types'
import { scopeLabel } from '../lib/format'

type Scope = 'hands' | 'favorites' | 'filter'

interface Props {
  open: boolean
  onClose: () => void
  scope: Scope
  handIds?: number[]
  filter?: Record<string, unknown>
  title?: string
}

function secondsText(value?: number | null): string {
  if (value === null || value === undefined) return '—'
  if (value < 60) return `约 ${Math.ceil(value)} 秒`
  return `约 ${Math.floor(value / 60)} 分 ${Math.ceil(value % 60)} 秒`
}

export default function RunModal({ open, onClose, scope, handIds = [], filter = {}, title }: Props) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedVersions, setSelectedVersions] = useState<number[]>([])
  const [repeat, setRepeat] = useState(1)
  const [dedupe, setDedupe] = useState(true)
  const [includeApprox, setIncludeApprox] = useState(true)
  const [confirmLarge, setConfirmLarge] = useState(false)
  const [largeInfo, setLargeInfo] = useState<RunPreview | null>(null)
  const initialized = useRef(false)

  const versionsQuery = useQuery<{ items: Version[] }>({
    queryKey: ['versions', 'active'],
    queryFn: () => api.get('/versions'),
    enabled: open,
  })
  const metaQuery = useQuery<Meta>({ queryKey: ['meta'], queryFn: () => api.get('/meta'), enabled: open })
  const versions = versionsQuery.data?.items || []
  const runnable = versions.filter((v) => v.runnable)

  useEffect(() => {
    if (!open) {
      initialized.current = false
      return
    }
    if (initialized.current || !metaQuery.data || !versionsQuery.data) return
    setConfirmLarge(false)
    setLargeInfo(null)
    const defaults = (metaQuery.data.defaults?.versionIds || []).filter((id) =>
      runnable.some((v) => v.id === id))
    if (defaults.length) setSelectedVersions(defaults)
    else if (runnable.length) setSelectedVersions([runnable[0].id])
    setRepeat(metaQuery.data.defaults?.repeat || 1)
    initialized.current = true
  }, [open, metaQuery.data, versionsQuery.data, runnable])

  const payload = useMemo(() => ({
    scope,
    handIds: scope === 'hands' ? handIds : undefined,
    filter,
    versionIds: selectedVersions,
    repeat,
    dedupe: scope === 'hands' && handIds.length === 1 ? false : dedupe,
    includeQuality: includeApprox ? ['exact', 'approx'] : ['exact'],
    confirmLarge,
  }), [scope, handIds, filter, selectedVersions, repeat, dedupe, includeApprox, confirmLarge])

  const preview = useQuery<RunPreview>({
    queryKey: ['run-preview', payload],
    queryFn: () => api.post('/runs/preview', payload),
    enabled: open && selectedVersions.length > 0,
    retry: false,
  })

  const create = useMutation({
    mutationFn: () => api.post<{ jobId: number }>('/runs', payload),
    onSuccess: (data) => {
      message.success(`已创建任务 #${data.jobId}`)
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
      onClose()
      navigate(`/jobs/${data.jobId}`)
    },
    onError: (error) => {
      if (error instanceof ApiClientError && error.code === 'TOO_MANY_CALLS') {
        const detail = error.detail as { estimate?: RunPreview } | null
        setLargeInfo((detail?.estimate as RunPreview | undefined) || preview.data || null)
        setConfirmLarge(false)
        message.warning(error.message)
        return
      }
      message.error(errorMessage(error))
    },
  })

  const singleHand = scope === 'hands' && handIds.length === 1
  const selectedData = versions.filter((v) => selectedVersions.includes(v.id))
  const canSubmit = selectedVersions.length > 0 && !!preview.data && (preview.data.handCount ?? 0) > 0
  const units = preview.data?.totalUnits ?? 0
  const maxCalls = metaQuery.data?.limits.maxCallsPerJob || 2000
  const overLimit = units > maxCalls

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={720}
      destroyOnClose
      title={title || `重跑${scopeLabel(scope)}`}
      okText={units ? `开始重跑 · ${units} 次调用` : '开始重跑'}
      cancelText="取消"
      okButtonProps={{ disabled: !canSubmit || (overLimit && !confirmLarge), icon: <ThunderboltOutlined /> }}
      confirmLoading={create.isPending}
      onOk={() => create.mutate()}
      className="run-modal"
    >
      <div className="run-modal-section">
        <div className="field-label">版本 <span className="field-hint">一个版本 = 模型 + 采样参数 + 提示词风格 + 桌风快照</span></div>
        {versionsQuery.isLoading ? <Spin size="small" /> : null}
        {!versionsQuery.isLoading && runnable.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
                 description="还没有可运行的 AI 版本。先去版本管理从 .env 导入一个。">
            <Button onClick={() => { onClose(); navigate('/versions') }}>去版本管理</Button>
          </Empty>
        ) : null}
        <div className="version-pick-list">
          {versions.map((version) => {
            const disabled = !version.runnable
            const checked = selectedVersions.includes(version.id)
            return (
              <div key={version.id} className={`version-pick${checked ? ' picked' : ''}${disabled ? ' disabled' : ''}`}>
                <Checkbox
                  checked={checked}
                  disabled={disabled}
                  onChange={(event) => {
                    setLargeInfo(null)
                    setConfirmLarge(false)
                    setSelectedVersions((current) =>
                      event.target.checked
                        ? [...current, version.id]
                        : current.filter((id) => id !== version.id))
                  }}
                />
                <div className="version-pick-main">
                  <div className="version-pick-name">{version.name}</div>
                  <div className="version-pick-meta">
                    {version.model} · {version.promptStyle} · {version.tableDynamicsLength ? `桌风 ${version.tableDynamicsLength} 字` : '无桌风'}
                  </div>
                  {!version.runnable ? (
                    <Tooltip title={version.blockers.map((b) => b.message).join('；')}>
                      <Tag color="red" className="mini-tag">不可重跑</Tag>
                    </Tooltip>
                  ) : null}
                </div>
                <div className="version-pick-stat">
                  {version.lastUsedAt ? `上次 ${version.lastUsedAt.slice(5, 16).replace('T', ' ')}` : '未用过'}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <Divider className="thin-divider" />

      <div className="run-options-grid">
        <div>
          <div className="field-label">重复次数</div>
          <Radio.Group value={repeat} onChange={(event) => { setRepeat(event.target.value); setLargeInfo(null) }} optionType="button" buttonStyle="solid">
            {[1, 2, 3, 5].map((n) => <Radio.Button key={n} value={n}>{n} 次</Radio.Button>)}
          </Radio.Group>
        </div>
        <div className="run-switches">
          <div className="switch-line">
            <Switch size="small" checked={singleHand ? false : dedupe}
                    disabled={singleHand} onChange={setDedupe} />
            <span>{singleHand ? '单手重跑不做去重' : '跳过内容重复的手牌'}</span>
          </div>
          <div className="switch-line">
            <Switch size="small" checked={!includeApprox} onChange={(v) => setIncludeApprox(!v)} />
            <span>只跑重建精确（exact）的决策点</span>
          </div>
        </div>
      </div>

      <div className="preview-panel">
        <div className="preview-head">
          <span><ExperimentOutlined /> 范围预估</span>
          <Tag>{scopeLabel(scope)}</Tag>
        </div>
        {preview.isFetching ? <div className="preview-loading"><Spin size="small" /> 解析范围…</div> : null}
        {preview.error ? <Alert type="error" showIcon message={errorMessage(preview.error)} /> : null}
        {preview.data ? (
          <>
            <div className="preview-grid">
              <div><span className="preview-num">{preview.data.handCount}</span><span className="preview-label">手牌</span></div>
              <div><span className="preview-num">{preview.data.decisionCount}</span><span className="preview-label">决策点</span></div>
              <div><span className="preview-num">{preview.data.totalUnits}</span><span className="preview-label">调用单元</span></div>
              <div><span className="preview-num">{secondsText(preview.data.estimatedSeconds)}</span><span className="preview-label">预计耗时</span></div>
            </div>
            <div className="preview-formula">
              {preview.data.decisionCount} 决策点 × {selectedData.length} 版本 × {repeat} 次 = {preview.data.totalUnits} 次调用
            </div>
            {preview.data.skipped.duplicates || preview.data.skipped.invalid || preview.data.skipped.lowQuality ? (
              <div className="skip-line">
                跳过：重复 {preview.data.skipped.duplicates} 手 · 不可重跑 {preview.data.skipped.invalid} 手 · 质量不足 {preview.data.skipped.lowQuality} 手
              </div>
            ) : null}
          </>
        ) : null}
      </div>

      {(largeInfo || overLimit) ? (
        <div className="large-warning">
          <Checkbox checked={confirmLarge} onChange={(event) => setConfirmLarge(event.target.checked)}>
            我知道这会消耗 {units} 次模型调用{overLimit ? `（超过单任务上限 ${maxCalls}）` : ''}
          </Checkbox>
        </div>
      ) : null}

      <div className="modal-footnote">
        <Space size={12} wrap>
          <span>单次超时 30s · 瞬态重试 2 次 · 限速 5 req/s</span>
          <span>版本配置创建后不可修改</span>
        </Space>
      </div>
    </Modal>
  )
}
