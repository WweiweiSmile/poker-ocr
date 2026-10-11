"""macOS 屏幕采集 + 抽帧去重。

这是「模拟器陪打」整条链的第一段：**录屏 → 每 0.5s 抽一帧 → 只留变化了的帧**。
后面才是上传给 `/ocr/table`、读底牌与行动状态、调 `allin.decide`、点按钮。

## 为什么必须去重

0.5s 一帧，一分钟就是 120 张。而这个游戏真正的「新决策点」一分钟也就几次 ——
牌桌静止时 120 张里有 110 多张是同一张图。不去重的话，下游每 0.5s 跑一次
OCR（一次推理 100ms 量级）纯属白烧，而且会把同一个局面重复判、重复点。

去重用一个自造的**差值感知哈希（dhash）**：缩到 9×8 灰度，逐行比相邻像素大小，
得 64 位。同样的画面哈希相同；局面一变（筹码移动、发牌、轮到我）就翻几位。
拿它和「**上一张已经输出过的帧**」比，汉明距离超过阈值才输出。

阈值调小 → 更灵敏、会多输出（漏检少、误报多）；调大 → 更省、但可能漏掉小变化。
默认 2。**这是这台机器上唯一需要按实际模拟器调的旋钮。**

## 两个 macOS 上的坑

1. **需要「屏幕录制」权限**：第一次跑，系统会弹窗要授权（给终端 / iTerm / IDE）。
   没授权时 `screencapture` 会直接失败并打印 `could not create image from
   display`（实测 macOS 15 / Darwin 25）。少数版本不报错、而是产出一张只有
   桌面壁纸的图，所以 `main()` 里仍然先探一帧、失败就立刻退出，不白录 60 秒。
2. **Retina 缩放**：`-R` 的区域是**逻辑点**，但产出的 PNG 是**物理像素**（2×）。
   所以别拿这里的像素坐标去硬编码槽位 —— 一律走 `profile` 的归一化坐标。

## 不负责的事

「局面稳定了没有」（动画播到一半别急着点）不在这里判，属于落子那一步。这里
只保证「输出的帧之间确实有变化」。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import cv2
import numpy as np

SCREENCAPTURE = "/usr/sbin/screencapture"

# 一次采样的默认间隔与「变化判定」阈值。见模块开头对阈值的说明。
DEFAULT_INTERVAL = 0.5
DEFAULT_MIN_CHANGE = 2
DEFAULT_HASH_SIZE = 8


# ─────────────────────────── 截屏 ───────────────────────────

def capture_screen(rect: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """截一帧，返回 BGR 数组。

    rect = (x, y, w, h)，单位是**逻辑点**（macOS 坐标，原点在左上）；
    传 None 截全屏。产出图在 Retina 上是 2 倍物理像素，见模块开头的说明。
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        path = Path(fh.name)
    try:
        cmd = [SCREENCAPTURE, "-x", "-t", "png"]
        if rect is not None:
            x, y, w, h = rect
            cmd += ["-R", f"{int(x)},{int(y)},{int(w)},{int(h)}"]
        cmd.append(str(path))
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            hint = ""
            if "could not create image" in err.lower():
                # 实测：新 macOS 上没权限就是这一句，而不是产出一张空图
                hint = ("。这是**没给屏幕录制权限**：去 系统设置 → 隐私与安全性 → "
                        "屏幕录制，勾上你运行脚本的程序（终端 / iTerm / IDE），"
                        "然后**完全退出并重开**它 —— 权限只在进程启动时读一次")
            raise RuntimeError(
                f"screencapture 失败（码 {proc.returncode}）：{err}{hint}")
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("截屏产出的文件解不开 —— 确认 screencapture 能写出正常 PNG")
        return img
    finally:
        path.unlink(missing_ok=True)


# ─────────────────────────── 感知哈希 ───────────────────────────

