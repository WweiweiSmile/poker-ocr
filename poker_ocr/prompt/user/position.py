"""位置事实：翻后位置（座位顺序）+ 身后还有谁没行动。

位置永远是**翻后**座位顺序；翻前另有行动顺序（BB 可能还没说话），所以
翻前的这行必须标成「翻后位置」，不能只写「位置」。身后人数只数「还没跟到
最高注」的人：先行动的人会绕回来再说话。
"""

from __future__ import annotations

from ...hand_track import PREFLOP
from ...pot_math import (
    _action_order, _big_blind_option_pending, _can_act, _street_bet,
    behind_opponents, hero_first_to_act,
)
from ..shared.format import _fmt_num


def render_position_zh(sit) -> list[str]:
    """位置 / 身后待行动人数一行（多人信息用「｜」连起来）。"""
    snap = sit.snap
    anomaly = sit.abnormal_no_opponents
    position: list[str] = []
    if snap.street != PREFLOP and (sit.opponents_in_hand or 0) == 0:
        # 状态帧把对手全标成弃牌时，任何「有位置/先行动」都是假的。
        position.append("位置：无在局对手（状态帧异常，翻后建议不可信）")
    elif sit.in_position is not None:
        # 位置永远是翻后座位顺序。翻前另有行动顺序（BB 可能还没说话），
        # 所以翻前的这行必须标成「翻后位置」，不能只写「位置」。
        label = "翻后位置" if snap.street == PREFLOP else "位置"
        if sit.in_position:
            position.append(f"{label}：Hero 有位置（还能行动的对手里最后）")
            allin_note = _allin_behind_zh(snap)
            if allin_note:
                position.append(allin_note)
        elif hero_first_to_act(snap):
            position.append(f"{label}：Hero 无位置（翻后先行动）")
        else:
            position.append(f"{label}：Hero 无位置（翻后身后还有能行动的对手）")
    if sit.players_left_to_act is not None and not anomaly:
        if snap.street == PREFLOP:
            position.insert(0, _behind_detail_zh(snap, prefix="翻前行动")
                            or f"翻前行动：{sit.players_left_to_act} 人")
        else:
            prefix = ("跟注后待行动" if (snap.to_call or 0.0) > 0
                      else "过牌后待行动")
            position.append(_behind_detail_zh(snap, prefix=prefix)
                            or f"{prefix}：{sit.players_left_to_act} 人")
        if any(_can_act(o) for o in snap.opponents):
            if snap.street == PREFLOP:
                position.append("加注/再加注会重新打开行动，已跟注/加注的对手"
                                "仍可再行动（位置优势≠没人能再加注）")
            else:
                position.append("下注/加注会重新打开行动，已过牌/跟注/下注的对手"
                                "仍可再行动（位置优势≠没人能加注）")
        elif (snap.to_call or 0.0) > 0:
            position.append("对手已全下，无人能再加注")
    elif snap.street == PREFLOP:
        position.insert(0, "翻前行动：未知（有对手位置未读出）")
    if sit.in_position is None and snap.street != PREFLOP:
        position.append("位置未知（座位未读出）：不要假设有/无位置，"
                        "按人数和行动线保守处理")
    if position:
        return ["｜".join(position)]
    return []


def _allin_behind_zh(snap) -> str:
    """身后已有对手全下时，说明他们不能再加注。"""
    order = _action_order(snap)
    hero = snap.hero_position
    if order is None or hero not in order:
        return ""
    idx = order.index(hero)
    stuck = [o for o in snap.opponents
             if (not o.folded and not _can_act(o) and o.position in order
                 and order.index(o.position) > idx)]
    if not stuck:
        return ""
    names = "、".join(o.position or o.seat_id for o in stuck[:4])
    if len(stuck) > 4:
        names += "…"
    return f"身后 {names} 已全下，不能再加注"


def _behind_detail_zh(snap, prefix: str = "过牌后待行动") -> str:
    """「过牌后待行动」一行：人数 + 位置名 + 谁还没跟到当前注。

    只写人数（旧口径）会让模型忽略「溜入者还没响应加注」这种关键约束；
    1c9 的 K8o 就是把它当成「收尾的便宜跟注」。
    """
    behind = behind_opponents(snap)
    order = _action_order(snap)
    if order is None or snap.hero_position not in order:
        return ""
    behind.sort(key=lambda o: order.index(o.position))
    max_bet = (snap.hero_bet or 0.0) + float(snap.to_call or 0.0)
    pending = [o for o in behind
               if 1e-9 < _street_bet(snap, o) < max_bet - 1e-9]
    pending_ids = {o.seat_id for o in pending}
    bb_option = _big_blind_option_pending(snap)
    parts = []
    if pending:
        pending_names = "、".join(o.position or o.seat_id for o in pending)
        parts.append(f"{pending_names} 还没跟到 {_fmt_num(max_bet)}BB")
    rest = [o.position or o.seat_id for o in behind
            if o.seat_id not in pending_ids
            and (bb_option is None or o.seat_id != bb_option.seat_id)]
    if rest:
        shown = "、".join(rest[:4]) + ("…" if len(rest) > 4 else "")
        parts.append(f"{shown} 还没行动")
    if bb_option is not None:
        parts.append("BB 还没行动，你跟注后 BB 仍有加注选项")
    text = f"{prefix}：{len(behind)} 人"
    if parts:
        text += "（" + "；".join(parts) + "）"
    return text
