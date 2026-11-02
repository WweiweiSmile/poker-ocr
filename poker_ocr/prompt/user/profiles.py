"""逐座位对手画像：这手还在局的每个座位是什么类型、范围多宽。

全部在局对手都进提示词（不截到 3 家）：多人池里模型的 sizing/弃牌决策依赖
「身后还剩谁、谁是什么类型」。渲染顺序由 `render_reads_zh` 按位置排。
"""

from __future__ import annotations

from ...opponent_profile import render_reads_zh


def render_profiles_zh(sit, *, suppress_action_hints: bool = False) -> list[str]:
    """「## 对手画像」整段；没有可信画像时返回空列表。"""
    snap = sit.snap
    active = {o.seat_id: (o.position or o.seat_id)
              for o in snap.opponents if not o.folded}
    in_hand_reads = [r for r in sit.opponent_reads
                     if r.archetype != "unknown" and r.seat_id in active]
    return render_reads_zh(
        in_hand_reads, active,
        header="## 对手画像（逐座位；优先于全场统计）",
        include_exploit=not suppress_action_hints)