def dhash(img: np.ndarray, size: int = DEFAULT_HASH_SIZE) -> int:
    """差值感知哈希：缩到 (size+1)×size 灰度，逐行比相邻像素，得 size² 位整数。

    比「整图逐像素比对」抗噪：轻微的渲染抖动、亚像素位移不会翻位，
    而真正的局面变化（筹码/牌/按钮）会翻好几位。
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = small[:, 1:] > small[:, :-1]           # (size, size) 布尔
    return int.from_bytes(np.packbits(bits).tobytes(), "big")


def hamming(a: int, b: int) -> int:
    """两个哈希的汉明距离 = 翻了几位。"""
    return (a ^ b).bit_count()


# ─────────────────────────── 抽帧 ───────────────────────────

@dataclass
class Frame:
    index: int                    # 第几张**输出**的帧（从 0 起）
    t: float                      # 距开始采集的秒数
    image: np.ndarray
    hash: int
    delta: int                    # 与上一张输出帧的汉明距离（第一张为 -1）

    def __repr__(self) -> str:
        h, w = self.image.shape[:2]
        return f"Frame(index={self.index}, t={self.t:.2f}s, {w}x{h}, delta={self.delta})"


def iter_frames(
    rect: tuple[int, int, int, int] | None = None,
    interval: float = DEFAULT_INTERVAL,
    min_change: int = DEFAULT_MIN_CHANGE,
    duration: float | None = None,
    max_frames: int | None = None,
    hash_size: int = DEFAULT_HASH_SIZE,
    capture_fn: Callable[[tuple[int, int, int, int] | None], np.ndarray] = capture_screen,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[Frame]:
    """按 interval 采样，只产出「与上一张输出帧差异超过 min_change」的帧。

    capture_fn / sleep_fn / clock 做成可注入的，是为了测试能在不真的截屏、
    不真的等待的情况下把去重逻辑跑遍 —— 这类循环最怕「测不了就只能靠手点」。
    """
    start = clock()
    last_hash: int | None = None
    out_index = 0

    while True:
        if duration is not None and clock() - start >= duration:
            return
        if max_frames is not None and out_index >= max_frames:
            return

        tick = clock()
        img = capture_fn(rect)
        h = dhash(img, hash_size)

        if last_hash is None or hamming(h, last_hash) >= min_change:
            delta = -1 if last_hash is None else hamming(h, last_hash)
            yield Frame(index=out_index, t=clock() - start, image=img, hash=h, delta=delta)
            out_index += 1
            last_hash = h

        # 补足到间隔。采样本身花了多少时间要扣掉，否则实际节奏会被拖成 interval + 耗时
        rest = interval - (clock() - tick)
        if rest > 0:
            sleep_fn(rest)


def collect(out_dir: Path, **kwargs) -> int:
    """把 iter_frames 的输出落盘，返回**落盘张数**。

    去重后的流里不含被丢掉的重复帧，所以总采样次数这里拿不到；
    要给用户一个「省了多少」的数，只能由调用方按时长/间隔估（见 CLI）。
    返回一个诚实的落盘数，不假装知道另一个。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for fr in iter_frames(**kwargs):
        p = out_dir / f"frame_{fr.index:04d}_{fr.t:07.2f}s.png"
        if not cv2.imwrite(str(p), fr.image):
            raise RuntimeError(f"写不出 {p}")
        saved += 1
        print(f"  [{fr.index:4d}] t={fr.t:7.2f}s  {p.name}  (Δ={fr.delta})")
    return saved


# ─────────────────────────── 命令行 ───────────────────────────

def _parse_region(text: str | None) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [int(v) for v in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("区域格式是 x,y,w,h，例如 100,80,1200,900")
    return parts[0], parts[1], parts[2], parts[3]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m poker_ocr.capture",
        description="录屏并按 0.5s 抽帧去重，把变化了的帧存下来。"
                    "第一次跑要先给终端「屏幕录制」权限。")
    ap.add_argument("--region", default=None,
                    help="截取区域 x,y,w,h（逻辑点）。不传=全屏")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help=f"采样间隔秒数（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--min-change", type=int, default=DEFAULT_MIN_CHANGE,
                    help=f"汉明距离达到多少算「变了」（默认 {DEFAULT_MIN_CHANGE}）")
    ap.add_argument("--duration", type=float, default=60.0, help="采集时长秒数（默认 60）")
    ap.add_argument("--max-frames", type=int, default=None, help="最多落盘多少张")
    ap.add_argument("--out", default="shots", help="落盘目录（默认 shots/）")
    args = ap.parse_args(argv)

    try:
        rect = _parse_region(args.region)
    except argparse.ArgumentTypeError as exc:
        ap.error(str(exc))

    # 探一帧：没权限 / 区域不合法时尽早报错，而不是录满 60s 才发现全是空图
    probe = capture_screen(rect)
    print(f"截图正常：{probe.shape[1]}x{probe.shape[0]} 像素"
          f"（Retina 上会是逻辑点的 2 倍）")

    t0 = time.monotonic()
    saved = collect(Path(args.out), rect=rect, interval=args.interval,
                    min_change=args.min_change, duration=args.duration,
                    max_frames=args.max_frames)
    spent = time.monotonic() - t0
    est = int(spent / args.interval) if args.interval > 0 else saved
    print(f"\n用了 {spent:.1f}s，采样约 {est} 次，落盘 {saved} 张"
          f"（去重掉约 {max(est - saved, 0)} 张重复帧）-> {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
