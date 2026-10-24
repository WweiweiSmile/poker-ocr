import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Checkbox, Divider, Empty, Input, Modal, Radio, Space,
  Table, Tag, Upload,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  DeleteOutlined, FileTextOutlined, InboxOutlined, UploadOutlined,
} from '@ant-design/icons'
import { useMutation } from '@tanstack/react-query'
import { api, errorMessage } from '../api'
import type {
  ImportFormat, ImportResult, ImportSample, ImportStatus,
} from '../types'
import { BoardCards, HandCards, QualityBadge, Stat } from './CardChips'

const FORMATS: { value: ImportFormat; label: string; hint: string }[] = [
  { value: 'auto', label: '自动识别', hint: '按记录形状判断' },
  { value: 'review', label: '复盘手牌', hint: 'review_hands 导出' },
  { value: 'live', label: '实时日志', hint: 'hands.json / JSONL' },
]

const STATUS_META: Record<ImportStatus, { label: string; color: string }> = {
  imported: { label: '可导入', color: 'green' },
  updated: { label: '更新', color: 'blue' },
  duplicate: { label: '重复', color: 'purple' },
  invalid: { label: '不可重跑', color: 'red' },
  approx: { label: '非精确', color: 'gold' },
  error: { label: '错误', color: 'orange' },
}

interface Props {
  open: boolean
  onClose: () => void
  onImported: (result: ImportResult) => void
}

