#!/usr/bin/env python3
"""角色解析：快照 → 每个在局对手的翻前先验 + 当前街 mode。

    .venv/bin/python tests/test_field_ranges.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.field_ranges import resolve  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

FAILS: list[str] = []
BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def act(seat: str, pos: str | None, action: str, amount: float | None = None,
        *, street: str = "preflop", hero: bool = False, all_in: bool = False):
    return ActionRecord(street=street, seat_id=seat, name=seat, position=pos,
                        action=action, amount=amount, all_in=all_in, is_hero=hero)


def snap(*, hero_pos="BTN", hero_cards=("Ah", "Qh"), board=("As", "Ks", "4d"),
         opponents, preflop=(), flop=(), shapes=("single-raise",),
         hero_is_pfr=True, street="flop", bomb_pot=False) -> HandSnapshot:
    opps = tuple(opponents)
    return HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=tuple(board),
        hero_cards=tuple(hero_cards), hero_position=hero_pos, hero_stack=97.0,
        hero_bet=0.0, hero_invested=6.0, to_call=0.0, second_bet=None,
        pot=20.0, pot_before_street=20.0, table_size=9, blinds=BLINDS,
        opponents=opps,
        streets=(StreetRecord("preflop", 0.0, tuple(preflop)),
                 StreetRecord(street, 20.0, tuple(flop))),
        preflop_shapes=shapes, hero_is_pfr=hero_is_pfr, cbet_spot=False,
        decision_key=("h1", street, 0, (), tuple(hero_cards)),
        bomb_pot=bomb_pot, players_dealt=len(opps) + 1,
    )


def opp(seat: str, pos: str | None, *, folded: bool = False,
        bet: float = 0.0) -> OpponentSnapshot:
    return OpponentSnapshot(seat, seat, pos, 97.0, bet, folded)


def by_seat(field):
    return {s.seat_id: s for s in field.specs}


def test_single_raise_roles_and_position() -> None:
    # hero UTG 开池，CO 冷跟（IP），BB 防守
    field = resolve(snap(
        hero_pos="UTG", hero_is_pfr=True, preflop=(
            act("hero", "UTG", "raise", 6.0, hero=True),
            act("co", "CO", "call", 6.0),
            act("bb", "BB", "call", 6.0),
        ), opponents=(opp("bb", "BB"), opp("co", "CO"))))
    m = by_seat(field)
    check(m["bb"].prior_id == "BB_DEF_vs_UTG",
          f"BB 对 UTG 应用 BB_DEF_vs_UTG，实际 {m['bb'].prior_id}")
    check(m["co"].prior_id == "COLD_CALL_IP_vs_EP",
          f"CO 在后位冷跟应用 IP，实际 {m['co'].prior_id}")
    check(m["bb"].role == "bb_def" and m["co"].role == "cold_call", "角色不对")
    check(all(s.mode == "check" for s in m.values()), "无人行动时 mode 应为 check")


def test_three_bet_roles() -> None:
    field = resolve(snap(
        hero_pos="BTN", hero_is_pfr=False, shapes=("three-bet",), preflop=(
            act("hero", "BTN", "raise", 6.0, hero=True),
            act("co", "CO", "raise", 18.0),
            act("bb", "BB", "call", 18.0),
            act("hero", "BTN", "call", 18.0, hero=True),
        ), flop=(act("co", "CO", "bet", 20.0, street="flop"),),
        opponents=(opp("co", "CO"), opp("bb", "BB"))))
    m = by_seat(field)
    check(m["co"].prior_id == "3BET_vs_LP",
          f"3-bettor 应用 3BET_vs_LP，实际 {m['co'].prior_id}")
    check(m["bb"].prior_id == "3BET_CALL_vs_LP",
          f"跟 3-bet 的 BB 应用 3BET_CALL，实际 {m['bb'].prior_id}")
    check(m["co"].mode == "bet", f"CO 下注 mode 应为 bet，实际 {m['co'].mode}")
    check(m["bb"].mode == "check", f"BB 未行动 mode 应为 check，实际 {m['bb'].mode}")


def test_limped_and_bomb_pots() -> None:
    limped = resolve(snap(
        hero_pos="SB", hero_is_pfr=False, shapes=("limped",), preflop=(
            act("co", "CO", "call", 1.0),
            act("bb", "BB", "call", 1.0),
        ), opponents=(opp("bb", "BB"), opp("co", "CO"))))
    m = by_seat(limped)
    check(m["bb"].prior_id == "BB_CHECK", f"溜入池 BB 应 BB_CHECK，实际 {m['bb'].prior_id}")
    check(m["co"].prior_id == "LIMP_LP", f"CO 溜入应 LIMP_LP，实际 {m['co'].prior_id}")

    bomb = resolve(snap(
        hero_pos="BTN", hero_is_pfr=False, shapes=(),
        opponents=(opp("bb", "BB"), opp("co", "CO")), bomb_pot=True))
    check(all(s.prior_id == "BOMB" for s in bomb.specs), "炸弹池全员 BOMB")
    check(all(s.role == "bomb" for s in bomb.specs), "炸弹池角色不对")


def test_modes_from_current_street() -> None:
    field = resolve(snap(
        hero_pos="BTN", opponents=(opp("a", "BB"), opp("b", "CO"), opp("c", "HJ")),
        preflop=(act("hero", "BTN", "raise", 6.0, hero=True),),
        flop=(act("a", "BB", "check", street="flop"),
              act("b", "CO", "call", 10.0, street="flop"),
              act("c", "HJ", "raise", 30.0, street="flop"))))
    m = by_seat(field)
    check(m["a"].mode == "check", f"过牌 mode，实际 {m['a'].mode}")
    check(m["b"].mode == "call", f"跟注 mode，实际 {m['b'].mode}")
    check(m["c"].mode == "raise", f"加注 mode，实际 {m['c'].mode}")


def test_all_in_mode() -> None:
    field = resolve(snap(
        hero_pos="BTN", opponents=(opp("a", "BB"),),
        preflop=(act("hero", "BTN", "raise", 6.0, hero=True),),
        flop=(act("a", "BB", "raise", 90.0, street="flop", all_in=True),)))
    check(field.specs[0].mode == "allin",
          f"全下徽章应映射 allin mode，实际 {field.specs[0].mode}")


def test_missing_and_contradictory_data_fail_open() -> None:
    missing = resolve(snap(
        hero_pos="BTN", opponents=(opp("x", None),),
        preflop=(act("hero", "BTN", "raise", 6.0, hero=True),)))
    check(missing.specs[0].prior_id is None, "位置缺失应 range unknown")
    check(any("range unknown" in w for w in missing.warnings), "位置缺失应有 warning")

    bad = resolve(snap(
        hero_pos="BTN", hero_is_pfr=False, shapes=("three-bet",),
        preflop=(act("x", "BB", "raise", 9.0),), opponents=(opp("x", "BB"),)))
    check(bad.specs[0].prior_id is None, "形状/动作矛盾时不能猜范围")
    check(any("contradictory" in w or "three-bet" in w for w in bad.warnings),
          f"矛盾应有 warning：{bad.warnings}")

    unread = resolve(snap(
        hero_pos="BTN", opponents=(opp("x", "BB", bet=8.0),),
        preflop=(act("hero", "BTN", "raise", 6.0, hero=True),)))
    check(unread.specs[0].mode == "unknown",
          f"有下注额没动作徽章应 unknown，实际 {unread.specs[0].mode}")


def test_folded_and_opponent_cap() -> None:
    field = resolve(snap(
        hero_pos="BTN",
        opponents=(opp("a", "BB", folded=True), opp("b", "CO"),
                   opp("c", "HJ"), opp("d", "LJ"), opp("e", "UTG+1"),
                   opp("f", "UTG")),
        preflop=(act("hero", "BTN", "raise", 6.0, hero=True),)))
    seats = [s.seat_id for s in field.specs]
    check("a" not in seats, "已弃牌对手不应出现")
    check(len(field.specs) == 4, f"5+ 对手应只算前 4 个，实际 {len(field.specs)}")
    check(any("only the first 4" in w for w in field.warnings), "应标注截断")


def main() -> int:
    tests = [
        test_single_raise_roles_and_position,
        test_three_bet_roles,
        test_limped_and_bomb_pots,
        test_modes_from_current_street,
        test_all_in_mode,
        test_missing_and_contradictory_data_fail_open,
        test_folded_and_opponent_cap,
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
    print("\n✅ 角色解析全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
