#!/usr/bin/env python3
"""空座「+」占位检测：离桌的唯一正向证据。

玩家中途离桌后客户端把头像换成「虚线圆 + 居中细十字」。它和「名字/筹码
读不到」不是一回事：读不到只说明遮挡，不能判离桌。这个检测器只认那个
细十字，且必须把头像照片里的白字（等待中 / 弃牌）排除掉。

    nv tests/test_empty_seats.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.card_icon import read_empty_seats  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402

PROFILE = Profile.load(ROOT / "config" / "table_default.json")
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def seats_of(path: Path) -> set[str]:
    img = cv2.imread(str(path))
    if img is None:
        raise AssertionError(f"读不到样张：{path}")
    return set(read_empty_seats(img, PROFILE))


def test_table_8p_has_exactly_one_plus_seat() -> None:
    """table-8p.jpg：8 人 + 1 个「+」空座（lowR），其余头像都不能误判。"""
    hits = seats_of(ROOT / "table-8p.jpg")
    check(hits == {"lowR"}, f"应只命中 lowR，实际 {sorted(hits)}")


def test_waiting_text_is_not_a_plus_seat() -> None:
    """头像写着「等待中」不是空座：它的白字不能连成细十字。"""
    hits = seats_of(ROOT / "images" / "status" / "waiting_frame.jpg")
    check(hits == set(), f"等待中头像不该判空座，实际 {sorted(hits)}")


def test_roi_frame_waiting_text_is_not_a_plus_seat() -> None:
    """实盘 ROI 样张里的「等待中」同样不能误判。"""
    path = (ROOT / "images" / "roi"
            / "微信图片_20261016133332_129_351.jpg")
    hits = seats_of(path)
    check(hits == set(), f"等待中头像不该判空座，实际 {sorted(hits)}")


def test_tiny_image_returns_nothing() -> None:
    """4x4 桩图（单测）太小：不判，避免把整桌人当空座。"""
    import numpy as np

    hits = read_empty_seats(np.zeros((4, 4, 3), np.uint8), PROFILE)
    check(hits == {}, f"小图应返回空，实际 {hits}")


def main() -> int:
    tests = [
        test_table_8p_has_exactly_one_plus_seat,
        test_waiting_text_is_not_a_plus_seat,
        test_roi_frame_waiting_text_is_not_a_plus_seat,
        test_tiny_image_returns_nothing,
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
    print("\n✅ 空座「+」占位检测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
