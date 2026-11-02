"""system 提示词的组装入口。

顺序固定：角色 → 输出契约 → 事实优先级 → 决策顺序 → 指标口径 → 筹码深度，
最后按**局面场景**追加条件段：前注局（`ante.py`）与短码/多人池/单挑/听牌/
河牌（`scenario.py`），只在该场景成立时出现。

为什么按段拆文件：一段规则一个文件，改「听牌口径」不用在 2000 行里找；
`SYSTEM_SECTIONS` 也就是提示词的骨架，测试可以逐段断言。
"""

from __future__ import annotations

from ..shared.format import _fmt_num
from .ante import ANTE_SECTION
from .decision import DECISION_ORDER
from .intro import ROLE_INTRO
from .metrics import METRICS
from .output import OUTPUT_CONTRACT
from .priority import FACTS_PRIORITY
from .scenario import scenario_sections
from .stack import STACK_DEPTH

# 恒定部分（与局面无关）。顺序即提示词顺序。
SYSTEM_SECTIONS = (
    ROLE_INTRO,
    OUTPUT_CONTRACT,
    FACTS_PRIORITY,
    DECISION_ORDER,
    METRICS,
    STACK_DEPTH,
)


def build_system(sit) -> str:
    """按局面拼 system：恒定段 + 前注局 + 当前场景段。

    恒定段只放「任何牌桌都成立」的口径；场景段（短码/多人池/单挑/听牌/
    河牌）只在该局面成立时出现，避免把条件规则读成普适规则。
    """
    parts = [section.rstrip() for section in SYSTEM_SECTIONS]
    blinds = sit.snap.blinds
    if blinds is not None and blinds.ante:
        parts.append(ANTE_SECTION.format(ante=_fmt_num(blinds.ante)).rstrip())
    parts.extend(section.rstrip() for section in scenario_sections(sit))
    return "\n\n".join(parts)
