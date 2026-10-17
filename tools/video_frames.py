#!/usr/bin/env python3
"""把视频按固定间隔抽帧成图片，喂给 roi_probe.py 当「抽帧流」用。

    # 0.5s 一帧（实时链路的抽帧节奏），存到 /tmp/video1_frames
    .venv/bin/python tools/video_frames.py images/video/video1.mp4 --every 0.5 \
        --out /tmp/video1_frames

    # 只取前 20 帧先看看
    .venv/bin/python tools/video_frames.py images/video/video1.mp4 --every 0.5 --limit 20

然后：

    .venv/bin/python tools/roi_probe.py /tmp/video1_frames/*.jpg --stream --quiet
    .venv/bin/python tools/roi_probe.py /tmp/video1_frames/*.jpg --no-dedup --quiet

**顺序读取 + 按帧号跳，不做时间戳 seek**：seek 在部分编码上会落到关键帧、
时间戳不准；顺序解码 2556 帧不到一秒，换来的是「第 N 帧就是第 N 帧」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--every", type=float, default=0.5,
                    help="抽帧间隔，秒（默认 0.5 = 实时链路的节奏）")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0, help="最多抽多少帧（0 = 全部）")
    ap.add_argument("--quality", type=int, default=92, help="JPEG 质量")
    args = ap.parse_args()

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        sys.exit(f"打不开视频：{args.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0:
        sys.exit("读不到帧率，没法按秒抽帧")

    step = max(1, round(fps * args.every))
    print(f"{args.video.name}: {w}x{h} {fps:.2f}fps {total} 帧 ≈ {total / fps:.1f}s"
          f"；每 {step} 帧取 1（{step / fps:.2f}s）")

    args.out.mkdir(parents=True, exist_ok=True)
    saved = 0
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            # 文件名带帧号 + 毫秒，排字典序就是时间序（probe 按命令行顺序喂）
            name = args.out / f"f{idx:05d}_{idx / fps:07.2f}s.jpg"
            if not cv2.imwrite(str(name), frame,
                               [cv2.IMWRITE_JPEG_QUALITY, args.quality]):
                sys.exit(f"写不进去：{name}")
            saved += 1
            if args.limit and saved >= args.limit:
                break
        idx += 1
    cap.release()

    print(f"抽出 {saved} 帧 → {args.out}")
    if saved:
        print(f"下一步：.venv/bin/python tools/roi_probe.py '{args.out}/*.jpg' --stream --quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
