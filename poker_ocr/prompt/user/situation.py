"""「当前局面」标题块：桌况 / 盲注 / 在局人数 / Hero / 街道 / 底池 / 需跟。

这是模型第一眼看到的事实，全部来自当前帧；行动线、牌力、范围都在这之后。
"""

from __future__ import annotations

from ...pot_math import call_amount
from ..shared.format import _fmt_num
from ..shared.labels import _STREET_ZH


def render_current_state_zh(sit) -> list[str]:
    """「## 当前局面」的前四行（桌况/在局/Hero/街道）。"""
    snap = sit.snap
    lines: list[str] = ["## 当前局面"]

    table = f"{snap.table_size} 人桌" if snap.table_size else "人数未知"
    if snap.blinds:
        blinds = f"{_fmt_num(snap.blinds.sb)}/{_fmt_num(snap.blinds.bb)}"
        if snap.blinds.ante:
            blinds += f"（前注 {_fmt_num(snap.blinds.ante)}）"
    else:
        blinds = "未知"
    lines.append(f"桌况：{table}｜盲注 {blinds}")
    in_hand = snap.opponents_in_hand
    folded = sum(1 for o in snap.opponents if o.folded)
    in_hand_line = (f"本手在局：{in_hand + 1} 人"
                    f"（Hero + {in_hand} 名对手）")
    if folded:
        in_hand_line += f"；已弃牌 {folded} 人"
    lines.append(in_hand_line)
    hero_cards = " ".join(snap.hero_cards) or "未知"
    stack_text = (f"{_fmt_num(snap.hero_stack)}BB"
                  if snap.hero_stack is not None else "未知")
    lines.append(f"Hero：{snap.hero_position or '位置未知'}｜底牌 {hero_cards}｜"
                 f"后手 {stack_text}")
    board = " ".join(snap.board) or "无"
    pot_text = (f"{_fmt_num(snap.pot)}BB"
                if snap.pot is not None and snap.pot > 0 else "未知")
    lines.append(f"街道：{_STREET_ZH.get(snap.street, snap.street)}｜"
                 f"公共牌 {board}｜底池 {pot_text}｜"
                 f"需跟 {_fmt_num(call_amount(snap))}BB")
    return lines
