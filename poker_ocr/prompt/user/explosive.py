"""爆炸底池：只给结构与权益事实，不写特殊打法规则。

炸弹池的结构差异是「前注固定 5BB/人、翻后 SPR 低、歪中的两对/三条多」；
打法仍按正常翻后规则（牌力、赔率、位置、对手线路），不再写「一条街」
「被下注即弃」这类硬规则。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...hand_track import PREFLOP
from ..shared.format import _fmt_num

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation


def render_explosive_zh(sit: Situation) -> list[str]:
    """炸弹池的结构与权益事实；不写特殊打法规则，按正常翻后规则打。"""
    if sit.street == PREFLOP:
        return ["炸弹池翻前：每人强制投入 5BB 前注后直接发翻牌，翻前没有可做的决策；"
                "翻牌后按正常翻后规则打。"]
    spr = sit.spr
    if spr is None:
        return ["炸弹池结构：SPR 未知，按有效筹码和底池几何，按正常翻后规则打。"]

    players = (sit.opponents_in_hand + 1
               if sit.opponents_in_hand is not None
               else (sit.snap.players_dealt or sit.snap.table_size or 0))
    players_text = f"{players} 人" if players else "多人"
    lines = [
        f"炸弹池结构：SPR {spr:.1f}，{players_text}池；按正常翻后规则"
        "（牌力、赔率、位置、对手线路）打。",
    ]

    hr = sit.hand_ranges
    if hr is not None:
        actual = (sit.opponents_in_hand + 1
                  if sit.opponents_in_hand is not None else 1)
        fair = 1.0 / max(actual, 1)
        random_fill = getattr(hr, "random_opponents", 0)
        detail = f"实际 {actual} 人；范围建模 {len(hr.opponents)} 家"
        if random_fill:
            detail += f" + 随机牌 {random_fill} 家"
        lines.append(
            f"炸弹池权益：对当前范围权益 {hr.equity:.0%}（按河牌、未计后续下注，{detail}；"
            f"公平份额 {fair:.1%}）。这只是当前权益，不自动等于价值下注。")
    return lines


def render_explosive_pot_zh(sit) -> list[str]:
    """「当前局面」里的爆炸底池两行：是不是 + 结构与权益。"""
    if not (sit.explosive_pot or sit.snap.bomb_pot):
        return []
    snap = sit.snap
    lines: list[str] = []
    explosive = "是"
    if snap.bomb_pot:
        explosive += "（屏幕出现「幸运暴击 / BOMB POT」横幅）"
    elif sit.explosive_pot and sit.pot_per_player is not None:
        from ...pot_math import previous_street_pot
        prev_pot = previous_street_pot(snap)
        count = snap.players_dealt or snap.table_size
        explosive += (f"（上一街底池 {_fmt_num(prev_pot)}BB ÷ {count} 人 = "
                      f"{sit.pot_per_player:.2f}BB/人，炸弹前注固定 5BB/人）")
    lines.append(f"爆炸底池：{explosive}")
    if sit.explosive_pot:
        lines.extend(render_explosive_zh(sit))
    return lines
