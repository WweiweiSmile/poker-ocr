#!/usr/bin/env python3
"""「至少一家领先我」双口径工具的单测（ticket #75）。

    nv tests/test_behind_any_caliber.py

锁定 `tools/behind_any_caliber_report.py` 里两个口径的**数学**，不锁定抽样噪声：

  · 独立近似是闭式 `1-(1-p)^n`，字面断言；
  · 联合口径是 Monte Carlo，只断言不变量与方向：
    n=1 时两者必须重合（这是两个口径可比的唯一前提）；
    同一副牌 n=4 时联合 > 独立（不放回抽样让「所有对手都比我弱」更难成立）。
数值只来自工具的公开函数，不碰生产路径，也不改任何返回值。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.coach import parse_cards, read_hand  # noqa: E402
from tools.behind_any_caliber_report import (  # noqa: E402
    independent_at_least, joint_at_least, summarize,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


HERO = "7s 7d"
BOARD = "10c Jd 2h"
SEED = 20261119


def test_closed_form() -> None:
    # 1 - 0.75**4 是字面值，和实现无关。
    check(independent_at_least(0.25, 4) == 0.68359375,
          f"闭式 n=4: {independent_at_least(0.25, 4)!r}")
    check(independent_at_least(0.0, 5) == 0.0, "p=0 时必然 0")
    check(independent_at_least(1.0, 5) == 1.0, "p=1 时必然 1")
    # n=1 必须退化成 p 本身——联合口径的对照基准。
    check(independent_at_least(0.3712, 1) == 0.3712, "n=1 必须等于 p")
    seq = [independent_at_least(0.3, n) for n in (1, 2, 3, 4, 5)]
    check(all(a < b for a, b in zip(seq, seq[1:])), f"n 单调递增: {seq}")
    check(independent_at_least(0.3, 0) == 0.0, "n=0 视为没有对手")


def test_summarize() -> None:
    got = summarize([-2.0, 1.0, 3.0])
    check(got["n"] == 3, f"n: {got['n']}")
    check(close(got["mean"], 2.0 / 3.0, 1e-12), f"mean: {got['mean']}")
    check(got["median"] == 1.0, f"median: {got['median']}")
    check(got["min"] == -2.0 and got["max"] == 3.0, f"极值: {got}")
    check(got["worst_abs"] == 3.0, f"worst_abs: {got['worst_abs']}")
    check(got["worst_index"] == 2, f"worst_index: {got['worst_index']}")
    check(got["n_positive"] == 2 and got["n_negative"] == 1, f"符号: {got}")
    empty = summarize([])
    check(empty["n"] == 0 and empty["mean"] is None, f"空集: {empty}")


def test_guard() -> None:
    hero = parse_cards(HERO)
    for board in ((), parse_cards("10c Jd")):
        try:
            joint_at_least(hero, board, 4, samples=200, seed=SEED)
        except ValueError:
            pass
        else:
            check(False, f"board={len(board)} 张时应拒绝（当前成牌口径要求翻牌后）")


def test_n1_matches_p1() -> None:
    """n=1 时联合抽样必须收敛到精确枚举的 p1（否则两个口径不可比）。"""
    hero = parse_cards(HERO)
    board = parse_cards(BOARD)
    p1 = read_hand(hero, board).beat_pct
    got = joint_at_least(hero, board, 1, samples=20000, seed=SEED)
    check(close(got, p1, 0.02),
          f"n=1 联合 {got:.4f} 应≈精确 {p1:.4f}")


def test_joint_above_independent() -> None:
    """同一副牌 n=4：不放回抽样 → 联合概率高于独立近似（独立偏低）。"""
    hero = parse_cards(HERO)
    board = parse_cards(BOARD)
    p1 = read_hand(hero, board).beat_pct
    indep = independent_at_least(p1, 4)
    joint = joint_at_least(hero, board, 4, samples=20000, seed=SEED)
    check(joint - indep > 0.01,
          f"联合 {joint:.4f} 应高于独立 {indep:.4f}（差 {joint - indep:+.4f}）")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILS:
        for msg in FAILS:
            print(f"❌ {msg}")
        return 1
    print("✅ behind_any 双口径工具")
    return 0


if __name__ == "__main__":
    sys.exit(main())
