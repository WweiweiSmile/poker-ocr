#!/usr/bin/env python3
"""弃牌曲线探针的价格口径测试：`tools/fold_curve_probe.py`。

    nv tests/test_fold_curve_probe.py

为什么值得单独测
────────────────
整条曲线只有一个公式：`price = 下注额 / (下注前底池 + 2×下注额)`。它错了，
所有桶都会平移，结论就反过来。三个不变量：

  · 1/2 池 → 0.25；1/3 池 → 0.20；满池 → 0.333；
  · Hero 加注之后价格换新，之前的跟注不能算到新价格上；
  · 短码 all-in 跟注的价格不同，必须剔除，不能记成「没弃」。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.fold_curve_probe import _scan  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _scan_local(pot: float, acts: list[dict]):
    return _scan("flop", pot, acts, "t", actor_key="seat_id",
                 amount_key="amount", hero_flag=True)


def test_half_pot_price() -> None:
    obs = _scan_local(100.0, [
        {"seat_id": "hero", "action": "bet", "amount": 50.0, "is_hero": True},
        {"seat_id": "a", "action": "fold"},
    ])
    check(len(obs) == 1, f"应得 1 个观测，实际 {len(obs)}")
    check(abs(obs[0].price - 0.25) < 1e-9, f"半池价格应为 0.25：{obs[0].price}")
    check(obs[0].fold == 1, "弃牌应记 1")


def test_third_and_full_pot() -> None:
    third = _scan_local(100.0, [
        {"seat_id": "hero", "action": "bet", "amount": 33.0, "is_hero": True},
        {"seat_id": "a", "action": "fold"},
    ])
    check(abs(third[0].price - 33 / 166) < 1e-6,
          f"1/3 池价格应约 0.199：{third[0].price}")
    pot = _scan_local(100.0, [
        {"seat_id": "hero", "action": "bet", "amount": 100.0, "is_hero": True},
        {"seat_id": "a", "action": "call", "amount": 100.0},
    ])
    check(abs(pot[0].price - 1 / 3) < 1e-6, f"满池价格应为 1/3：{pot[0].price}")
    check(pot[0].fold == 0, "跟注应记 0")


def test_raise_replaces_price() -> None:
    """Hero 下注 50 → 对手加到 150：之后的人面对 150，不能再算 0.25。"""
    obs = _scan_local(100.0, [
        {"seat_id": "hero", "action": "bet", "amount": 50.0, "is_hero": True},
        {"seat_id": "a", "action": "raise", "amount": 150.0},
        {"seat_id": "b", "action": "fold"},
    ])
    check(len(obs) == 1, f"对手加注后应停止归集，实际 {len(obs)} 个观测")
    check(abs(obs[0].price - 0.25) < 1e-9, f"加注者本身按 0.25 记：{obs[0].price}")


def test_short_allin_call_dropped() -> None:
    """短码 only 跟到 10（应跟 50）：价格不同，既不能记弃也不能记跟。"""
    obs = _scan_local(100.0, [
        {"seat_id": "hero", "action": "bet", "amount": 50.0, "is_hero": True},
        {"seat_id": "a", "action": "call", "amount": 10.0},
    ])
    check(obs == [], f"短码跟注应剔除，实际 {obs}")


def test_no_hero_bet_no_observation() -> None:
    obs = _scan_local(100.0, [
        {"seat_id": "a", "action": "check"},
        {"seat_id": "b", "action": "bet", "amount": 50.0},
        {"seat_id": "a", "action": "fold"},
    ])
    check(obs == [], f"不是 Hero 下注不该产生观测：{obs}")


def test_multiway_counts_each_opponent() -> None:
    obs = _scan_local(100.0, [
        {"seat_id": "hero", "action": "bet", "amount": 50.0, "is_hero": True},
        {"seat_id": "a", "action": "fold"},
        {"seat_id": "b", "action": "call", "amount": 50.0},
        {"seat_id": "c", "action": "fold"},
    ])
    check(len(obs) == 3, f"三个人应得 3 个观测，实际 {len(obs)}")
    check(sum(o.fold for o in obs) == 2, "其中两个弃牌")


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
    print("\n✅ 弃牌曲线探针全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
