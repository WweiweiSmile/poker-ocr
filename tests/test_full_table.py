#!/usr/bin/env python3
"""全图路线（M3.2）的测试。

两部分：
  1. 纯逻辑：宽松筹码解析、配置加载、防抖记录器。
  2. 真图：table-8p.jpg（1280x2844，8 人桌）上跑完整全图 OCR，
     对齐 golden 的名字、筹码、庄位和位置。

    .venv/bin/python tests/test_full_table.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.full_table import (  # noqa: E402
    DEFAULT_UI_WORDS, DealerTracker, FullStateRecorder, FullTableConfig,
    FullTableResult, SeatObservation, _is_name, load_full_config, observe_full,
    parse_chip_generic,
)
from poker_ocr.ocr import OcrEngine  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def seat(sid: str, name: str | None, stack: float | None,
         pos: str, dealer: bool = False) -> SeatObservation:
    return SeatObservation(
        seat_id=sid, center=(0.5, 0.5), angle=0.0, name=name, stack=stack,
        position=pos, is_dealer=dealer)


def result(seats=None, dealer="s0", status="found") -> FullTableResult:
    return FullTableResult(
        size=(1280, 720), table_center=(0.5, 0.45),
        seats=seats or [seat("s0", "A", 100, "BTN", True),
                        seat("s1", "B", 50, "SB")],
        dealer_seat_id=dealer, dealer_status=status,
        dealer_center=(0.5, 0.6), pot=12.0, blinds=None,
        direction="cw", warnings=[], debug={})


# ─────────────────────────── 纯逻辑 ───────────────────────────

def test_parse_chip_generic() -> None:
    cases = {
        "1,234": 1234.0,
        "$1,234": 1234.0,
        "243DB": 243.0,
        "478.5B8": 478.5,
        "230.588": 230.588,
        "93.500": 93.5,
        "18B": 18.0,
        "0.5BB": 0.5,
    }
    for text, want in cases.items():
        got, _ = parse_chip_generic(text)
        check(got == want, f"{text!r} 应解析为 {want}，实际 {got}")
    for text in ("星际小杨66", "1/2", "21:36", "200代入拉新300手100", "弃牌", ""):
        got, _ = parse_chip_generic(text)
        check(got is None, f"{text!r} 不该当筹码，实际 {got}")


def test_ui_words_filter() -> None:
    for text in ("D", "BB", "SB", "带入800", "桌面玩家", "性率13%带入200",
                 "正在带人", "平均底池：106"):
        check(not _is_name(text, DEFAULT_UI_WORDS),
              f"{text!r} 不该当名字")
    for text in ("星际小杨66", "kelly828", "一只猫ii", "A河牌救我"):
        check(_is_name(text, DEFAULT_UI_WORDS),
              f"{text!r} 应该当名字")


def test_load_config_and_unknown_field() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "cfg.json"
        p.write_text(json.dumps({"pair_dy_max": 0.3,
                                 "region_y": [0.1, 0.9],
                                 "dealer_ref_size": [1440, 3200]}),
                     encoding="utf-8")
        cfg = load_full_config(p)
        check(cfg.pair_dy_max == 0.3 and cfg.region_y == (0.1, 0.9),
              f"配置没加载对：{cfg}")
        bad = Path(td) / "bad.json"
        bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
        try:
            load_full_config(bad)
        except ValueError:
            pass
        else:
            FAILS.append("未知配置字段应报错")


def test_recorder_debounce_and_discard() -> None:
    rec = FullStateRecorder(stable_frames=2)
    first = rec.push(result(), 0.0, "f1")
    check(first.kind == "record" and first.record["index"] == 1,
          f"首帧应记录：{first.kind}")
    same = rec.push(result(), 0.5, "f2")
    check(same.kind == "no_change", f"同状态应无变化：{same.kind}")

    changed = result([seat("s0", "A", 120, "BTN", True),
                      seat("s1", "B", 50, "SB")])
    one = rec.push(changed, 1.0, "f3")
    two = rec.push(changed, 1.5, "f4")
    check(one.kind == "no_change", f"变化第一帧不该记录：{one.kind}")
    check(two.kind == "record" and two.record["seats"][0]["stack"] == 120,
          "变化第二帧应记录新筹码")

    no_dealer = result(status="not_found", dealer=None)
    check(rec.push(no_dealer, 2.0, "f5").kind == "discard",
          "没有庄位应丢弃")
    check(rec.summary()["discarded_reasons"].get("dealer:not_found") == 1,
          "庄位丢弃计数不对")


def _dealer_result(status: str, dealer: str | None) -> FullTableResult:
    seats = [
        SeatObservation("s0", (0.5, 0.8), 80.0, name="A", stack=100,
                        position="BTN" if dealer == "s0" else None,
                        is_dealer=(dealer == "s0")),
        SeatObservation("s1", (0.1, 0.5), 160.0, name="B", stack=50),
        SeatObservation("s2", (0.9, 0.5), 200.0, name="C", stack=30),
    ]
    return FullTableResult(
        size=(1280, 720), table_center=(0.5, 0.45), seats=seats,
        dealer_seat_id=dealer, dealer_status=status,
        dealer_center=(0.5, 0.8) if dealer else None,
        pot=10.0, blinds=None, direction="cw", warnings=[], debug={})


def test_dealer_carry() -> None:
    tracker = DealerTracker(max_carry=2)
    found = tracker.apply(_dealer_result("found", "s0"))
    check(found.dealer_status == "found", "found 应保持 found")

    carried = tracker.apply(_dealer_result("not_found", None))
    check(carried.dealer_status == "carried" and carried.dealer_seat_id == "s0",
          f"第一帧丢失应沿用庄位：{carried.dealer_status}/{carried.dealer_seat_id}")
    check(all(s.position for s in carried.seats),
          f"沿用庄位后应重新推位置：{[s.position for s in carried.seats]}")

    check(tracker.apply(_dealer_result("not_found", None)).dealer_status
          == "carried", "第二帧仍在沿用窗口内")
    check(tracker.apply(_dealer_result("not_found", None)).dealer_status
          == "not_found", "超过 max_carry 后应放弃沿用")

    rec = FullStateRecorder(stable_frames=1)
    rec.push(_dealer_result("found", "s0"), 0.0, "f1")
    out = rec.push(_dealer_result("carried", "s0"), 1.0, "f2")
    check(out.kind != "discard", f"carried 不应被丢弃：{out.kind}")


# ─────────────────────────── 真图 ───────────────────────────

def test_table_8p_full_pipeline() -> None:
    img = cv2.imread(str(ROOT / "table-8p.jpg"))
    check(img is not None, "table-8p.jpg 没读到")
    if img is None:
        return
    cfg = FullTableConfig(
        dealer_ref_size=(1440, 3200),
        dealer_blob={"area_min": 1200, "area_max": 2600},
        pair_dy_max=0.25,
    )
    res = observe_full(img, OcrEngine(), cfg)
    check(res.table_size == 8, f"应聚出 8 个座位，实际 {res.table_size}")
    check(res.dealer_status == "found", f"庄位应找到，实际 {res.dealer_status}")

    expected = {
        "BTN": ("svsvbmbm", 296.5, True),
        "SB": ("AKA炸弹人", 254.5, False),
        "BB": ("百大连锁酒", 76.5, False),
        "UTG": ("欣宝1", 221.5, False),
        "UTG+1": ("颜值犯规者", 45.0, False),
        "LJ": ("紧紧呀", 178.0, False),
        "HJ": ("小蔡不吃菜", 89.5, False),
        "CO": ("厕所管理员", 148.5, False),
    }
    got = {s.position: (s.name, s.stack, s.is_dealer) for s in res.seats}
    for pos, want in expected.items():
        check(got.get(pos) == want, f"{pos} 期望 {want}，实际 {got.get(pos)}")
    hero = next((s for s in res.seats if s.is_hero), None)
    check(hero is not None and hero.position == "BTN" and hero.is_dealer,
          f"hero 应是庄家 BTN，实际 {hero.to_dict() if hero else None}")


def main() -> int:
    tests = [
        test_parse_chip_generic,
        test_ui_words_filter,
        test_load_config_and_unknown_field,
        test_recorder_debounce_and_discard,
        test_dealer_carry,
        test_table_8p_full_pipeline,
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
    print("\n✅ 全图路线全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
