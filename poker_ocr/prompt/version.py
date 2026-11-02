"""提示词版本号。

版本号会写进 session_start / advice JSONL，复盘归因与 A/B 都用它对齐
「模型当时看到的提示词」。改动 system、事实块或模块顺序时**必须** bump；
`replay/versioning.py` 的 `CURRENT_ADVICE_VERSION` 由这里派生，不再手工同步。
"""

from __future__ import annotations

# 2026-11-03.3：硬缺口改由「选项是否降级」派生（不再从告警文案猜），
# 新增「规则仲裁」恒定段（含「各段让位句以本表为准」），恒定段里的场景规则
# （听牌/人数/河牌/20BB）下沉到对应场景段，顶对强踢阈值 T→J，领先/落后两套
# 口径写明，删掉恒定的「无」行与「另有 N 条未展开」；动作线缺失但已有下注时
# 按面对加注处理（选项给 raise-*，不再给 open-*）。
PROMPT_VERSION = "lean-zh-2026-11-03.3"
PROMPT_LANGUAGE = "zh"
