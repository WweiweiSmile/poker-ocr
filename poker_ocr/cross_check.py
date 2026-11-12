"""模型建议 × 规则引擎的交叉校验。

只做一件事：把模型选的 option 归到 fold / passive / aggressive 三类，
和规则引擎的结论比一次。**不投票、不改动作**——它是安全闸与观测信号，
不是第二套策略（两套策略会立刻漂成两份规则，这正是 offline/ 的教训）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .hand_track import PREFLOP

# 严重程度：ok < mild < severe
_OK, _MILD, _SEVERE = "ok", "mild", "severe"


@dataclass(frozen=True)
class CrossCheck:
    conflict: str          # ok | mild | severe
    rule_action: str | None
    detail: str = ""


def _bucket(kind: str) -> str:
    if kind == "fold":
        return "fold"
    if kind in ("check", "call"):
        return "passive"
    return "aggressive"      # bet | raise | allin


def check(sit, option) -> CrossCheck:
    """对比模型动作与规则引擎动作；覆盖范围外一律返回 ok（不误伤）。"""
    snap = sit.snap
    if snap.street == PREFLOP or not snap.hero_cards or len(snap.board) < 3:
        return CrossCheck(_OK, None, "翻前/无牌面：不做交叉校验")
    if snap.pot is None or snap.pot <= 0:
        return CrossCheck(_OK, None, "底池未知：不做交叉校验")
    from .coach import advise as rule_advise
    from .situation import _coach_actions

    try:
        d = rule_advise(
            list(snap.hero_cards),
            "".join(str(c) for c in snap.board[:5]),
            actions=_coach_actions(snap),
            street=snap.street, pot=float(snap.pot),
            hero_position=snap.hero_position,
            opponents=snap.opponents_in_hand,
            stack_bb=snap.hero_stack,
            hero_invested_bb=snap.hero_invested,
        )
    except (ValueError, KeyError):
        return CrossCheck(_OK, None, "规则引擎无法评估")
    if not d.ok or not d.action:
        return CrossCheck(_OK, None, d.reason or "规则引擎超出覆盖范围")

    mine, theirs = _bucket(option.kind), _bucket(d.action)
    if mine == theirs:
        return CrossCheck(_OK, d.action)
    if mine == "aggressive" and theirs == "fold":
        return CrossCheck(_SEVERE, d.action,
                          f"模型 {option.kind} vs 规则 {d.action}")
    return CrossCheck(_MILD, d.action,
                      f"模型 {option.kind} vs 规则 {d.action}")
