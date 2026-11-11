#!/usr/bin/env python3
"""标注模式：把点击器的「可点区域」和 OCR 命中点画到屏幕上，肉眼核对。

    # 实时（浮层盖在游戏窗口上，点击穿透，不影响操作）
    nv tools/click_marks.py --window "title:WePoker-H5" --url cam:0

    # 只存一张带标注的**真实截图**（验证屏幕映射用）
    nv tools/click_marks.py --window "title:WePoker-H5" --url cam:0 \
        --save logs/click_marks.png

    # 用单帧离线核对（不连摄像头）：把槽位/命中点画在图上
    nv tools/click_marks.py --window "title:WePoker-H5" \
        --image logs/mac_video/hero/002520.jpg --save logs/marks.png --no-overlay

颜色：蓝框 = 点击器的可点区域；绿十字 = OCR 按文字找到的按钮中心；
红十字「出界」= 文字中心跑到可点区域外（这种点下去必偏）。

浮层只画在游戏窗口那块矩形上，透明、置顶、点击穿透；Ctrl-C 结束。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.action_buttons import find_action_buttons  # noqa: E402
from poker_ocr.bet_slider import (  # noqa: E402
    PROFILE_BUTTON_SLOTS, WindowCapture, WindowMapper, WindowRect,
    clicker_config_from_profile,
)
from poker_ocr.box_editor import (  # noqa: E402
    EDITABLE_SLOTS, BoxEditor, slot_to_canvas,
)
from poker_ocr.click_marks import (  # noqa: E402
    SHORT_LABELS, Mark, annotate_alignment, compose_on_screenshot,
    marks_from_buttons, predicted_offset_pt, render_overlay, targets_to_marks,
)
from poker_ocr.live import AspectFit, _open_capture  # noqa: E402
from poker_ocr.ocr import OcrEngine  # noqa: E402
from poker_ocr.overlay import ScreenOverlay  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402

DEFAULT_PROFILE = ROOT / "config" / "table_wpk_mac.json"
DEFAULT_CONFIG = ROOT / "config" / "bet_slider_mac.json"


_SOURCE_PREFIXES = ("window:", "screen:", "cam:", "camera:", "rtsp:",
                    "rtmp:", "http://", "https://")


def _open_source(url: str):
    """视频源：带 scheme 或本地文件原样开；title:/index:/x,y,w,h 先补 window:。"""
    if url.startswith(_SOURCE_PREFIXES) or Path(url).is_file():
        return _open_capture(url)
    cap = _open_capture("window:" + url)
    return cap if cap is not None else _open_capture(url)


def _window_spec(args):
    """窗口写法：--window 优先，其次 bet_slider 配置里的 window。"""
    if args.window:
        return args.window
    if args.bet_slider_config.is_file():
        try:
            raw = json.loads(args.bet_slider_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict) and raw.get("window"):
            return raw["window"]
    raise SystemExit('没给窗口：--window "title:WePoker-H5"（或 index:N / x,y,w,h）')


def _geometry_line(window: WindowRect, raw_size: tuple[int, int]) -> str:
    offset = predicted_offset_pt(window, raw_size)
    if abs(offset) < 0.5:
        return "帧和窗口高度一致（偏差 < 0.5pt）"
    if offset > 0:
        return (f"帧比窗口矮 {offset:.1f}pt：帧顶对齐时动作行"
                f"(ny≈0.85) 会偏低 ≈{offset * 0.85:.1f}pt")
    return (f"帧比窗口高 {-offset:.1f}pt：帧顶对齐时动作行"
            f"(ny≈0.85) 会偏高 ≈{-offset * 0.85:.1f}pt")


def _caption(rect: WindowRect, raw_size: tuple[int, int], found: dict,
             stamp: str, window: WindowRect) -> str:
    frame_txt = (f"{raw_size[0]}x{raw_size[1]}" if raw_size and raw_size[0]
                 else "-")
    line1 = (f"窗口 {rect.x:g},{rect.y:g} {rect.w:g}x{rect.h:g}"
             f"  ｜ 帧 {frame_txt}")
    offset = predicted_offset_pt(window, raw_size)
    if abs(offset) >= 0.5:
        line1 += f" ｜ 偏 {offset:+.1f}pt"
    line2 = stamp + ("  ｜ OCR: " + ", ".join(sorted(found)) if found
                     else "  ｜ OCR: 无命中")
    return line1 + "\n" + line2


def _report(rect: WindowRect, window: WindowRect, scale: float,
            frame_size: tuple[int, int] | None, raw_size: tuple[int, int] | None,
            fit, cfg, found: dict, ocr_marks: list[Mark]) -> None:
    print(f"窗口(点击网格)  {rect.x:g},{rect.y:g} {rect.w:g}x{rect.h:g}   "
          f"scale={scale:.2f}", flush=True)
    if frame_size is None or raw_size is None:
        print("视频帧          未连上（只显示 profile 槽位）", flush=True)
    else:
        fw, fh = raw_size
        scale_txt = f"   ROI 缩放 {fit.scale[0]:.4f}/{fit.scale[1]:.4f}" if fit else ""
        print(f"视频帧          {fw}x{fh} → {window.w:g}x"
              f"{window.w * fh / fw:.1f}pt{scale_txt}", flush=True)
        print(f"几何            {_geometry_line(window, raw_size)}", flush=True)
    slots = " ｜ ".join(
        f"{SHORT_LABELS.get(name, name)} {r.x:.0f},{r.y:.0f} "
        f"{r.w:.0f}x{r.h:.0f}"
        for name, r in cfg.buttons.items())
    print(f"可点区域        {slots}", flush=True)
    if not found:
        print("OCR 命中        无（按 profile 槽位点）", flush=True)
        return
    parts = []
    for m in ocr_marks:
        flag = f" 出界 {m.note}" if m.kind == "warn" else " ✓"
        parts.append(f"{m.label} ({m.x:.1f},{m.y:.1f}){flag}")
    print("OCR 命中        " + " ｜ ".join(parts), flush=True)


def _frame_marks(profile: Profile, frame_size: tuple[int, int],
                 found: dict) -> list[Mark]:
    """**帧内坐标**的标注：离线核对槽位和单帧里的真实按钮。"""
    w, h = frame_size
    mapper = WindowMapper(profile, WindowRect(0.0, 0.0, float(w), float(h)))
    out: list[Mark] = []
    for name, (slot_key, shape) in PROFILE_BUTTON_SLOTS.items():
        slot = profile.global_slots.get(slot_key)
        if slot is None:
            continue
        r = mapper.rect(slot)
        out.append(Mark(r.x, r.y, r.w, r.h,
                        label=SHORT_LABELS.get(name, name), kind="slot",
                        shape=shape))
    out += [Mark(float(cx), float(cy), label=action, kind="ocr")
            for action, (cx, cy) in found.items()]
    return out


def _run_editor(args, profile, rect, window, scale, cfg, mapper, capture,
                detect, hide_while_grab: bool) -> int:
    """编辑模式：显示游戏截图 + 可拖拽框，S 存回 profile。"""
    path = args.write_profile or args.profile
    if not path.is_file():
        raise SystemExit(f"profile 不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"读不了 profile {path}：{exc}")

    boxes: dict[str, tuple] = {}
    labels: dict[str, str] = {}
    shapes: dict[str, str] = {}
    for key, label, shape in EDITABLE_SLOTS:
        slot = profile.global_slots.get(key)
        if slot is None:
            continue
        boxes[key] = slot_to_canvas(slot, window, profile.canvas)
        labels[key] = label
        shapes[key] = shape
    if not boxes:
        raise SystemExit("profile 里没有可编辑的按钮槽位")

    det = detect()
    bg0 = None
    hits0: dict = {}
    if det is not None:
        raw_frame, img, raw_size, fit, found, ocr_marks = det
        _report(rect, window, scale, (img.shape[1], img.shape[0]), raw_size,
                fit, cfg, found, ocr_marks)
        bg0 = (raw_frame, window.w * raw_size[1] / raw_size[0])
        hits0 = {m.label: (m.x, m.y) for m in ocr_marks}

    if bg0 is None:
        # 没有视频帧（摄像头被占/没开）：用启动时这一张截图当背景，R 不更新
        try:
            shot = capture.capture()
        except Exception:      # noqa: BLE001 — 截不到就纯黑背景，框照样能拖
            shot = None
        if shot is not None and getattr(shot, "size", 0):
            bg0 = (shot, window.h)
            print("[编辑] 没有视频帧，背景用启动截图（R 不会刷新）", flush=True)

    def reload_once():
        d = detect()
        if d is None:
            return (None, None)
        raw_frame, _img, raw_size, _fit, _found, marks = d
        bg = (raw_frame, window.w * raw_size[1] / raw_size[0])
        return (bg, {m.label: (m.x, m.y) for m in marks})

    print(f"[编辑] 可拖拽 {len(boxes)} 个区域："
          + "、".join(labels.values()), flush=True)
    print("[编辑] 先停掉 live！编辑器会挡住自动点击；"
          "拖框体移动、拖四角/四边改大小", flush=True)
    print("[编辑] 快捷键：S 保存 / R 重截 / Tab 换框 / ←→↑↓ 微调 / "
          "Z 撤销 / Esc 退出", flush=True)

    editor = BoxEditor(window=window, mapper=mapper, boxes=boxes,
                       labels=labels, shapes=shapes, profile_path=path, raw=raw,
                       bg0=bg0, hits0=hits0, reload_fn=reload_once,
                       hide_while_grab=hide_while_grab, log=print)
    code = editor.run()
    print("[编辑] 退出；改完重启 live 让新槽位生效", flush=True)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="标注模式：可点区域 + OCR 命中点")
    ap.add_argument("--window", help="游戏窗口：title:... / index:N / x,y,w,h")
    ap.add_argument("--url", default="cam:auto",
                    help="视频帧源（默认 cam:auto；和 live 用同一个才有意义）")
    ap.add_argument("--image", type=Path, help="用单帧图代替视频源（离线核对）")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--bet-slider-config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="浮层保持秒数；0 = 直到 Ctrl-C")
    ap.add_argument("--interval", type=float, default=1.0, help="刷新间隔秒")
    ap.add_argument("--no-overlay", action="store_true", help="不显示浮层")
    ap.add_argument("--save", type=Path, help="把带标注的图/截图存到这里")
    ap.add_argument("--edit", action="store_true",
                    help="编辑模式：拖框改可点区域的位置/大小，S 存回 profile")
    ap.add_argument("--write-profile", type=Path,
                    help="--edit 写回哪个 profile（默认就是 --profile）")
    args = ap.parse_args()

    profile = Profile.load(args.profile)
    try:
        capture = WindowCapture(_window_spec(args))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"窗口打不开：{exc}")

    def build_geometry():
        rect_ = capture.rect
        shot_ = capture.capture()
        scale_ = shot_.shape[1] / rect_.w if rect_.w else 2.0
        window_ = capture.effective_rect()
        mapper_ = WindowMapper(profile, window_)
        cfg_ = clicker_config_from_profile(profile, window_)
        return rect_, shot_, scale_, window_, mapper_, cfg_

    rect, shot, scale, window, mapper, cfg = build_geometry()
    slots = marks_from_buttons(cfg)
    engine = OcrEngine()

    frame_cap = None
    static_frame = None
    if args.image is not None:
        if not args.image.is_file():
            raise SystemExit(f"没有这张图：{args.image}")
        static_frame = cv2.imread(str(args.image))
        if static_frame is None:
            raise SystemExit(f"读不出这张图：{args.image}")
    else:
        frame_cap = _open_source(args.url)
        if frame_cap is None:
            print(f"[标注] 视频源打不开（{args.url}），先只画槽位", flush=True)

    def current_frame():
        if static_frame is not None:
            return static_frame
        if frame_cap is None:
            return None
        for _ in range(3):
            ok, img = frame_cap.read()
            if ok and img is not None:
                return img
            time.sleep(0.05)
        return None

    def detect():
        """抓一帧 → AspectFit → 按文字找按钮；返回 (img, raw, fit, found, marks)。"""
        frame = current_frame()
        if frame is None or not getattr(frame, "size", 0):
            return None
        raw_size_ = (frame.shape[1], frame.shape[0])
        fit_ = AspectFit.for_size(raw_size_, profile)
        img_ = fit_.apply(frame)
        found_ = find_action_buttons(img_, engine)
        marks_ = annotate_alignment(
            targets_to_marks(found_, (img_.shape[1], img_.shape[0]), mapper),
            cfg.buttons)
        return frame, img_, raw_size_, fit_, found_, marks_

    if args.edit:
        hide = (frame_cap is not None and static_frame is None
                and not str(args.url).startswith(
                    ("cam:", "camera:", "rtsp:", "rtmp:", "http")))
        return _run_editor(args, profile, rect, window, scale, cfg,
                           mapper, capture, detect, hide)

    overlay = None
    if not args.no_overlay:
        overlay = ScreenOverlay(rect, scale, log=print)
        if not overlay.start():
            overlay = None

    deadline = None if args.seconds <= 0 else time.monotonic() + args.seconds
    last_sig: tuple | None = None
    last_rect = rect
    saved = False
    try:
        while True:
            t0 = time.monotonic()
            if capture.refresh() and capture.rect != last_rect:
                rect, shot, scale, window, mapper, cfg = build_geometry()
                slots = marks_from_buttons(cfg)
                last_rect = rect
                last_sig = None
                print(f"[标注] 窗口变了，重建几何：{rect.to_dict()}", flush=True)
                if overlay is not None:
                    overlay.close()
                    overlay = ScreenOverlay(rect, scale, log=print)
                    if not overlay.start():
                        overlay = None

            det = detect()
            raw_size = None
            frame_size = None
            fit = None
            img = None
            found: dict = {}
            ocr_marks: list[Mark] = []
            if det is not None:
                _raw_frame, img, raw_size, fit, found, ocr_marks = det
                frame_size = (img.shape[1], img.shape[0])

            sig = (last_rect, raw_size, tuple(sorted(found.items())))
            if sig != last_sig:
                last_sig = sig
                _report(rect, window, scale, frame_size, raw_size, fit, cfg,
                        found, ocr_marks)
                if args.save is not None:
                    stamp = time.strftime("%H:%M:%S")
                    if static_frame is not None and img is not None:
                        # 离线：画在单帧上（帧内坐标），核对槽位 vs 真实按钮
                        fw, fh = img.shape[1], img.shape[0]
                        out = compose_on_screenshot(
                            img, WindowRect(0.0, 0.0, float(fw), float(fh)),
                            1.0, _frame_marks(profile, (fw, fh), found),
                            caption=f"{stamp}  {args.image.name}")
                    else:
                        # 实时：画在**新截的真实截图**上（屏幕坐标），验证映射
                        fresh = capture.capture()
                        out = compose_on_screenshot(
                            fresh, rect, scale, slots + ocr_marks,
                            caption=_caption(rect, raw_size or (0, 0), found,
                                             stamp, window))
                    args.save.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(args.save), out)
                    saved = True
                    print(f"已保存 {args.save}", flush=True)

            if overlay is not None:
                stamp = time.strftime("%H:%M:%S")
                overlay.show(render_overlay(
                    rect, scale, slots + ocr_marks,
                    caption=_caption(rect, raw_size or (0, 0), found, stamp,
                                     window)))

            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        if overlay is not None:
            overlay.close()
        if frame_cap is not None:
            try:
                frame_cap.release()
            except Exception:   # noqa: BLE001 — 释放失败不影响退出
                pass
    if args.save is not None and not saved:
        print("[标注] 没拿到画面，未保存", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
