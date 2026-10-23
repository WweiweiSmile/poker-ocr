import { Alert, Button, Card, Descriptions, Space, Tag, Tooltip } from 'antd'
import { ReloadOutlined, CheckCircleFilled, CloseCircleFilled } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { api, errorMessage } from '../api'
import type { Health, Meta } from '../types'
import { formatDate, shortHash } from '../lib/format'

function HealthCard({ ok, title, detail, hint }: { ok: boolean; title: string; detail: string; hint?: string }) {
  return (
    <Card className={`health-card ${ok ? 'ok' : 'bad'}`} bordered={false}>
      <div className="health-card-head">
        {ok ? <CheckCircleFilled /> : <CloseCircleFilled />}
        <span>{title}</span>
        <Tag color={ok ? 'green' : 'red'}>{ok ? 'OK' : '异常'}</Tag>
      </div>
      <div className="health-card-detail">{detail}</div>
      {hint ? <div className="health-card-hint">{hint}</div> : null}
    </Card>
  )
}

export default function SettingsPage() {
  const health = useQuery<Health>({
    queryKey: ['health'],
    queryFn: () => api.get('/health'),
    refetchInterval: 15_000,
  })
  const meta = useQuery<Meta>({
    queryKey: ['meta'],
    queryFn: () => api.get('/meta'),
    refetchInterval: 30_000,
  })
  const h = health.data || meta.data?.health
  const settings = (meta.data?.settings || {}) as Record<string, any>

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="eyebrow">SETTINGS & HEALTH</div>
          <h1>设置与健康</h1>
          <p className="page-sub">连接状态、运行上限、保留策略与代码版本。所有诊断信息只读。</p>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => { health.refetch(); meta.refetch() }} loading={health.isFetching}>
            刷新
          </Button>
        </Space>
      </div>

      {health.error ? <Alert type="error" showIcon message={errorMessage(health.error)} /> : null}

      <div className="health-grid">
        <HealthCard
          ok={!!h?.db.ok}
          title="MySQL"
          detail={h ? `${h.db.host}/${h.db.name}` : '检查中'}
          hint={h?.db.detail}
        />
        <HealthCard
          ok={!!h?.reviewHands.ok}
          title="review_hands 可读"
          detail={h?.reviewHands.detail || '—'}
          hint="控制台只读，不写入生产表"
        />
        <HealthCard
          ok={!!h?.aiKey.ok}
          title="AI Key"
          detail={h?.aiKey.detail || '—'}
          hint="版本只存环境变量名，不存明文"
        />
        <HealthCard
          ok={h?.worker.enabled === false ? true : !!h?.worker.alive}
          title="Worker"
          detail={h?.worker.enabled === false ? '已禁用' : h?.worker.currentJobId ? `任务 #${h.worker.currentJobId}` : '空闲'}
          hint={`队列深度 ${h?.queueDepth ?? '—'} · 并发 ${h?.worker.workers ?? '—'}`}
        />
      </div>

      <div className="settings-grid">
        <Card bordered={false} className="settings-card">
          <div className="card-title">当前代码语义版本</div>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="advice_version">
              <span className="mono">{meta.data?.current.adviceVersion || '—'}</span>
            </Descriptions.Item>
            <Descriptions.Item label="reconstruct_version">
              <span className="mono">{meta.data?.current.reconstructVersion || '—'}</span>
            </Descriptions.Item>
            <Descriptions.Item label="engine_commit">
              <span className="mono">{shortHash(meta.data?.current.engine.commit)}</span>
              {meta.data?.current.engine.dirty ? <Tag className="mini-tag">dirty</Tag> : null}
            </Descriptions.Item>
            <Descriptions.Item label="数据库 schema">
              {(h?.schema || []).map((item) => <Tag key={item.version} className="mini-tag">{item.version}</Tag>)}
            </Descriptions.Item>
            <Descriptions.Item label="服务器时间">{formatDate(h?.serverTime)}</Descriptions.Item>
          </Descriptions>
        </Card>

        <Card bordered={false} className="settings-card">
          <div className="card-title">运行上限与保留</div>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="单任务调用上限">{meta.data?.limits.maxCallsPerJob ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="队列上限">{meta.data?.limits.maxQueueDepth ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="重复上限">{meta.data?.limits.repeatMax ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="默认每页">{meta.data?.limits.pageSize ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="每任务超时">{settings.timeoutSeconds ?? '—'}s</Descriptions.Item>
            <Descriptions.Item label="unit 并发">{settings.workers ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="提示词保留">{settings.storePrompts ? '开启' : '关闭'}</Descriptions.Item>
            <Descriptions.Item label="保留天数">{settings.retentionDays ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="只读用户过滤">
              {settings.handUserId ? `user_id=${settings.handUserId}` : '全部用户'}
            </Descriptions.Item>
            <Descriptions.Item label="控制台 user_id">{settings.consoleUserId ?? 0}</Descriptions.Item>
          </Descriptions>
        </Card>

        <Card bordered={false} className="settings-card">
          <div className="card-title">数据库连接</div>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="host">{settings.db?.host || '—'}:{settings.db?.port || '—'}</Descriptions.Item>
            <Descriptions.Item label="schema">{settings.db?.name || '—'}</Descriptions.Item>
            <Descriptions.Item label="user">{settings.db?.user || '—'}</Descriptions.Item>
            <Descriptions.Item label="连接池">{settings.db?.poolSize ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="环境文件">
              {(settings.loadedEnvFiles || []).map((file: string) => (
                <Tooltip title={file} key={file}><Tag className="mini-tag">{file.split('/').slice(-2).join('/')}</Tag></Tooltip>
              ))}
            </Descriptions.Item>
          </Descriptions>
        </Card>

        <Card bordered={false} className="settings-card">
          <div className="card-title">运行策略</div>
          <ul className="policy-list">
            <li>任务创建时冻结手牌范围、版本配置与决策点快照；运行期间改手牌/桌风文件不影响本次结果。</li>
            <li>结果是 <span className="mono">replay_results</span> 的一行；唯一键是断点，重启后跳过已有 unit。</li>
            <li>取消是协作式的：当前模型调用完成后停止，已产出的结果保留。</li>
            <li>模型失败/兜底会单独标记，不计入模型一致率的分母。</li>
            <li>重建质量分 exact / approx / invalid；批量可只跑 exact。</li>
            <li>桌风、提示词版本、代码 commit 都随结果可追溯。</li>
          </ul>
        </Card>
      </div>

      <Card bordered={false} className="settings-card docs-card">
        <div className="card-title">设计文档与 CLI 对照</div>
        <div className="docs-links">
          <a href="/docs" target="_blank" rel="noreferrer">FastAPI 接口文档 /docs</a>
          <span>设计文档：<span className="mono">docs/复跑控制台-设计文档.md</span></span>
          <span className="muted">CLI: tools/compare_review_hands.py · tools/replay_hand_repeat.py</span>
        </div>
      </Card>
    </div>
  )
}
