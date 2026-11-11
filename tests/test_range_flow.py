#!/usr/bin/env python3
"""事件模型：口径、单调性、权益一致性、本手回归。

验收标准来自 `docs/对手范围-事件模型-设计.md` §9：

  · §9.1 口径单测：donk A、players_behind、jam、n_callers；
  · §9.2 单调性：多人 → 诈唬乘数不增；facing_bet_and_call < facing_bet_open；
  · §9.3 权益一致性：纯花听翻牌 ≈ 加权回翻牌；
  · §9.4 回归：本手河牌两次决策权益低于门槛；combo draw 不被强度门槛杀掉；
    短码顶对 jam 倾向高于深码。

    nv tests/test_range_flow.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import field_ranges, hand_vs_ranges, range_flow, ranges  # noqa: E402
from poker_ocr.coach import parse_cards  # noqa: E402
from poker_ocr.field_ranges import OpponentRangeSpec  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")


def act(seat: str, pos: str | None, action: str, amount: float | None = None,
        *, street: str = "flop", hero: bool = False, all_in: bool = False,
        inferred: bool = False) -> ActionRecord:
    return ActionRecord(street=street, seat_id=seat, name=seat, position=pos,
                        action=action, amount=amount, all_in=all_in,
                        is_hero=hero, inferred=inferred)


def opp(seat: str, pos: str | None, stack: float = 100.0, *,
        folded: bool = False, bet: float = 0.0) -> OpponentSnapshot:
    return OpponentSnapshot(seat, seat, pos, stack, bet, folded)


def snapshot(*, street: str, board: tuple[str, ...], hero: tuple[str, str],
             hero_pos: str, hero_stack: float, pot: float,
             pot_before_street: float, to_call: float, opponents, streets,
             bomb_pot: bool = False) -> HandSnapshot:
    return HandSnapshot(
        hand_id="h-evt", hand_index=1, street=street, board=board,
        hero_cards=hero, hero_position=hero_pos, hero_stack=hero_stack,
        hero_bet=0.0, hero_invested=0.0, to_call=to_call, second_bet=None,
        pot=pot, pot_before_street=pot_before_street, table_size=9,
        blinds=BLINDS, opponents=tuple(opponents), streets=tuple(streets),
        preflop_shapes=(), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h-evt", street, 0, board, hero),
        bomb_pot=bomb_pot, players_dealt=len(opponents) + 1,
    )


# ─────────────────────── §9.1 口径单测 ───────────────────────

def test_bucket_of_donk_and_lead() -> None:
    """donk A：首个行动、下注进上一街 aggressor；被抢先行动就不是 donk。"""
    base = dict(street="river", board="6hKd9d4hKh", seat_id="x", action="bet",
                prev_street_aggressor="hero")

    donk = range_flow.ActionEvent(
        **base, is_first_to_act=True, players_behind=2, is_donk=True)
    check(range_flow.bucket_of(donk) == "donk_multiway",
          f"多人池 donk 应是 donk_multiway，实际 {range_flow.bucket_of(donk)}")

    hu = range_flow.ActionEvent(
        **base, is_first_to_act=True, players_behind=1, is_donk=True)
    check(range_flow.bucket_of(hu) == "donk_hu",
          f"单挑 donk 应是 donk_hu，实际 {range_flow.bucket_of(hu)}")

    # 有人先行动过（过牌/下注都算）：不是首个行动 → 不是 donk
    stab = range_flow.ActionEvent(
        **base, is_first_to_act=False, players_behind=2, is_donk=False)
    check(range_flow.bucket_of(stab) == "stab",
          f"前面有人过牌后下注应是 stab，实际 {range_flow.bucket_of(stab)}")

    lead = range_flow.ActionEvent(
        street="flop", board="Ks7d2c", seat_id="x", action="bet",
        prev_street_aggressor=None, is_first_to_act=True, players_behind=3,
        is_donk=False)
    check(range_flow.bucket_of(lead) == "lead_multiway",
          f"无 aggressor 的首个下注是 lead_multiway，实际 {range_flow.bucket_of(lead)}")

    closing = range_flow.ActionEvent(
        street="flop", board="Ks7d2c", seat_id="x", action="check",
        is_first_to_act=False, players_behind=0)
    check(range_flow.bucket_of(closing) == "closing_check",
          f"最后行动过牌应是 closing_check，实际 {range_flow.bucket_of(closing)}")


def test_bucket_of_facing_bet() -> None:
    """面对下注的四个桶：封口 / 身后有人 / 有跟注 / 面对加注。"""
    def event(**kw):
        base = dict(street="turn", board="Ks7d2c4h", seat_id="x",
                    action="call", facing_bet=True)
        base.update(kw)
        return range_flow.ActionEvent(**base)

    check(range_flow.bucket_of(event(players_behind=0))
          == "facing_bet_closing", "身后无人应是封口桶")
    check(range_flow.bucket_of(event(players_behind=2))
          == "facing_bet_open", "身后有人应是 facing_bet_open")
    check(range_flow.bucket_of(event(players_behind=2, n_callers=1))
          == "facing_bet_and_call", "已有跟注应是 facing_bet_and_call")
    check(range_flow.bucket_of(event(players_behind=1, raises_before=2))
          == "facing_raise", "面对加注应是 facing_raise")


def _field_snapshot() -> HandSnapshot:
    """U18 式多人池：翻牌全过，转牌 Hero 下注、HJ 跟、UTG+1 跟。"""
    return snapshot(
        street="turn", board=("6h", "Kd", "9d", "4h"),
        hero=("Ks", "3c"), hero_pos="LJ", hero_stack=180.0, pot=75.0,
        pot_before_street=45.0, to_call=0.0,
        opponents=(opp("upR", "UTG", 100.0, folded=True),
                   opp("lowR", "UTG+2", 95.0),
                   opp("midR", "UTG+1", 93.5),
                   opp("lowL", "HJ", 140.5),
                   opp("midL", "CO", 249.0),
                   opp("topL", "SB", 237.5),
                   opp("topR", "BB", 482.0),
                   opp("upL", "BTN", 173.0, folded=True)),
        streets=(
            StreetRecord("preflop", 45.0, ()),
            StreetRecord("flop", 45.0, (
                act("upR", "UTG", "fold", street="flop"),
                act("hero", "LJ", "check", street="flop", hero=True),
                act("lowL", "HJ", "check", street="flop"),
                act("lowR", "UTG+2", "check", street="flop"),
                act("midL", "CO", "check", street="flop"),
                act("midR", "UTG+1", "check", street="flop"),
                act("topL", "SB", "check", street="flop"),
                act("topR", "BB", "check", street="flop"),
            )),
            StreetRecord("turn", 45.0, (
                act("hero", "LJ", "bet", 30.0, street="turn", hero=True),
                act("lowL", "HJ", "call", 30.0, street="turn"),
                act("midR", "UTG+1", "call", 30.0, street="turn"),
            )),
        ),
    )


def test_events_players_behind_and_callers() -> None:
    """players_behind 只数还能行动、且本街还没行动过的对手。"""
    events, warnings = field_ranges.build_events(_field_snapshot())
    mid = events["midR"]
    check(len(mid) == 2, f"UTG+1 应有翻牌过牌 + 转牌跟注两条事件：{mid}")
    flop, turn = mid
    check(flop.street == "flop" and flop.action == "check", "第一条应是翻牌过牌")
    check(turn.street == "turn" and turn.action == "call", "第二条应是转牌跟注")
    # Hero 下注、HJ 已跟：身后只剩 UTG+2 / CO / SB / BB 四个还能行动
    check(turn.players_behind == 4,
          f"UTG+1 跟注时身后应是 4 人，实际 {turn.players_behind}")
    check(turn.n_callers == 1,
          f"UTG+1 行动前本街已有 1 个跟注（HJ），实际 {turn.n_callers}")
    check(turn.facing_bet, "UTG+1 面对 Hero 的下注")
    check(range_flow.bucket_of(turn) == "facing_bet_and_call",
          f"UTG+1 应为 facing_bet_and_call，实际 {range_flow.bucket_of(turn)}")

    hj = [e for e in events["lowL"] if e.street == "turn"][0]
    check(hj.players_behind == 5,
          f"HJ 跟注时身后应是 5 人，实际 {hj.players_behind}")
    check(hj.n_callers == 0, f"HJ 是第一个跟注者，n_callers 应为 0，实际 {hj.n_callers}")
    check(range_flow.bucket_of(hj) == "facing_bet_open",
          f"HJ 应为 facing_bet_open，实际 {range_flow.bucket_of(hj)}")


def _river_lead_snapshot(turn: tuple[ActionRecord, ...]) -> HandSnapshot:
    """LJ（midR）对 CO（hero）的单挑河牌：河牌 LJ 先下注 21。"""
    return snapshot(
        street="river", board=("As", "Kd", "2c", "3d", "3c"),
        hero=("Ad", "5h"), hero_pos="CO", hero_stack=100.0, pot=63.5,
        pot_before_street=42.5, to_call=21.0,
        opponents=(opp("midR", "LJ", 42.0),),
        streets=(
            StreetRecord("preflop", 4.5, (
                act("midR", "LJ", "call", 1.0, street="preflop"),
                act("hero", "CO", "call", 1.0, street="preflop", hero=True),
            )),
            StreetRecord("flop", 9.5, (
                act("midR", "LJ", "check", street="flop"),
                act("hero", "CO", "check", street="flop", hero=True),
            )),
            StreetRecord("turn", 9.5, turn),
            StreetRecord("river", 33.5, (
                act("midR", "LJ", "bet", 21.0, street="river"),
            )),
        ),
    )


def test_self_lead_is_not_a_donk() -> None:
    """口径 A：自己是上一街 aggressor 时，下一街先下注是续注，不是 donk。"""
    # 转牌 LJ 下注、Hero 跟 → 河牌 LJ 再下注：上一街 aggressor 是他自己
    self_lead = _river_lead_snapshot((
        act("midR", "LJ", "bet", 12.0, street="turn"),
        act("hero", "CO", "call", 12.0, street="turn", hero=True),
    ))
    events, _ = field_ranges.build_events(self_lead)
    river = [e for e in events["midR"] if e.street == "river"][0]
    check(river.prev_street_aggressor == "midR",
          f"河牌上一街 aggressor 应是 LJ 自己：{river.prev_street_aggressor}")
    check(not river.is_donk, "自己是上一街 aggressor 时不应标成 donk")
    check(range_flow.bucket_of(river) == "lead_hu",
          f"续注应走 lead_hu，实际 {range_flow.bucket_of(river)}")

    # 转牌 LJ 过、Hero 下注、LJ 跟 → 河牌 LJ 先下注：donk 进 Hero
    donk = _river_lead_snapshot((
        act("midR", "LJ", "check", street="turn"),
        act("hero", "CO", "bet", 12.0, street="turn", hero=True),
        act("midR", "LJ", "call", 12.0, street="turn"),
    ))
    events, _ = field_ranges.build_events(donk)
    river = [e for e in events["midR"] if e.street == "river"][0]
    check(river.prev_street_aggressor == "hero",
          f"河牌上一街 aggressor 应是 Hero：{river.prev_street_aggressor}")
    check(river.is_donk, "下注进上一街 aggressor 应标成 donk")
    check(range_flow.bucket_of(river) == "donk_hu",
          f"单挑 donk 应走 donk_hu，实际 {range_flow.bucket_of(river)}")


def test_jam_is_not_a_new_action() -> None:
    """全下不是独立动作档：仍是 bet/raise，只由 SPR 调倾向。"""
    cls = range_flow.HandClass("medium", "none", "value")
    shallow = range_flow.likelihood(
        "raise", cls, facing=True, bucket="facing_bet_open",
        spr_bucket="0.5-1", all_in=True)
    deep = range_flow.likelihood(
        "raise", cls, facing=True, bucket="facing_bet_open",
        spr_bucket="4+", all_in=True)
    check(shallow > deep,
          f"短码顶对的 jam 倾向应高于深码：{shallow:.3f} vs {deep:.3f}")
    check(range_flow.spr_bucket(0.3) == "<0.5"
          and range_flow.spr_bucket(0.8) == "0.5-1"
          and range_flow.spr_bucket(1.5) == "1-2"
          and range_flow.spr_bucket(3.0) == "2-4"
          and range_flow.spr_bucket(9.0) == "4+", "SPR 分桶口径不对")


# ─────────────────────── §9.2 单调性 ───────────────────────

def test_bucket_multipliers_are_monotone() -> None:
    """多人 + 面对下注 → 诈唬降；封口跟注 → 跟注宽度不降。"""
    lead_hu = range_flow.group_multiplier("lead_hu", "bluff")
    lead_multi = range_flow.group_multiplier("lead_multiway", "bluff")
    donk_multi = range_flow.group_multiplier("donk_multiway", "bluff")
    check(lead_multi <= lead_hu,
          f"多人 lead 的诈唬乘数应不高于单挑：{lead_multi} vs {lead_hu}")
    check(donk_multi < lead_multi,
          f"多人 donk 的诈唬乘数应低于多人 lead：{donk_multi} vs {lead_multi}")

    check(range_flow.group_multiplier("facing_bet_and_call", "bluff")
          < range_flow.group_multiplier("facing_bet_open", "bluff"),
          "面对下注+跟注的诈唬乘数应低于面对下注")
    check(range_flow.group_multiplier("donk_multiway", "value")
          > range_flow.group_multiplier("lead_hu", "value"),
          "多人 donk 的价值乘数应高于单挑 lead")

    cls = range_flow.HandClass("medium", "none", "value")
    closing = range_flow.likelihood(
        "call", cls, facing=True, bucket="facing_bet_closing")
    opened = range_flow.likelihood(
        "call", cls, facing=True, bucket="facing_bet_open")
    check(closing >= opened,
          f"封口跟注的宽度不应低于面对下注：{closing:.3f} vs {opened:.3f}")


def test_likelihood_order_by_made_hand() -> None:
    """同一上下文：越强的成牌越敢加注，空气越倾向弃牌。"""
    buckets = ("lead_hu", "facing_bet_open", "donk_multiway")
    tiers = ["nut", "strong", "medium", "weak", "air"]
    for bucket in buckets:
        facing = bucket.startswith("facing")
        action = "raise" if facing else "bet"
        values = [
            range_flow.likelihood(
                action, range_flow.HandClass(tier, "none", "value"),
                facing=facing, bucket=bucket)
            for tier in tiers
        ]
        check(all(a >= b - 1e-9 for a, b in zip(values, values[1:])),
              f"{bucket} 的 {action} 倾向应随成牌档单调：{values}")


def test_alpha_shrinks_long_lines() -> None:
    check(abs(range_flow.effective_alpha(1.0, 3) - 1.0) < 1e-9,
          "3 个动作以内应保持给定温度")
    check(range_flow.effective_alpha(1.0, 5) < 1.0,
          "4 个以上动作应降温，避免连乘过度自信")
    check(range_flow.effective_alpha(1.0, 9) >= 0.5,
          "降温要有下限，不能把范围压成两三个 combo")


# ─────────────────────── §9.3 权益一致性 ───────────────────────

def test_flush_draw_equity_consistency() -> None:
    """纯花听：翻牌权益 == 按转牌发牌概率加权的转牌权益（±2%）。"""
    hero = "As6s"
    flop = "7s2sKd"
    # 对手固定 AA（Hero 的 A 已被自己拿走，只剩 1 张 A 能反超）：Hero 基本
    # 只有成花才能赢，换街不能把听牌从范围里筛掉。
    opp_combo = (parse_cards("AcAd")[0], parse_cards("AcAd")[1])
    opp_range = ranges.make_range("OPP", {opp_combo: 1.0})
    assert opp_range is not None

    def equity(board: str) -> float:
        hand_vs_ranges.reset_cache()
        got = hand_vs_ranges.compute(hero, board, [("a", None, opp_range)],
                                     samples=8000)
        assert got is not None, board
        return got.equity

    flop_eq = equity(flop)
    check(0.33 <= flop_eq <= 0.48,
          f"花听翻牌权益应在 33–48%（9 张花 outs + 1 张 A outs），"
          f"实际 {flop_eq:.1%}")

    dead = set(parse_cards(hero)) | set(parse_cards(flop)) | set(opp_combo)
    deck = [c for c in ranges.DECK if c not in dead]
    weighted = 0.0
    spade_turn = 0.0
    blank_eq = 0.0
    blanks = 0
    for card in deck:
        board = flop + f"{ranges.RANK_CHARS[card[0]]}{card[1]}"
        eq = equity(board)
        weighted += eq / len(deck)
        if card[1] == "s":
            spade_turn = eq
        else:
            blank_eq += eq
            blanks += 1
    blank_eq /= max(blanks, 1)
    # AA 在转牌成花后仍有 3 张河牌能反超（第三张 K 做成 KKKAA 葫芦），
    # 所以「成花 = 100%」只在对手没有葫芦 outs 时成立；这里取 ≥90%。
    check(spade_turn >= 0.90,
          f"转牌成花应接近 100%（AA 仍有葫芦 outs），实际 {spade_turn:.1%}")
    check(0.17 <= blank_eq <= 0.26,
          f"转牌空白应≈19.6–23%（9 张花 outs 单街），实际 {blank_eq:.1%}")
    check(abs(weighted - flop_eq) <= 0.02,
          f"加权回翻牌应≈翻牌权益：{weighted:.3f} vs {flop_eq:.3f}")


# ─────────────────────── §9.4 本手回归 ───────────────────────

def _trigger_snapshot(river_index: int) -> HandSnapshot:
    """20261101-d32f55ecc1（Hero Ks3c，炸弹池）的河牌决策点。

    river_index=0：UTG+1 下注 63，Hero 还没行动；
    river_index=1：Hero 跟注 63 后，HJ 加注到 110。
    """
    hero_cards = ("Ks", "3c")
    board = ("6h", "Kd", "9d", "4h", "Kh")
    opponents = (
        opp("lowL", "HJ", 110.5),
        opp("lowR", "UTG+2", 95.0, folded=True),
        opp("midL", "CO", 249.0, folded=True),
        opp("midR", "UTG+1", 63.5),
        opp("topL", "SB", 237.5, folded=True),
        opp("topR", "BB", 482.0, folded=True),
        opp("upL", "BTN", 173.0, folded=True),
        opp("upR", "UTG", 1008.5, folded=True),
    )
    flop = (act("upR", "UTG", "fold", street="flop", inferred=True),
            act("hero", "LJ", "check", street="flop", hero=True, inferred=True),
            act("lowL", "HJ", "check", street="flop", inferred=True),
            act("lowR", "UTG+2", "check", street="flop", inferred=True),
            act("midL", "CO", "check", street="flop", inferred=True),
            act("midR", "UTG+1", "check", street="flop", inferred=True),
            act("topL", "SB", "check", street="flop", inferred=True),
            act("topR", "BB", "check", street="flop", inferred=True))
    turn = (act("hero", "LJ", "bet", 30.0, street="turn", hero=True),
            act("lowL", "HJ", "call", 30.0, street="turn"),
            act("midR", "UTG+1", "call", 30.0, street="turn"),
            act("lowR", "UTG+2", "fold", street="turn", inferred=True),
            act("midL", "CO", "fold", street="turn", inferred=True),
            act("topL", "SB", "fold", street="turn", inferred=True),
            act("topR", "BB", "fold", street="turn", inferred=True))
    river = [act("midR", "UTG+1", "bet", 63.0, street="river", all_in=True)]
    if river_index == 0:
        return snapshot(
            street="river", board=board, hero=hero_cards, hero_pos="LJ",
            hero_stack=180.5, pot=198.0, pot_before_street=135.0,
            to_call=63.0, opponents=opponents,
            streets=(StreetRecord("preflop", 45.0, ()),
                     StreetRecord("flop", 45.0, flop),
                     StreetRecord("turn", 45.0, turn),
                     StreetRecord("river", 135.0, tuple(river))),
            bomb_pot=True)
    river += [act("hero", "LJ", "call", 63.0, street="river", hero=True),
              act("lowL", "HJ", "raise", 110.0, street="river", all_in=True)]
    return snapshot(
        street="river", board=board, hero=hero_cards, hero_pos="LJ",
        hero_stack=117.5, pot=371.0, pot_before_street=135.0, to_call=47.0,
        opponents=opponents,
        streets=(StreetRecord("preflop", 45.0, ()),
                 StreetRecord("flop", 45.0, flop),
                 StreetRecord("turn", 45.0, turn),
                 StreetRecord("river", 135.0, tuple(river))),
        bomb_pot=True)


class _FakeSit:
    def __init__(self, snap: HandSnapshot) -> None:
        self.snap = snap
        self.opponent_reads: tuple = ()


def test_trigger_hand_river_folds() -> None:
    """本手：UTG+1 的河牌 donk 全下把范围收窄成同花/葫芦/三条，两次都该弃。"""
    first = hand_vs_ranges.lookup(_FakeSit(_trigger_snapshot(0)))
    assert first is not None, "第一次河牌决策应算得出范围权益"
    check(first.equity < 0.10,
          f"第一次河牌对范围权益应 <10%，实际 {first.equity:.1%}")
    # 门槛 63 / (198 + 63) = 24.1%：权益必须低于门槛，结论才是 fold。
    check(first.equity < 63.0 / (198.0 + 63.0),
          f"第一次河牌权益 {first.equity:.1%} 必须低于门槛 24.1%")

    jammer = next(e for e in first.opponents if e.seat_id == "midR")
    check(jammer.bucket == "donk_multiway",
          f"UTG+1 的河牌下注应是 donk_multiway，实际 {jammer.bucket}")
    assert jammer.composition is not None
    check(jammer.composition.made_hand >= 0.90,
          f"donk 全下范围应以成牌为主，实际 {jammer.composition.made_hand:.0%}")
    check(jammer.composition.bluff <= 0.10,
          f"donk 全下范围的空气应 ≤10%，实际 {jammer.composition.bluff:.0%}")
    check(jammer.eff_width_pct < 20.0,
          f"有效宽度应是收窄后的个位数百分比，实际 {jammer.eff_width_pct:.1f}%")

    second = hand_vs_ranges.lookup(_FakeSit(_trigger_snapshot(1)))
    assert second is not None, "第二次河牌决策应算得出范围权益"
    check(second.equity < 0.05,
          f"第二次河牌对范围权益应 <5%，实际 {second.equity:.1%}")
    check(second.equity < 47.0 / (371.0 + 47.0),
          f"第二次河牌权益 {second.equity:.1%} 必须低于门槛 11.2%")
    raiser = next(e for e in second.opponents if e.seat_id == "lowL")
    check(raiser.bucket == "facing_bet_and_call",
          f"HJ 的河牌加注应是 facing_bet_and_call，实际 {raiser.bucket}")


def test_prompt_renders_line_partition_and_warning() -> None:
    """设计 §6：线路、分区、只赢诈唬警告都要进提示词。"""
    from poker_ocr.advice import build_situation, render_facts_zh

    sit = build_situation(_trigger_snapshot(1))
    text = render_facts_zh(sit)
    check("河牌donk全下进2人" in text,
          f"范围第一行要写线路（含 donk 进几人）：\n{text[-1200:]}")
    check("成牌 100%" in text or "成牌 99%" in text,
          "范围要写分区（成牌占比）")
    check("赢你的：同花" in text, "要按牌型写「赢你的」")
    check("你赢的：" in text, "要按牌型写「你赢的」")
    check("范围警告" in text, "河牌面对全下的范围警告不能丢")
    check("沿用上一街收窄后的范围" in text or "本街还没有动作" not in text,
          "事件模型下不能再说范围沿用翻前先验")


def test_comb_indexing_via_events_keeps_draws() -> None:
    """翻牌全下：combo draw 不能被成牌强度门槛杀掉（旧路径的问题）。"""
    prior = ranges.uniform_range("UNIFORM")
    draw = range_flow.hand_class("QhTh", "Ks9h2h")
    check(draw.draw == "flush",
          f"QhTh 在 K92 两红桃面应是花听，实际 {draw.draw}")
    events = (
        range_flow.ActionEvent(
            street="flop", board="Ks9h2h", seat_id="x", action="bet",
            all_in=True, facing_bet=False, players_behind=0,
            is_first_to_act=True, spr=0.8),
    )
    built = range_flow.build_range(prior, events)
    assert built is not None
    comp = range_flow.composition(built, "Ks9h2h")
    # 设计 §9.4：semi_bluff 占比 > 0，不能被强度门槛杀掉。
    check(comp.semi > 0.03,
          f"全下范围里半诈唬组必须非零，实际 {comp.semi:.0%}（听牌 "
          f"{comp.draw:.0%}）")
    combo = tuple(sorted(parse_cards("QhTh")))
    check(built.combo_weights.get(combo, 0.0) > 0,
          "花听 QhTh 必须保留在范围里，不能被强度门槛砍掉")


def test_composition_separates_top_pair_and_two_pair_plus() -> None:
    """封顶事实要能拆出「两对+」和「顶对」：它们都在「一对/成牌」里，
    不拆出来就看不出连续过牌后强牌还剩多少。"""
    board = "3dAc2cKh9s"
    prior = ranges.uniform_range("UNIFORM")
    event = range_flow.ActionEvent(
        street="turn", board="3dAc2cKh", seat_id="x", action="check",
        players_behind=2, is_first_to_act=False, spr=2.0)
    built = range_flow.build_range(prior, (event,),
                                   dead=set(parse_cards(board)))
    assert built is not None
    comp = range_flow.composition(built, board)
    check(comp.two_pair_plus > 0.03,
          f"河牌面应有两对+ 占比，实际 {comp.two_pair_plus:.3f}")
    check(comp.top_pair > 0.02,
          f"河牌面应有顶对占比，实际 {comp.top_pair:.3f}")
    check(comp.top_pair + comp.two_pair_plus <= comp.made_hand + 1e-9,
          f"顶对+两对+ 不能超过成牌占比："
          f"{comp.top_pair:.3f}+{comp.two_pair_plus:.3f} vs "
          f"{comp.made_hand:.3f}")


def test_draw_weight_follows_rule_of_4_2() -> None:
    """听牌权重按街缩放：转牌是翻牌的一半（2 张发牌 vs 4 张），河牌为 0。"""
    def event(board: str, street: str) -> range_flow.ActionEvent:
        return range_flow.ActionEvent(
            street=street, board=board, seat_id="x", action="call",
            facing_bet=True, players_behind=2, spr=3.0)

    flop = event("Ks9h2h", "flop")
    turn = event("Ks9h2h4c", "turn")
    draw = range_flow.HandClass("air", "flush", "semi_bluff")
    made = range_flow.HandClass("strong", "none", "value")

    flop_draw = range_flow.event_factor(flop, draw)
    turn_draw = range_flow.event_factor(turn, draw)
    check(flop_draw > 0 and abs(turn_draw / flop_draw - 0.5) < 1e-9,
          f"转牌听牌权重应是翻牌的一半：{turn_draw:.4f} vs {flop_draw:.4f}")
    check(range_flow.event_factor(turn, made)
          == range_flow.event_factor(flop, made),
          "成牌权重不该被街因子缩放")
    check(range_flow.draw_street_factor(3) == 1.0
          and range_flow.draw_street_factor(4) == 0.5
          and range_flow.draw_street_factor(5) == 0.0,
          "街因子应是翻牌 1.0 / 转牌 0.5 / 河牌 0")


def test_event_range_narrows_over_streets() -> None:
    """同一批事件下，范围随动作顺序单调收窄（不是每街重算）。"""
    prior = ranges.uniform_range("UNIFORM")
    flop_event = range_flow.ActionEvent(
        street="flop", board="Ks7d2c", seat_id="x", action="call",
        facing_bet=True, players_behind=2, spr=4.0)
    turn_event = range_flow.ActionEvent(
        street="turn", board="Ks7d2c4h", seat_id="x", action="call",
        facing_bet=True, players_behind=1, spr=2.0)
    one = range_flow.build_range(prior, (flop_event,))
    two = range_flow.build_range(prior, (flop_event, turn_event))
    assert one is not None and two is not None
    check(two.effective_width_pct() <= one.effective_width_pct() + 1e-9,
          f"多一个动作后有效宽度不应变宽："
          f"{one.effective_width_pct():.1f}% → "
          f"{two.effective_width_pct():.1f}%")
    # 空事件返回 None：调用方要退回旧路径，不能拿 100% 先验冒充收窄结果。
    check(range_flow.build_range(prior, ()) is None, "没有事件应返回 None")


# ─────────────────────── 兼容/接线 ───────────────────────

def test_spec_carries_events() -> None:
    """Role 解析出来的 spec 必须带事件序列（hand_vs_ranges 靠它走新路径）。"""
    field = field_ranges.resolve(_field_snapshot())
    spec = next(s for s in field.specs if s.seat_id == "midR")
    check(bool(spec.events), "OpponentRangeSpec 应带事件序列")
    check(spec.events[-1].street == "turn", "最后一条事件应是转牌跟注")
    check(isinstance(field.warnings, tuple), "warnings 应是 tuple")


def test_off_switch_falls_back_to_old_path() -> None:
    """RANGE_EVENT_MODEL=0：spec 不带事件，范围宽度回到旧口径。"""
    import os
    old = os.environ.get("RANGE_EVENT_MODEL")
    os.environ["RANGE_EVENT_MODEL"] = "0"
    try:
        check(not range_flow.enabled(), "开关应关闭事件模型")
        field = field_ranges.resolve(_field_snapshot())
        check(all(not s.events for s in field.specs),
              "关闭时不应生成事件序列")
    finally:
        if old is None:
            os.environ.pop("RANGE_EVENT_MODEL", None)
        else:
            os.environ["RANGE_EVENT_MODEL"] = old
    check(range_flow.enabled(), "默认应开启事件模型")


def main() -> int:
    tests = [
        test_bucket_of_donk_and_lead,
        test_bucket_of_facing_bet,
        test_events_players_behind_and_callers,
        test_self_lead_is_not_a_donk,
        test_jam_is_not_a_new_action,
        test_bucket_multipliers_are_monotone,
        test_likelihood_order_by_made_hand,
        test_alpha_shrinks_long_lines,
        test_flush_draw_equity_consistency,
        test_trigger_hand_river_folds,
        test_prompt_renders_line_partition_and_warning,
        test_comb_indexing_via_events_keeps_draws,
        test_draw_weight_follows_rule_of_4_2,
        test_event_range_narrows_over_streets,
        test_spec_carries_events,
        test_off_switch_falls_back_to_old_path,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print("\n❌ " + "\n❌ ".join(FAILS))
        return 1
    print("\n✅ 事件模型全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
