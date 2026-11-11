#!/usr/bin/env python3
"""下注/加注滑杆：单帧识别，或在屏幕上长按 + 滚动 + 确定。

    # 1) 只识别一帧（视频抽帧 / 截图都行）
    nv tools/bet_slider.py --image logs/mac_video/hero/002520.jpg

    # 2) 在屏幕上操作到 5BB（窗口位置自己给）
    nv tools/bet_slider.py --apply --target 5 \
        --window 100,80,734,833

    # 3) 同上，但自动取前台窗口的位置/大小（需要辅助功能权限）
    nv tools/bet_slider.py --apply --target 5 --window front

    # 4) 只看计划，不发鼠标事件
    nv tools/bet_slider.py --dry-run --image ... --target 5

坐标口径：profile 的槽位是整帧 0-1；`--window` 是游戏画面在屏幕上的
逻辑点矩形。Retina 下 screencapture 会把 734x833 的点截成 1468x1666 的图，
正好和 table_wpk_mac.json 的 reference_size 一致。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.bet_slider import (  # noqa: E402
    BetSliderController, SliderSettings, WindowCapture, WindowMapper,
    WindowRect, clicker_config_from_profile, list_windows, plan_slider,
    read_slider, resolve_window,
)
from poker_ocr.clicker import (  # noqa: E402
    CGEventBackend, ClickerUnavailable, DryRunBackend,
)
from poker_ocr.ocr import OcrEngine  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402

DEFAULT_PROFILE = ROOT / "config" / "table_wpk_mac.json"
DEFAULT_CONFIG = ROOT / "config" / "bet_slider_mac.json"


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"读不了滑杆配置 {path}：{exc}")
    return raw if isinstance(raw, dict) else {}


def _print_state(state, target: float | None, notch_bb: float) -> None:
    print(f"滑杆 opened={state.open}  amount={state.amount_bb}BB  "
          f"fraction={state.fraction_pct}% / {state.multiplier}x  "
          f"dark={state.dark_frac:.3f}")
    if state.amount_text:
        print(f"  顶部气泡: {state.amount_text!r} (score {state.amount_score:.2f})")
    if state.fraction_text:
        print(f"  左侧气泡: {state.fraction_text!r} (score {state.fraction_score:.2f})")
    if target is not None and state.amount_bb is not None:
        plan = plan_slider(state.amount_bb, target, notch_bb)
        if plan["reason"] == "already_at_target":
            print(f"  目标 {target:g}BB：已在目标额")
        else:
            arrow = "向上" if plan["direction"] > 0 else "向下"
            print(f"  目标 {target:g}BB：{arrow}滚约 {plan['estimated_notches']} 格"
                  f"（按 {notch_bb:g}BB/格估算，实际闭环读回）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", type=Path, help="只识别这一帧")
    ap.add_argument("--target", type=float, help="目标下注额（BB，加注到总额）")
    ap.add_argument("--apply", action="store_true", help="真的在屏幕上操作")
    ap.add_argument("--window",
                    help="游戏画面窗口：index:N / title:... / owner:... / "
                         "x,y,w,h；先跑 --list-windows")
    ap.add_argument("--list-windows", action="store_true",
                    help="列出屏幕上的窗口（owner/title/bounds），不依赖前台")
    ap.add_argument("--dump-buttons", type=Path, metavar="PATH",
                    help="把 profile+window 算出的按钮圆画到截图上，肉眼核对位置")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--hold-ms", type=int, help="长按毫秒（默认 650）")
    ap.add_argument("--max-steps", type=int, help="最多滚几轮（默认 80）")
    ap.add_argument("--invert-scroll", action="store_true",
                    help="滚轮方向反了时加这个")
    ap.add_argument("--keep-held", action="store_true",
                    help="长按后不松手，保持按住滚动（默认松手再滚）")
    ap.add_argument("--no-fraction", action="store_true",
                    help="不读左侧百分比气泡（省一次 det）")
    ap.add_argument("--dry-run", action="store_true",
                    help="不发真实鼠标/滚轮事件")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.list_windows:
        try:
            wins = list_windows()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if args.json:
            print(json.dumps([w.to_dict() for w in wins],
                             ensure_ascii=False, indent=2))
        else:
            print("索引 | owner | title | x,y,w,h")
            for win in wins:
                print(win.format())
            print("\n用法：--window index:0 或 --window title:WePoker "
                  "或 --window x,y,w,h")
        return 0

    profile = Profile.load(args.profile)
    cfg = load_config(args.config)
    engine = OcrEngine()

    if args.dump_buttons:
        if args.image:
            img = cv2.imread(str(args.image))
            if img is None:
                raise SystemExit(f"读不了图片：{args.image}")
            h, w = img.shape[:2]
            window = (resolve_window(args.window) if args.window
                      else WindowRect(0, 0, w, h))
        else:
            window_text = args.window or cfg.get("window")
            if not window_text:
                raise SystemExit("--dump-buttons 需要 --image 或 --window")
            capture = WindowCapture(window_text)
            img = capture.capture()
            window = capture.rect
        btn_cfg = clicker_config_from_profile(profile, window)
        vis = img.copy()
        h, w = img.shape[:2]
        for name, r in btn_cfg.buttons.items():
            cx = (r.cx - window.x) / window.w * w
            cy = (r.cy - window.y) / window.h * h
            rad = min(r.w / window.w * w, r.h / window.h * h) / 2
            cv2.circle(vis, (int(cx), int(cy)), max(2, int(rad)), (0, 0, 255), 2)
            cv2.putText(vis, name, (int(cx - rad), int(cy - rad - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
        args.dump_buttons.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(args.dump_buttons), vis)
        print(f"按钮叠加图已写 {args.dump_buttons}（{w}x{h}）")
        for name, r in btn_cfg.buttons.items():
            print(f"  {name:10} {r.x:.0f},{r.y:.0f} {r.w:.0f}x{r.h:.0f} {r.shape}")
        return 0

    if args.image:
        img = cv2.imread(str(args.image))
        if img is None:
            raise SystemExit(f"读不了图片：{args.image}")
        state = read_slider(img, profile, engine,
                            read_fraction=not args.no_fraction)
        if args.json:
            print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_state(state, args.target,
                         float(cfg.get("notch_bb", 1.0)))
        return 0 if state.open else 1

    if not args.apply:
        raise SystemExit("要操作请加 --apply（或用 --image 只识别）")
    if args.target is None:
        raise SystemExit("--apply 需要 --target")

    window_text = args.window or cfg.get("window")
    if not window_text:
        raise SystemExit(
            "缺少 --window：先运行 --list-windows，再填 index:N / "
            "title:... / owner:... 或 x,y,w,h")
    command = str(cfg.get("capture_command")
                  or "screencapture -x -R {x},{y},{w},{h} {out}")
    window_command = str(cfg.get("window_capture_command")
                         or "screencapture -x -o -l {id} {out}")
    try:
        frames = WindowCapture(window_text, command,
                               window_command=window_command)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"窗口坐标不合法：{exc}") from exc
    window = frames.rect
    backend = DryRunBackend() if args.dry_run else CGEventBackend()
    if not args.dry_run:
        try:
            if not backend.preflight():
                raise SystemExit(
                    "没有辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，"
                    "允许终端/当前解释器")
        except ClickerUnavailable as exc:
            raise SystemExit(str(exc)) from exc

    try:
        settings = SliderSettings.from_config(cfg, source=args.config)
    except ValueError as exc:
        raise SystemExit(f"滑杆配置有误：{exc}") from exc
    # 命令行覆盖（与配置文件同一份校验：先合并再交给 dataclass 验一遍）
    overrides: dict = {}
    if args.hold_ms is not None:
        overrides["hold_ms"] = args.hold_ms
    if args.max_steps is not None:
        overrides["max_steps"] = args.max_steps
    if args.keep_held:
        overrides["release_after_open"] = False
    if args.invert_scroll:
        overrides["invert_scroll"] = True
    if args.no_fraction:
        overrides["read_fraction"] = False
    if overrides:
        try:
            settings = replace(settings, **overrides)
        except ValueError as exc:
            raise SystemExit(f"命令行参数有误：{exc}") from exc
    controller = BetSliderController(
        profile, backend, frames, WindowMapper(profile, window), settings,
        engine=engine, dry_run=args.dry_run,
        log=lambda msg: print(msg, file=sys.stderr))
    result = controller.set_amount(args.target)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"结果: {result.status}  {result.start_bb}BB → {result.final_bb}BB  "
              f"steps={result.steps} scrolls={result.scrolls} "
              f"{result.elapsed_ms}ms")
        if result.error:
            print(f"  失败: {result.error}")
    return 0 if result.status in ("set", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
