"""偏移占位（#81）：书计划 -> 桌况修正的渲染缝。

书页给基线，桌况（前注、松紧、码深）只改**尺寸/频率**，不改动作（设计
`docs/harrington-决策层-设计-2026-11-19.md` §2.3）。偏移规则本身归 #82；
#81 先把这条渲染缝接上：`Situation.plan_offsets` 为空时整段不出现，一个
字节都不加；#82 只要往这个属性里放 `OffsetDelta` 就能渲染出
「书上 -> 最终」的逐条修正，不需要再动提示词组装。

刻意不做默认值、不做配置读取：没有偏移就没有这一行，避免出现「偏移：无」
这种占位噪音，也避免 #82 落地前伪造修正。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation

OFFSETS_HEADER = "## 偏移（书上是基线；只改尺寸/频率，不改动作）"

# 设计 §2.3：target 只允许这三个，动作不在偏移层里改。
OFFSET_TARGETS = ("size_bb", "size_pct", "freq")


@dataclass(frozen=True)
class OffsetDelta:
    """一条偏移：书上多少 -> 最终多少，以及依据。"""

    target: str
    baseline: str
    final: str
    reason: str


def render_offsets_zh(deltas: Sequence[OffsetDelta]) -> list[str]:
    """渲染逐条偏移；空序列返回 []（整段不出现）。"""
    if not deltas:
        return []
    lines = [OFFSETS_HEADER]
    for delta in deltas:
        if delta.target not in OFFSET_TARGETS:
            # fail open：未知 target 不渲染（偏移层不许改动作）。
            continue
        tail = f"（依据：{delta.reason}）" if delta.reason else ""
        lines.append(f"- {delta.target}：{delta.baseline} -> {delta.final}{tail}")
    return lines if len(lines) > 1 else []


def plan_offsets(sit: Situation) -> Sequence[OffsetDelta]:
    """取本局偏移；#82 落地前恒为空（属性不存在 = 没有偏移）。"""
    return tuple(getattr(sit, "plan_offsets", None) or ())
