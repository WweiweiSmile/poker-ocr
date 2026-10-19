#!/usr/bin/env python3
"""批量裁掉截图/抓帧的黑边，并按 profile 参考比例对齐（ROI 预处理）。

    .venv/bin/python tools/crop_frames.py logs/live_frames \
        --out logs/live_frames_cropped

流程和实时链路完全一致：

    plan_crop(auto)  →  AspectFit  →  存图

- `auto`：找非黑内容包围盒。OBS 画布左侧放牌桌、右侧留黑时只保留牌桌。
- `AspectFit`：把画面宽度改成 ``高 × 参考宽高比``，保证 ROI 的 x/y 缩放一致。
- 默认只处理 jpg/jpeg/png，输出保持原文件名。

输出目录里是**和实时 worker 看到的完全一样**的画面，可以直接喂
`tools/roi_probe.py` 或作为标定样张。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.live import AspectFit, plan_crop  # noqa: E402
from poker_ocr.profile import Profile           # noqa: E402

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"
SUFFIXES = {".jpg", ".jpeg", ".png"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path, help="原始帧目录")
    ap.add_argument("--out", type=Path, required=True, help="裁剪后输出目录")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--crop", choices=("auto", "left", "none"), default="auto")
    ap.add_argument("--no-fit", action="store_true",
                    help="只裁黑边，不做参考比例对齐")
    ap.add_argument("--quality", type=int, default=95, help="JPEG 质量（默认 95）")
    args = ap.parse_args()

    if not args.src.is_dir():
        ap.error(f"不是目录：{args.src}")
    files = sorted(p for p in args.src.iterdir()
                   if p.is_file() and p.suffix.lower() in SUFFIXES)
    if not files:
        ap.error(f"{args.src} 里没有 jpg/png")

    profile = Profile.load(args.profile)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"profile={profile.path.name} 参考={profile.reference_size} "
          f"crop={args.crop} fit={'off' if args.no_fit else 'on'}")

    saved = 0
    for path in files:
        img = cv2.imread(str(path))
        if img is None:
            print(f"  ⚠️ 读不出来：{path.name}")
            continue
        h, w = img.shape[:2]
        plan = plan_crop((h, w), profile, args.crop, image=img)
        cropped = plan.apply(img)
        fit = None
        if not args.no_fit:
            fit = AspectFit.for_size(plan.out_size, profile)
            cropped = fit.apply(cropped)
        out_path = args.out / path.name
        if not cv2.imwrite(str(out_path), cropped,
                           [cv2.IMWRITE_JPEG_QUALITY, args.quality]):
            print(f"  ⚠️ 写不进去：{out_path}")
            continue
        saved += 1
        rect = list(plan.rect) if plan.rect else None
        extra = ""
        if fit is not None and fit.resized:
            extra = (f" → {fit.out_size[0]}x{fit.out_size[1]}"
                     f"（比例 {fit.aspect:.4f}→{fit.reference_aspect:.4f}）")
        print(f"  {path.name}: {w}x{h} rect={rect}"
              f" → {cropped.shape[1]}x{cropped.shape[0]}{extra}")

    print(f"\n裁好 {saved}/{len(files)} 张 → {args.out}")
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
