"""system 提示词的组装入口：只留事实口径、决策依据与最小输出契约。

#81 起 system 不再承载策略：书页给计划，偏移层给修正，模型只从「可选动作」
里选。旧的 aggression / hand_type / scenario / ante / metrics / arbitration /
decision / stack 段随这次原子切换一起删除（git revert 是回滚手段）。
"""

from __future__ import annotations

from .basis import DECISION_BASIS
from .intro import ROLE_INTRO
from .output import OUTPUT_CONTRACT
from .priority import FACTS_PRIORITY

# 恒定部分（与局面无关）。顺序即提示词顺序。
SYSTEM_SECTIONS = (
    ROLE_INTRO,
    DECISION_BASIS,
    FACTS_PRIORITY,
    OUTPUT_CONTRACT,
)


def build_system(sit) -> str:
    """拼 system。签名保留 `sit`：旧版按牌型/场景拼装，现在恒定。

    保留参数是为了不改 `build_messages` 的调用面与测试缝；书页/偏移按局面
    变化，但它们是 user 段（`## 书中计划` / `## 偏移`），不进 system。
    """
    return "\n\n".join(section.rstrip() for section in SYSTEM_SECTIONS)
