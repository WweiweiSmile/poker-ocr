#!/usr/bin/env python3
"""多人池权益收进**全部**在局对手（ticket #115，父规格 #110）。

    nv tests/test_equity_all_opponents.py

锁定五件事：

  1. `field_ranges.resolve` 不再截断到前 4 家：6 / 9 个在局对手全部生成
     spec，已弃牌对手仍然不出现；
  2. 有范围证据的对手展开逐家范围，没证据的按随机牌补位，补位人数等于
     实际缺证人数，且 `len(opponents) + random_opponents == 在局人数`；
  3. 补位人数写进一条中文事实 warning（「N 个对手按随机牌处理」），并且
     在 4 行数据质量预算下不被英文 range-unknown 缺口挤掉；
  4. 所有对手在同一次 `_monte_carlo` 里整组拒绝重抽（`behind_any` 是联合
     值，不是逐家独立近似再拼接）；
  5. ≤4 家逐位不变（数值用 60b2798 的实测字面值锁定），4-bet+ 与 <40BB
     两条既有警告原文保持逐字不变。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import hand_vs_ranges, ranges  # noqa: E402
from poker_ocr.advice import AISettings, advise  # noqa: E402
from poker_ocr.field_ranges import resolve  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.prompt.user import build_messages  # noqa: E402
from poker_ocr.situation import build_situation  # noqa: E402

FAILS: list[str] = []

BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")
SETTINGS = AISettings(api_key="k", base_url="https://example.invalid", model="m")
# 9 人桌的全部位置名；按顺序发给对手，保证「几乎每家都能推出先验」。
POSITIONS = ("BB", "SB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN")


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def act(seat: str, pos: str | None, action: str, amount: float | None = None,
        *, street: str = "preflop", hero: bool = False,
        all_in: bool = False) -> ActionRecord:
    return ActionRecord(street=street, seat_id=seat, name=seat, position=pos,
                        action=action, amount=amount, all_in=all_in,
                        is_hero=hero)


def opp(seat: str, pos: str | None = None, *, folded: bool = False,
        stack: float | None = 97.0) -> OpponentSnapshot:
    return OpponentSnapshot(seat, seat, pos, stack, 0.0, folded)


def snap(*, opponents, hero_cards=("Ah", "Qh"), board=("As", "Ks", "4d"),
         street="flop", hero_pos="BTN", hero_stack=97.0, pot=20.0,
         preflop=(), postflop=(), shapes=("limped",), hero_is_pfr=False,
         table_size=9, warnings=()) -> HandSnapshot:
    """最小可用的翻后快照（与 test_field_ranges 同构）。"""
    return HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=tuple(board),
        hero_cards=tuple(hero_cards), hero_position=hero_pos,
        hero_stack=hero_stack, hero_bet=0.0, hero_invested=2.0, to_call=0.0,
        second_bet=None, pot=pot, pot_before_street=pot,
        table_size=table_size, blinds=BLINDS,
        opponents=tuple(opponents),
        streets=(StreetRecord("preflop", 0.0, tuple(preflop)),
                 StreetRecord(street, pot, tuple(postflop))),
        preflop_shapes=shapes, hero_is_pfr=hero_is_pfr, cbet_spot=False,
        decision_key=("h1", street, 0, (), tuple(hero_cards)),
        warnings=tuple(warnings), players_dealt=len(opponents) + 1,
    )


def make_range(rid: str, keys) -> ranges.Range:
    combos = []
    for key in keys:
        combos.extend(ranges.combos_of_key(key))
    return ranges.make_range(rid, {c: 1.0 for c in combos})


def test_resolve_models_every_live_opponent() -> None:
    """6 / 9 个在局对手全部进 specs；弃牌过滤保留，截断与英文 cap 告警删除。"""
    for n in (6, 9):
        opponents = [opp(f"o{i}", POSITIONS[i]) for i in range(n)]
        opponents.append(opp("folded1", "UTG", folded=True))
        field = resolve(snap(opponents=opponents))
        seats = [spec.seat_id for spec in field.specs]
        check(len(field.specs) == n,
              f"{n} 个在局对手应全部建模，实际 {len(field.specs)} 个")
        check("folded1" not in seats, "已弃牌对手不应出现")
        check(not any("only the first" in w for w in field.warnings),
              f"截断告警应删除：{field.warnings}")
        check(all(spec.prior_id for spec in field.specs),
              f"这些座位都有位置证据，应都能推出先验："
              f"{[(s.seat_id, s.prior_id) for s in field.specs]}")


def test_random_fill_warning_states_the_count() -> None:
    """补位人数写成一条中文事实 warning，且 warning 不参与抽样种子。"""
    rA = make_range("rA", ("KK", "44", "AKs", "AKo", "KJo"))
    hero, board = ("Ah", "Qh"), ("As", "Ks", "4d")
    filled = hand_vs_ranges.compute(hero, board, [("a", None, rA)],
                                    random_opponents=2, seed_key="h1")
    check(filled is not None, "1 家有范围 + 2 家补位应能算出结果")
    if filled is None:
        return
    check("2 个对手按随机牌处理" in filled.warnings,
          f"应写出补位人数：{list(filled.warnings)}")
    check(sum(1 for w in filled.warnings if "按随机牌处理" in w) == 1,
          "补位 warning 只应有一条")
    check(filled.random_opponents == 2 and len(filled.opponents) == 1,
          f"2 家补位 + 1 家有范围，实际 {len(filled.opponents)}"
          f"+{filled.random_opponents}")

    exact = hand_vs_ranges.compute(hero, board, [("a", None, rA)], seed_key="h1")
    check(exact is not None and not any("按随机牌处理" in w
                                       for w in exact.warnings),
          "没有补位时不应出现这条 warning")

    noisy = hand_vs_ranges.compute(hero, board, [("a", None, rA)],
                                   random_opponents=2, seed_key="h1",
                                   warnings=("caller gap",))
    check(noisy is not None and noisy.equity == filled.equity
          and noisy.behind_any == filled.behind_any,
          "warning 不参与 _seed：数值必须与无额外 warning 时一致")


def test_eight_opponent_random_fill_survives_prompt_budget() -> None:
    """8 家局面：补位事实进 sit.warnings、prompt 与 advice facts，不被 4 行预算挤掉。"""
    opponents = [opp("o0", "BB"), opp("o1", "CO")]
    opponents += [opp(f"o{i}", None, stack=None) for i in range(2, 8)]
    sit = build_situation(snap(
        opponents=opponents,
        warnings=("hero opening stack unread, pot-commitment ratio unknown",
                  "ante 按桌配置假设：按桌配置 0.5BB"),
    ))
    line = "6 个对手按随机牌处理"
    check(line in sit.warnings, f"sit.warnings 缺补位事实：{sit.warnings}")
    check(sit.hand_ranges is not None
          and sit.hand_ranges.random_opponents == 6,
          "8 家：2 家有范围证据 → 6 家补位")

    _system, user = build_messages(sit)
    check("数据质量" in user, "user 段应有数据质量节")
    check(line in user, f"补位 warning 被其他缺口挤掉了：{user}")

    adv = advise(sit, SETTINGS,
                 complete=lambda *a, **k: {"option": "call", "reason": "x"})
    facts = adv.to_dict().get("facts") or {}
    check(line in (facts.get("warnings") or []),
          f"advice facts 缺补位 warning：{facts.get('warnings')}")
    check((facts.get("hand_ranges") or {}).get("random_opponents") == 6,
          f"advice facts 的 hand_ranges 补位人数不对："
          f"{facts.get('hand_ranges')}")


def test_decision_context_counts_match_active_opponents() -> None:
    """决策上下文 seam：逐家范围 + 随机补位 = 全部在局对手数。"""
    for n, with_pos in ((6, 6), (9, 6)):
        opponents = [opp(f"o{i}", POSITIONS[i] if i < with_pos else None)
                     for i in range(n)]
        sit = build_situation(snap(opponents=opponents))
        hr = sit.hand_ranges
        check(hr is not None, f"{n} 家应有范围权益块")
        if hr is None:
            continue
        check(len(hr.opponents) == with_pos,
              f"{n} 家：有证据 {with_pos} 家应全部逐家建模，"
              f"实际 {len(hr.opponents)}")
        check(hr.random_opponents == n - with_pos,
              f"{n} 家：补位人数应等于缺证数 {n - with_pos}，"
              f"实际 {hr.random_opponents}")
        check(len(hr.opponents) + hr.random_opponents == n,
              f"{n} 家：逐家 + 补位 ≠ 在局人数")
        if n - with_pos:
            check(f"{n - with_pos} 个对手按随机牌处理" in sit.warnings,
                  f"{n} 家：sit.warnings 缺补位人数")
        else:
            check(not any("按随机牌处理" in w for w in sit.warnings),
                  "全员有证据时不应出现补位 warning")


def test_nine_opponents_joint_draw_not_independent_product() -> None:
    """9 家在局：权益来自一次联合抽样，不是逐家独立近似再拼接。

    `behind_k` 仍是 `_exact_edge` 的单挑精确枚举（改它就会破坏 ≤4 家逐位
    一致），这里只要求 behind_any 严格大于「把各家 behind_k 当独立事件
    相乘」的朴素预测 —— 联合抽样里任一家冲突都整组重抽，重叠的强牌会
    合并，朴素乘积会系统性低估。
    """
    hero, board = ("Ah", "Qh"), ("As", "Ks", "4d")
    rA = make_range("rA", ("KK", "44", "AKs", "AKo", "KJo"))
    rB = make_range("rB", ("QQ", "JJ", "KQs", "KJs", "QJs"))
    nine = hand_vs_ranges.compute(hero, board, [("a", "BB", rA),
                                                ("b", "CO", rB)],
                                  random_opponents=7, seed_key="h1")
    check(nine is not None, "9 家在局应能算出联合权益")
    if nine is None:
        return
    behinds = [e.behind for e in nine.opponents]
    check(len(nine.opponents) == 2 and nine.random_opponents == 7,
          f"9 家：2 家有范围 + 7 家补位，实际 {len(nine.opponents)}"
          f"+{nine.random_opponents}")
    check(len(behinds) + nine.random_opponents == 9,
          "逐家 + 补位必须等于在局人数")
    naive = 1.0
    for p in behinds:
        naive *= 1.0 - p
    naive = 1.0 - naive
    check(nine.behind_any > naive,
          f"behind_any={nine.behind_any} 应大于独立拼接预测 {naive}"
          "（联合抽样里重叠强牌被合并）")
    check(nine.behind_any > max(behinds),
          f"behind_any={nine.behind_any} 应严格大于任何单家 behind_i"
          f"={max(behinds)}")
    check(nine.samples == 1500, f"9 家样本数应被压到 1500，实际 {nine.samples}")


def test_four_or_fewer_opponents_bit_identical() -> None:
    """≤4 家逐位不变：数值字面值来自 60b2798（删截断前）的实测。

    范围 id 用 A/B 是为了复现实测脚本的 seed：`_seed` 文本里含 range_id。
    """
    hero, board = ("Ah", "Qh"), ("As", "Ks", "4d")
    rA = make_range("A", ("KK", "44", "AKs", "AKo", "KJo"))
    rB = make_range("B", ("QQ", "JJ", "KQs", "KJs", "QJs"))

    two = hand_vs_ranges.compute(hero, board, [("a", None, rA),
                                               ("b", None, rB)],
                                 seed_key="h1")
    check(two is not None, "2 家应能算出")
    if two is not None:
        check(two.equity == 0.331, f"2 家 equity 变了：{two.equity}")
        check(two.behind_any == 0.565, f"2 家 behind_any 变了：{two.behind_any}")
        check(two.tie_any == 0.0, f"2 家 tie_any 变了：{two.tie_any}")
        check(two.samples == 2000, f"2 家样本数变了：{two.samples}")
        check(two.seed == 1247241916, f"2 家 seed 变了：{two.seed}")
        check([e.behind for e in two.opponents]
              == [0.5714285714285714, 0.0],
              f"2 家 behind_i 变了：{[e.behind for e in two.opponents]}")

    three = hand_vs_ranges.compute(hero, board, [("a", None, rA),
                                                 ("b", None, rB)],
                                   random_opponents=1, seed_key="h1")
    check(three is not None, "3 家（1 家补位）应能算出")
    if three is not None:
        check(three.equity == 0.2696666666666666,
              f"3 家 equity 变了：{three.equity}")
        check(three.behind_any == 0.6013333333333334,
              f"3 家 behind_any 变了：{three.behind_any}")
        check(three.samples == 1500, f"3 家样本数变了：{three.samples}")

    four = hand_vs_ranges.compute(hero, board, [], random_opponents=4,
                                  seed_key="h1")
    check(four is not None, "4 家全随机应能算出")
    if four is not None:
        check(four.equity == 0.541, f"4 家全随机 equity 变了：{four.equity}")
        check(four.behind_any == 0.096,
              f"4 家全随机 behind_any 变了：{four.behind_any}")
        check(four.tie_any == 0.024666666666666667,
              f"4 家全随机 tie_any 变了：{four.tie_any}")
        check(four.samples == 1500, f"4 家全随机样本数变了：{four.samples}")


def test_existing_warnings_verbatim() -> None:
    """4-bet+ 与 <40BB 两条既有警告逐字保留。"""
    raises = tuple(act("o0", "UTG", "raise", 3.0) for _ in range(3))
    field = resolve(snap(opponents=[opp("o0", "UTG")],
                         preflop=raises, shapes=("three-bet",)))
    check("4-bet+ pot: no range table for this spot (range unknown)"
          in field.warnings, f"4-bet+ 警告原文被改了：{field.warnings}")

    short = resolve(snap(opponents=[opp("o0", "BB", stack=30.0)]))
    check("short stack (<40BB): preflop ranges are approximate"
          in short.warnings, f"<40BB 警告原文被改了：{short.warnings}")


def main() -> int:
    tests = [
        test_resolve_models_every_live_opponent,
        test_random_fill_warning_states_the_count,
        test_eight_opponent_random_fill_survives_prompt_budget,
        test_decision_context_counts_match_active_opponents,
        test_nine_opponents_joint_draw_not_independent_product,
        test_four_or_fewer_opponents_bit_identical,
        test_existing_warnings_verbatim,
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
    print("\n✅ 全员权益全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
