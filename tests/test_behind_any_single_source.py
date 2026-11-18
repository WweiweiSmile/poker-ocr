#!/usr/bin/env python3
"""「至少一家领先我」单一实现 + 近似口径显式标注（ticket #76）。

    nv tests/test_behind_any_single_source.py

锁定四件事：

  1. 闭式 `1-(1-p)**n` 只有一处实现：`coach.at_least_one_behind`；
     提示词兜底（`situation`）与评分枚举（`coach.rank_hands`）都调它。
  2. 同一个 p / n 下，`situation.beat_at_least_pct` 与
     `rank_hands(...).beat_at_least_pct` 在 3 / 4 / 5 人池上数值相等。
  3. 复盘 facts 给近似值带 basis，不静默（`beatAtLeastBasis` /
     `handStrength.behindAnyBasis`）。
  4. 3 / 4 / 5 人池上「独立近似 vs 联合拒绝重抽」的差仍在票 #75 记录的
     容差内；联合口径用固定 seed 的 8000 样本 MC，确定性、不连库、不调模型。

票 #75 的量化和判据见 `docs/至少一家领先-双口径偏差-2026-11-19.md`。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "replay-console"))

from poker_ocr import situation  # noqa: E402
from poker_ocr.advice import build_situation  # noqa: E402
from poker_ocr.coach import (  # noqa: E402
    at_least_one_behind, parse_cards, rank_hands, read_hand,
)
from poker_ocr.hand_track import (  # noqa: E402
    HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from replay.decisions import decision_facts  # noqa: E402
from tools.behind_any_caliber_report import (  # noqa: E402
    independent_at_least, joint_at_least,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")


def snap(*, board, hero_cards, street="flop", n_opp=3, to_call=0.0,
         pot=20.0, table_size=9) -> HandSnapshot:
    """与 test_beat_rate 相同的最小快照：只有牌面 + 人数有意义。"""
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


HERO = parse_cards("7s 7d")
BOARD = parse_cards("10c Jd 2h")


def test_closed_form_has_one_home() -> None:
    """闭式是字面值，不依赖实现；半天真、半天边界。"""
    check(at_least_one_behind(0.25, 4) == 0.68359375,
          f"1-0.75**4 应为 0.68359375，实际 {at_least_one_behind(0.25, 4)!r}")
    check(at_least_one_behind(0.3712, 1) == 0.3712, "n=1 必须等于 p")
    check(at_least_one_behind(0.3712, 0) == 0.3712,
          "n<=1 视为单挑（生产里 n>=1）")
    check(at_least_one_behind(0.0, 5) == 0.0, "p=0 必然 0")
    check(at_least_one_behind(1.0, 5) == 1.0, "p=1 必然 1")
    seq = [at_least_one_behind(0.3, n) for n in (1, 2, 3, 4, 5)]
    check(all(a < b for a, b in zip(seq, seq[1:])), f"n 单调递增: {seq}")

    # 精确枚举入口（评分/ground_truth）与提示词兜底必须是同一个函数。
    check(situation.at_least_one_behind is at_least_one_behind,
          "situation 应直接引用 coach.at_least_one_behind，而不是自己再算一遍")
    src = inspect.getsource(situation)
    check("1.0 - (1.0 -" not in src,
          "situation 里仍有内联闭式：第二份实现又回来了")

    p1 = read_hand(HERO, BOARD).beat_pct
    for n in (1, 2, 3, 4, 5):
        detail = rank_hands(HERO, BOARD, n)
        check(detail.beat_at_least_pct == at_least_one_behind(p1, n),
              f"rank_hands n={n}: {detail.beat_at_least_pct!r} != "
              f"{at_least_one_behind(p1, n)!r}")


def test_situation_fallback_equals_rank_hands() -> None:
    """强制 gt 失败走兜底分支，3 / 4 / 5 人池与 rank_hands 逐位相等。"""
    for n_opp in (2, 3, 4):
        with mock.patch.object(situation, "compute_ground_truth",
                               side_effect=ValueError("forced: gt unavailable")):
            sit = build_situation(snap(board=("10c", "Jd", "2h"),
                                       hero_cards=("7s", "7d"),
                                       n_opp=n_opp))
        detail = rank_hands(HERO, BOARD, n_opp)
        check(sit.beat_pct == round(read_hand(HERO, BOARD).beat_pct * 100, 1),
              f"n={n_opp} 兜底 beat_pct 不一致：{sit.beat_pct}")
        check(sit.beat_at_least_pct == round(detail.beat_at_least_pct * 100, 1),
              f"n={n_opp} 兜底至少一家领先 {sit.beat_at_least_pct} != "
              f"{round(detail.beat_at_least_pct * 100, 1)}")


def test_replay_facts_carry_basis() -> None:
    """复盘 facts 显式写近似来源；behindAny 与数字同级带 basis。"""
    hs = SimpleNamespace(kind="value", size="big", behind_any=0.592,
                         equity=0.3777, n=5)
    fake = SimpleNamespace(
        hand_tier="强成牌", hand_label="明三条", hand_label_en="Trips",
        category="made", texture="花面", texture_en="Flush-y",
        equity_pct=51.2, beat_pct=43.7, beat_at_least_pct=56.3,
        ground_truth=None, hand_strength=hs, options=(),
        opponents_in_hand=2, table_dynamics=False, pot_per_player=15.0,
        explosive_pot=False, range_verdict=None, warnings=(),
    )
    facts = decision_facts(fake)
    check(facts["beatAtLeastBasis"] == "random_hands_independent_approx",
          f"beatAtLeastBasis: {facts.get('beatAtLeastBasis')!r}")
    check(facts["beatAtLeastPct"] == 56.3, "beatAtLeastPct 数字形状不应改")
    check(facts["handStrength"]["behindAny"] == 0.592,
          "behindAny 数值/形状不应改")
    check(facts["handStrength"]["behindAnyBasis"]
          == "joint_resample_over_ranges",
          f"behindAnyBasis: {facts['handStrength'].get('behindAnyBasis')!r}")

    # 没有牌力解析时也不能丢掉「这个百分比是近似」的说明。
    fake.hand_strength = None
    facts = decision_facts(fake)
    check(facts["handStrength"] is None, "无 hand_strength 时应为 None")
    check(facts["beatAtLeastBasis"] == "random_hands_independent_approx",
          "beatAtLeastBasis 应始终存在（旧记录 + 无 hand_strength 都靠它）")


# 票 #75 §5 / §7 记录的 3 / 4 / 5 人池最坏 |独立−联合|（pp）。
WORST_PP = {3: 1.66, 4: 2.18, 5: 2.51}
# 8000 样本的单点 MC 噪声 95% 半宽 ≈ ±1.10pp（票 #75 §3）。
NOISE_PP = 1.2
POOLS = ((2, 3), (3, 4), (4, 5))          # (对手数, 人数池)
JOINT_CASES = (("7s 7d", "10c Jd 2h"), ("Kh Js", "3h 7h 3c"))


def test_pool_gap_within_ticket_75_tolerance() -> None:
    """3/4/5 人池：独立近似与联合重抽的差仍在票 #75 的容差内。"""
    for hero_text, board_text in JOINT_CASES:
        hero, board = parse_cards(hero_text), parse_cards(board_text)
        p1 = read_hand(hero, board).beat_pct
        for n_opp, pool in POOLS:
            indep = independent_at_least(p1, n_opp)
            # 生产实现与工具里的字面闭式必须同值（两处独立代码交叉验证）。
            check(at_least_one_behind(p1, n_opp) == indep,
                  f"{hero_text} n={n_opp}: 生产闭式 {at_least_one_behind(p1, n_opp)!r}"
                  f" != 工具闭式 {indep!r}")
            joint = joint_at_least(hero, board, n_opp, samples=8000,
                                   seed=20261119)
            diff_pp = (indep - joint) * 100
            limit = WORST_PP[pool] + NOISE_PP
            check(abs(diff_pp) <= limit,
                  f"{hero_text}/{board_text} {pool} 人池 |差|={abs(diff_pp):.2f}pp"
                  f" 超过票 #75 容差 {limit:.2f}pp")
            if pool >= 4:
                # 票 #75：独立近似偏低（4 人 86/92、5 人 89/92 个点）。
                check(diff_pp < 0,
                      f"{hero_text}/{board_text} {pool} 人池方向应为独立偏低，"
                      f"实际 {diff_pp:+.2f}pp")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILS:
        for msg in FAILS:
            print(f"❌ {msg}")
        return 1
    print("✅ 至少一家领先：单一实现 + 近似口径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
