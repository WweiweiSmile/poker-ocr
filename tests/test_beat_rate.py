#!/usr/bin/env python3
"""牌型「名字 vs 排名」的回归：board_ceiling 的葫芦修正 + beat_rate + 价值指令降级。

    .venv/bin/python tests/test_beat_rate.py
    .venv/bin/python -m pytest tests/test_beat_rate.py -q

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

from poker_ocr.advice import (  # noqa: E402
    DOMINATED_BEAT_PCT, build_situation, render_hand_facts, value_directive,
)
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

def test_dominated_two_pair_is_not_a_value_hand() -> None:
    """h75 / h100 这两手必须被拦下。"""
    h75 = build_situation(snap(board=("8d", "8h", "10h"), hero_cards=("2d", "2c"),
                               n_opp=7, pot=40.0))
    d = value_directive(h75) or ""
    check("NOT A VALUE HAND BY RANK" in d, f"h75 的 8822 不该被当成价值牌：{d}")
    check("for value" in d and "Do NOT bet" in d, f"必须显式禁止价值下注：{d}")
    check(str(round(h75.beat_at_least_pct)) in d or
          f"{h75.beat_at_least_pct:g}" in d, f"指令里要带具体概率：{d}")

    h100 = build_situation(snap(board=("Qs", "Js", "2s", "6d", "6c"),
                                hero_cards=("4d", "4c"), street="river",
                                n_opp=3, pot=24.5))
    d = value_directive(h100) or ""
    check("NOT A VALUE HAND BY RANK" in d, f"h100 的 6644 不该被当成价值牌：{d}")


def test_legit_made_hands_keep_their_value_directive() -> None:
    """别误伤：花面的顺子、公对面的同花、干燥面两对仍然该收价值。"""
    straight = build_situation(snap(board=("9h", "8h", "7h", "2c"),
                                    hero_cards=("Ts", "Jd"), street="river",
                                    n_opp=5, pot=60.0))
    d = value_directive(straight) or ""
    check("NOT A VALUE HAND" not in d, f"花面的顺子仍是价值牌：{d}")
    check("VALUE CAUTION" in d, f"花面的顺子该提示不是坚果：{d}")

    flush = build_situation(snap(board=("7h", "7c", "Kh", "2h"),
                                 hero_cards=("Ah", "3h"), street="river",
                                 n_opp=3, pot=40.0))
    d = value_directive(flush) or ""
    check("NOT A VALUE HAND" not in d, f"公对面的同花仍该收价值：{d}")

    two_pair = build_situation(snap(board=("8h", "7s", "2c"),
                                    hero_cards=("8c", "7d"), n_opp=3, pot=20.0))
    d = value_directive(two_pair) or ""
    check("NOT A VALUE HAND" not in d, f"干燥面的真两对不该被降级：{d}")
    check("VALUE" in d, f"干燥面的真两对该有正向价值指令：{d}")


def test_draws_are_not_judged_by_beat_rate() -> None:
    """听牌的 beat_rate 反映的是「还没成牌」，不能拿来判「不是价值牌」。"""
    draw = build_situation(snap(board=("10s", "8s", "9s"),
                                hero_cards=("As", "Kd"), n_opp=2, pot=34.5))
    d = value_directive(draw) or ""
    check("NOT A VALUE HAND BY RANK" not in d,
          f"坚果同花听牌不该被降级（tier={draw.hand_tier}, "
          f"beat={draw.beat_at_least_pct}）：{d}")


# ─────────────────────────── 提示词渲染 ───────────────────────────

def test_non_nut_boards_downgrade_straight_and_flush() -> None:
    """花面的顺子 / 公对面的同花 / 公对面的顺子 —— 都不是坚果，只能跟不能加。

    这是用户的牌桌读法：桌上都是跟注站，能跟到底的恰恰是同花和葫芦。
    所以「随机手牌只有 4.3% 能赢我」不代表能收价值 —— 加注只会引来更强的牌。
    """
    caution = {
        "花面的顺子": dict(board=("9h", "8h", "7h", "2c"),
                       hero_cards=("Ts", "Jd"), n_opp=5),
        "公对面的同花": dict(board=("7h", "7c", "Kh", "2h"),
                        hero_cards=("Ah", "3h"), n_opp=3),
        "公对面的顺子": dict(board=("7c", "7h", "8d", "9s"),
                        hero_cards=("Ts", "Jd"), n_opp=3),
    }
    for name, kw in caution.items():
        sit = build_situation(snap(street="river", pot=40.0, **kw))
        d = value_directive(sit) or ""
        check("VALUE CAUTION" in d, f"{name} 应提示不是坚果：{d}")
        check("Do NOT raise" in d, f"{name} 必须禁止加注：{d}")

    # 对照：既没有花面也没有公对的顺子，就是价值牌
    nut = build_situation(snap(street="river", board=("9c", "8d", "7s", "2c"),
                               hero_cards=("Ts", "Jd"), n_opp=3, pot=40.0))
    d = value_directive(nut) or ""
    check("VALUE DEFAULT" in d, f"干燥面的顺子该收价值：{d}")
    check("2/3" in d or "raise-2/3" in d, f"对跟注站要用大尺度：{d}")


def test_gate_uses_per_opponent_not_field() -> None:
    """判据是**单人**比例，不是「全桌至少一家领先」——后者会被人数放大。

    h75 翻牌只剩 2 家时：单人 25.9%、全桌 45.1%。按全桌 60% 的门槛会放过去
    （实测确实被放过去了，模型又去下注），按单人 22% 才拦得住。
    反过来，顶对在 8 人池里全桌也才 27.7%，不能因为人多就把强牌降级。
    """
    # 同一手 22，两个对手：必须拦下
    small = build_situation(snap(board=("8d", "8h", "10h"), hero_cards=("2d", "2c"),
                                 n_opp=2, pot=80.0))
    d = value_directive(small) or ""
    check("NOT A VALUE HAND BY RANK" in d,
          f"8822 只剩 2 家时仍不是价值牌（单人 {small.beat_pct}%）：{d}")

    # 顶对在 8 人池：全桌比例不低，但单人只有 4%，不能降级
    tptk = build_situation(snap(board=("Jc", "7d", "2s"), hero_cards=("Ah", "Jh"),
                                n_opp=8, pot=40.0))
    d = value_directive(tptk) or ""
    check("NOT A VALUE HAND" not in d,
          f"8 人池里的顶对不该被降级（单人 {tptk.beat_pct}%，"
          f"全桌 {tptk.beat_at_least_pct}%）：{d}")


def test_prompt_renders_made_hand_rank() -> None:
    """这句必须进 prompt —— 否则模型还是只看得到「两对 = 强成牌」。"""
    text = render_hand_facts(build_situation(
        snap(board=("8d", "8h", "10h"), hero_cards=("2d", "2c"),
             n_opp=7, pot=40.0)))
    check("Made-hand rank" in text, "提示词缺 Made-hand rank 一行")
    check("LOWER bound" in text, "要说明随机手牌是下界")
    check("87.7%" in text or "87." in text, f"要带具体百分比：{text[-400:]}")


def main() -> int:
    tests = [
        test_paired_board_ceiling_is_full_house,
        test_beat_rate_separates_small_two_pair_from_big_hands,
        test_beat_rate_bounds,
        test_dominated_two_pair_is_not_a_value_hand,
        test_legit_made_hands_keep_their_value_directive,
        test_draws_are_not_judged_by_beat_rate,
        test_non_nut_boards_downgrade_straight_and_flush,
        test_gate_uses_per_opponent_not_field,
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
