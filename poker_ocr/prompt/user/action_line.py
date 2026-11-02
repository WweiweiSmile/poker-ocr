"""行动线事实：这一手每个街的动作顺序（模型判断范围的主输入）。"""

from __future__ import annotations

from ...hand_track import ActionRecord
from ..shared.format import _fmt_num
from ..shared.labels import _STREET_ZH


def _action_zh(action: ActionRecord) -> str:
    if action.action == "check":
        return "过牌"
    if action.action == "fold":
        return "弃牌"
    if action.action == "call":
        return "跟注" + (f" {_fmt_num(action.amount)}BB"
                         if action.amount is not None else "")
    if action.action == "bet":
        return "下注" + (f" {_fmt_num(action.amount)}BB"
                         if action.amount is not None else "")
    if action.action == "raise":
        return "加注到" + (f" {_fmt_num(action.amount)}BB"
                           if action.amount is not None else " ?")
    if action.action == "allin":
        return "全下" + (f" {_fmt_num(action.amount)}BB"
                         if action.amount is not None else "")
    return action.action


def _fmt_actions_zh(actions: list[ActionRecord]) -> str:
    if not actions:
        return "（无行动）"
    parts = []
    for action in actions:
        who = ("Hero" if action.is_hero
               else (action.position or action.name or action.seat_id))
        tags = [name for name, on in (("全下", action.all_in),
                                      ("推断", action.inferred)) if on]
        extra = f"（{'、'.join(tags)}）" if tags else ""
        parts.append(f"{who} {_action_zh(action)}{extra}")
    return " → ".join(parts)


def render_action_line_zh(sit) -> list[str]:
    """「## 行动线」整段。

    行动线来自 OCR/视频解码，可能有归因/顺序误差；标题下面先声明这条口径，
    模型才不会把漏抓的 check 当成「没人行动」。
    """
    snap = sit.snap
    lines: list[str] = ["## 行动线"]
    lines.append("（行动线来自 OCR/视频解码，可能有归因或顺序误差；"
                 "与「当前局面」硬事实冲突时，以硬事实为准。）")
    if any(a.inferred for rec in snap.streets for a in rec.actions):
        lines.append("标注「推断」的动作是视频漏抓后按底池对账补出来的；"
                     "金额是「本街加到总额」。")
    for rec in snap.streets:
        pot_start = (f"（街前底池 {_fmt_num(rec.pot_start)}BB）"
                     if rec.pot_start is not None else "")
        lines.append(f"{_STREET_ZH.get(rec.street, rec.street)}{pot_start}："
                     f"{_fmt_actions_zh(list(rec.actions))}")
    return lines
