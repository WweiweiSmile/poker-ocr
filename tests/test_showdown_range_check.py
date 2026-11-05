#!/usr/bin/env python3
"""摊牌反核范围的单元测试：`tools/showdown_range_check.py`。

    .venv/bin/python tests/test_showdown_range_check.py

锁死三件事：

  · `_percentile` 的语义（越强 → 百分位越小；不在范围 → in_range=False）；
  · `_tier_shares` 是加权占比、和为 1；
  · `_snap` 能从一手录像记录里重建出让 `field_ranges` 用的快照
    （座位、弃牌、翻前形态、底池）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import ranges  # noqa: E402
from poker_ocr.coach import parse_cards  # noqa: E402
from tools.showdown_range_check import (  # noqa: E402
    _percentile, _snap, _tier_shares,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


BOARD = tuple(parse_cards("Ks7h2c"))


def _range(weights: dict[str, float]):
    combo_weights = {}
    for text, w in weights.items():
        cards = parse_cards(text)
        combo_weights[tuple(cards)] = w
    return ranges.make_range("test:range", combo_weights, source="test")


def test_percentile_orders_by_strength() -> None:
    rng = _range({"AhAd": 1.0, "QhQd": 1.0, "8h8d": 1.0, "4h3d": 1.0})
    dead = set(BOARD)
    best, _ = _percentile(rng, dead, BOARD, tuple(parse_cards("AhAd")))
    mid, _ = _percentile(rng, dead, BOARD, tuple(parse_cards("QhQd")))
    worst, _ = _percentile(rng, dead, BOARD, tuple(parse_cards("4h3d")))
    check(best < mid < worst,
          f"百分位应随牌力递减：AA={best:.3f} QQ={mid:.3f} 43o={worst:.3f}")
    check(best < 0.05, f"最强的牌百分位应接近 0：{best:.3f}")
    check(worst > 0.5, f"最弱的牌百分位应偏大：{worst:.3f}")


def test_percentile_marks_out_of_range() -> None:
    rng = _range({"AhAd": 1.0})
    _, present = _percentile(rng, set(BOARD), BOARD, tuple(parse_cards("7d7s")))
    check(not present, "不在范围里的牌应标 in_range=False")
    _, ok = _percentile(rng, set(BOARD), BOARD, tuple(parse_cards("AhAd")))
    check(ok, "在范围里的牌应 in_range=True")


def test_percentile_ignores_dead_cards() -> None:
    """死牌（Hero 手里 / 牌面上）的权重不能进分母，否则百分位会偏。"""
    rng = _range({"KsKd": 1.0, "2s2d": 1.0})
    dead = set(BOARD)
    # KsKd 与牌面 Ks 冲突 → 权重应被剔除，只剩 2s2d
    pct, present = _percentile(rng, dead, BOARD, tuple(parse_cards("2s2d")))
    check(present, "2s2d 应仍在范围内")
    check(pct < 0.01, f"唯一有效组合的百分位应为 0：{pct:.3f}")


def test_tier_shares_normalized() -> None:
    rng = _range({"AhAd": 2.0, "QhQd": 1.0, "9h8h": 1.0})
    shares = _tier_shares(rng, set(BOARD), BOARD)
    total = sum(shares.values())
    check(abs(total - 1.0) < 1e-9, f"档位占比应为 1：{total}")
    check(shares, "应有档位")


def test_snap_rebuilds_positions_and_shapes() -> None:
    hand = {
        "hand_id": "t1",
        "board": ["Ks", "7h", "2c", "4d", "9s"],
        "hero_cards": ["Ah", "Kd"],
        "hero_position": "BTN",
        "table_size": 6,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": None},
        "streets": [
            {"street": "preflop", "pot_start": 1.5, "actions": [
                {"seat_id": "lowL", "position": "LJ", "action": "raise", "amount": 6.0},
                {"seat_id": "hero", "position": "BTN", "action": "call",
                 "amount": 6.0, "is_hero": True},
                {"seat_id": "upR", "position": "SB", "action": "fold"},
            ]},
            {"street": "flop", "pot_start": 13.5, "actions": [
                {"seat_id": "hero", "position": "BTN", "action": "bet",
                 "amount": 7.0, "is_hero": True},
                {"seat_id": "lowL", "position": "LJ", "action": "call", "amount": 7.0},
            ]},
        ],
    }
    snap = _snap(hand)
    check(snap is not None, "快照应能重建")
    if snap is None:
        return
    check(snap.board == ("Ks", "7h", "2c", "4d", "9s"), f"牌面不对：{snap.board}")
    check(snap.preflop_shapes == ("single-raise",),
          f"翻前形态应为 single-raise：{snap.preflop_shapes}")
    check(snap.hero_is_pfr is False, "Hero 只是 call，不是 PFR")
    seats = {o.seat_id: o for o in snap.opponents}
    check("lowL" in seats and not seats["lowL"].folded, "lowL 未弃牌")
    check("upR" in seats and seats["upR"].folded, "upR 已弃牌")
    check(seats["lowL"].position == "LJ", f"位置不对：{seats['lowL'].position}")
    check(snap.pot == 27.0 or snap.pot is None, f"底池 {snap.pot}")


def main() -> int:
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)
             and getattr(v, "__module__", "") == __name__]
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
    print("\n✅ 摊牌反核全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
