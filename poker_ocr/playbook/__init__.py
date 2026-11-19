"""Harrington 书中层：场景卡、匹配与解析。

这一层只回答「书里这个局面怎么打」，不产出最终建议，也不读提示词。
事实 -> Spot -> ScenarioCard -> BookPlan；偏移层与提示词层在外层。
"""

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
    "OFFSET_POLICIES",
    "Card",
    "OptionMatch",
    "Plan",
    "Resolution",
    "Size",
    "Spot",
    "choose_option",
    "load_cards",
    "render_book_plan_text",
    "render_book_plan_zh",
    "resolve",
    "resolve_for_situation",
    "validate_cards",
]
