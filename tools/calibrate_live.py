#!/usr/bin/env python3
"""从实时流标定 runtime profile：全图 OCR 一次 → 生成固定 ROI 配置。

    .venv/bin/python tools/calibrate_live.py \
        --url rtsp://127.0.0.1:8554/live \
        --frames 5 --crop auto \
        --out config/runtime_browser.json

之后实时链路走固定 ROI：

    .venv/bin/python -m poker_ocr.live --recognizer roi \
        --profile config/runtime_browser.json --crop auto

标定只在当前窗口尺寸/布局下有效；浏览器窗口大小或站点布局变了要重新跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.dealer import build_detector              # noqa: E402
from poker_ocr.frame_state import NameTracker, build_state  # noqa: E402
from poker_ocr.full_table import load_full_config, observe_full  # noqa: E402
from poker_ocr.live import FFMPEG_OPTIONS, AspectFit, plan_crop  # noqa: E402
from poker_ocr.ocr import OcrEngine                      # noqa: E402
from poker_ocr.profile import DEFAULT_PROFILE, Profile  # noqa: E402
from poker_ocr.roi_dedup import RoiStream                # noqa: E402
from poker_ocr.runtime_profile import build_runtime_profile  # noqa: E402


def _score(result) -> tuple:
    complete = sum(1 for s in result.seats if s.name is not None and s.stack is not None)
    return (result.dealer_status == "found", result.table_size, complete,
            result.pot is not None, result.blinds is not None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="rtsp://127.0.0.1:8554/live")
    ap.add_argument("--frames", type=int, default=5, help="抽几帧做标定（默认 5）")
    ap.add_argument("--interval", type=float, default=0.5, help="帧间隔秒")
    ap.add_argument("--crop", choices=("auto", "left", "none"), default="auto")
    ap.add_argument("--full-config", type=Path, default=Path("config/full_browser.json"))
    ap.add_argument("--out", type=Path, default=Path("config/runtime_profile.json"))
    ap.add_argument("--no-validate", action="store_true",
                    help="生成后不跑一遍 ROI 自检")
    args = ap.parse_args()

    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", FFMPEG_OPTIONS)
    full_cfg = load_full_config(args.full_config)
    crop_ref = Profile.load(DEFAULT_PROFILE)
    engine = OcrEngine()

    cap = cv2.VideoCapture(args.url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        sys.exit(f"打不开流：{args.url}")

    best = None
    best_score = None
    best_img = None
    try:
        for i in range(max(1, args.frames)):
            ok, img = cap.read()
            if not ok:
                sys.exit("读帧失败，确认 OBS 正在推流")
            plan = plan_crop(img.shape[:2], crop_ref, args.crop, image=img)
            fit = AspectFit.for_size(plan.out_size, crop_ref)
            cropped = fit.apply(plan.apply(img))
            result = observe_full(cropped, engine, full_cfg)
            score = _score(result)
            print(f"[帧 {i + 1}] {cropped.shape[1]}x{cropped.shape[0]}  "
                  f"座位={result.table_size} 庄位={result.dealer_status} "
                  f"pot={result.pot} blinds={'yes' if result.blinds else 'no'}  "
                  f"complete={score[2]}")
            if best_score is None or score > best_score:
                best, best_score, best_img = result, score, cropped.copy()
            time.sleep(max(0.0, args.interval))
    finally:
        cap.release()

    if best is None or best_img is None or best.table_size < 2:
        sys.exit("没有标定出可用牌桌：确认画面完整、D 在画面内、OBS 没有缩放")

    size = (best_img.shape[1], best_img.shape[0])
    payload = build_runtime_profile(
        best, size, source=args.url,
        region_y=full_cfg.region_y,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    missing = [k for k in ("pot", "blinds") if k not in payload["global_slots"]]
    print(f"\n→ {args.out}")
    print(f"座位={len(payload['seats'])} hero={payload['hero_seat_id']} "
          f"庄位={payload['_provenance']['dealer_status']} "
          f"缺全局槽={missing or '无'}")
    if "blinds" in missing:
        print("提示：盲注横幅没标定到，实时启动时加 --initial-blinds 1/2"
              "（按实际盲注），之后走正常继承。")

    if args.no_validate:
        return 0

    # 用生成的 profile 跑一遍 ROI，确认名字/筹码/位置能读出来
    print("\n自检（同一帧走 ROI）：")
    try:
        profile = Profile.load(args.out)
        stream = RoiStream(profile, engine, build_detector(profile, None, engine),
                           with_dealer=True)
        frame = stream.read(best_img, "calibration")
        state = build_state(frame, profile, name_tracker=NameTracker())
        print("  errors:", state.errors)
        print("  pot:", state.pot, "dealer:", state.dealer)
        for p in state.players:
            print(f"  {p.seat_id:6} {str(p.position):5} {p.name!r:16} "
                  f"stack={p.stack} bet={p.bet}")
    except SystemExit as exc:
        print(f"  ❌ profile 校验失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
