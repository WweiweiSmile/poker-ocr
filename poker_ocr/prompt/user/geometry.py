"""几何事实：有效筹码 / 可玩深度 / SPR / 底池类型 / 短码 / 本手已投入。

SPR 决定「顶对能不能打光」，有效筹码决定「对手能不能把我打光」；这些数
由程序算，模型不自己推，避免同一手牌出现两套口径。
"""

from __future__ import annotations

from ...pot_math import (
    _effective_stack, _hero_total_stack, _opponent_total_stack, _playable_depth,
    _unknown_stack_opponents,
)
from ..shared.format import _fmt_num
from ..shared.labels import _POT_TYPE_ZH


def render_geometry_zh(sit) -> list[str]:
    """有效筹码 / 可玩深度 / SPR / 底池类型（0 对手异常时只写「未知」）。"""
    snap = sit.snap
    anomaly = sit.abnormal_no_opponents
    lines: list[str] = []

    geometry: list[str] = []
    effective = _effective_stack(snap)
    playable = _playable_depth(snap)
    if effective is not None:
        geometry.append(f"有效筹码：{_fmt_num(effective)}BB")
        # 多人池里一个短码会把 min 压到很短，但开池/单挑范围该按能跟注的
        # 最深对手算；两个数都写出来，模型才不会把整桌当短码局。
        if (playable is not None and effective < playable - 0.5
                and (sit.opponents_in_hand or 0) >= 2):
            geometry.append(f"可玩深度：{_fmt_num(playable)}BB"
                            "（对最深对手的后手；翻前开池/单挑范围按此，"
                            "不含本街已下注；翻后套池看有效筹码）")
    if sit.spr is not None:
        spr_text = "<0.1" if 0 < sit.spr < 0.1 else f"{sit.spr:.1f}"
        geometry.append(f"SPR {spr_text}")
    if sit.pot_type:
        geometry.append("底池类型：" + _POT_TYPE_ZH.get(sit.pot_type, sit.pot_type))
    if anomaly:
        lines.append("有效筹码/SPR：未知（状态帧异常，在局对手为 0）")
    if geometry:
        lines.append("｜".join(geometry))
    return lines


def render_side_pot_zh(sit) -> list[str]:
    """短码/side pot 关系 + 本手已投入（沉没成本，只作事实）。"""
    snap = sit.snap
    anomaly = sit.abnormal_no_opponents
    lines: list[str] = []

    hero_total = _hero_total_stack(snap)
    short: list[tuple] = []
    for o in snap.opponents:
        if o.folded:
            continue
        total = _opponent_total_stack(snap, o)
        if (total is not None and hero_total is not None
                and total < hero_total * 0.95):
            short.append((o, total))
    if short:
        detail = "、".join(
            f"{o.position or o.seat_id} {_fmt_num(total)}BB"
            for o, total in short)
        lines.append(f"短码/side pot：{detail}（side pot 未建模；"
                     "能争夺的是他们本手还能投入的总额 = 后手 + 本街已下注）")
    elif anomaly:
        lines.append("短码/side pot：未知（状态帧异常，在局对手为 0）")
    elif hero_total is None:
        # 自己的后手没读出来时，不能写「无」：短码关系根本没算过。
        lines.append("短码/side pot：未知（Hero 后手未读出）")
    elif _unknown_stack_opponents(snap):
        lines.append("短码/side pot：未知（有对手筹码未读出）")
    else:
        lines.append("短码/side pot：无")
    if snap.hero_invested is not None:
        lines.append(f"本手已投入：{_fmt_num(snap.hero_invested)}BB")
    return lines
