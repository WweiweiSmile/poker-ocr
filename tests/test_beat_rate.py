#!/usr/bin/env python3
"""牌型「名字 vs 排名」的回归：board_ceiling 的葫芦修正 + beat_rate + 价值指令降级。

    nv tests/test_beat_rate.py
    nv -m pytest tests/test_beat_rate.py -q

背景（2026-10-23 那局的复盘）：h75 拿 22 在 88T 上、h100 拿 44 在 QJ266 上，
两次都被当成「我中了两对 → 收价值」，实际上 8822 / 6644 是最差的两对，
枚举下来 7 家 87.7%、3 家 79.4% 的情况已经被领先。输的 189 里有 54.5 来自这两手。

根因有两层：
  1. `board_ceiling` 对「板面成一对」返回 TRIPS，但对手拿「对子 rank 一张 +
     另一个板面 rank 一张」就是葫芦（7,7,8 板上 78 = 77788）。于是
     「公对面的同花/顺子」永远标不成被压制（同花 > 三条）。
  2. 光有布尔值不够：「花面的顺子」只有 19.9% 被领先，仍然该收价值；
     「8822」是 87.7%。两者差一个数量级，必须给出**概率**而不是「可能」。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import build_situation, render_facts_zh  # noqa: E402
from poker_ocr.coach import (  # noqa: E402
    FLUSH, FULL_HOUSE, ONE_PAIR, STRAIGHT, beat_rate, board_ceiling, parse_cards,
)
from poker_ocr.hand_track import (  # noqa: E402
    HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")


def snap(*, board, hero_cards, street="flop", n_opp=3, to_call=0.0,
         pot=20.0, table_size=9) -> HandSnapshot:
    """造一个只有「牌面 + 人数」有意义的最小快照。"""
    opps = tuple(OpponentSnapshot(f"o{i}", None, None, 100.0, 0.0, False)
                 for i in range(n_opp))
    return HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=tuple(board),
        hero_cards=tuple(hero_cards), hero_position="BB", hero_stack=100.0,
        hero_bet=0.0, hero_invested=2.0, to_call=to_call, second_bet=None,
        pot=pot, pot_before_street=pot, table_size=table_size, blinds=BLINDS,
        opponents=opps,
        streets=(StreetRecord("preflop", 0.0, ()),
                 StreetRecord(street, pot, ())),
        preflop_shapes=(), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h1", street, 0, pot, to_call, tuple(hero_cards)),
        players_dealt=n_opp + 1,
    )


# ─────────────────────────── board_ceiling ───────────────────────────

def test_paired_board_ceiling_is_full_house() -> None:
    """板面成**一对**就是葫芦，不是三条 —— 这是「公对面的同花/顺子」漏判的根因。"""
    cases = [
        (("7c", "7h", "8d", "9s"), FULL_HOUSE, "一对 + 两个 rank（对手 78 就是 77788）"),
        (("7h", "7c", "Kh", "2h"), FULL_HOUSE, "一对 + 同花面（对手 7K 就是 777KK）"),
        (("8d", "8h", "10h"), FULL_HOUSE, "h75 的 88T"),
    ]
    for board, want, why in cases:
        got = board_ceiling(parse_cards("".join(board)))
        check(got == want, f"{why}：board_ceiling({board}) 应为 {want}，实际 {got}")

    # 反例：不能把所有牌面都抬成葫芦，否则等于没判
    check(board_ceiling(parse_cards("2c7d9s")) == ONE_PAIR,
          "无对子干燥面应是一对")
    check(board_ceiling(parse_cards("9c8d7s2c")) == STRAIGHT,
          "三连无对子面应是顺子")
    check(board_ceiling(parse_cards("2c4c2d5s6c")) >= FULL_HOUSE,
          "成对 + 同花面至少是葫芦")


# ─────────────────────────── beat_rate ───────────────────────────

def test_beat_rate_separates_small_two_pair_from_big_hands() -> None:
    """8822 与「花面的顺子」差一个数量级，必须能分开。"""
    per_small, at_small = beat_rate(parse_cards("2d2c"), parse_cards("8d8h10h"), 7)
    per_straight, at_straight = beat_rate(parse_cards("TsJd"),
                                          parse_cards("9h8h7h2c"), 5)

    check(0.20 < per_small < 0.30, f"8822@88T 单个对手应约 25.9%，实际 {per_small:.1%}")
    check(at_small > 0.80, f"8822@88T 7 家应 >80% 已被领先，实际 {at_small:.1%}")
    check(per_straight < 0.08, f"花面的顺子单个对手应约 4.3%，实际 {per_straight:.1%}")
    check(at_straight < 0.30, f"花面的顺子 5 家应 <30%，实际 {at_straight:.1%}")
    check(at_small > at_straight * 2.5,
          f"小两对必须明显比花面的顺子危险：{at_small:.1%} vs {at_straight:.1%}")


def test_beat_rate_bounds() -> None:
    """0..1、单调、人多更危险。"""
    _, one = beat_rate(parse_cards("2d2c"), parse_cards("8d8h10h"), 1)
    _, seven = beat_rate(parse_cards("2d2c"), parse_cards("8d8h10h"), 7)
    check(0.0 <= one <= seven <= 1.0, f"人数越多越危险：{one:.3f} → {seven:.3f}")

    # 坚果同花不可能被「随机两手牌」打败（同花顺也打不过它）
    per_nut, _ = beat_rate(parse_cards("AhKh"),
                           parse_cards("Qh9h2h3c4c"), 3)
    check(per_nut < 0.02, f"坚果同花几乎不会被随机牌打败，实际 {per_nut:.2%}")


# ─────────────────────────── value_directive ───────────────────────────

def test_prompt_renders_made_hand_rank() -> None:
    """中文事实要带「当前成牌落后至少一家」的具体比例。"""
    sit = build_situation(snap(board=("8d", "8h", "10h"),
                               hero_cards=("2d", "2c"),
                               n_opp=7, pot=40.0))
    text = render_facts_zh(sit)
    check("## 实时牌力" in text, "提示词缺实时牌力")
    check("当前牌型：两对" in text, f"应标两对：{text[-400:]}")
    check(sit.beat_at_least_pct is not None
          and f"{sit.beat_at_least_pct:.1f}%" in text,
          f"要带具体百分比：{text[-400:]}")


def main() -> int:
    tests = [
        test_paired_board_ceiling_is_full_house,
        test_beat_rate_separates_small_two_pair_from_big_hands,
        test_beat_rate_bounds,
        test_prompt_renders_made_hand_rank,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 牌型排名相关用例全部通过")
    return 0


def test_zz_all_checks_passed() -> None:
    """pytest 兜底：本文件的 check() 只累积不抛异常，没有这条时 pytest 会把失败当通过。"""
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    sys.exit(main())
