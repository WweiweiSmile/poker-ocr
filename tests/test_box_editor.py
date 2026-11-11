#!/usr/bin/env python3
"""按钮框编辑器单测：槽位 ↔ 画布、命中测试、拖动、写回 profile。

    nv tests/test_box_editor.py

纯几何 + 临时文件，不开 Tk 窗口。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.bet_slider import WindowRect  # noqa: E402
from poker_ocr.box_editor import (  # noqa: E402
    EDITABLE_SLOTS, apply_drag, canvas_to_slot, handle_points, hit_test,
    save_slots, slot_to_canvas,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def test_slot_canvas_roundtrip() -> None:
    window = WindowRect(0.0, 33.0, 736.0, 838.0)
    slot = (0.347147, 0.812013, 0.418478, 0.874662)
    rect = slot_to_canvas(slot, window)
    check(abs(rect[0] - 0.347147 * 736) < 0.01, f"x1 换算不对：{rect}")
    check(abs(rect[3] - 0.874662 * 838) < 0.01, f"y2 换算不对：{rect}")
    back = canvas_to_slot(rect, window)
    check(all(abs(a - b) < 1e-9 for a, b in zip(slot, back)),
          f"往返不一致：{slot} → {back}")


def test_slot_canvas_with_letterbox_canvas() -> None:
    window = WindowRect(0.0, 0.0, 400.0, 300.0)
    canvas = (0.1, 0.05, 0.9, 0.95)
    rect = slot_to_canvas((0.0, 0.0, 1.0, 1.0), window, canvas)
    check(abs(rect[0] - 40) < 1e-6 and abs(rect[1] - 15) < 1e-6,
          f"canvas 左上应缩到 (40,15)：{rect}")
    check(abs(rect[2] - 360) < 1e-6 and abs(rect[3] - 285) < 1e-6,
          f"canvas 右下应缩到 (360,285)：{rect}")
    back = canvas_to_slot(rect, window, canvas)
    check(all(abs(a - b) < 1e-9 for a, b in
              zip((0.0, 0.0, 1.0, 1.0), back)), f"带 canvas 往返不一致：{back}")


def test_hit_test_prefers_handles_then_box() -> None:
    boxes = {"fold": (100.0, 100.0, 140.0, 140.0),
             "call": (200.0, 100.0, 240.0, 140.0)}
    name, handle = hit_test(boxes, 120.0, 120.0, selected="fold")
    check((name, handle) == ("fold", "move"), f"框内应是 move：{name},{handle}")
    name, handle = hit_test(boxes, 100.0, 100.0, selected="fold", handle_px=5)
    check((name, handle) == ("fold", "nw"), f"左上角应是 nw：{name},{handle}")
    name, handle = hit_test(boxes, 140.0, 120.0, selected="fold", handle_px=5)
    check((name, handle) == ("fold", "e"), f"右边应是 e：{name},{handle}")
    name, handle = hit_test(boxes, 300.0, 300.0, selected="fold")
    check(name is None, f"空白处应没命中：{name}")
    # 没选中时把手不生效，但框体仍可命中
    name, handle = hit_test(boxes, 100.0, 100.0, selected=None, handle_px=5)
    check((name, handle) == ("fold", "move"), f"未选中应直接 move：{name},{handle}")


def test_handles_cover_eight_points() -> None:
    pts = handle_points((10.0, 20.0, 30.0, 40.0))
    check(set(pts) == {"nw", "n", "ne", "e", "se", "s", "sw", "w"},
          f"把手不全：{sorted(pts)}")
    check(pts["nw"] == (10.0, 20.0) and pts["se"] == (30.0, 40.0), "角点不对")
    check(pts["n"] == (20.0, 20.0) and pts["w"] == (10.0, 30.0), "边中点不对")


def test_apply_drag_move_clamps_inside_window() -> None:
    start = (100.0, 100.0, 140.0, 140.0)
    got = apply_drag(start, "move", 10.0, -10.0, (736.0, 838.0))
    check(got == (110.0, 90.0, 150.0, 130.0), f"普通移动不对：{got}")
    got = apply_drag(start, "move", -500.0, -500.0, (736.0, 838.0))
    check(got == (0.0, 0.0, 40.0, 40.0), f"越界应夹到左上：{got}")
    got = apply_drag(start, "move", 5000.0, 5000.0, (736.0, 838.0))
    check(got == (696.0, 798.0, 736.0, 838.0), f"越界应夹到右下：{got}")


def test_apply_drag_resize_keeps_min_size() -> None:
    start = (100.0, 100.0, 140.0, 140.0)
    got = apply_drag(start, "e", -200.0, 0.0, (736.0, 838.0))
    check(got[2] - got[0] >= 6.0, f"宽度不能小于 6：{got}")
    got = apply_drag(start, "w", -200.0, 0.0, (736.0, 838.0))
    check(got[0] == 0.0 and got[2] == 140.0, f"左边应夹到 0：{got}")
    got = apply_drag(start, "se", 50.0, 50.0, (736.0, 838.0))
    check(got == (100.0, 100.0, 190.0, 190.0), f"右下拉大不对：{got}")
    got = apply_drag(start, "nw", -10.0, -10.0, (736.0, 838.0))
    check(got == (90.0, 90.0, 140.0, 140.0), f"左上拉大不对：{got}")
    got = apply_drag(start, "s", 0.0, 900.0, (736.0, 838.0))
    check(got[3] == 838.0, f"底边应夹到窗口高：{got}")


def test_save_slots_writes_and_backs_up() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "profile.json"
        raw = {"_comment": "keep me",
               "global_slots": {"hero_fold_button": [0.3, 0.8, 0.4, 0.9],
                                "hero_call_button": [0.5, 0.8, 0.6, 0.9]}}
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        changed = save_slots(path, raw,
                             {"hero_fold_button": (0.31, 0.81, 0.41, 0.91)})
        check(changed == ["hero_fold_button"], f"改动的键不对：{changed}")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        check(on_disk["global_slots"]["hero_fold_button"]
              == [0.31, 0.81, 0.41, 0.91], f"没写进去：{on_disk}")
        check(on_disk["_comment"] == "keep me", "其它键不能被吃掉")
        backup = Path(str(path) + ".bak")
        check(backup.is_file(), "要留 .bak 备份")
        check(json.loads(backup.read_text(encoding="utf-8"))
              ["global_slots"]["hero_fold_button"] == [0.3, 0.8, 0.4, 0.9],
              "备份应是改之前的版本")
        again = save_slots(path, on_disk,
                           {"hero_fold_button": (0.31, 0.81, 0.41, 0.91)})
        check(again == [], f"没变化不该改写：{again}")


def test_save_slots_rejects_bad_rect() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "p.json"
        raw = {"global_slots": {}}
        path.write_text(json.dumps(raw), encoding="utf-8")
        for bad in ((0.5, 0.5, 0.4, 0.6), (0.1, 0.1, 1.2, 0.9), (0.1, 0.1, 0.2, 0.1)):
            try:
                save_slots(path, raw, {"x": bad})
            except ValueError:
                continue
            FAILS.append(f"非法槽位应报错：{bad}")


def test_editable_slots_cover_slider_buttons() -> None:
    keys = {k for k, _label, _shape in EDITABLE_SLOTS}
    for key in ("hero_fold_button", "hero_call_button", "hero_raise_button",
                "hero_check_button", "hero_bet_confirm", "hero_bet_close",
                "hero_bet_slider", "hero_bet_amount"):
        check(key in keys, f"可编辑区域里缺 {key}")
    presets = {k for k in keys if k.startswith("bet_preset")}
    check(len(presets) == 5, f"五个档位都要在：{sorted(presets)}")


def test_editable_slot_shapes_match_real_buttons() -> None:
    shapes = {k: shape for k, _label, shape in EDITABLE_SLOTS}
    for key in ("hero_fold_button", "hero_call_button", "hero_raise_button",
                "hero_check_button", "bet_preset_1_3", "hero_bet_confirm"):
        check(shapes[key] in ("disc", "circle"),
              f"{key} 在 UI 里是圆钮：{shapes[key]}")
    for key in ("hero_bet_slider", "hero_bet_amount", "hero_bet_close"):
        check(shapes[key] == "rect", f"{key} 是长条/空白区：{shapes[key]}")
    check(set(shapes.values()) <= {"rect", "disc", "circle"},
          f"未知形状：{set(shapes.values())}")


def main() -> int:
    tests = [
        test_slot_canvas_roundtrip,
        test_slot_canvas_with_letterbox_canvas,
        test_hit_test_prefers_handles_then_box,
        test_handles_cover_eight_points,
        test_apply_drag_move_clamps_inside_window,
        test_apply_drag_resize_keeps_min_size,
        test_save_slots_writes_and_backs_up,
        test_save_slots_rejects_bad_rect,
        test_editable_slots_cover_slider_buttons,
        test_editable_slot_shapes_match_real_buttons,
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
    print("\n✅ 按钮框编辑器全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
