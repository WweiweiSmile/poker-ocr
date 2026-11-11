#!/usr/bin/env python3
"""点击标注的单测：槽位/命中点 → 浮层像素、几何偏移、出界判定。

    nv tests/test_click_marks.py

纯逻辑 + 合成图，不连屏幕、不点鼠标、不需要 osascript。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.bet_slider import WindowMapper, WindowRect  # noqa: E402
from poker_ocr.click_marks import (  # noqa: E402
    Mark, annotate_alignment, compose_on_screenshot, marks_from_buttons,
    predicted_offset_pt, render_overlay, targets_to_marks,
)
from poker_ocr.clicker import ClickerConfig, Rect  # noqa: E402
from poker_ocr.overlay import _OVERLAY_JS  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def make_config() -> ClickerConfig:
    return ClickerConfig(buttons={
        "fold": Rect(283, 700, 33, 34, "disc"),
        "call": Rect(453, 700, 33, 34, "disc"),
        "raise-1/3": Rect(256, 612, 35, 35, "disc"),
    })


# ─────────────────────────── 标记构造 ───────────────────────────

def test_marks_from_buttons() -> None:
    marks = marks_from_buttons(make_config())
    check(len(marks) == 3, "三个按钮三个框")
    by_label = {m.label: m for m in marks}
    check("弃牌" in by_label and "跟注" in by_label,
          f"短标签要用中文按钮名：{sorted(by_label)}")
    fold = by_label["弃牌"]
    check((fold.x, fold.y, fold.w, fold.h) == (283, 700, 33, 34),
          f"框坐标要和 Rect 一致：{fold}")
    check(all(m.kind == "slot" for m in marks), "按钮框的 kind 应为 slot")
    check(by_label["1/3"].label == "1/3", "档位用短标签 1/3")
    check(fold.shape == "disc", f"圆钮形状要透传：{fold.shape}")


def test_targets_to_marks_uses_same_mapping_as_live() -> None:
    window = WindowRect(0.0, 33.0, 736.0, 838.0)
    mapper = WindowMapper(_fake_profile(), window)
    frame = (1472, 1666)
    marks = targets_to_marks({"call": (736.0, 1416.1)}, frame, mapper)
    check(len(marks) == 1, "一个命中点一个标记")
    m = marks[0]
    # 归一化 (0.5, 0.85) → 屏幕 (368, 33 + 0.85*838=745.3)
    check(abs(m.x - 368.0) < 0.01, f"x 映射不对：{m.x}")
    check(abs(m.y - (33.0 + 1416.1 / 1666.0 * 838.0)) < 0.01,
          f"y 映射不对：{m.y}")
    check(m.kind == "ocr" and m.w == 0 and m.h == 0, "命中点是点标记")
    check(targets_to_marks({}, frame, mapper) == [], "空命中返回空")
    check(targets_to_marks({"call": (0, 0)}, (0, 0), mapper) == [],
          "帧尺寸为 0 时不炸")


def test_annotate_alignment_flags_outside() -> None:
    buttons = make_config().buttons
    inside = Mark(460.0, 710.0, label="call", kind="ocr")
    outside = Mark(540.0, 710.0, label="call", kind="ocr")   # 右 87pt 外
    far = Mark(300.0, 710.0, label="raise", kind="ocr")      # 无对应槽位
    got = annotate_alignment([inside, outside, far], buttons)
    check(got[0].kind == "ocr" and got[0].note == "",
          f"框内不该标出界：{got[0]}")
    check(got[1].kind == "warn" and got[1].note.startswith("出界"),
          f"框外要变红并写出界：{got[1]}")
    check(got[2].kind == "ocr" and got[2].note == "",
          "raise 没有槽位对应，不该判出界")
    check(got[1].label == "call", "出界标记仍要保留动作名")


def test_predicted_offset_reads_frame_vs_window() -> None:
    # 实盘那次：窗口 845pt，帧 1472x1666（=736x833pt）→ 帧矮 12pt
    got = predicted_offset_pt(WindowRect(0, 33, 736, 845), (1472, 1666))
    check(abs(got - 12.0) < 0.05, f"845 vs 1666 应为 +12pt：{got}")
    got = predicted_offset_pt(WindowRect(0, 33, 736, 838), (1472, 1676))
    check(abs(got) < 0.05, f"838 vs 1676 应一致：{got}")
    got = predicted_offset_pt(WindowRect(0, 33, 736, 838), (1472, 1690))
    check(got < 0, f"帧比窗口高时应为负（点击偏高）：{got}")
    check(predicted_offset_pt(WindowRect(0, 33, 736, 838), (0, 0)) == 0.0,
          "帧尺寸为 0 时返回 0")


# ─────────────────────────── 渲染 ───────────────────────────

def _alpha(img: np.ndarray, x: int, y: int) -> int:
    return int(img[y, x, 3])


def test_render_overlay_paints_marks_on_transparent_canvas() -> None:
    window = WindowRect(0.0, 0.0, 100.0, 100.0)
    marks = [Mark(10, 10, 20, 20, label="弃牌", kind="slot"),
             Mark(70, 70, label="call", kind="ocr")]
    img = render_overlay(window, 1.0, marks, caption="")
    check(img.shape == (100, 100, 4), f"画布尺寸不对：{img.shape}")
    check(_alpha(img, 20, 20) > 0, "槽位框内部应有像素")
    check(_alpha(img, 70, 70) > 0, "命中点十字中心应有像素")
    check(_alpha(img, 95, 5) == 0, "没标注的地方必须完全透明")
    check(img.dtype == np.uint8, "RGBA 应为 uint8")


def test_render_overlay_draws_disc_for_round_buttons() -> None:
    # 圆钮：画正圆，外接框的四个角必须是透明的
    window = WindowRect(0.0, 0.0, 100.0, 100.0)
    img = render_overlay(window, 1.0,
                         [Mark(10, 10, 20, 20, label="弃牌", shape="disc")])
    check(_alpha(img, 11, 11) == 0, "圆钮的框角不该有点")
    check(_alpha(img, 20, 10) > 0, f"圆的顶边应有描边：{_alpha(img, 20, 10)}")
    check(_alpha(img, 30, 20) > 0, "圆的右边应有描边")
    # 方框：上边中线有线（圆角矩形，四个角是圆的）
    img = render_overlay(window, 1.0,
                         [Mark(10, 10, 20, 20, label="滑杆", shape="rect")])
    check(_alpha(img, 20, 10) > 0, "方框的上边应有描边")
    check(_alpha(img, 30, 20) > 0, "方框的右边应有描边")


def test_render_overlay_scales_with_retina() -> None:
    window = WindowRect(0.0, 0.0, 50.0, 40.0)
    img = render_overlay(window, 2.0, [Mark(0, 0, 10, 10)], caption="标题")
    check(img.shape == (80, 100, 4), f"2x 时画布应翻倍：{img.shape}")
    check(_alpha(img, 5, 5) > 0, "2x 下标注也要在")


def test_render_overlay_offsets_by_window_origin() -> None:
    # 窗口在屏幕 (100, 200)：屏幕坐标 (150,250) 应落在浮层 (50,50)
    window = WindowRect(100.0, 200.0, 100.0, 100.0)
    img = render_overlay(window, 1.0, [Mark(150, 250, label="call", kind="ocr")])
    check(_alpha(img, 50, 50) > 0, "浮层必须减掉窗口原点")
    check(_alpha(img, 150 - 100, 250 - 200) > 0, "同一点")
    check(_alpha(img, 5, 5) == 0, "窗口外的绝对坐标不该画进来")


def test_compose_on_screenshot_keeps_background() -> None:
    shot = np.zeros((200, 200, 3), dtype=np.uint8)
    window = WindowRect(0.0, 0.0, 100.0, 100.0)
    out = compose_on_screenshot(shot, window, 2.0,
                                [Mark(10, 10, 20, 20, label="弃牌")],
                                caption="")
    check(out.shape == shot.shape, f"输出尺寸要和截图一致：{out.shape}")
    check(out[40, 40].any(), f"标注应落在 2x 的 (40,40)：{out[40, 40]}")
    check(not out[180, 180].any(), "没标注的区域保持黑底")


def test_overlay_js_has_expected_contract() -> None:
    for token in ("NSWindow", "setIgnoresMouseEvents", "NSImageView",
                  "JSON.parse", "alive"):
        check(token in _OVERLAY_JS, f"浮层 JS 缺关键调用：{token}")


def test_overlay_js_no_quit_before_drawing() -> None:
    check("orderFront" in _OVERLAY_JS, "浮层要真正显示出来")


def _fake_profile():
    """targets_to_marks 只用到 canvas（None 即可）。"""
    from types import SimpleNamespace
    return SimpleNamespace(canvas=None, reference_size=(1472, 1676))


def main() -> int:
    tests = [
        test_marks_from_buttons,
        test_targets_to_marks_uses_same_mapping_as_live,
        test_annotate_alignment_flags_outside,
        test_predicted_offset_reads_frame_vs_window,
        test_render_overlay_paints_marks_on_transparent_canvas,
        test_render_overlay_draws_disc_for_round_buttons,
        test_render_overlay_scales_with_retina,
        test_render_overlay_offsets_by_window_origin,
        test_compose_on_screenshot_keeps_background,
        test_overlay_js_has_expected_contract,
        test_overlay_js_no_quit_before_drawing,
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
    print("\n✅ 点击标注全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