export default function ImportHandsModal({ open, onClose, onImported }: Props) {
  const { message } = App.useApp()
  const [format, setFormat] = useState<ImportFormat>('auto')
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [fileSize, setFileSize] = useState(0)
  const [fileLoaded, setFileLoaded] = useState(false)
  const [dedupe, setDedupe] = useState(true)
  const [onlyExact, setOnlyExact] = useState(true)
  const [allowInvalid, setAllowInvalid] = useState(false)
  const [includePartial, setIncludePartial] = useState(false)
  const [includeMidHand, setIncludeMidHand] = useState(false)
  const [result, setResult] = useState<ImportResult | null>(null)

  useEffect(() => {
    if (!open) return
    setFormat('auto')
    setContent('')
    setFilename('')
    setFileSize(0)
    setFileLoaded(false)
    setResult(null)
    setDedupe(true)
    setOnlyExact(true)
    setAllowInvalid(false)
    setIncludePartial(false)
    setIncludeMidHand(false)
  }, [open])

  const payload = useMemo(() => ({
    format,
    content,
    filename: filename || undefined,
    dedupe,
    onlyExact,
    allowInvalid,
    includePartial,
    includeMidHand,
  }), [format, content, filename, dedupe, onlyExact, allowInvalid, includePartial, includeMidHand])

  const preview = useMutation({
    mutationFn: () => api.post<ImportResult>('/hands/import', { ...payload, dryRun: true }),
    onSuccess: (data) => setResult(data),
    onError: (error) => message.error(errorMessage(error)),
  })

  const confirm = useMutation({
    mutationFn: () => api.post<ImportResult>('/hands/import', { ...payload, dryRun: false }),
    onSuccess: (data) => {
      setResult(data)
      const total = data.imported + data.updated
      message.success(`导入完成：新增 ${data.imported} 手，更新 ${data.updated} 手`)
      if (total > 0) onImported(data)
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const readFile = async (file: File) => {
    try {
      const text = await file.text()
      setContent(text)
      setFilename(file.name)
      setFileSize(file.size)
      setFileLoaded(true)
      setResult(null)
    } catch (error) {
      message.error(`读取文件失败：${errorMessage(error)}`)
    }
  }

  const clearInput = () => {
    setContent('')
    setFilename('')
    setFileSize(0)
    setFileLoaded(false)
    setResult(null)
  }

  const importable = result ? result.imported + result.updated : 0
  const canImport = !!result?.dryRun && importable > 0 && !confirm.isPending

  const columns: ColumnsType<ImportSample> = [
    {
      title: '状态',
      dataIndex: 'status',
      width: 88,
      render: (status: ImportStatus) => {
        const meta = STATUS_META[status] || STATUS_META.error
        return <Tag color={meta.color} className="mini-tag">{meta.label}</Tag>
      },
    },
    {
      title: '手牌',
      dataIndex: 'heroCards',
      width: 150,
      render: (_: unknown, item) => (
        <div className="import-hand-cell">
          <HandCards cards={item.heroCards || ''} small />
          <div>
            <div className="import-hand-title">{item.title || '—'}</div>
            <div className="mono muted import-hand-id">
              {item.externalId || `#${item.index + 1}`}
            </div>
          </div>
        </div>
      ),
    },
    {
      title: '公共牌',
      dataIndex: 'board',
      width: 130,
      render: (board: string | null) => <BoardCards board={board || ''} />,
    },
    {
      title: '位置',
      dataIndex: 'heroPosition',
      width: 60,
      render: (position: string | null) => position || '—',
    },
    {
      title: '决策点',
      dataIndex: 'decisionCount',
      width: 72,
      render: (count: number, item) => (
        <Space size={4}>
          <span>{count || '—'}</span>
          <QualityBadge quality={item.quality} showText={false} />
        </Space>
      ),
    },
    {
      title: '说明',
      dataIndex: 'reason',
      render: (reason: string | null) => (
        <span className={reason ? 'import-reason' : 'muted'}>{reason || '—'}</span>
      ),
    },
  ]

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={900}
      title={<span><UploadOutlined /> 导入手牌</span>}
      className="import-modal"
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        <Button key="preview" onClick={() => preview.mutate()}
          loading={preview.isPending} disabled={!content.trim()}>
          解析预览
        </Button>,
        <Button key="import" type="primary" onClick={() => confirm.mutate()}
          loading={confirm.isPending} disabled={!canImport}>
          确认导入 {importable || ''} 手
        </Button>,
      ]}
    >
      <div className="import-copy">
        支持两类来源：
        <b>复盘手牌</b>（<span className="mono">review_hands</span> 导出的 JSON/JSONL）
        与<b>实时日志手牌记录</b>（<span className="mono">hands.json</span> 或
        <span className="mono"> live.states.jsonl</span>）。
        同一来源 + 相同内容会更新原记录，不会重复插入。
      </div>

      <div className="import-format-row">
        <span className="import-label">格式</span>
        <Radio.Group
          value={format}
          optionType="button"
          buttonStyle="solid"
          onChange={(event) => {
            setFormat(event.target.value as ImportFormat)
            setResult(null)
          }}
          options={FORMATS.map((item) => ({
            value: item.value,
            label: (
              <span className="import-format-option">
                {item.label}
                <span className="import-format-hint">{item.hint}</span>
              </span>
            ),
          }))}
        />
      </div>

      {fileLoaded ? (
        <div className="import-file">
          <div className="import-file-main">
            <FileTextOutlined />
            <div>
              <div className="import-file-name">{filename}</div>
              <div className="muted import-file-size">
                {(fileSize / 1024).toFixed(1)} KB · 已读取，确认后才会写库
              </div>
            </div>
          </div>
          <Button size="small" icon={<DeleteOutlined />} onClick={clearInput}>移除</Button>
        </div>
      ) : (
        <>
          <Upload.Dragger
            accept=".json,.jsonl,.txt"
            multiple={false}
            showUploadList={false}
            beforeUpload={(file) => {
              void readFile(file)
              return false
            }}
            className="import-upload"
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">拖入 JSON / JSONL 文件，或点击选择</p>
            <p className="ant-upload-hint">文件只在浏览器里读取，预览不会写库；默认上限 32MB。</p>
          </Upload.Dragger>
          <Divider className="thin-divider">或直接粘贴</Divider>
          <Input.TextArea
            value={content}
            onChange={(event) => {
              setContent(event.target.value)
              setFilename('')
              setResult(null)
            }}
            autoSize={{ minRows: 6, maxRows: 12 }}
            placeholder={'复盘示例：[{"heroCards":"AhKd","streets":[...]}]\n实时示例：{"type":"hand","hero_cards":["Ah","Kd"],"streets":[...]}'}
            className="mono import-textarea"
          />
        </>
      )}

      <div className="import-options">
        <Checkbox checked={onlyExact} onChange={(event) => {
          setOnlyExact(event.target.checked)
          setResult(null)
        }}>
          只导入可精确重建的（exact）
        </Checkbox>
        <Checkbox checked={dedupe} onChange={(event) => {
          setDedupe(event.target.checked)
          setResult(null)
        }}>
          跳过库中已有内容的手牌
        </Checkbox>
        <Checkbox checked={allowInvalid} disabled={onlyExact} onChange={(event) => {
          setAllowInvalid(event.target.checked)
          setResult(null)
        }}>
          也导入不可重跑的手牌
        </Checkbox>
        {format !== 'review' ? (
          <>
            <Checkbox checked={includePartial} onChange={(event) => {
              setIncludePartial(event.target.checked)
              setResult(null)
            }}>
              保留 partial（退出时截断的手）
            </Checkbox>
            <Checkbox checked={includeMidHand} onChange={(event) => {
              setIncludeMidHand(event.target.checked)
              setResult(null)
            }}>
              保留翻后接管的手牌
            </Checkbox>
          </>
        ) : null}
      </div>

      {result ? (
        <>
          <Divider className="thin-divider" />
          <div className="import-result-head">
            <div>
              <Tag color={result.dryRun ? 'processing' : 'green'}>
                {result.dryRun ? '预览' : '已导入'}
              </Tag>
              <span>
                {result.sourceLabel} · {result.container.toUpperCase()}
                {result.filename ? ` · ${result.filename}` : ''}
              </span>
            </div>
            <span className="muted">
              读取 {result.total} 条记录，识别 {result.hands} 手
            </span>
          </div>

          <div className="import-summary">
            <Stat label="新增" value={result.imported} hint="未在库中" />
            <Stat label="更新" value={result.updated} hint="相同内容" />
            <Stat label="重复" value={result.duplicates} hint="已跳过" />
            <Stat label="不可重跑" value={result.invalid} hint="缺底牌/行动线" />
            {result.skippedNotExact ? <Stat label="非精确" value={result.skippedNotExact} hint="approx，已跳过" /> : null}
            <Stat label="错误" value={result.errors} hint="解析/写库失败" />
            {result.advices ? <Stat label="建议" value={result.advices} hint="并入手牌" /> : null}
            {result.skippedPartial ? <Stat label="partial" value={result.skippedPartial} hint="已跳过" /> : null}
            {result.skippedMidHand ? <Stat label="中途接管" value={result.skippedMidHand} hint="已跳过" /> : null}
          </div>

          {result.issues.length ? (
            <Alert
              className="import-issues"
              type="warning"
              showIcon
              message={`${result.issues.length} 条记录没有导入`}
              description={
                <ul>
                  {result.issues.slice(0, 6).map((issue, index) => (
                    <li key={`${issue.index}-${index}`}>
                      {issue.line ? `第 ${issue.line} 行：` : issue.index !== undefined ? `第 ${issue.index + 1} 条：` : ''}
                      {issue.reason}
                    </li>
                  ))}
                </ul>
              }
            />
          ) : null}

          {result.samples.length ? (
            <Table<ImportSample>
              className="import-samples"
              size="small"
              rowKey={(item) => `${item.index}-${item.status}`}
              columns={columns}
              dataSource={result.samples}
              pagination={{ pageSize: 8, showSizeChanger: false }}
            />
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有识别到可导入的手牌" />}

          {!result.dryRun && (result.imported + result.updated) > 0 ? (
            <Alert
              className="import-done"
              type="success"
              showIcon
              message={`已写入 ${result.imported + result.updated} 手`}
              description="回到手牌库即可看到；来源筛选可区分「复盘库 / 导入·复盘 / 导入·实时」。"
            />
          ) : null}
          {result.dryRun ? (
            <div className="import-footnote">
              预览不会写库。确认导入后，这些手牌会和 <span className="mono">review_hands</span>
              的手牌一起出现在手牌库中，并可直接重跑。
            </div>
          ) : null}
        </>
      ) : null}
    </Modal>
  )
}
