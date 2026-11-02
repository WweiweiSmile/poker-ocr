"""价格事实：赔率门槛与可争夺底池。

赔率分母只用「跟注后我实际能争夺的底池」：对手超额全下会被退回，牌桌显示
的 pot 里那部分不在池中。门槛和权益必须同口径，模型才比得对。
"""

from __future__ import annotations

from ...pot_math import call_amount, uncalled_excess, winnable_pot
from ..shared.format import _fmt_num


def render_price_zh(sit) -> list[str]:
    """赔率门槛 / 可争夺底池两行（没有需跟额时不写门槛）。"""
    snap = sit.snap
    lines: list[str] = []
    owed = call_amount(snap)
    winnable = winnable_pot(snap)
    if snap.pot is None or snap.pot <= 0:
        return lines
    excess = uncalled_excess(snap)
    if owed > 0 and winnable > 0:
        line = (f"赔率门槛：{_fmt_num(owed)}BB / 可争夺底池 "
                f"{_fmt_num(winnable)}BB = {owed / winnable * 100:.1f}%"
                "（分母为含我跟注的可争夺底池）")
        if excess > 0:
            line += (f"；牌桌底池 {_fmt_num(snap.pot)}BB 含对手超额 "
                     f"{_fmt_num(excess)}BB，已扣除")
        lines.append(line)
    else:
        line = f"可争夺底池：{_fmt_num(winnable)}BB"
        if excess > 0:
            line += (f"（牌桌底池 {_fmt_num(snap.pot)}BB 含对手超额 "
                     f"{_fmt_num(excess)}BB，已扣除）")
        lines.append(line)
    return lines
