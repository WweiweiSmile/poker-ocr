"""对手范围事实：线路 + 有效宽度 + 成牌/听牌/空气分区 + 谁赢我。

范围由「翻前先验 × 翻后每个动作按时间顺序更新」得到（事件模型），不是每街
从翻前先验重算。range id / combo 数 / mode 英文串是内部实现，只留在 JSONL，
不进提示词。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation


_RANGE_MODE_ZH = {
    "call": "跟注范围",
    "bet": "下注范围",
    "check": "过牌范围",
    "raise": "加注范围",
    "unacted": "未行动范围",
}

# 牌型/分组名（hand_vs_ranges 用英文做内部键）→ 中文。同一张表同时服务
# “赢你的”和“你赢的”，避免两处各写一份翻译。
_RANGE_GROUP_ZH = {
    "quads": "四条",
    "straight flush": "同花顺",
    "full house": "葫芦",
    "flush": "同花",
    "straight": "顺子",
    "trips": "三条",
    "sets": "三条",
    "two pair": "两对",
    "overpair": "超对",
    "top pair": "顶对",
    "second pair": "中对",
    "middle pair": "中对",
    "bottom pair": "底对",
    "underpair": "低对",
    "board pair": "公对",
    "one pair": "一对",
    "high card / air": "空气",
    # hand_vs_ranges 的 ahead_groups 直接用 CATEGORY_NAMES_EN
    "high card": "空气",
}

_STREET_ZH_SHORT = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}


def _event_action_zh(event) -> str:
    action = getattr(event, "action", "")
    if action == "check":
        return "过牌"
    if action == "call":
        return "跟注"
    if action == "bet":
        return "全下" if getattr(event, "all_in", False) else "下注"
    if action == "raise":
        return "加注全下" if getattr(event, "all_in", False) else "加注"
    return action


def _range_line_zh(events) -> str:
    """动作链 → 一行线路（设计 §6：线路是模型判断范围强弱最需要的事实）。

    只取最近三条动作，控制在一屏内；最后一条带上行动前上下文（donk 进几人 /
    面对下注+跟注 / 封口），这是判断「这个动作有多强」的关键。
    """
    events = tuple(events or ())
    if not events:
        return ""
    tokens: list[str] = []
    for event in events[-3:]:
        text = _event_action_zh(event)
        if getattr(event, "is_donk", False):
            text = "donk" + text
        tokens.append(f"{_STREET_ZH_SHORT.get(event.street, event.street)}{text}")
    last = events[-1]
    if getattr(last, "is_donk", False):
        behind = int(getattr(last, "players_behind", 0) or 0)
        tokens[-1] += f"进{behind}人"
    elif getattr(last, "facing_bet", False):
        callers = int(getattr(last, "n_callers", 0) or 0)
        behind = int(getattr(last, "players_behind", 0) or 0)
        if callers:
            tokens[-1] += f"（面对下注+{callers}跟注）"
        elif behind <= 0:
            tokens[-1] += "（封口）"
        else:
            tokens[-1] += f"（面对下注，身后{behind}人）"
    return "→".join(tokens)


def _range_group_zh(name: str | None) -> str:
    key = (name or "").strip().lower()
    return _RANGE_GROUP_ZH.get(key, name or "未知")


def _composition_zh(edge) -> str:
    """范围的成牌/听牌/空气分区。没有事件模型的数据时返回空串。"""
    comp = getattr(edge, "composition", None)
    if comp is None:
        return ""
    parts = [f"成牌 {comp.made_hand:.0%}"]
    # 分项只列成牌类型：空气已经单独写在后面，混进括号会变成
    # 「成牌 100%（一对 63%、空气 37%）」这种自相矛盾的句子。
    types = "、".join(f"{name} {share:.0%}" for name, share in comp.types
                      if name != "空气")
    if types:
        parts[0] += f"（{types}）"
    if comp.draw >= 0.05:
        parts.append(f"听牌 {comp.draw:.0%}")
    parts.append(f"空气 {comp.air:.0%}")
    return "；".join(parts)


def _range_short(edge) -> str:
    """最近两条动作的短标签：`翻牌过牌→转牌过牌`。

    第一行汇总用；逐家明细只写位置，避免同一个长标签在一屏里出现两遍。
    只写最后一条会把「连续过牌」压成「转牌过牌」——模型看不出这是第二次
    过牌，也就不会把顶对/两对+ 从范围里拿走。
    """
    events = tuple(getattr(edge, "events", ()) or ())
    if events:
        return _range_line_zh(events[-2:])
    return _RANGE_MODE_ZH.get(edge.mode or "", edge.mode or "")


def _trailing_checks(edge) -> int:
    """整条翻后线路都是过牌时，返回过牌次数；有任何下注/加注/跟注返回 0。

    只认「纯过牌」线路：过牌后再跟注/加注的范围已经由那个动作重新收窄，
    不能再用「连续过牌封顶」解释。
    """
    events = tuple(getattr(edge, "events", ()) or ())
    if not events:
        return 0
    if any(getattr(event, "action", "") != "check" for event in events):
        return 0
    return len(events)


def _range_cap_zh(edge, actual_opponents: int, behind_any: float,
                  equity: float) -> str:
    """对手连续过牌后的「范围封顶」事实：强牌剩多少、单家/全场各是多少。

    这一段直接回答用户 2026-11-01 的漏：只看「权益 21%/32%」会把连续
    过牌的中等成牌看扁。把「两对+ / 顶对」的残量、「单家你领先多少」和
    「N 家至少一家领先你」并排写出来，模型才不会把单家领先当成全场领先。
    """
    checks = _trailing_checks(edge)
    comp = getattr(edge, "composition", None)
    if checks <= 0 or comp is None:
        return ""
    lead = max(0.0, 1.0 - edge.behind - edge.tie)
    label = "连续过牌封顶" if checks >= 2 else "过牌后范围收窄"
    text = (f"- {label}：两对+ {comp.two_pair_plus:.0%}、"
            f"顶对 {comp.top_pair:.0%}；")
    if actual_opponents <= 1:
        text += f"你领先 {lead:.0%}"
    else:
        text += (f"单家范围你领先 {lead:.0%}，但 {actual_opponents} 家"
                 f"至少一家领先你 {behind_any:.0%}")
    if actual_opponents > 0 and equity > 0:
        fair = 1.0 / (actual_opponents + 1)
        text += (f"；权益 {equity:.0%} 是公平份额 {fair:.1%} 的 "
                 f"{equity / fair:.1f} 倍")
    return text


def _range_label(edge) -> str:
    """`BTN [check]` → `BTN 过牌范围`；内部 range id / combo 数不进 prompt。"""
    position = edge.position or edge.seat_id
    return f"{position} {_range_short(edge)}".strip()


def render_ranges_zh(sit: Situation) -> list[str]:
    """`Hand vs ranges` 的中文事实：线路 + 宽度 + 分区 + 谁赢我。

    设计 §6 的要求：把**线路**写出来（前置位 donk 进 2 人），把**分区**写出来
    （成牌/听牌/空气），再按牌型列「你领先的 / 赢你的」——避免模型把
    「领先 76%」误读成「76% 会推」。range id、combo 数、mode 英文串是内部
    实现，留在 JSONL，不进 prompt。
    """
    hr = sit.hand_ranges
    if hr is None:
        return []
    modeled = len(hr.opponents)
    random_fill = getattr(hr, "random_opponents", 0)
    actual = (sit.opponents_in_hand
              if sit.opponents_in_hand is not None
              else sit.snap.opponents_in_hand)
    shown = hr.opponents
    more = 0
    if len(shown) > 3:
        shown = tuple(sorted(shown, key=lambda e: -e.behind)[:3])
        more = len(hr.opponents) - 3

    def width(edge) -> str:
        eff = getattr(edge, "eff_width_pct", 0.0) or edge.width_pct
        return f"≈{eff:.1f}%"

    ranges = "｜".join(f"{_range_label(o)} {width(o)}" for o in shown)
    if more:
        ranges += f"｜另有 {more} 家只计入权益"
    lines = [f"范围：{ranges}" if ranges else "范围：全部按随机牌计入"]

    # 「本街还没动作」只在**整手都没有翻后动作**时才提；事件模型下范围是
    # 上一街收窄后的状态，不是翻前先验。
    stale = [o for o in hr.opponents
             if getattr(o, "mode", None) == "unacted"
             and not getattr(o, "events", ())]
    if stale:
        lines.append("- 本街还没有动作：范围沿用翻前先验，不是已过牌")

    for edge in shown:
        composition = _composition_zh(edge)
        detail = composition + "；" if composition else ""
        position = edge.position or edge.seat_id
        lead = max(0.0, 1.0 - edge.behind - edge.tie)
        lines.append(f"- {position}：{detail}当前成牌领先我 "
                     f"{edge.behind:.1%}（平局 {edge.tie:.1%}；"
                     f"你领先 {lead:.1%}）")

    # 连续过牌是**强范围退场**的证据：把「两对+/顶对还剩多少」写成一行，
    # 并同时给出「单家领先」和「N 家至少一家领先」，避免模型把单家领先
    # 当成全场领先。只取一个代表（各座位同一线路时数值相同）。
    capped = [e for e in shown if _trailing_checks(e) > 0
              and getattr(e, "composition", None) is not None]
    if capped:
        rep = max(capped, key=_trailing_checks)
        lines.append(_range_cap_zh(rep, actual, hr.behind_any, hr.equity))

    # 按牌型列「你赢的 / 赢你的」，只在事件模型给出分区时才写：旧路径的
    # ahead_groups 只有类别名，混进去会让模型把「类别」当成「范围比例」。
    hit = [g for edge in shown
           for g in getattr(edge, "beaten_by", ()) if g.share >= 0.03]
    beat = [g for edge in shown
            for g in getattr(edge, "ahead_groups", ()) if g.share >= 0.03]
    if hit:
        names: dict[str, float] = {}
        for group in hit:
            zh = _range_group_zh(group.name)
            # 「空气压过我」只是踢脚比较（A 高 vs K 高），不是能打败我的牌型；
            # 写进去会让模型把“空气”当成威胁。
            if zh == "空气":
                continue
            names[zh] = max(names.get(zh, 0.0), group.share)
        ranked = sorted(names.items(), key=lambda kv: -kv[1])[:4]
        if ranked:
            lines.append("- 赢你的：" + "、".join(name for name, _ in ranked))
    if beat and any(getattr(e, "composition", None) for e in shown):
        names = dict.fromkeys(_range_group_zh(g.name) for g in beat)
        lines.append("- 你赢的：" + "、".join(list(names)[:4]))

    # 只在「面对全下」时给警告：这是最容易被当前成牌强度带偏的节点。
    jammers = [e for e in shown
               if getattr(e, "jam", False) and getattr(e, "composition", None)]
    facing_jam = bool(sit.snap.to_call and sit.snap.to_call > 0 and jammers)
    if facing_jam:
        if sit.snap.street == "river":
            # 只对**全下的人**说「你只赢诈唬」：还没行动的人范围宽是正常的，
            # 拿他的空气占比来算会让警告时有时无。
            # 用「空气」分区而不是三组里的 bluff：三组按牌型归属，底对不算诈唬。
            air = max(e.composition.air for e in jammers)
            behind = max(e.behind for e in jammers)
            if behind >= 0.80:
                if air < 0.02:
                    lines.append(
                        f"- 范围警告：河牌面对全下，这个范围里 {behind:.0%} 压过你，"
                        "几乎没有诈唬；不要拿「我现在有牌」当跟注理由，"
                        "按权益和价格判断")
                else:
                    lines.append(
                        f"- 范围警告：河牌面对全下，{behind:.0%} 压过你，"
                        f"你只能赢那 {air:.0%} 的诈唬；不要拿「我现在有牌」"
                        "当跟注理由，按权益和价格判断")
        else:
            semi = max(e.composition.draw for e in jammers)
            if semi >= 0.10:
                lines.append(
                    f"- 范围警告：全下范围里听牌约 {semi:.0%}，"
                    "权益要按剩余牌发完算，不能只看「他现在还没成牌」")

    if random_fill:
        lines.append(f"- 范围建模 {modeled} 家；其余 {random_fill} 家"
                     "按随机牌计入权益，不展开逐家范围")
    elif modeled < actual:
        lines.append(f"- 建模 {modeled} 家，实际 {actual} 家；"
                     "未建模对手未计入范围权益")
    return lines
