"""桌况偏移渲染（#82）：书计划 -> 本桌修正。

书页给基线，桌况（前注、松紧、码深）只改**尺寸/频率**，不改动作（设计
`docs/harrington-决策层-设计-2026-11-19.md` §2.3）。偏移规则住在
`poker_ocr/playbook/offsets.json`，由 `playbook.offset.apply_offsets`
计算；这一层只负责把「书上 -> 最终」逐条渲染进提示词。

没有偏移就没有这一段，避免「偏移：无」的占位噪音；未知 target 一律不渲染，
因为偏移层不许改动作。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from ...playbook.offset import OFFSET_TARGETS, OffsetDelta

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation

OFFSETS_HEADER = "## 偏移（书上是基线；只改尺寸/频率，不改动作）"


def render_offsets_zh(deltas: Sequence[OffsetDelta]) -> list[str]:
    """渲染逐条偏移；空序列返回 []（整段不出现）。"""
    if not deltas:
        return []
    lines = [OFFSETS_HEADER]
    for delta in deltas:
        if delta.target not in OFFSET_TARGETS:
            # fail open：未知 target 不渲染（偏移层不许改动作）。
            continue
        tail_parts: list[str] = []
        if delta.applicability:
            tail_parts.append(f"适用：{delta.applicability}")
        if delta.reason:
            tail_parts.append(f"依据：{delta.reason}")
        tail = f"（{'；'.join(tail_parts)}）" if tail_parts else ""
        lines.append(f"- {delta.target}：{delta.baseline} -> {delta.final}{tail}")
    return lines if len(lines) > 1 else []


def plan_offsets(sit: Situation) -> Sequence[OffsetDelta]:
    """取本局偏移；属性不存在 = 没有偏移。"""
    return tuple(getattr(sit, "plan_offsets", None) or ())
