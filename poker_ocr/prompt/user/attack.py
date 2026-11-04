"""攻击/剥削块：宽范围死钱局里「能不能开火」的单一裁决。

触发条件很窄：翻前溜入池 + 本街没人下注 + Hero 最后一个行动。这里只输出
**一个结论**（不开火 / 仅强牌下注 / 可小注 / 可高频小注）。

弃牌率证据来自**逐座位画像**（`OpponentRead.fold_to_bet_{street}_pct` +
机会数），不再有「桌况合并值」这一层：多人池真正需要的是「每个仍能行动
的对手都会弃」，那不是均值能表达的量。合并均值与全弃估计只作描述、
不作裁决依据，放在块末。

「对手过牌-加注频繁 / 跟 c-bet 多」走另一条分支（跟着过牌）：那条分支
不给弃牌率证据，否则又变成「他们其实会弃，可以开火」的第二套结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...hand_track import PREFLOP, HandSnapshot
from ...pot_math import _action_order, _can_act, _same_actor, count_limpers
from .notes import _notes_ace_weakness

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation


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
# 样本门槛按「所有有实测的座位合计机会数」算。单人河牌面对下注的机会数
# 普遍只有 6–12 次，按单人 20 次判会把所有结论都压成「样本不足」。
_ATTACK_MIN_TOTAL_OPPS = 20.0

_STREET_CN = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}


@dataclass(frozen=True)
class _SeatFold:
    """一个在局对手在当前街的弃牌率证据。"""

    position: str
    pct: float | None          # 实测百分比；None = 没有可用数据
    opps: float | None         # 机会数
    measured: bool             # 实测且未被先验收缩（才能进裁决与合并）
    note: str = ""             # 无数据时的原因：无画像 / 已全下 / 没有记录


def _fold_seats(sit: Situation, street: str) -> list[_SeatFold]:
    """本手在局对手的当前街弃牌率，按行动顺序（Hero 之后依次）。"""
    live = [o for o in sit.snap.opponents if not o.folded]
    order = _action_order(sit.snap) or []
    rank = {position: i for i, position in enumerate(order)}
    live.sort(key=lambda o: (rank.get(o.position, len(rank)), o.position or ""))
    reads = {r.seat_id: r for r in sit.opponent_reads}
    out: list[_SeatFold] = []
    for opponent in live:
        position = opponent.position or opponent.seat_id
        if not _can_act(opponent):
            # 全下的人不会再弃牌，把他算进「弃牌率」等于虚增弃牌率。
            out.append(_SeatFold(position, None, None, False, "已全下，不会弃牌"))
            continue
        read = reads.get(opponent.seat_id)
        if read is None or read.archetype == "unknown":
            out.append(_SeatFold(position, None, None, False,
                                 "无画像（按未知处理）"))
            continue
        metric = f"fold_to_bet_{street}"
        pct = getattr(read, f"{metric}_pct", None)
        opps = read.opp(metric)
        if pct is None:
            out.append(_SeatFold(position, None, opps, False,
                                 "没有该街面对下注记录"))
            continue
        measured = metric not in read.prior_dominated
        out.append(_SeatFold(position, pct, opps, measured))
    return out


def _fold_verdict(seats: list[_SeatFold]) -> str:
    """逐座位弃牌率 → 单一裁决（support/weak/neutral/small/unknown）。

    顺序有讲究：任何一家实测偏低就是 weak（不能被别人达标盖过去）；
    合计机会数不够是 small；只要还有座位没有可用数据，最高只能到 neutral
    ——「已知的人会弃」不等于「全部人都会弃」。
    """
    measured = [seat for seat in seats if seat.measured]
    if not measured:
        return "unknown"
    if any(seat.pct < _ATTACK_FOLD_WEAK_PCT for seat in measured):
        return "weak"
    total = sum(seat.opps or 0.0 for seat in measured)
    if total < _ATTACK_MIN_TOTAL_OPPS:
        return "small"
    if len(measured) < len(seats):
        return "neutral"
    if all(seat.pct >= _ATTACK_FOLD_SUPPORT_PCT for seat in measured):
        return "support"
    return "neutral"


def _verdict_reason(seats: list[_SeatFold], verdict: str) -> str:
    """结论后面那句最短理由：说明「为什么不是全开火/为什么不能开火」。"""
    measured = [seat for seat in seats if seat.measured]
    if verdict == "weak":
        weak = [seat for seat in measured
                if seat.pct < _ATTACK_FOLD_WEAK_PCT]
        return "；".join(f"{seat.position} 只有 {seat.pct:.0f}%"
                        for seat in weak[:2])
    if verdict == "small":
        total = sum(seat.opps or 0.0 for seat in measured)
        return f"机会合计只有 {total:.0f} 次"
    if verdict == "neutral":
        # 区分「完全没有数据」和「只有被先验收缩的估计」：这两件事都让裁决
        # 停在 neutral，但理由不一样，不能都写成「没有可用数据」（证据行
        # 明明列了 ≈58%，再写没有数据就是自相矛盾）。
        blank = [seat.position for seat in seats if seat.pct is None]
        shrunk = [seat.position for seat in seats
                  if seat.pct is not None and not seat.measured]
        parts: list[str] = []
        if blank:
            parts.append(f"{'、'.join(blank[:3])} 没有可用数据")
        if shrunk:
            parts.append(f"{'、'.join(shrunk[:3])} 只有先验估计")
        if parts:
            return "；".join(parts)
        low = [seat for seat in measured if seat.pct < _ATTACK_FOLD_SUPPORT_PCT]
        if low:
            return "；".join(f"{seat.position} 只有 {seat.pct:.0f}%"
                            for seat in low[:2])
        return ""
    if verdict == "unknown":
        return "没有任何对手有该街面对下注的记录"
    return ""


def _seat_evidence_line(seats: list[_SeatFold], street: str) -> str:
    """「依据」第一行：全部在局对手，逐座位，按行动顺序。"""
    parts: list[str] = []
    for seat in seats:
        if seat.pct is None:
            parts.append(f"{seat.position} {seat.note}")
            continue
        mark = "" if seat.measured else "≈"
        text = f"{seat.position} {mark}{seat.pct:.0f}%"
        if seat.opps:
            text += f"（{seat.opps:.0f} 次）"
        parts.append(text)
    cn = _STREET_CN.get(street, street)
    return f"依据（{cn}面对下注弃牌，逐座位；按行动顺序）：" + "｜".join(parts)


def _pool_notes(seats: list[_SeatFold]) -> list[str]:
    """块末的描述性两行：机会加权合并 + 全弃估计。

    **不作裁决依据**：裁决已经由 `_fold_verdict()` 按逐座位下过了。这里
    只回答「这几个人整体多久弃一次」和「同时都弃有多罕见」，避免模型拿
    单家数字自己脑补成「三家都会弃」。
    只统计实测且机会数达标的座位：被先验收缩的（≈）和没有数据的都不进
    分母 —— 否则一个 5 次机会的 100% 就能把均值拉走。
    """
    measured = [seat for seat in seats if seat.measured and seat.opps]
    if len(measured) < 2:
        # 只剩一家时「合并值」就是那个人的数字，纯重复，不渲染。
        return []
    opps = sum(seat.opps for seat in measured)
    mean = sum(seat.pct / 100.0 * seat.opps for seat in measured) / opps * 100
    lines = [f"合并（机会加权，仅描述，不作裁决依据）：{mean:.0f}%"
             f"（合计 {opps:.0f} 次机会，{len(measured)} 名机会达标）"]
    all_fold = 1.0
    for seat in measured:
        all_fold *= seat.pct / 100.0
    unknown = len(seats) - len(measured)
    tail = f"；另 {unknown} 人未计入" if unknown else ""
    lines.append(f"{len(measured)} 家全弃约 {all_fold * 100:.0f}%"
                 f"（按独立相乘的粗略估计，仅参考{tail}）")
    return lines


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


def _board_wet(sit: Situation, street: str) -> bool:
    """牌面是否「更容易连上」。河牌没有听牌：花听不算湿，只剩成花/顺子可能。

    `flush_draw_possible` 在河牌仍然为真（牌面确实有两张同花），但那时它
    描述的是「不可能成花」，拿它说「降低开火频率」会无故压低结论。
    """
    gt = sit.ground_truth
    if gt is None:
        return False
    info = gt.board_info
    draw = info.flush_draw_possible and street != "river"
    return bool(info.flush_made_possible or info.straight_possible or draw)


def _attack_evidence(sit: Situation, seats: list[_SeatFold], street: str,
                     fold_verdict: str) -> list[str]:
    """攻击块的「依据」：先给全部对手的逐座位事实，再给牌面/A 高面约束。"""
    lines: list[str] = [_seat_evidence_line(seats, street)]

    if _board_wet(sit, street):
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
    xr_known = _xr_data_known(sit)
    wet = _board_wet(sit, street)

    if raisers or callers:
        # 这条分支不给弃牌率证据：结论是「跟着过牌」，再列一遍「他们其实
        # 会弃多少」会变成第二套相反结论（旧桌况河牌 hint 就栽在这里）。
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

    seats = _fold_seats(sit, street)
    fold_verdict = _fold_verdict(seats)

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
    reason = _verdict_reason(seats, fold_verdict)
    lines.append("结论：" + conclusion + (f"（{reason}）" if reason else ""))
    lines.extend(_attack_evidence(sit, seats, street, fold_verdict))
    if fold_verdict == "support" and xr_known and not wet:
        lines.append(
            "范围封顶 + 弃牌率达标 + 没有高 xr/高跟注证据；"
            "被跟注后按下一街权益继续，被加注要放弃。")
    # 描述性两行放在块末：不参与上面的裁决。
    lines.extend(_pool_notes(seats))
    return lines


def attack_signals(sit) -> tuple[list[str], list[str]]:
    """桌级 c-bet 建议是否该被压掉：攻击块存在或已有反向证据。"""
    return _attack_trap_reads(sit)
