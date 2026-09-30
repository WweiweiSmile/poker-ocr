#!/usr/bin/env python3
"""位置推导的单元测试。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_positions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.positions import (  # noqa: E402
    POSITIONS_BY_TABLE_SIZE, assign_positions,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def test_all_sizes_all_dealers() -> None:
    """每个桌号 × 庄家在环上每个位置，都要分出一套合法且唯一的位置。"""
    for n in range(2, 10):
        ring = [f"s{i}" for i in range(n)]
        expected = sorted(POSITIONS_BY_TABLE_SIZE[n])
        for d in range(n):
            r = assign_positions(ring, f"s{d}")
            check(r.ok, f"n={n} dealer=s{d}: 应当成功，实际 {r.error}")
            vals = sorted(r.positions.values())
            check(vals == expected,
                  f"n={n} dealer=s{d}: 位置集合应={expected}，实际={vals}")

            # 庄家左边第一个应当是 SB —— 单挑是唯一例外（按钮位下小盲）
            left = ring[(d + 1) % n]
            if n > 2:
                check(r.positions[left] == "SB",
                      f"n={n} dealer=s{d}: 庄位左侧应是 SB，实际 {r.positions[left]}")
                check(r.positions[f"s{d}"] == "BTN",
                      f"n={n} dealer=s{d}: 庄家应是 BTN，实际 {r.positions[f's{d}']}")
            else:
                check(r.positions[f"s{d}"] == "SB",
                      f"n=2 dealer=s{d}: 单挑时庄家应是 SB（按钮位下小盲）")
                check(r.positions[left] == "BB", f"n=2: 非庄家应是 BB")


def test_ring_rotation_is_irrelevant() -> None:
    """环的起点是任意的 —— 同一张桌从不同座位开始写环，位置结果必须一致。"""
    ring = ["a", "b", "c", "d", "e", "f"]
    base = assign_positions(ring, "c").positions
    for shift in range(len(ring)):
        rotated = ring[shift:] + ring[:shift]
        got = assign_positions(rotated, "c").positions
        check(got == base, f"环旋转 {shift} 位后结果变了：{got} != {base}")


def test_heads_up() -> None:
    r = assign_positions(["A", "B"], "A")
    check(r.ok and r.positions == {"A": "SB", "B": "BB"},
          f"单挑位置错误：{r.positions}")


def test_errors() -> None:
    cases = [
        (["a", "b", "c"], None, "没庄位"),
        (["a", "b", "c"], "zzz", "庄位不在环上"),
        (["a"], "a", "1 人桌"),
        ([], None, "空环"),
    ]
    for ring, dealer, label in cases:
        r = assign_positions(ring, dealer)
        check(not r.ok, f"{label}: 应当失败，实际成功了 {r.positions}")


def test_matches_callback_contract() -> None:
    """和 call-back/utils/hand.go 的 positionsByTableSize 逐项对齐。

    这份表是两边共享的契约，改动要同步 —— 这里做一道防漂移的闸。
    """
    expected = {
        9: ["SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"],
        8: ["SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO", "BTN"],
        7: ["SB", "BB", "UTG", "LJ", "HJ", "CO", "BTN"],
        6: ["SB", "BB", "UTG", "HJ", "CO", "BTN"],
        5: ["SB", "BB", "UTG", "CO", "BTN"],
        4: ["SB", "BB", "UTG", "BTN"],
        3: ["SB", "BB", "BTN"],
        2: ["SB", "BB"],
    }
    check(POSITIONS_BY_TABLE_SIZE == expected,
          f"位置契约表和 call-back 不一致：\n  本地 {POSITIONS_BY_TABLE_SIZE}\n  契约 {expected}")


def main() -> int:
    tests = [
        test_all_sizes_all_dealers,
        test_ring_rotation_is_irrelevant,
        test_heads_up,
        test_errors,
        test_matches_callback_contract,
    ]
    for t in tests:
        t()
        print(f"  {'❌' if FAILS else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 位置推导全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
