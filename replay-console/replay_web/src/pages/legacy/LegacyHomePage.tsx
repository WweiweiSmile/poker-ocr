import { Card, Col, Row, Tag } from 'antd'
import { Link } from 'react-router-dom'
import LegacyBanner from '../../components/LegacyBanner'

type Entry = {
  to: string
  title: string
  desc: string
  replacedBy: string
}

const ENTRIES: Entry[] = [
  {
    to: '/legacy/experiments',
    title: 'A/B 实验',
    desc: '冻结手牌集，baseline / variant 各跑一遍，按 dev/holdout 看一致率，再按决策点成对复盘。',
    replacedBy: '案例集 → 运行',
  },
  {
    to: '/legacy/sets',
    title: '手牌集',
    desc: '人工挑选的回归集（旧词：按「手」计数）。新链路的取样单位是决策点，见案例集。',
    replacedBy: '案例集',
  },
  {
    to: '/legacy/range-ab',
    title: '范围模型 A/B',
    desc: '旧范围路径（RANGE_EVENT_MODEL=0）vs 事件范围模型（=1）的权益与建议对照。',
    replacedBy: '运行 / 偏差',
  },
]

export default function LegacyHomePage() {
  return (
    <div>
      <div className="section-head">
        <div>
          <h2>历史页面</h2>
          <p className="muted">
            旧 experiments / 手牌集 / range-ab 的入口都收在这里，链接保留是为了让旧实验还能翻。
          </p>
        </div>
      </div>

      <LegacyBanner />

      <Row gutter={[12, 12]}>
        {ENTRIES.map((entry) => (
          <Col span={8} key={entry.to}>
            <Card
              bordered={false}
              title={<Link to={entry.to}>{entry.title}</Link>}
              extra={<Tag>已被「{entry.replacedBy}」取代</Tag>}
            >
              <p className="muted">{entry.desc}</p>
            </Card>
          </Col>
        ))}
      </Row>
    </div>
  )
}
