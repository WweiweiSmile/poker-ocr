#!/usr/bin/env python3
"""列出本机摄像头索引 —— 找 OBS 虚拟摄像头用。

    .venv/bin/python tools/list_cameras.py

用法：

  1. 在 OBS 里点 **Start Virtual Camera**；
  2. 跑这个脚本：帧率等于 OBS 视频 FPS（通常是 15）的那一项就是虚拟摄像头，
     每个摄像头还会存一张预览图到 `/tmp/poker_cameras/camN.jpg`；
  3. 用它的索引启动 worker：`--url cam:1`。

⚠️ macOS 的 OBS 虚拟摄像头固定按 **1920x1080** 输出（OBS 32 仍然如此）。
1080x2400 的竖屏画布会被缩放/裁剪进 16:9，实际内容可能只剩 ~486x1080，
筹码数字会太小。先看预览图：牌桌如果变小/被裁掉，就不要走虚拟摄像头这条路。

OBS 和 worker 在**同一台机器**上时，虚拟摄像头可以整段跳过
编码 → SRT → MediaMTX → RTSP → 解码，是延迟最低的接法。

macOS 首次运行需要允许终端访问相机：
系统设置 → 隐私与安全性 → 相机。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--save-dir", type=Path, default=Path("/tmp/poker_cameras"),
                    help="每个摄像头存一张预览图（默认 /tmp/poker_cameras）")
    args = ap.parse_args()
    args.save_dir.mkdir(parents=True, exist_ok=True)

    print("探测摄像头（OBS 虚拟摄像头需先在 OBS 里 Start Virtual Camera）")
    found = False
    for index in range(10):
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            cap.release()
            # 设备是连续编号的：找到过设备之后再失败就可以停了，别刷一屏
            # 「out device of bound」
            if found:
                break
            continue
        ok, frame = cap.read()
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        cap.release()
        if ok:
            found = True
            preview = args.save_dir / f"cam{index}.jpg"
            cv2.imwrite(str(preview), frame)
            print(f"  cam:{index}  {width}x{height} @ {fps:.1f}fps  预览={preview}")
        else:
            print(f"  cam:{index}  已打开但读不到画面（权限/被占用）")

    if not found:
        print("\n没找到可用摄像头：")
        print("  · 系统设置 → 隐私与安全性 → 相机：允许终端 / iTerm / 你的 IDE")
        print("  · OBS 是否已经 Start Virtual Camera")
    else:
        print("\n分辨率与 OBS 画布一致的那一项就是虚拟摄像头。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
