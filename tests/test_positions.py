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


def test_dead_button_dealer_left() -> None:
    """庄家退出 / 暂离（按钮留在空座）：按完整座位环排位置，不报错。

    9 人桌 dealer=s0，s0 中途退出，剩 s1..s8。按钮下家 s1 是 SB，s2 是 BB，
    最后一个在场的人 s8 拿 CO（这一手没有真正的 BTN 玩家）。
    """
    seat_ring = [f"s{i}" for i in range(9)]
    active = seat_ring[1:]
    r = assign_positions(active, "s0", seat_ring=seat_ring)
    check(r.ok, f"死按钮应能定出位置，实际 {r.error}")
    check(r.dead_button, "应标记为死按钮")
    check(r.table_size == 8, f"桌号应为在场人数 8，实际 {r.table_size}")
    check(r.positions.get("s1") == "SB",
          f"按钮下家应是 SB，实际 {r.positions.get('s1')}")
    check(r.positions.get("s2") == "BB",
          f"再下家应是 BB，实际 {r.positions.get('s2')}")
    check(r.positions.get("s8") == "CO",
          f"最后一个在场的人应是 CO（不是 BTN），实际 {r.positions.get('s8')}")
    check("s0" not in r.positions, "退出的人不该有位置")
    check(sorted(r.positions.values()) == sorted(POSITIONS_BY_TABLE_SIZE[9][:-1]),
          f"9 人桌去掉 BTN 的位置集合应完整且唯一：{r.positions}")


def test_empty_seats_are_skipped_from_fixed_ring() -> None:
    """没坐满的桌：空座只跳过，位置按固定座位环的相对顺序发。"""
    seat_ring = [f"s{i}" for i in range(9)]
    active = ["s0", "s1", "s3", "s4", "s5", "s6", "s7", "s8"]  # s2 空座
    r = assign_positions(active, "s0", seat_ring=seat_ring)
    check(r.ok, f"应能定出位置，实际 {r.error}")
    check(r.positions.get("s0") == "BTN", f"s0 应是 BTN，实际 {r.positions.get('s0')}")
    check(r.positions.get("s1") == "SB", f"s1 应是 SB，实际 {r.positions.get('s1')}")
    check(r.positions.get("s3") == "BB", f"s3 应是 BB，实际 {r.positions.get('s3')}")


def test_dead_button_requires_dealer_in_seat_ring() -> None:
    """庄位既不在在场环、也不在完整座位环 —— 仍应报「不在环上」。"""
    r = assign_positions(["a", "b", "c"], "zzz", seat_ring=["a", "b", "c"])
    check(not r.ok and "不在环上" in (r.error or ""),
          f"庄位不在完整环上应失败，实际 {r.positions} / {r.error}")


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
        test_dead_button_dealer_left,
        test_empty_seats_are_skipped_from_fixed_ring,
        test_dead_button_requires_dealer_in_seat_ring,
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
