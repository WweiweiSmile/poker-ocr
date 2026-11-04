"""逐座位对手画像：这手还在局的每个座位是什么类型、范围多宽。

全部在局对手都进提示词（不截到 3 家）：多人池里模型的 sizing/弃牌决策依赖
「身后还剩谁、谁是什么类型」。渲染顺序必须对应 Hero 后续行动顺序：先按本街座位顺序排列，再逐家评估；不能用静态位置排序代替行动顺序。
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
    # 位置表本身是翻前顺序。将它从 Hero 下家旋转，得到 Hero 本次下注后
    # 对手依次获得行动的循环顺序；无法识别的位置仍由 renderer 放在末尾。
    from ...positions import POSITIONS_BY_TABLE_SIZE
    preflop_order = POSITIONS_BY_TABLE_SIZE.get(snap.table_size, [])
    if snap.hero_position in preflop_order:
        i = preflop_order.index(snap.hero_position)
        action_order = preflop_order[i + 1:] + preflop_order[:i]
    else:
        action_order = preflop_order
    order_rank = {position: i for i, position in enumerate(action_order)}
    # 显式保留该输入顺序，renderer 默认仍维持静态翻前顺序以兼容其他调用方。
    ordered_reads = sorted(
        in_hand_reads,
        key=lambda read: (order_rank.get(active.get(read.seat_id, ""), len(order_rank)),
                          active.get(read.seat_id, "")))
    header = "## 对手画像（逐座位；Hero 下注后行动顺序；优先于全场统计）"
    lines = render_reads_zh(
        ordered_reads, active,
        header=header,
        include_exploit=not suppress_action_hints,
        preserve_input_order=True)
    # 桌况段删除后，「本手还有谁没有画像」只能在这里说。不列出来，模型会
    # 把「没出现画像行」当成「没有这个对手」。
    known = {read.seat_id for read in ordered_reads}
    missing = [o.position or o.seat_id for o in snap.opponents
               if not o.folded and o.seat_id not in known]
    if not lines and not missing:
        return []
    if lines:
        lines.append(
            "（n=手数；置信=0–1 的可信度；≈=向人群先验收缩的估计值；"
            "（N 次）=该指标的机会数；暂定=样本不足只作参考；"
            "推断%=动作中由视频补出的比例）")
    else:
        # 一个画像都没有时也要有段头，否则「无画像：SB」会飘到上一段末尾。
        lines = [header, "（本手在局对手都没有可用画像）"]
    if missing:
        lines.append(f"无画像：{'、'.join(missing)}"
                     "（按未知对手处理，不伪造数据）")
    return lines
