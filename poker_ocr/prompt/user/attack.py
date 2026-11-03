"""攻击/剥削块：宽范围死钱局里「能不能开火」的单一裁决。

触发条件很窄：翻前溜入池 + 本街没人下注 + Hero 最后一个行动。这里只输出
**一个结论**（不开火 / 仅强牌下注 / 可小注 / 可高频小注），依据里的弃牌率、
xr、牌面、A 高面都只描述单项，不另下结论。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...hand_track import PREFLOP, HandSnapshot
from ...pot_math import _action_order, _can_act, _same_actor, count_limpers
from ..shared.format import _fmt_num
from .notes import _notes_ace_weakness
from .table_stats import _TABLE_SMALL_OPPS

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation
    from ...opponent_profile import PooledStats


def _attack_trap_reads(sit: Situation) -> tuple[list[str], list[str]]:
    """活对手里「跟 c-bet 多」/「过牌-加注频繁」的人（先验估计不算硬结论）。

    剥削不能只看「对手范围宽」：如果对手 xc（过牌-跟注）/xr（过牌-加注）
    频率高，翻牌小注诈唬没有弃牌率，还会被加注惩罚 —— 这时正确动作是
    跟着过牌，而不是高频开火。
    """
    live = {o.seat_id: o for o in sit.snap.opponents if not o.folded}
    callers: list[str] = []
    raisers: list[str] = []
    for read in sit.opponent_reads:
        seat = read.seat_id
        if seat not in live or read.archetype == "unknown":
            continue
        position = live[seat].position or seat
        call = read.call_to_cbet_pct
        fold = read.fold_to_cbet_pct
        if ("call_to_cbet" not in read.prior_dominated
                and call is not None and call >= 55):
            callers.append(f"{position} 跟 c-bet {call:.0f}%")
        elif ("fold_to_cbet" not in read.prior_dominated
                and fold is not None and fold <= 30):
            callers.append(f"{position} 对 c-bet 只弃 {fold:.0f}%")
        xr = read.check_raise_pct
        if ("check_raise" not in read.prior_dominated
                and xr is not None and xr >= 20):
            raisers.append(f"{position} 过牌-加注 {xr:.0f}%")
    return callers, raisers


_ATTACK_FOLD_SUPPORT_PCT = 50.0
_ATTACK_FOLD_WEAK_PCT = 35.0


def _checked_before_hero(snap: HandSnapshot) -> list:
    """本街在 Hero 之前已经过牌、且还能行动的对手。

    `behind_opponents()` 只数位置在 Hero 之后的人；已经过牌的人不在里面，
    但 Hero 下注后他们会再行动一次，可以 check-raise。攻击块必须看到这批人。
    """
    order = _action_order(snap)
    hero = snap.hero_position
    if order is None or hero not in order:
        return []
    idx = order.index(hero)
    checks = [a for a in snap.actions_of(snap.street) if a.action == "check"]
    out = []
    for opponent in snap.opponents:
        if (opponent.folded or not _can_act(opponent)
                or opponent.position not in order):
            continue
        if order.index(opponent.position) >= idx:
            continue
        if any(_same_actor(a, opponent) for a in checks):
            out.append(opponent)
    return out


def _checkraise_note(checked: list) -> str:
    """把「前面过牌的人仍能加注」写成一句硬事实。"""
    if checked:
        names = "、".join(o.position or o.seat_id for o in checked[:4])
        if len(checked) > 4:
            names += "…"
        return (f"前面 {len(checked)} 人已过牌（{names}），"
                "他们仍可过牌-加注；这不是无风险下注。")
    return ("前面可能还有人已过牌（行动线未记录全），"
            "仍要防过牌-加注；这不是无风险下注。")


def _xr_data_known(sit: Situation) -> bool:
    """是否至少有一名活对手的过牌-加注频率是实测值。

    只判断「有没有数据」；xr 高不高由 `_attack_trap_reads()` 判断，
    后者命中时整个攻击块会走「跟着过牌」分支。
    """
    live = {o.seat_id for o in sit.snap.opponents if not o.folded}
    return any(
        r.seat_id in live and r.archetype != "unknown"
        and r.check_raise_pct is not None
        and "check_raise" not in r.prior_dominated
        for r in sit.opponent_reads)


def _attack_fold(stats: "PooledStats | None", street: str):
    """当前街「面对下注弃牌率」的值和机会数。"""
    value = getattr(stats, f"fold_to_bet_{street}", None) if stats else None
    opps = (stats.opportunities or {}).get(street, 0.0) if stats else 0.0
    return value, opps


def _attack_fold_verdict(value: float | None, opps: float) -> str:
    """只看弃牌率这一项的结论；最终开火/不开火由攻击块统一裁决。"""
    if value is None:
        return "unknown"
    if opps < _TABLE_SMALL_OPPS:
        return "small"
    if value >= _ATTACK_FOLD_SUPPORT_PCT:
        return "support"
    if value <= _ATTACK_FOLD_WEAK_PCT:
        return "weak"
    return "neutral"


def _board_wet(sit: Situation) -> bool:
    gt = sit.ground_truth
    return bool(gt is not None and (
        gt.board_info.flush_draw_possible
        or gt.board_info.flush_made_possible
        or gt.board_info.straight_possible))


def _attack_evidence(sit: Situation, stats: "PooledStats | None", street: str,
                     scope_label: str, fold_verdict: str = "unknown") -> list[str]:
    """攻击块的「依据」：只陈述单项事实，不再给第二套结论。"""
    lines: list[str] = []
    value, opps = _attack_fold(stats, street)
    if value is not None:
        cn = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}.get(
            street, street)
        line = f"{scope_label}：对手面对{cn}下注弃 {value:.0f}%"
        if opps < _TABLE_SMALL_OPPS:
            line += f"（仅 {_fmt_num(opps)} 次机会）"
            verdict = "机会数不足，不能单独作为开火理由"
        elif value >= _ATTACK_FOLD_SUPPORT_PCT:
            verdict = "弃牌率支持开火"
        elif value <= _ATTACK_FOLD_WEAK_PCT:
            verdict = "弃牌率不足，不支持开火"
        else:
            verdict = "弃牌率一般，不能单独作为开火理由"
        # 「只描述这一项」避免和上面的总裁决读成两句相反建议。
        lines.append(line + f" → {verdict}（只描述这一项，结论见上）")

    if _board_wet(sit):
        lines.append(
            "牌面有花/顺可能：对手范围更容易连上，降低开火频率；"
            "优先用有摊牌价值或能改进的牌，纯空气别硬开。")

    if any(str(card)[:1].upper() == "A" for card in sit.snap.board):
        if _notes_ace_weakness(sit.table_dynamics):
            if fold_verdict == "support":
                lines.append(
                    "A 高面：人工观察说本池对手见 A 抵抗力下降（容易弃牌）→ "
                    "可以借 A 施压（本街弃牌率也支持）；仍受 xr/跟注约束。")
            else:
                # 观察只作背景，不改变「弃牌率不足/没有 xr 数据」的结论。
                lines.append(
                    "A 高面：人工观察说本池对手见 A 容易弃牌，但本街弃牌率"
                    "不足/没有 xr 数据，不能只凭 A 高开火；按上面的结论。")
        else:
            # 没有现场观察时只给事实，不断言「A 高面一定更有效」。
            lines.append(
                "A 高面：A 是牌面最大张；对手会不会弃牌要看上面的弃牌率"
                "和画像，不能只凭 A 高默认开火。")
    return lines


def render_attack_zh(sit: Situation) -> list[str]:
    """宽范围/死钱局的剥削判断（程序判断「范围封顶 + Hero 收尾」）。

    触发条件很窄：**翻前溜入池 + 本街没人下注 + Hero 最后一个行动**。
    这里只输出**一个结论**（不开火 / 仅强牌下注 / 可小注 / 可高频小注），
    依据里的弃牌率、xr、牌面、A 高面都只描述单项，不另下结论。

    用户 2026-10-31 口径：这个池子的人看到公共牌有 A 时抵抗力明显下降，
    A 高面要多打。人工观察写在 `.table_dy`，这里只在 A 高面引用它；
    但对手 xc/xr 高时，A 高面也不硬开。
    """
    snap = sit.snap
    street = snap.street
    if street == PREFLOP or sit.pot_type != "limped pot":
        return []
    if (snap.to_call or 0.0) > 0 or sit.players_left_to_act != 0:
        return []
    opponents = sit.opponents_in_hand or 0
    if opponents < 2:
        return []

    limpers = count_limpers(snap)
    limp_text = f"翻前 {limpers} 人溜入" if limpers else "翻前多人溜入"
    checked = _checked_before_hero(snap)
    # 只有「所有还能行动的对手都记录了过牌」才能说「全部过牌到 Hero」。
    # 全下的人不需要 check；OCR 漏抓 check 时 players_left_to_act 仍可能是 0。
    expected_checks = sum(1 for o in snap.opponents if _can_act(o))
    if expected_checks == 0:
        # 所有人都全下：没有「过牌到 Hero」这回事，也没有可攻击的弃牌率。
        return []
    all_checked = len(checked) >= expected_checks
    if all_checked:
        dead_money = (
            f"{limp_text}、没人加注；本街全部过牌到 Hero"
            "（你收尾）→ 对手范围宽，底池是死钱。")
    else:
        dead_money = (
            f"{limp_text}、没人加注；本街没有下注，但行动线未记录全，"
            "不能确认全部过牌（按可能 check-raise 处理）→ "
            "对手范围可能仍宽，底池是死钱，但不能当无风险开火点。")
    callers, raisers = _attack_trap_reads(sit)
    stats = sit.table_stats
    scope = (getattr(stats, "scope", "population")
             if stats is not None else "population")
    scope_label = "本桌" if scope == "current_table" else "全场先验"
    cbet_opps = (stats.opportunities or {}).get("fcbet", 0.0) if stats else 0.0
    table_sticky = bool(
        stats is not None and scope == "current_table"
        and stats.fold_to_cbet is not None
        and cbet_opps >= _TABLE_SMALL_OPPS and stats.fold_to_cbet <= 35)
    xr_known = _xr_data_known(sit)
    fold_value, fold_opps = _attack_fold(stats, street)
    fold_verdict = _attack_fold_verdict(fold_value, fold_opps)
    wet = _board_wet(sit)

    if raisers or callers or table_sticky:
        lines = ["## 剥削判断（宽范围/死钱）",
                 "（以下结论是程序建议，不是事实；与逐座位画像冲突时以画像为准）",
                 dead_money]
        lines.append(_checkraise_note(checked))
        lines.append("但他们的过牌不一定是放弃：")
        if raisers:
            lines.append(
                "、".join(raisers) + " —— 过牌-加注频繁，这是「过牌控池」点，"
                "不是开火点；用中等牌/空气下注会被 xr 惩罚。")
        if callers:
            lines.append(
                "、".join(callers) + " —— 跟 c-bet 多，小注诈唬没有弃牌率。")
        if table_sticky:
            lines.append(
                f"{scope_label}对 c-bet 只弃 {stats.fold_to_cbet:.0f}%"
                f"（{_fmt_num(cbet_opps)} 次机会）—— 诈唬没有弃牌率。")
        if snap.street == "river":
            lines.append(
                "结论：跟着过牌，进入摊牌；只有可下注价值（两对+/超对/顶对强踢）"
                "可以下注拿价值；空气不要为看牌而下注。")
        else:
            lines.append(
                "结论：跟着过牌，看免费牌；只有可下注价值（两对+/超对/顶对强踢）"
                "可以下注拿价值；强听没有弃牌率、按默认过牌，空气过牌。")
        if (any(str(card)[:1].upper() == "A" for card in snap.board)
                and _notes_ace_weakness(sit.table_dynamics)):
            lines.append("A 高面也一样：对手 xc/xr 高时，怕 A 不等于会弃牌。")
        return lines

    lines = ["## 攻击机会（宽范围/死钱）",
             "（以下结论是程序建议，不是事实；与逐座位画像冲突时以画像为准）",
             dead_money]
    lines.append(_checkraise_note(checked))
    if not xr_known:
        lines.append(
            "没有对手过牌-加注数据，不能把这里当无风险开火点。")

    if fold_verdict == "support" and xr_known and not wet:
        conclusion = ("可高频小注攻击（1/3–1/2 池）：可下注价值牌拿价值，"
                      "强听可按牌型段下注诈唬")
    elif fold_verdict == "support" and xr_known:
        conclusion = ("可小注，但牌面有花/顺可能，降低频率；"
                      "可下注价值牌拿价值，强听可按牌型段下注诈唬")
    elif fold_verdict == "support":
        conclusion = ("可小注，但不默认高频（没有 xr 数据）；"
                      "可下注价值牌下注拿价值，强听可按牌型段小注诈唬，空气过牌")
    elif fold_verdict == "weak":
        conclusion = ("不开火：可下注价值（两对+/超对/顶对强踢）下注拿价值，"
                      "强听/空气按默认过牌")
    elif fold_verdict == "small":
        conclusion = ("仅可下注价值牌下注（弃牌率样本不足）；"
                      "强听没有弃牌率支撑，按默认过牌")
    elif fold_verdict == "neutral":
        conclusion = ("仅可下注价值牌下注（弃牌率一般，不能单独作为开火理由）；"
                      "强听/空气过牌")
    else:
        conclusion = "仅可下注价值牌下注（没有该街弃牌率）；强听/空气过牌"
    lines.append("结论：" + conclusion)
    lines.append("依据：")
    lines.extend(_attack_evidence(sit, stats, street, scope_label,
                                  fold_verdict))
    if fold_verdict == "support" and xr_known and not wet:
        lines.append(
            "范围封顶 + 弃牌率达标 + 没有高 xr/高跟注证据；"
            "被跟注后按下一街权益继续，被加注要放弃。")
    return lines


def attack_signals(sit) -> tuple[list[str], list[str]]:
    """桌级 c-bet 建议是否该被压掉：攻击块存在或已有反向证据。"""
    return _attack_trap_reads(sit)
