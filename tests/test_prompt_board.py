#!/usr/bin/env python3
"""牌面结构渲染（票 70）的测试。

    ./.venv/bin/python -m pytest tests/test_prompt_board.py -q

牌面结构这行是「按牌面选尺寸」的输入：成花/花听/彩虹、顺子可能、公对面
三段是既有口径（文案语义不动），后面补同花张数、连接、单张/多张成顺。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.ground_truth import compute_ground_truth  # noqa: E402
from poker_ocr.prompt.user.board import (  # noqa: E402
    _board_structure_zh, _longest_run, _straight_makers, _suit_run,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label}: 期望 {want!r}，实际 {got!r}")


# 底牌避开公共牌（重复牌会让 ground_truth 判无效返回 None）。
HOLES = {"KcQdJhTs": "Ah2c", "KcQdJhTs2d": "Ah3c"}


def structure(board: str) -> str:
    hole = HOLES.get(board, "AhKc")
    return _board_structure_zh(compute_ground_truth(hole, board, opponents=2))


# ─────────────────────────── 1. 整行文案 ───────────────────────────

CASES = [
    ("Ks7d2c", "彩虹面（无同花可能）｜顺子不可能｜公对面：否"),
    ("Jc8dKdQc", "花听面（未成花）｜顺子可能｜公对面：否｜连接：3 连"),
    ("9h8h7h", "成花面（同花已可能）｜顺子可能｜公对面：否｜同花：红桃 3 张"
               "｜连接：3 连"),
    ("QsJsTs", "成花面（同花已可能）｜顺子可能｜公对面：否｜同花：黑桃 3 张"
               "｜连接：3 连"),
    ("KsKd7c", "彩虹面（无同花可能）｜顺子不可能｜公对面：是"),
    ("9s9h9d", "彩虹面（无同花可能）｜顺子不可能｜公对面：是（三条）"),
    ("2c5d9h", "彩虹面（无同花可能）｜顺子不可能｜公对面：否"),
    ("6h7d8cTh", "花听面（未成花）｜顺子可能｜公对面：否｜连接：3 连"
                 "｜单张成顺（9）"),
    ("6h7d8c9sTh", "最多两张同花（不可能成花）｜顺子可能｜公对面：否"
                   "｜连接：牌面成顺"),
    ("KcQdJhTs", "彩虹面（无同花可能）｜顺子可能｜公对面：否｜连接：4 连"
                 "｜多张成顺（2 个点）"),
    ("KcQdJhTs2d", "最多两张同花（不可能成花）｜顺子可能｜公对面：否"
                   "｜连接：4 连"),
]


def test_board_structure_lines() -> None:
    for board, want in CASES:
        eq(structure(board), want, f"牌面结构 {board}")


def test_board_structure_single_line() -> None:
    for board, _want in CASES:
        out = structure(board)
        check("\n" not in out, f"{board} 结构必须是单行：{out!r}")


def test_board_structure_keeps_legacy_segments() -> None:
    """既有三段文案一个字符都没动（tests/test_ground_truth.py 断言的子串）。"""
    check("花听面（未成花）" in structure("Jc8dKdQc"), "花听面文案")
    check(structure("Jc8dKdQc").startswith("花听面（未成花）｜顺子可能｜公对面：否"),
          "前三段顺序不变")
    check("彩虹面（无同花可能）" in structure("Ks7d2c"), "彩虹面文案")
    check("顺子不可能" in structure("Ks7d2c"), "顺子不可能文案")


def test_short_board_renders_nothing() -> None:
    fake = SimpleNamespace(board=[(11, "s"), (5, "d")], board_info=SimpleNamespace())
    eq(_board_structure_zh(fake), "", "两张牌不渲染牌面结构")


# ─────────────────────────── 2. 拆件 ───────────────────────────

def test_suit_run() -> None:
    eq(_suit_run([(11, "s"), (3, "s"), (5, "d")]), ("s", 2), "最多花色与张数")
    eq(_suit_run([]), None, "空牌面 → None")


def test_longest_run() -> None:
    eq(_longest_run({4, 5, 6}), 3, "三连")
    eq(_longest_run({4, 5, 7}), 2, "断开的只有 2 连")
    eq(_longest_run({12, 0, 1, 2}), 4, "A 当 1 的 4 连")
    eq(_longest_run({12, 0, 1, 2, 3}), 5, "A 当 1 的轮子")
    eq(_longest_run({0, 1, 2, 3, 4}), 5, "普通 5 连")


def test_straight_makers() -> None:
    eq([__import__("poker_ocr.coach", fromlist=["rank_char"]).rank_char(c)
        for c in _straight_makers({4, 5, 6, 8})], ["9"], "只差 9 一张成顺")
    eq(len(_straight_makers({4, 5, 6})), 0, "三张连牌差两张，没有单张成顺")
    eq(len(_straight_makers({4, 5, 6, 7})), 2, "四张连牌两端都能成顺")


# ─────────────────────────── runner ───────────────────────────

def main() -> int:
    tests = [
        test_board_structure_lines,
        test_board_structure_single_line,
        test_board_structure_keeps_legacy_segments,
        test_short_board_renders_nothing,
        test_suit_run,
        test_longest_run,
        test_straight_makers,
    ]
    for t in tests:
        before = len(FAILS)
        t()
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 牌面结构渲染测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())