"""爆炸底池：只给结构与权益事实，不写特殊打法规则。

炸弹池的结构差异是「前注固定 5BB/人、翻后 SPR 低、歪中的两对/三条多」。
炸弹池横幅确认且翻前没有动作时（#114），牌局由「炸弹池守则」段给固定
打法，这里只说结构/权益事实并指向守则；横幅串手、翻前有真实动作时仍按
正常翻后规则（牌力、赔率、位置、对手线路），不写「一条街」「被下注即弃」
这类硬规则。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...hand_track import PREFLOP
from ...playbook.bomb import bomb_guard_applies
from ..shared.format import _fmt_num

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation


def render_explosive_zh(sit: Situation) -> list[str]:
    """炸弹池的结构与权益事实；不写特殊打法规则。

    守则生效（#114）时把「按正常翻后规则打」改成指向「炸弹池守则」段；
    横幅串手等守则不生效的情况保持原文，避免指向不存在的段。
    """
    guard = bomb_guard_applies(sit)
    plan_hint = ("翻牌后按下方「炸弹池守则」处理。"
                 if guard else "翻牌后按正常翻后规则打。")
    if sit.street == PREFLOP:
        return ["炸弹池翻前：每人强制投入 5BB 前注后直接发翻牌，翻前没有可做的决策；"
                + plan_hint]
    spr = sit.spr
    if spr is None:
        return ["炸弹池结构：SPR 未知，按有效筹码和底池几何，"
                + ("翻牌后按下方「炸弹池守则」处理。" if guard
                   else "按正常翻后规则打。")]

    players = (sit.opponents_in_hand + 1
               if sit.opponents_in_hand is not None
               else (sit.snap.players_dealt or sit.snap.table_size or 0))
    players_text = f"{players} 人" if players else "多人"
    structure_tail = ("翻牌后按下方「炸弹池守则」处理。"
                      if guard else
                      "按正常翻后规则（牌力、赔率、位置、对手线路）打。")
    lines = [
        f"炸弹池结构：SPR {spr:.1f}，{players_text}池；{structure_tail}",
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
