import { useEffect, useMemo, useState } from 'react'
import {
  App, Button, Card, DatePicker, Divider, Dropdown, Empty, Input, Segmented,
  Select, Space, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { TablePaginationConfig } from 'antd/es/table/interface'
import {
  DeleteOutlined, FilterOutlined, ReloadOutlined, StarFilled, StarOutlined,
  ThunderboltOutlined, UploadOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import dayjs from 'dayjs'
import { api, errorMessage, toQuery } from '../api'
import type { HandDeleteResult, HandsPage, HandSummary, Meta } from '../types'
import {
  BoardCards, HandCards, QualityBadge,
} from '../components/CardChips'
import ImportHandsModal from '../components/ImportHandsModal'
import RunModal from '../components/RunModal'
import { formatAmount, formatBB, formatDate, QUALITY } from '../lib/format'

const POSITIONS = ['UTG', 'UTG+1', 'UTG+2', 'LJ', 'HJ', 'CO', 'BTN', 'SB', 'BB']
const RESULTS = [
  { value: 'win', label: '赢' },
  { value: 'lose', label: '输' },
  { value: 'fold', label: '弃牌' },
  { value: 'unknown', label: '未知' },
]
const SOURCE_COLORS: Record<string, string> = {
  db: 'default',
  review: 'cyan',
  live: 'geekblue',
}

function ResultValue({ hand }: { hand: HandSummary }) {
  if (hand.resultAmount !== null && hand.resultAmount !== undefined) {
    return (
      <span className={`money ${hand.resultAmount > 0 ? 'up' : hand.resultAmount < 0 ? 'down' : ''}`}>
        {formatBB(hand.resultAmount)}
      </span>
    )
  }
  const label = RESULTS.find((item) => item.value === hand.result)?.label || '—'
  return <span className={`result-text result-${hand.result}`}>{label}</span>
}

export default function HandsPage() {
  const { message, modal: modalApi } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selected, setSelected] = useState<React.Key[]>([])
  const [modal, setModal] = useState<null | 'hands' | 'favorites' | 'filter'>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [keywordDraft, setKeywordDraft] = useState(searchParams.get('keyword') || '')

  const queryString = searchParams.toString()
  const page = Number(searchParams.get('page') || 1)
  const pageSize = Number(searchParams.get('pageSize') || 50)
  const query = useMemo(() => Object.fromEntries(searchParams.entries()), [queryString])
  const handsKey = ['hands', queryString]

  const hands = useQuery<HandsPage>({
    queryKey: handsKey,
    queryFn: () => api.get(`/hands${toQuery({ ...query, page, pageSize })}`),
  })
  const meta = useQuery<Meta>({ queryKey: ['meta'], queryFn: () => api.get('/meta') })
  const tagOptions = hands.data?.tagOptions || meta.data?.tags || []
  const sourceOptions = meta.data?.handSources?.length
    ? meta.data.handSources.map((item) => ({
        value: item.value,
        label: `${item.label} (${item.count})`,
      }))
    : [
        { value: 'db', label: '复盘库' },
        { value: 'review', label: '导入·复盘' },
        { value: 'live', label: '导入·实时' },
      ]

  useEffect(() => { setSelected([]) }, [queryString])
  useEffect(() => {
    setKeywordDraft(searchParams.get('keyword') || '')
  }, [searchParams])

  const setFilter = (key: string, value: unknown) => {
    const next = new URLSearchParams(searchParams)
    if (value === undefined || value === null || value === '' ||
        (Array.isArray(value) && value.length === 0)) {
      next.delete(key)
    } else if (Array.isArray(value)) {
      next.set(key, value.join(','))
    } else if (typeof value === 'boolean') {
      next.set(key, value ? '1' : '0')
    } else {
      next.set(key, String(value))
    }
    if (key !== 'page' && key !== 'pageSize') next.delete('page')
    setSearchParams(next, { replace: true })
  }

  const resetFilters = () => {
    const next = new URLSearchParams()
    if (searchParams.get('pageSize')) next.set('pageSize', searchParams.get('pageSize')!)
    setSearchParams(next, { replace: true })
  }

  const markMutation = useMutation({
    mutationFn: ({ handId, favorite }: { handId: number; favorite: boolean }) =>
      api.patch(`/hands/${handId}/mark`, { favorite }),
    onMutate: async ({ handId, favorite }) => {
      await queryClient.cancelQueries({ queryKey: handsKey })
      const previous = queryClient.getQueryData<HandsPage>(handsKey)
      queryClient.setQueryData<HandsPage>(handsKey, (old) => old ? {
        ...old,
        items: old.items.map((item) => item.id === handId ? { ...item, favorite } : item),
      } : old)
      return { previous }
    },
    onError: (error, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(handsKey, context.previous)
      message.error(errorMessage(error))
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (handIds: number[]) =>
      api.post<HandDeleteResult>('/hands/delete', { handIds }),
    onMutate: async (handIds) => {
      await queryClient.cancelQueries({ queryKey: handsKey })
      const previous = queryClient.getQueryData<HandsPage>(handsKey)
      const removed = new Set(handIds)
      queryClient.setQueryData<HandsPage>(handsKey, (old) => old ? {
        ...old,
        total: Math.max(0, old.total - old.items.filter((item) => removed.has(item.id)).length),
        items: old.items.filter((item) => !removed.has(item.id)),
      } : old)
      return { previous }
    },
    onSuccess: (result) => {
      setSelected([])
      queryClient.invalidateQueries({ queryKey: ['hands'] })
      queryClient.invalidateQueries({ queryKey: ['meta'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
      const details = [
        result.imported ? `导入 ${result.imported}` : '',
        result.reviewHidden ? `隐藏复盘 ${result.reviewHidden}` : '',
      ].filter(Boolean).join(' · ')
      message.success(`已删除 ${result.deleted} 手牌${details ? `（${details}）` : ''}`)
      if (result.missing.length) {
        message.warning(`${result.missing.length} 手牌已不存在或已删除，已跳过`)
      }
    },
    onError: (error, _handIds, context) => {
      if (context?.previous) queryClient.setQueryData(handsKey, context.previous)
      message.error(errorMessage(error))
    },
  })

  const confirmDelete = (handIds: number[], label: string) => {
    modalApi.confirm({
      title: label,
      content: (
        <div>
          <div>导入手牌会软删除，已跑结果保留，重新导入同一份内容可恢复。</div>
          <div className="muted">
            review_hands 的复盘手牌只会从控制台隐藏，不会修改 call-back 生产表。
          </div>
        </div>
      ),
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          await deleteMutation.mutateAsync(handIds)
        } catch {
          // 错误已由 mutation 的 onError 提示；保留弹窗打开状态
          throw new Error('delete failed')
        }
      },
    })
  }

  const onTableChange = (pagination: TablePaginationConfig, _filters: unknown, sorter: unknown) => {
    const next = new URLSearchParams(searchParams)
    next.set('page', String(pagination.current || 1))
    next.set('pageSize', String(pagination.pageSize || 50))
    const single = Array.isArray(sorter) ? sorter[0] : (sorter as { field?: string; order?: string })
    const sortMap: Record<string, string> = {
      resultAmount: 'result_amount',
      decisionCount: 'decision_count',
      lastRun: 'last_run',
      createdAt: 'created_at',
      updatedAt: 'updated_at',
    }
    if (single?.order && single.field) {
      next.set('sort', sortMap[single.field] || single.field)
      next.set('order', single.order === 'ascend' ? 'asc' : 'desc')
    } else {
      next.delete('sort')
      next.delete('order')
    }
    setSearchParams(next, { replace: true })
  }

  const columns: ColumnsType<HandSummary> = [
    {
      title: '',
      dataIndex: 'favorite',
      width: 36,
      render: (_: unknown, hand) => (
        <button
          className={`star-btn${hand.favorite ? ' on' : ''}`}
          disabled={markMutation.isPending && markMutation.variables?.handId === hand.id}
          onClick={(event) => {
            event.stopPropagation()
            const cached = queryClient.getQueryData<HandsPage>(handsKey)
            const current = cached?.items.find((item) => item.id === hand.id)?.favorite ?? hand.favorite
            markMutation.mutate({ handId: hand.id, favorite: !current })
          }}
          aria-label={hand.favorite ? '取消收藏' : '收藏'}
        >
          {hand.favorite ? <StarFilled /> : <StarOutlined />}
        </button>
      ),
    },
    {
      title: '手牌',
      dataIndex: 'heroCards',
      width: 210,
      render: (_: unknown, hand) => (
        <div className="hand-cell">
          <HandCards cards={hand.heroCards} small />
          <div className="hand-cell-copy">
            <div className="hand-cell-title">
              {hand.title}
              {hand.duplicate ? (
                <Tooltip title={`与 ${hand.duplicateIds.filter((id) => id !== hand.id).join(', ')} 内容重复`}>
                  <Tag className="mini-tag" color="purple">重复</Tag>
                </Tooltip>
              ) : null}
            </div>
            <div className="hand-cell-sub mono">
              #{hand.id}
              {hand.source !== 'db' ? (
                <Tag className="mini-tag source-tag" color={SOURCE_COLORS[hand.source] || 'default'}>
                  {hand.sourceLabel}
                </Tag>
              ) : null}
              {hand.externalId ? <span className="hand-external-id">{hand.externalId}</span> : null}
            </div>
          </div>
        </div>
      ),
    },
    {
      title: '公共牌',
      dataIndex: 'board',
      width: 160,
      render: (_: unknown, hand) => <BoardCards board={hand.board} />,
    },
    {
      title: '位置 / 筹码',
      dataIndex: 'heroPosition',
      width: 112,
      render: (_: unknown, hand) => (
        <div className="pos-cell">
          <span className="position-tag">{hand.heroPosition || '—'}</span>
          <span className="pos-meta">
            {hand.heroStackBb !== null && hand.heroStackBb !== undefined
              ? `${formatAmount(hand.heroStackBb)}BB` : '—'}
            {hand.tableSize ? ` · ${hand.tableSize}人` : ''}
          </span>
        </div>
      ),
    },
    {
      title: '结果',
      dataIndex: 'resultAmount',
      width: 70,
      sorter: true,
      render: (_: unknown, hand) => <ResultValue hand={hand} />,
    },
    {
      title: '决策点',
      dataIndex: 'decisionCount',
      width: 78,
      sorter: true,
      render: (_: unknown, hand) => (
        <div className="decision-cell">
          <span className="decision-num">{hand.decisionCount || '—'}</span>
          <QualityBadge quality={hand.quality} showText={false} />
          {hand.approxCount > 0 ? (
            <Tooltip title={`${hand.approxCount} 个决策点底池/行动线为推算值`}>
              <span className="approx-dot" />
            </Tooltip>
          ) : null}
        </div>
      ),
    },
    {
      title: '最近重跑',
      dataIndex: 'lastRun',
      width: 130,
      sorter: true,
      render: (_: unknown, hand) => hand.lastRun ? (
        <div className="last-run">
          <div className="last-run-main">
            {hand.lastRun.versions.join(' + ')}
            <span className="muted"> ×{hand.lastRun.repeat}</span>
          </div>
          <div className="last-run-sub">{formatDate(hand.lastRun.at)} · 任务 #{hand.lastRun.jobId}</div>
        </div>
      ) : <span className="muted">未跑</span>,
    },
    {
      title: '操作',
      width: 112,
      fixed: 'right',
      render: (_: unknown, hand) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            onClick={(event) => {
              event.stopPropagation()
              navigate(`/hands/${hand.id}`)
            }}
          >
            详情
          </Button>
          <Button
            danger
            type="link"
            size="small"
            onClick={(event) => {
              event.stopPropagation()
              confirmDelete([hand.id], `删除手牌 #${hand.id}`)
            }}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ]

  const selectedIds = selected.map(Number)
  const activeFilterCount = ['favorite', 'source', 'position', 'result', 'tag', 'hasRun', 'quality', 'keyword',
    'dateFrom', 'dateTo'].filter((key) => searchParams.get(key)).length
  const favoriteCount = meta.data?.favoriteCount ?? 0
  const maxCalls = meta.data?.limits.maxCallsPerJob || 2000

  return (
    <div className="hands-page">
      <div className="page-head">
        <div>
          <div className="eyebrow">HAND LIBRARY</div>
          <h1>手牌库</h1>
          <p className="page-sub">
            从 call-back 的 <span className="mono">review_hands</span> 只读加载，
            或导入复盘 JSON / 实时日志手牌 ·
            收藏典型手牌，逐街对比多版本 AI 建议。
          </p>
        </div>
        <Space size={8} wrap>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
            导入手牌
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => hands.refetch()} loading={hands.isFetching}>
            刷新
          </Button>
          <Button
            icon={<StarOutlined />}
            onClick={() => setModal('favorites')}
            disabled={!favoriteCount}
          >
            重跑全部收藏 ({favoriteCount})
          </Button>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            disabled={selectedIds.length === 0}
            onClick={() => setModal('hands')}
          >
            重跑选中 ({selectedIds.length})
          </Button>
          <Dropdown
            menu={{
              items: [
                { key: 'filter', label: '重跑当前筛选结果', icon: <FilterOutlined /> },
              ],
              onClick: ({ key }) => {
                if (key === 'filter') setModal('filter')
              },
            }}
          >
            <Button>更多</Button>
          </Dropdown>
        </Space>
      </div>

      <div className="library-body">
        <Card className="filter-card" bordered={false}>
          <div className="filter-head">
            <span><FilterOutlined /> 筛选</span>
            {activeFilterCount ? (
              <Button type="link" size="small" onClick={resetFilters}>清除 {activeFilterCount}</Button>
            ) : null}
          </div>

          <div className="filter-field">
            <label>收藏</label>
            <Segmented
              block
              value={searchParams.get('favorite') || 'all'}
              onChange={(value) => setFilter('favorite', value === 'all' ? '' : value)}
              options={[
                { label: '全部', value: 'all' },
                { label: '已收藏', value: '1' },
                { label: '未收藏', value: '0' },
              ]}
            />
          </div>

          <div className="filter-field">
            <label>来源</label>
            <Select
              mode="multiple"
              allowClear
              placeholder="全部来源"
              value={(searchParams.get('source') || '').split(',').filter(Boolean)}
              onChange={(value) => setFilter('source', value)}
              options={sourceOptions}
            />
          </div>

          <div className="filter-field">
            <label>位置</label>
            <Select
              mode="multiple"
              allowClear
              placeholder="全部位置"
              value={(searchParams.get('position') || '').split(',').filter(Boolean)}
              onChange={(value) => setFilter('position', value)}
              options={POSITIONS.map((position) => ({ value: position, label: position }))}
            />
          </div>

          <div className="filter-field">
            <label>结果</label>
            <Select
              mode="multiple"
              allowClear
              placeholder="全部结果"
              value={(searchParams.get('result') || '').split(',').filter(Boolean)}
              onChange={(value) => setFilter('result', value)}
              options={RESULTS}
            />
          </div>

          <div className="filter-field">
            <label>标签</label>
            <Select
              mode="multiple"
              allowClear
              placeholder="全部标签"
              value={(searchParams.get('tag') || '').split(',').filter(Boolean)}
              onChange={(value) => setFilter('tag', value)}
              options={tagOptions.map((tag) => ({ value: tag, label: tag }))}
            />
          </div>

          <div className="filter-field">
            <label>重跑状态</label>
            <Select
              allowClear
              placeholder="全部"
              value={searchParams.get('hasRun') || undefined}
              onChange={(value) => setFilter('hasRun', value)}
              options={[
                { value: '0', label: '未跑过' },
                { value: '1', label: '跑过' },
              ]}
            />
          </div>

          <div className="filter-field">
            <label>重建质量</label>
            <Select
              mode="multiple"
              allowClear
              placeholder="全部"
              value={(searchParams.get('quality') || '').split(',').filter(Boolean)}
              onChange={(value) => setFilter('quality', value)}
              options={[
                { value: 'exact', label: `精确 (${QUALITY.exact.label})` },
                { value: 'approx', label: '重建可疑' },
                { value: 'invalid', label: '不可重跑' },
              ]}
            />
          </div>

          <div className="filter-field">
            <label>关键词</label>
            <Input.Search
              placeholder="手牌 / 公共牌 / 标题"
              value={keywordDraft}
              allowClear
              onChange={(event) => setKeywordDraft(event.target.value)}
              onSearch={(value) => setFilter('keyword', value)}
            />
          </div>

          <div className="filter-field">
            <label>创建日期</label>
            <DatePicker.RangePicker
              style={{ width: '100%' }}
              value={searchParams.get('dateFrom') && searchParams.get('dateTo')
                ? [dayjs(searchParams.get('dateFrom')!), dayjs(searchParams.get('dateTo')!)]
                : null}
              onChange={(values) => {
                const next = new URLSearchParams(searchParams)
                if (values?.[0]) next.set('dateFrom', values[0].format('YYYY-MM-DD'))
                else next.delete('dateFrom')
                if (values?.[1]) next.set('dateTo', values[1].format('YYYY-MM-DD'))
                else next.delete('dateTo')
                next.delete('page')
                setSearchParams(next, { replace: true })
              }}
            />
          </div>

          <Divider className="thin-divider" />
          <div className="filter-note">
            <div><strong>{hands.data?.total ?? 0}</strong> 手符合条件</div>
            <div>单任务上限 {maxCalls} 次调用</div>
          </div>
        </Card>

        <div className="library-main">
          <div className="table-toolbar">
            <div className="table-summary">
              共 <strong>{hands.data?.total ?? 0}</strong> 手
              {selectedIds.length ? <span className="select-summary"> · 已选 {selectedIds.length}</span> : null}
            </div>
            <div className="table-summary-muted">
              点击行进入逐街对比 · 星标即时保存
            </div>
          </div>

          <Card className="table-card" bordered={false}>
            <Table<HandSummary>
              rowKey="id"
              loading={hands.isLoading}
              dataSource={hands.data?.items || []}
              columns={columns}
              size="middle"
              scroll={{ x: 980 }}
              rowSelection={{
                selectedRowKeys: selected,
                onChange: setSelected,
                preserveSelectedRowKeys: true,
              }}
              pagination={{
                current: hands.data?.page || page,
                pageSize: hands.data?.pageSize || pageSize,
                total: hands.data?.total || 0,
                showSizeChanger: true,
                showTotal: (total) => `共 ${total} 手`,
                pageSizeOptions: [20, 50, 100],
              }}
              onChange={onTableChange}
              onRow={(hand) => ({
                onClick: () => navigate(`/hands/${hand.id}`),
                className: 'hand-row',
              })}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={activeFilterCount
                      ? '当前筛选没有手牌'
                      : '还没有可重跑的手牌，先导入复盘 JSON 或实时日志'}
                  >
                    {activeFilterCount
                      ? <Button onClick={resetFilters}>清除筛选</Button>
                      : <Button type="primary" icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>导入手牌</Button>}
                  </Empty>
                ),
              }}
            />
          </Card>
        </div>
      </div>

      {selectedIds.length > 0 ? (
        <div className="batch-bar">
          <span>已选 <strong>{selectedIds.length}</strong> 手</span>
          <Space size={8}>
            <Button size="small" onClick={() => setSelected([])}>取消选择</Button>
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              onClick={() => confirmDelete(selectedIds, `删除选中的 ${selectedIds.length} 手`)}
            >
              删除选中
            </Button>
            <Button size="small" type="primary" icon={<ThunderboltOutlined />} onClick={() => setModal('hands')}>
              重跑选中
            </Button>
          </Space>
        </div>
      ) : null}

      <RunModal
        open={modal !== null}
        scope={modal || 'hands'}
        handIds={selectedIds}
        filter={query}
        onClose={() => setModal(null)}
        title={modal === 'favorites'
          ? `重跑全部收藏 (${favoriteCount})`
          : modal === 'filter'
            ? '重跑当前筛选结果'
            : `重跑 ${selectedIds.length} 手`}
      />

      <ImportHandsModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={() => {
          queryClient.invalidateQueries({ queryKey: ['hands'] })
          queryClient.invalidateQueries({ queryKey: ['meta'] })
          queryClient.invalidateQueries({ queryKey: ['health'] })
        }}
      />
    </div>
  )
}
