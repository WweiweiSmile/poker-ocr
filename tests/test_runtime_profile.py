#!/usr/bin/env python3
"""Runtime profile（M3.3）：全图结果 → 固定 ROI 配置 → 再用 ROI 读一遍。

用 table-8p.jpg 做端到端验证：
  observe_full（全图）→ build_runtime_profile → Profile.load → RoiStream.read
名字和筹码必须和 golden 一致。底池/盲注槽位可能缺失（这张图没有底池），
不影响座位/名字/筹码的验证。

    .venv/bin/python tests/test_runtime_profile.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.dealer import build_detector          # noqa: E402
from poker_ocr.frame_state import NameTracker, build_state  # noqa: E402
from poker_ocr.full_table import FullTableConfig, observe_full  # noqa: E402
from poker_ocr.ocr import OcrEngine                  # noqa: E402
from poker_ocr.profile import Profile                # noqa: E402
from poker_ocr.roi_dedup import RoiStream            # noqa: E402
from poker_ocr.runtime_profile import build_runtime_profile  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


EXPECTED = {
    "svsvbmbm": 296.5,
    "AKA炸弹人": 254.5,
    "百大连锁酒": 76.5,
    "欣宝1": 221.5,
    "颜值犯规者": 45.0,
    "紧紧呀": 178.0,
    "小蔡不吃菜": 89.5,
    "厕所管理员": 148.5,
}


def test_runtime_profile_from_table_8p() -> None:
    img = cv2.imread(str(ROOT / "table-8p.jpg"))
    check(img is not None, "table-8p.jpg 没读到")
    if img is None:
        return
    size = (img.shape[1], img.shape[0])
    full_cfg = FullTableConfig(
        dealer_ref_size=(1440, 3200),
        dealer_blob={"area_min": 1200, "area_max": 2600},
        pair_dy_max=0.25,
    )
    engine = OcrEngine()
    result = observe_full(img, engine, full_cfg)
    check(result.table_size == 8, f"全图应聚出 8 座，实际 {result.table_size}")

    payload = build_runtime_profile(result, size, source="table-8p.jpg")
    check(payload.get("runtime") is True, "应标记 runtime=true")
    check(len(payload["seats"]) == 8, f"profile 座位数不对：{len(payload['seats'])}")
    for seat in payload["seats"]:
        for key in ("name_slot", "chip_slot", "avatar_slot"):
            slot = seat.get(key)
            check(isinstance(slot, list) and len(slot) == 4,
                  f"{seat['seat_id']}.{key} 不是合法槽位：{slot}")
            if isinstance(slot, list) and len(slot) == 4:
                x1, y1, x2, y2 = slot
                check(0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1,
                      f"{seat['seat_id']}.{key} 越界：{slot}")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "runtime.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        profile = Profile.load(path)
        check(profile.runtime, "Profile.load 应读出 runtime=true")
        check(len(profile.seats) == 8, f"ROI profile 座位数不对：{len(profile.seats)}")

        stream = RoiStream(profile, engine,
                           build_detector(profile, None, engine),
                           with_dealer=True)
        frame = stream.read(img, "calibration")
        state = build_state(frame, profile, name_tracker=NameTracker())
        got = {p.name: p.stack for p in state.players if p.in_hand and p.name}
        for name, stack in EXPECTED.items():
            check(got.get(name) == stack,
                  f"{name} 期望 {stack}，ROI 读到 {got.get(name)}")
        check(sum(1 for p in state.players if p.in_hand) == 8,
              f"ROI 应在场 8 人，实际 {sum(1 for p in state.players if p.in_hand)}")


def main() -> int:
    tests = [test_runtime_profile_from_table_8p]
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
    print("\n✅ runtime profile 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
