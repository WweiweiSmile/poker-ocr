"""提示词包：按功能与场景拆分的 system / user / shared / offline。

    prompt/
    ├── system/      系统提示词：输出契约、事实优先级、决策顺序、指标口径、
    │                筹码深度，以及按局面追加的条件场景段（前注局……）
    ├── user/        用户事实块：当前局面（几何/位置/价格/爆炸）、行动线、
    │                翻前、实时牌力、范围、对手画像、桌况统计、人工观察、
    │                攻击/剥削、数据质量、可选动作
    ├── shared/      数字格式与中文词表（各段共用，避免同名不同译）
    └── offline/     旧英文提示词与策略层，只服务复盘/离线审计，不进 live

对外入口只有两个：

    build_messages(sit) -> (system, user)
    build_system(sit)   -> system
    render_facts_zh(sit) -> user

各段都是 `list[str]` 渲染函数，返回空列表表示该场景不出现。想加一段事实，
在 `user/` 下新建模块、在 `user/__init__.py` 的组装表里注册；想加一条场景
规则，在 `system/scenario.py` 里加条件函数，不要往恒定段里塞绝对规则。
"""

from __future__ import annotations

from .system import build_system
from .user import build_messages, render_facts_zh
from .version import PROMPT_LANGUAGE, PROMPT_VERSION

__all__ = [
    "PROMPT_LANGUAGE",
    "PROMPT_VERSION",
    "build_messages",
    "build_system",
    "render_facts_zh",
]
