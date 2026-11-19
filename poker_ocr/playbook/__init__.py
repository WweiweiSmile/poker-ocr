"""Harrington 书中层：场景卡、匹配、解析与桌况偏移。

这一层只回答「书里这个局面怎么打」，不产出最终建议，也不读提示词。
事实 -> Spot -> ScenarioCard -> BookPlan -> OffsetDelta；偏移只改尺寸/频率。
"""

from .offset import (
    OFFSET_OPS,
    OFFSET_TARGETS,
    OffsetDelta,
    OffsetRule,
    apply_offsets,
    load_offset_rules,
    offsets_for_situation,
    validate_offset_rules,
)
from .options import OptionMatch, choose_option
from .prompt import (
    BOOK_PLAN_CHAR_LIMIT,
    BOOK_PLAN_HEADER,
    render_book_plan_text,
    render_book_plan_zh,
    resolve_for_situation,
)
from .schema import (
    ACTIONS,
    BOOK_CONFIDENCE,
    OFFSET_POLICIES,
    Card,
    Plan,
    Resolution,
    Size,
    Spot,
    load_cards,
    resolve,
    validate_cards,
)

__all__ = [
    "ACTIONS",
    "BOOK_CONFIDENCE",
    "BOOK_PLAN_CHAR_LIMIT",
    "BOOK_PLAN_HEADER",
    "OFFSET_OPS",
    "OFFSET_POLICIES",
    "OFFSET_TARGETS",
    "Card",
    "OffsetDelta",
    "OffsetRule",
    "OptionMatch",
    "Plan",
    "Resolution",
    "Size",
    "Spot",
    "apply_offsets",
    "choose_option",
    "load_cards",
    "load_offset_rules",
    "offsets_for_situation",
    "render_book_plan_text",
    "render_book_plan_zh",
    "resolve",
    "resolve_for_situation",
    "validate_cards",
    "validate_offset_rules",
]
