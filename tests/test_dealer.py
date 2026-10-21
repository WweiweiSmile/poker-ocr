#!/usr/bin/env python3
"""庄位检测的单元测试，用合成图跑，不依赖照片。

重点覆盖**不该检出的时候不检出** —— 误报一个庄位比漏检危险得多，
因为位置推导会跟着整体错位，而且看起来还挺像那么回事。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_dealer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.dealer import (  # noqa: E402
    AMBIGUOUS, FOUND, NOT_FOUND, BlobDealerDetector,
)
from poker_ocr.profile import Profile  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# 毛毡绿，和真图实测一致（BGR）
FELT = (80, 102, 2)


def make_felt(profile: Profile) -> tuple[np.ndarray, tuple[int, int]]:
    w, h = profile.reference_size
    img = np.full((h, w, 3), FELT, dtype=np.uint8)
    return img, (w, h)


def seat_avatar_center(profile: Profile, seat_id: str, size: tuple[int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = profile.slot_to_px(profile.seat(seat_id).avatar_slot, size)
    return (x1 + x2) // 2, (y1 + y2) // 2


def draw_disc(img, cx, cy, diameter, color=(235, 235, 235)) -> None:
    cv2.circle(img, (cx, cy), diameter // 2, color, -1)


def detect(img, profile, size, engine=None):
    return BlobDealerDetector(profile.dealer_cfg.get("blob"), engine=engine).detect(
        img, profile, size)


def test_blank_felt_finds_nothing() -> None:
    """纯毛毡：一个白点都没有，必须报 not_found，不能硬塞给某个座位。"""
    profile = Profile.load()
    img, size = make_felt(profile)
    r = detect(img, profile, size)
    check(r.status == NOT_FOUND and r.seat_id is None,
          f"空桌面应当 not_found，实际 {r.status}/{r.seat_id}")


def test_size_filter_rejects_bet_chips() -> None:
    """下注筹码（实测 68-73px）比庄位圆片（50px）大，必须被面积门挡掉。"""
    profile = Profile.load()
    img, size = make_felt(profile)
    cx, cy = seat_avatar_center(profile, "midL", size)
    draw_disc(img, cx + 300, cy, 70)          # 筹码大小
    r = detect(img, profile, size)
    check(r.status == NOT_FOUND,
          f"筹码大小的白圆片不该被当成庄位，实际 {r.status}")


def test_size_filter_rejects_text_blobs() -> None:
    """文字笔画（实测 20-25px）比庄位圆片小，也要挡掉。"""
    profile = Profile.load()
    img, size = make_felt(profile)
    cx, cy = seat_avatar_center(profile, "topL", size)
    draw_disc(img, cx, cy + 200, 22)
    r = detect(img, profile, size)
    check(r.status == NOT_FOUND,
          f"文字大小的白点不该被当成庄位，实际 {r.status}")


def test_correct_size_is_found_and_goes_to_nearest_seat() -> None:
    """50px 圆片画在谁旁边就归谁 —— 这里不传 engine，走几何兜底路径。"""
    profile = Profile.load()
    img, size = make_felt(profile)
    target = "midR"
    cx, cy = seat_avatar_center(profile, target, size)
    draw_disc(img, cx - 220, cy + 60, 50)
    r = detect(img, profile, size)
    check(r.status == FOUND and r.seat_id == target,
          f"应当归给 {target}，实际 {r.status}/{r.seat_id}")
    check(r.confidence is not None and r.confidence < 0.6,
          f"没认出 D 时应降权（x0.6），实际 confidence={r.confidence}")


def test_two_discs_is_ambiguous_not_a_guess() -> None:
    """两个候选必须报 ambiguous。'9 席恰有 1 个庄位' 是牌局事实，
    检测器没有资格在两个里挑一个。"""
    profile = Profile.load()
    img, size = make_felt(profile)
    for sid in ("lowL", "upR"):
        cx, cy = seat_avatar_center(profile, sid, size)
        draw_disc(img, cx, cy + 250, 50)
    r = detect(img, profile, size)
    check(r.status == AMBIGUOUS and r.seat_id is None,
          f"两个候选应当 ambiguous，实际 {r.status}/{r.seat_id}")


def test_disc_far_from_every_seat_is_rejected() -> None:
    """圆片画在桌子正中央、离哪个头像都远，不该硬塞给最近的座位。"""
    profile = Profile.load()
    img, size = make_felt(profile)
    cx, cy = profile.point_to_px(profile.table_center, size)
    draw_disc(img, int(cx), int(cy), 50)
    r = detect(img, profile, size)
    check(r.status == NOT_FOUND,
          f"离所有座位都远的圆片应当 not_found，实际 {r.status}/{r.seat_id}")


def test_reads_d_stops_after_confident_first_variant() -> None:
    """第一档就高置信度认出 D 时不该再跑后两档 —— 每帧省 ~2/3 的 dealer OCR。"""
    class CountingEngine:
        def __init__(self) -> None:
            self.calls = 0

        def read_text(self, _img):
            self.calls += 1
            return [("D", 0.96)]

    profile = Profile.load()
    engine = CountingEngine()
    det = BlobDealerDetector(profile.dealer_cfg.get("blob"), engine=engine)
    img = np.full((120, 120, 3), 240, np.uint8)
    got = det._reads_d(img, (40, 40, 40, 40))
    check(got[0] and got[1] == "D", f"应认出 D，实际 {got}")
    check(engine.calls == 1, f"第一档可信就该收工，实际跑了 {engine.calls} 档")


def test_reads_d_reuses_cache_until_pixels_change() -> None:
    """同一位置的同一块画面跨帧复用；按钮挪走/被盖住（像素变了）才重读。"""
    class CountingEngine:
        def __init__(self) -> None:
            self.calls = 0

        def read_text(self, _img):
            self.calls += 1
            return [("D", 0.96)]

    profile = Profile.load()
    engine = CountingEngine()
    det = BlobDealerDetector(profile.dealer_cfg.get("blob"), engine=engine)
    img = np.full((120, 120, 3), 240, np.uint8)
    det._reads_d(img, (40, 40, 40, 40))
    det._reads_d(img.copy(), (40, 40, 40, 40))
    check(engine.calls == 1, f"画面没变应命中缓存，实际重读 {engine.calls} 次")

    changed = img.copy()
    changed[40:80, 40:80] = 20
    det._reads_d(changed, (40, 40, 40, 40))
    check(engine.calls == 2, f"画面变了应重读，实际只读了 {engine.calls} 次")


def test_reads_d_tries_all_variants_when_unconfident() -> None:
    """第一档读得不像 D 时，仍要跑满三档再下结论。"""
    class GarbageEngine:
        def __init__(self) -> None:
            self.calls = 0

        def read_text(self, _img):
            self.calls += 1
            return [("X", 0.5)]

    profile = Profile.load()
    engine = GarbageEngine()
    det = BlobDealerDetector(profile.dealer_cfg.get("blob"), engine=engine)
    img = np.full((120, 120, 3), 240, np.uint8)
    got = det._reads_d(img, (40, 40, 40, 40))
    check(not got[0], f"读不出 D 应判 False，实际 {got}")
    check(engine.calls == 3, f"不可信时三档都要跑，实际 {engine.calls} 档")


def main() -> int:
    tests = [
        test_blank_felt_finds_nothing,
        test_size_filter_rejects_bet_chips,
        test_size_filter_rejects_text_blobs,
        test_correct_size_is_found_and_goes_to_nearest_seat,
        test_two_discs_is_ambiguous_not_a_guess,
        test_disc_far_from_every_seat_is_rejected,
        test_reads_d_stops_after_confident_first_variant,
        test_reads_d_reuses_cache_until_pixels_change,
        test_reads_d_tries_all_variants_when_unconfident,
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
    print("\n✅ 庄位检测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
