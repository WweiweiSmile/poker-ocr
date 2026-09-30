#!/usr/bin/env python3
"""回归测试：拿 golden 里的图各跑一遍，逐项比对。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_extract.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.dealer import build_detector          # noqa: E402
from poker_ocr.ocr import OcrEngine                  # noqa: E402
from poker_ocr.pipeline import extract               # noqa: E402
from poker_ocr.profile import Profile                # noqa: E402

GOLDEN_DIR = ROOT / "tests" / "golden"

# 每份 golden 自带 image 字段，测试自动发现 —— 换图/加图只要丢一份 golden 进去，
# 不用改这里的代码。dealer_method 决定庄位怎么来：
#   manual -> 传 golden 里的 dealer_seat_id（验的是「人工指定生效」）
#   blob   -> 不传，走自动检测（验的是「D 圆片真的被认出来了」）
FIELDS = ("position", "name", "stack", "unit", "status", "is_dealer")


def discover_cases() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("*.expected.json"))


def run_case(golden_path: Path) -> list[str] | None:
    """返回失败项列表；图不在时返回 None 表示跳过。"""
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    image_name = expected["image"]
    image = ROOT / image_name
    if not image.exists():
        print(f"  跳过 {image_name}：图不在（golden 仍保留，图放回来就会自动重跑）")
        return None

    profile = Profile.load()
    img = cv2.imread(str(image))
    engine = OcrEngine()

    manual = expected["dealer_seat_id"] if expected.get("dealer_method") == "manual" else None
    result = extract(img, profile, engine,
                     build_detector(profile, manual, engine=engine),
                     frame_id=image.name)

    failures: list[str] = []
    got = {s.seat_id: s for s in result.seats}

    for exp in expected["seats_ring_from_hero"]:
        sid = exp["seat_id"]
        seat = got.get(sid)
        if seat is None:
            failures.append(f"{sid}: 结果里没有这个座位")
            continue
        for f in FIELDS:
            if getattr(seat, f) != exp[f]:
                failures.append(f"{sid}.{f}: 期望 {exp[f]!r}，实际 {getattr(seat, f)!r}")
        if seat.ring_index != exp["ring_index"]:
            failures.append(f"{sid}.ring_index: 期望 {exp['ring_index']}，"
                            f"实际 {seat.ring_index}")

    # 噪声归属：该被槽位门挡住的、该被区域门挡住的，各归各的
    noise = {b["text"] for b in result.debug["noise_boxes"]}
    gated = {b["text"] for b in result.debug["gated_out"]}
    for t in expected.get("noise_texts", []):
        if t not in noise:
            failures.append(f"噪声 {t!r} 应该被槽位门挡住，实际没挡住")
    for t in expected.get("gated_out_texts", []):
        if t not in gated:
            failures.append(f"{t!r} 应该被区域门挡住，实际没挡住")

    # 不变量
    if result.direction != expected["direction"]:
        failures.append(f"direction: 期望 {expected['direction']}，实际 {result.direction}")
    if result.dealer.seat_id != expected["dealer_seat_id"]:
        failures.append(f"dealer: 期望 {expected['dealer_seat_id']}，"
                        f"实际 {result.dealer.seat_id}")
    if expected.get("dealer_method") and result.dealer.method != expected["dealer_method"]:
        failures.append(f"dealer.method: 期望 {expected['dealer_method']}，"
                        f"实际 {result.dealer.method}")
    if len(result.seats) != 9:
        failures.append(f"座位数应为 9，实际 {len(result.seats)}")
    dealers = [s for s in result.seats if s.is_dealer]
    if expected["dealer_seat_id"] and len(dealers) != 1:
        failures.append(f"庄位应有且仅有 1 个，实际 {len(dealers)}")
    if result.table_size != expected["table_size"]:
        failures.append(f"table_size: 期望 {expected['table_size']}，"
                        f"实际 {result.table_size}")

    # 位置：每个位置恰好出现一次，且都在该桌号的合法集合里
    from poker_ocr.positions import positions_for_table_size
    legal = positions_for_table_size(result.table_size)
    got_positions = [s.position for s in result.seats if s.position]
    if sorted(got_positions) != sorted(legal):
        failures.append(f"位置集合应={sorted(legal)}，实际={sorted(got_positions)}")

    return failures


def main() -> int:
    cases = discover_cases()
    if not cases:
        print(f"没找到 golden：{GOLDEN_DIR}/*.expected.json")
        return 1

    total_failures = 0
    ran = 0
    for golden_path in cases:
        print(f"[{golden_path.name}]")
        failures = run_case(golden_path)
        if failures is None:
            continue          # run_case 已经打了跳过说明
        ran += 1
        if failures:
            total_failures += len(failures)
            print(f"  ❌ {len(failures)} 项不符：")
            for f in failures:
                print(f"     - {f}")
        else:
            print(f"  ✅ 9 座位 × {len(FIELDS)} 字段全对，噪声分离正确，庄位与位置正确")

    if total_failures:
        print(f"\n共 {total_failures} 项失败")
        return 1
    print(f"\n✅ {ran} 张图全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
