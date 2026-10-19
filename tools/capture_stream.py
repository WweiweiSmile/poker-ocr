#!/usr/bin/env python3
"""从 RTSP 流按固定间隔截图保存（默认 0.5s 一张）。

    # 连续截图（Ctrl-C 结束），默认存到 logs/live_frames/
    .venv/bin/python tools/capture_stream.py

    # 先取 10 张看看
    .venv/bin/python tools/capture_stream.py --max-frames 10 --out /tmp/live_frames

    # 指定地址 / 间隔 / 质量
    .venv/bin/python tools/capture_stream.py \
        --url rtsp://127.0.0.1:8554/live --every 0.5 --quality 92

为什么要单独一个工具：`poker_ocr.live --dump-frames` 也能存采样帧，
但它必须先跑 OCR（约 0.5s/帧），而且会和状态机共用采样节奏。
这里只拉流 + 存图，不加载模型，30fps 的流也能稳定按 0.5s 落一张。

只保留最新帧：持续 `grab()` 把缓冲吃掉，到点再 `retrieve()` 解码，
所以存下来的一定是**当前时刻**的画面，不会越积越旧。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.live import FFMPEG_OPTIONS, _open_capture  # noqa: E402

DEFAULT_URL = "rtsp://127.0.0.1:8554/live"
DEFAULT_OUT = Path("logs/live_frames")


def _filename(now: datetime, seq: int) -> str:
    """墙钟时间 + 序号。按文件名排序 = 按时间排序。"""
    return f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}_{seq:05d}.jpg"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"RTSP 地址（默认 {DEFAULT_URL}）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"输出目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--every", type=float, default=0.5,
                    help="截图间隔秒（默认 0.5）")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="最多存多少张（0 = 一直截到 Ctrl-C）")
    ap.add_argument("--quality", type=int, default=92, help="JPEG 质量（默认 92）")
    ap.add_argument("--reconnect-delay", type=float, default=1.0,
                    help="断流后重连等待秒数（默认 1）")
    args = ap.parse_args()
    if args.every <= 0:
        ap.error("--every 必须大于 0")
    if args.max_frames < 0:
        ap.error("--max-frames 不能为负")

    # 必须在 VideoCapture 之前设置，让 FFmpeg 用 TCP 且少缓冲
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", FFMPEG_OPTIONS)

    stop = False

    def _on_signal(_signum, _frame):
        nonlocal stop
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"[启动] 拉流 {args.url}")
    print(f"[启动] 每 {args.every}s 一张 → {args.out}"
          f"（{'不限张数，Ctrl-C 结束' if not args.max_frames else f'最多 {args.max_frames} 张'}）")

    saved = 0
    seq = 0
    started = time.monotonic()
    next_at = started
    last_log = started

    while not stop:
        cap = _open_capture(args.url)
        if cap is None:
            print(f"[重连] 打不开 {args.url}，{args.reconnect_delay:.0f}s 后重试")
            time.sleep(args.reconnect_delay)
            continue

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        print(f"[连接] {w}x{h} @ {fps:.1f}fps")

        try:
            while not stop:
                # grab 只推进缓冲、不解码；到点才 retrieve 解码最新一帧
                if not cap.grab():
                    break
                now = time.monotonic()
                if now < next_at:
                    continue

                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    continue

                wall = datetime.now()
                path = args.out / _filename(wall, seq)
                if not cv2.imwrite(str(path), frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, args.quality]):
                    print(f"[错误] 写不进去：{path}")
                    return 1
                saved += 1
                seq += 1
                next_at += args.every
                # 卡顿/断流后不要连拍补帧，直接对齐到当前时刻
                if now - next_at > args.every:
                    next_at = now + args.every

                if args.max_frames and saved >= args.max_frames:
                    stop = True
                    break
                if time.monotonic() - last_log >= 5.0:
                    print(f"[截图] 已存 {saved} 张，最新 {path.name}")
                    last_log = time.monotonic()
        finally:
            cap.release()

        if not stop:
            print(f"[重连] 流中断，{args.reconnect_delay:.0f}s 后重试")
            time.sleep(args.reconnect_delay)

    elapsed = time.monotonic() - started
    rate = saved / elapsed if elapsed else 0.0
    print(f"\n结束：存了 {saved} 张，耗时 {elapsed:.1f}s（{rate:.2f} 张/s）")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
