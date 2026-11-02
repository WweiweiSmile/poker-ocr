"""英文旧事实块（**legacy offline only**，不进入 live 提示词）。

`render_hand_facts()` 是中文 lean 之前的英文 prompt，保留它只为复跑旧记录
时还能看到当时的英文事实 + Value directive / Math gate。live 链路的
system/user 在 `prompt/system/` 与 `prompt/user/`，不要在这里加东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...coach import STRONG_DRAW, WEAK_DRAW
from ...hand_track import PREFLOP, ActionRecord
from ...opponent_profile import render_reads
from ...pot_math import (
    EXPLOSIVE_POT_BB_PER_PLAYER, PREFLOP_MIN_RAISE_BB, call_amount,
    count_limpers, previous_street_pot, uncalled_excess, winnable_pot,
)
from .strategy import (
    _aggressive_context, _is_set_mine_pair, math_gate, value_directive,
)

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...advice import Situation


# ─────────────────────── 英文标签 ───────────────────────

_STREET_EN = {"preflop": "Preflop", "flop": "Flop", "turn": "Turn", "river": "River"}

# coach 的中文分档/质地/牌力 → 英文，只用于给模型的提示词渲染。
_TIER_EN = {
    "强成牌": "Strong made hand",
    "超对": "Overpair",
    "顶对强踢": "Top pair, strong kicker",
    "顶对弱踢": "Top pair, weak kicker",
    "中底对": "Middle/bottom pair",
    "强听": "Strong draw",
    "弱听": "Weak draw",
    "空气": "Air",
    "打牌面": "Board play",
}


def _action_en(a: ActionRecord) -> str:
    if a.action == "check":
        return "checks"
    if a.action == "fold":
        return "folds"
    if a.action == "call":
        return "calls" + (f" {a.amount}BB" if a.amount else "")
    if a.action == "bet":
        return "bets" + (f" {a.amount}BB" if a.amount else "")
    if a.action == "raise":
        return "raises to" + (f" {a.amount}BB" if a.amount else " ?")
    return a.action


def _fmt_actions(actions: list[ActionRecord], hero_name: str = "Hero") -> str:
    if not actions:
        return "(none)"
    parts = []
    for a in actions:
        who = hero_name if a.is_hero else (a.position or a.name or a.seat_id)
        # inferred = 视频流漏抓、按街末底池对账补出来的动作。模型要能看到这个
        # 标记，才知道这条行动是程序补的、金额是「本街加到总额」而不是差值
        tags = [name for name, on in (("all-in", a.all_in),
                                      ("inferred", a.inferred)) if on]
        extra = f" ({', '.join(tags)})" if tags else ""
        parts.append(f"{who} {_action_en(a)}{extra}")
    return " → ".join(parts)


def _position_en(pos: str | None) -> str:
    return pos or "unknown"


def render_opponent_reads(sit: Situation) -> list[str]:
    """把本手在局对手的画像写进事实块。unknown 不输出，tentative 标注。

    渲染逻辑与 `opponent_profile.render_reads` 共用，避免两处格式漂移；
    这里只负责把快照里的座位映射成「在局 + 位置」。**legacy offline only**：
    中文 lean 用的是 `render_reads_zh`。
    """
    active = {o.seat_id: o for o in sit.snap.opponents if not o.folded}
    positions = {seat: (o.position or seat) for seat, o in active.items()}
    return render_reads(sit.opponent_reads, positions)


def render_hand_facts(sit: Situation) -> str:
    """英文旧事实块。**legacy offline only**：只给 tools/replay 审计历史用。

    实时链路已经改用 `render_facts_zh`；这里保留是为了复跑旧记录时仍能看到
    当时的英文事实 + Value directive / Math gate。
    """
    snap = sit.snap
    blinds = snap.blinds
    lines: list[str] = []

    table = f"{snap.table_size}-handed" if snap.table_size else "table size unknown"
    lines.append(f"Table: {table}")
    if blinds:
        ante = f" (ante {blinds.ante})" if blinds.ante else ""
        lines.append(f"Blinds: {blinds.sb}/{blinds.bb}{ante}")
    else:
        lines.append("Blinds: unknown")
    opps = [o for o in snap.opponents if not o.folded]
    folded = [o for o in snap.opponents if o.folded]
    stacks = [o.stack for o in opps if o.stack is not None]
    effective = min([*stacks, snap.hero_stack]) if snap.hero_stack is not None \
        and stacks else snap.hero_stack

    lines.append(f"Hero position: {_position_en(snap.hero_position)}｜"
                 f"Hole cards: {''.join(snap.hero_cards) or 'unknown'}｜"
                 f"Stack: {snap.hero_stack if snap.hero_stack is not None else 'unknown'}BB")
    if effective is not None:
        hero_stack = (f"{snap.hero_stack}BB" if snap.hero_stack is not None
                      else "unknown")
        lines.append(f"Effective stack: {round(effective, 2)}BB "
                     f"(my stack behind: {hero_stack})")
    if opps:
        lines.append("Opponents in hand: " + ", ".join(
            f"{o.position or o.seat_id}"
            f"{'(' + o.name + ')' if o.name else ''} "
            f"{o.stack if o.stack is not None else '?'}BB"
            for o in opps))
    if folded:
        lines.append("Folded: " + ", ".join(
            f"{o.position or o.seat_id}" for o in folded))
    # 短码会把 min() 算出的 Effective stack 拉到 2.5BB 这种值。牌桌上真正的
    # 问题是「跟他只能赢/输这么多、其余是 side pot」，必须单独说一句，
    # 否则模型会拿全池算赔率（990051 的教训）。
    short = [o for o in opps if o.stack is not None
             and snap.hero_stack is not None and o.stack < snap.hero_stack * 0.95]
    if short:
        lines.append("Short stacks: " + ", ".join(
            f"{o.position or o.seat_id} {o.stack:g}BB" for o in short) +
            " — side pots are not modeled; only that many BB are at risk "
            "against them.")

    lines.append("")
    to_call = round(snap.to_call, 2)
    # 后手不足以下注额时，实际只需跟到全下：客户端也只会给 Fold / All in。
    # 赔率和跟注比例必须按**实际能跟出的金额**算，否则会把门槛算高。
    owed = call_amount(snap)
    winnable = winnable_pot(snap)
    excess = uncalled_excess(snap)
    lines.append(f"Street: {_STREET_EN.get(snap.street, snap.street)}｜"
                 f"Board: {''.join(snap.board) or 'none'}｜"
                 f"Pot: {snap.pot if snap.pot is not None else 'unknown'}BB｜"
                 f"To call: {owed}BB")
    spot = [sit.pot_type] if sit.pot_type else []
    if sit.in_position is not None:
        spot.append("Hero in position" if sit.in_position
                    else "Hero out of position")
    if sit.players_left_to_act is not None:
        n = sit.players_left_to_act
        spot.append(f"{n} player{'s' if n != 1 else ''} left to act")
    if sit.spr is not None:
        # 短码全下时 SPR 可能只有 0.03：写成 0 会看起来像「没有筹码」。
        spr_text = "<0.1" if 0 < sit.spr < 0.1 else f"{sit.spr:.1f}"
        spot.append(f"SPR {spr_text}")
    if spot:
        lines.append("Spot: " + "｜".join(spot))
    if owed < to_call:
        lines.append(f"The bet covers my stack: calling {owed}BB puts me "
                     f"all-in; raising is not possible.")
    if excess > 0 and winnable > 0:
        # 牌桌底池包含对手无人能跟的筹码，直接拿它算赔率会低估门槛。
        matched = round((snap.hero_bet or 0.0) + owed, 2)
        lines.append(
            f"Uncalled bet: {excess:g}BB of the table pot is above my "
            f"{matched:g}BB matched total and is returned — the winnable pot "
            f"after my call is {winnable:g}BB. Use {winnable:g}BB for pot odds, "
            f"not the table pot.")
    if owed > 0 and winnable > 0:
        # 赔率门槛：跟注额 ÷ 跟注后可争夺的底池。技能里的赔率表就是按这个口径
        bep = owed / winnable * 100
        before_call = max(winnable - owed, 1e-9)
        lines.append(f"Break-even equity to call: {bep:.0f}%; "
                     f"call is {owed / before_call:.0%} of the pot before my call")

    lines.append("")
    lines.append("## Action line")
    if any(a.inferred for rec in snap.streets for a in rec.actions):
        lines.append('Actions tagged "inferred" were reconstructed from the pot '
                     'ledger after the video missed that frame; treat them as real '
                     'actions (amounts are "total invested on the street"). If an '
                     'inferred raise is present, its size is exact, but the raiser '
                     'is the earliest actor consistent with the pot and action order.')
    for rec in snap.streets:
        pot_start = f" (pot before street {rec.pot_start}BB)" \
            if rec.pot_start is not None else ""
        lines.append(f"{_STREET_EN.get(rec.street, rec.street)}{pot_start}: "
                     f"{_fmt_actions(list(rec.actions))}")

    lines.append("")
    lines.append("## Computed facts (trust these, don't recompute)")
    if sit.street == PREFLOP:
        shape = {"unopened": "no one has entered the pot",
                 "limped": "limpers are in, no raise",
                 "single-raise": "one raise already",
                 "three-bet": "a re-raise already"}
        shapes = ", ".join(shape.get(s, s) for s in snap.preflop_shapes) or "unknown"
        lines.append(f"Preflop shape: {shapes}｜Hero is preflop raiser: "
                     f"{'yes' if snap.hero_is_pfr else 'no'}")
        # 范围归属由程序算，不靠模型回忆那张表。实测模型会编出
        # 「AJo is in UTG+1's RFI range」这种和表直接矛盾的理由。
        if sit.range_verdict:
            lines.append(sit.range_verdict)
        limpers = count_limpers(snap)
        if limpers:
            # 行动线可能漏掉平跟（没有动作徽章）；用同帧下注额补出来，
            # 让模型知道前面到底有几个人溜入，而不是只看底池猜。
            lines.append(f"Limpers before me: {limpers}")
        # 本桌规则：翻前任何加注不得低于 5.5BB。选项生成已经按这个下限抬过；
        # 这里再写一句，防止模型在 reason 里建议「加到 2.5-3BB」这种点不出来的尺度。
        if any(o.kind in ("raise", "bet") for o in sit.options):
            lines.append(
                f"Preflop minimum raise: {PREFLOP_MIN_RAISE_BB:g}BB (table rule); "
                "any raise option below it has already been raised to that minimum.")
        # 抽 set 的隐含赔率：翻前不渲染成牌胜率，但小对子需要这组数字才能
        # 判断「跟 1BB 看翻牌」划不划算。阈值（15-20×）来自 preflop-limp.md。
        # 只对 22-99 有意义：TT+ 是超对价值，根本不按抽 set 打，给这组数字
        # 只会让模型把 JJ/TT 也当成「够赔率就跟」。
        if (to_call > 0 and effective is not None
                and _is_set_mine_pair(snap.hero_cards)):
            if "three-bet" in (snap.preflop_shapes or ()):
                # 面对 3-bet 时不能再拿 10:1/15:1 当跟注理由：3-bet 范围窄到
                # 大对子 + AK，中 set 要么撞更大 set，要么他直接弃、拿不到支付。
                lines.append(
                    "Implied odds (pocket pair): effective stack / call = "
                    f"{effective / to_call:.0f}:1, but the 15:1 rule does NOT apply "
                    "vs a 3-bet: his range is QQ+/AK, so a flopped set is either "
                    "set-under-set or gets no pay-off when he whiffs. Do not set-mine "
                    "a 3-bet with this pair.")
            else:
                lines.append(
                    f"Implied odds (pocket pair): effective stack / call = "
                    f"{effective / to_call:.0f}:1; flopping a set is 11.8% (1 in 8.5), "
                    f"so 15:1 or better makes set-mining profitable (10:1 with antes).")
        lines.extend(render_opponent_reads(sit))
    else:
        tier_en = _TIER_EN.get(sit.hand_tier, sit.hand_tier or "unknown")
        lines.append(f"My hand strength: {sit.hand_label_en or 'unreadable'}"
                     f" (tier: {tier_en})")
        if sit.ground_truth is not None:
            # 成手牌、最好五张、精确排名、坚果校验、听牌、牌面结构。
            # 这一段是模型唯一允许引用的牌力事实；不要再在别处重复。
            # 纯听牌走 draw_focus：不渲染 NUT STATUS/Beaten by，避免模型
            # 用「当前未成牌不是坚果」否决 SEMI-BLUFF DEFAULT。
            is_draw = sit.hand_tier in (STRONG_DRAW, WEAK_DRAW)
            lines.append(sit.ground_truth.render(draw_focus=is_draw))
            if is_draw:
                # 听牌时排名只反映「还没成牌」（坚果同花听牌此刻可能只是 A 高）。
                lines.append(
                    "Note: the rank above is for the CURRENT unmade hand. This does "
                    "NOT apply to a draw — judge a draw by its outs and the pot odds, "
                    "not by this number.")
        else:
            lines.append(f"Board texture: {sit.texture_en or 'unknown'}｜"
                         f"Draw: {'yes' if sit.has_draw else 'no'}｜"
                         f"Plays the board (best five all from board): "
                         f"{'yes' if sit.plays_board else 'no'}")
            # 「两对」「顺子」只是名字，不是强度。把实际排名算出来给模型看 ——
            # 8822 在 88T 上 87.7% 已被领先，和「花面的顺子」19.9% 差一个量级，
            # 靠 capped_by_board 这个布尔值分不开。见 coach.beat_rate。
            if sit.beat_pct is not None and sit.beat_at_least_pct is not None:
                n = sit.opponents_in_hand or 0
                if sit.hand_tier in (STRONG_DRAW, WEAK_DRAW):
                    # 听牌时这个数只反映「还没成牌」（坚果同花听牌此刻只是 A 高，
                    # 会被算成 40%+ 已落后）。不提示清楚会把它推去弃牌。
                    lines.append(
                        f"Made-hand rank vs RANDOM hands (lower bound) of the CURRENT "
                        f"(unmade) hand: {sit.beat_at_least_pct:g}% of the time "
                        f"someone already has you beaten right now. This does NOT "
                        f"apply to a draw — judge a draw by its outs and the pot odds, "
                        f"not by this number.")
                else:
                    lines.append(
                        f"Made-hand rank vs RANDOM hands (lower bound; exact count "
                        f"over all {n + 1}-way holdings): beaten by {sit.beat_pct:g}% "
                        f"of random hands per opponent; with {n} opponent(s) in the "
                        f"hand, {sit.beat_at_least_pct:g}% chance at least one is "
                        f"ahead. Random hands are a LOWER bound — anyone who called "
                        f"or raised has a stronger range than random.")
        if "three-bet" in (snap.preflop_shapes or ()):
            lines.append(
                "3-bet pot: the opponent's range is far narrower than random "
                "(premium pairs, big aces, bluffs). The rank above is an UPPER "
                "bound on your equity, NOT your equity against his actual range "
                "— discount it heavily when he bets again.")
        if sit.street in ("turn", "river") and owed > 0:
            lines.append(
                "Facing another barrel: his range has narrowed to value + draws; "
                "the random-hand rank overstates your equity in this spot.")
        if snap.bomb_pot:
            lines.append("Bomb pot banner: YES (幸运暴击 / BOMB POT shown on screen)")
        prev_pot = previous_street_pot(snap)
        if sit.pot_per_player is not None and prev_pot is not None:
            dealer_count = snap.players_dealt or snap.table_size
            why = ("bomb-pot banner on screen" if snap.bomb_pot else
                   f"threshold exactly {EXPLOSIVE_POT_BB_PER_PLAYER:g}BB/player")
            lines.append(
                f"Pot per player entering this street: "
                f"{sit.pot_per_player:.2f}BB ({prev_pot:g}BB ÷ {dealer_count})｜"
                f"Explosive pot: {'YES' if sit.explosive_pot else 'no'} ({why})")
        elif snap.bomb_pot:
            lines.append("Explosive pot: YES (bomb-pot banner on screen)")
        aggressive = _aggressive_context(sit)
        if sit.opponent_strength_en:
            o = sit.opponent_strength_en
            suffix = (" — on this LAG/maniac table, aggression is not automatically "
                      "value." if aggressive else "")
            lines.append(f"Opponent strength from actions: {o['label']} ({o['reason']})"
                         f"{suffix}")
        lines.extend(render_opponent_reads(sit))
        if aggressive:
            lines.append(
                "Table read: value-light aggression (LAG/maniac present) — their bets "
                "and raises are NOT automatically value. Do not thin-value a medium "
                "made hand; check-call and let them barrel.")
        if sit.equity_pct and sit.hand_tier in (STRONG_DRAW, WEAK_DRAW):
            # 四二法则只对**听牌**有意义。成牌（顺子/三条等）的 outs 数的是
            # 「还能不能改进」，不是胜率，直接乘会得到 92% 这种假精确值
            lines.append(f"Estimated draw equity (rule of 4/2): "
                         f"{sit.equity_pct:.0f}%")
            gate = math_gate(sit)
            if gate is not None:
                # 把「权益 vs 门槛」直接算成结论。h104 里模型自己写出
                # 「36% vs 29%」还是弃牌 —— 规则不够，得给它一句判定。
                lines.append(gate.text)
            if (sit.opponents_in_hand or 0) >= 2:
                lines.append(
                    "Multiway draw: the rule-of-4/2 number is the chance to "
                    "COMPLETE, not to win against the field — treat it as an "
                    "upper bound.")
        directive = value_directive(sit)
        if directive:
            lines.append(f"Value directive: {directive}")
        if sit.hand_ranges is not None:
            lines.append("")
            lines.append(sit.hand_ranges.render())
        if sit.fold_eq is not None:
            # 紧跟在「他的范围是什么」之后：范围决定他现在还剩什么牌，
            # 弃牌率决定这些牌里有多少会被打走，两件事必须连着读。
            lines.append("")
            lines.append(sit.fold_eq.render())
        if sit.hand_strength is not None:
            # 牌力解析是「主答案」：behind/equity 三个正交数合成的一行结论。
            # 位置刻意放在 Hand vs ranges 之后、Board structure 之前。
            lines.append(f"Hand-strength read: {sit.hand_strength.summary_en()}")
        if sit.board_eq is not None:
            lines.append("")
            lines.append(sit.board_eq.render())
        if sit.gto is not None:
            lines.append("")
            lines.append(sit.gto.render())
    if snap.hero_invested is not None and snap.hero_stack:
        ratio = snap.hero_invested / max(snap.hero_stack + snap.hero_invested, 1e-6)
        lines.append(f"I have invested {round(snap.hero_invested, 2)}BB"
                     f" (about {ratio:.0%} of current stack)")

    if sit.warnings:
        # 缺牌/缺筹码这类缺口要让模型知道，它才会在理由里说明而不是硬猜
        lines.append("")
        lines.append("Data gaps: " + "; ".join(sit.warnings))

    if sit.previous is not None and sit.previous.street != sit.street:
        # 实时链路每街都是独立调用：不回灌上一街自己的决定，模型会在转牌
        # 把翻牌的「抓诈计划」改成弃牌，前后自相矛盾。
        prev = sit.previous
        plan = f" Plan: {prev.plan}." if getattr(prev, "plan", "") else ""
        lines.append("")
        lines.append("## My previous decision (same hand)")
        lines.append(f"{_STREET_EN.get(prev.street, prev.street)}: I chose "
                     f"{prev.option.label} — \"{prev.reason[:200]}\".{plan}")
        lines.append("Keep that plan unless a computed fact contradicts it; do not "
                     "reverse a line you chose last street without a reason.")

    if sit.table_dynamics:
        # 人工维护的**桌级**倾向（不含玩家名字/单人风格）。放在选项之前：
        # 模型对「最后看到的几条事实」更敏感，而且它不是本手牌的计算结果。
        # 逐座位画像/人工 pin 永远优先于这段桌级描述。
        lines.append("")
        lines.append("## Table context (table-level; per-seat profiles outrank it)")
        lines.append(sit.table_dynamics)

    lines.append("")
    lines.append("## Options (pick exactly one id)")
    for o in sit.options:
        amount = f", amount {o.amount}BB" if o.amount is not None else ""
        note = f" ({o.note})" if o.note else ""
        lines.append(f"- {o.id} — {o.label}{amount}{note}")
    return "\n".join(lines)
