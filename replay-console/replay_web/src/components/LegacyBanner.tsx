import { Alert } from 'antd'

/**
 * /legacy 下所有旧页面共用的横幅：旧链路已冻结。
 * 新链路 = 案例集 → 运行 → 偏差；这里只读追溯，不再写数据。
 */
export default function LegacyBanner({ scope }: { scope?: string }) {
  return (
    <Alert
      type="warning"
      showIcon
      className="legacy-banner"
      message={scope ? `${scope} · 历史页面，只读` : '历史页面，只读'}
      description="新链路已由「案例集 → 运行 → 偏差」取代。这里保留旧实验的数据用于追溯，这些历史页面不再提供新建、运行、打分、冻结等写入操作；新链路稳定后整块删除。"
    />
  )
}
