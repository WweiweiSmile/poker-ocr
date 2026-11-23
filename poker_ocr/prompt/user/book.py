"""「书中计划」段：把当前事实归成书层局面，渲染唯一场景卡的计划。

票 #80 只加不删：这一段接在旧规则台账段之后，旧策略段原样保留，用来对照
「书计划确实进了 prompt」；下一票 #81 再做原子切换。

书层解析失败时 fail open —— 整段不出现，绝不阻塞 OCR 主循环，也不伪造
书页。零匹配不走异常，由 `render_book_plan_zh` 明确写「书中无此局面」；
炸弹池守则生效时（#114）书层整体让位，本段返回 []，由守则段占位。
"""

from __future__ import annotations

from ...playbook.prompt import (
    BOOK_PLAN_CHAR_LIMIT,
    BOOK_PLAN_HEADER,
    render_book_plan_zh as _render_book_plan_zh,
)


def render_book_plan_zh(sit) -> list[str]:
    """渲染书计划段；任何解析/加载异常都退化成「这一段不出现」。"""
    try:
        return _render_book_plan_zh(sit)
    except Exception:  # noqa: BLE001 - 书层坏卡不能阻塞主循环（fail open）
        return []


__all__ = ["BOOK_PLAN_CHAR_LIMIT", "BOOK_PLAN_HEADER", "render_book_plan_zh"]
