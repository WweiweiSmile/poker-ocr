#!/usr/bin/env python3
"""M2.1 / M2.2 的验证载体：把固定 ROI 的读数打出来给人眼看，并量去重的效果。

    # 纯单帧（M2.1）：每张图独立读，不跨帧复用
    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --no-dedup

    # 抽帧流（M2.2）：按命令行给的顺序当一段视频喂进去，没变的 ROI 复用上一帧读数
    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --stream

    # 视频夹具：先抽帧再跑（0.5s 一帧 = 实时链路的节奏）
    .venv/bin/python tools/video_frames.py images/video/video1.mp4 --every 0.5 --out /tmp/v1
    .venv/bin/python tools/roi_probe.py /tmp/v1/*.jpg --stream --quiet

    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --stream --mad-eps 0    # 退回纯 dhash
    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --stream --max-distance 2
    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --stream --hash 9x8   # 复现碰撞
    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --json /tmp/roi.json
    .venv/bin/python tools/roi_probe.py images/roi/*.jpg --dump-crops /tmp/crops

为什么要有这个脚本：这类 OCR 活的错法全是「安静地读成另一个东西」
（`11BB`→`11B`、`01BB` 开头那个 0 是筹码图标、动作徽章盖住名字）。
光看 json 布尔值是发现不了的，必须把原文 + 单块耗时摆出来扫一遍。

被复用的格子前面带 `↺` —— **去重的风险全在这里**：复用错了（画面其实变了）时，
它在下游看起来和「识别器读对了」一模一样。所以这个标记必须显眼。

顺带量三件事：单块 ROI 的真实耗时（和 M1 的 14ms 对得上吗）、「只跑 rec」到底
跑没跑 det（走的是 read_text，det 一次都不会跑）、以及去重把单帧压到多少毫秒。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.dealer import build_detector          # noqa: E402
from poker_ocr.ocr import OcrEngine                  # noqa: E402
from poker_ocr.profile import Profile                # noqa: E402
from poker_ocr.roi_dedup import (                    # noqa: E402
    HASH_H, HASH_W, TILES, DEFAULT_MAX_DISTANCE, DEFAULT_MAD_EPS, RoiStream,
)
from poker_ocr.roi_reader import (                   # noqa: E402
    GLOBAL_FIELDS, SEAT_FIELDS, RoiRead, read_frame,
)

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"

# 表格里一列最多显示的字符数。原文在 --json 里是全的。
_COL = 18
# 单元格宽度：多留 2 格给「↺ 」前缀，有它没它都不串列
_CELL_W = _COL + 6


def imread(path: Path) -> np.ndarray | None:
    """按字节读再解码 —— cv2.imread 在含中文的路径上会静默返回 None。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def clip(text: str) -> str:
    if not text:
        return "-"
    t = text.replace("\n", " ")
    return t if len(t) <= _COL else t[: _COL - 1] + "…"


def cell(read) -> str:
    """一格：`↺原文 分数`（↺ = 复用了上一帧的读数），空读数就是 `-`。列宽固定。"""
    if read is None:
        return "-".ljust(_CELL_W)
    prefix = "↺" if getattr(read, "reused", False) else " "
    if not read.text:
        return f"{prefix}-{f' [{read.note}]' if read.note else ''}".ljust(_CELL_W)
    return f"{prefix}{clip(read.text)} {read.score:.2f}".ljust(_CELL_W)


def print_frame(frame, path: Path, img=None, dump_dir: Path | None = None,
                quiet: bool = False) -> None:
    n_roi = frame.n_roi
    n_reused = frame.n_reused
    fresh = n_roi - n_reused
    avg = frame.ms_roi / max(1, fresh)          # 均值只算真跑了的，别被 0 拉低
    dealer = frame.dealer.to_debug() if frame.dealer else None
    if dealer is None:
        d_str = "跳过了"
    elif dealer["status"] == "found":
        d_str = f"{dealer['status']} {dealer['seat_id']}"
    else:
        d_str = dealer["status"]

    turn = {True: "轮到我", False: "不是我", None: "不知道"}[frame.hero_turn]
    ev = frame.hero_turn_evidence
    if ev.get("text"):
        detail = f"（预设 {ev['text']} {ev.get('score', 0):.2f}）"
    elif ev.get("error"):
        detail = f"（{ev['error']}）"
    else:
        detail = ""
    print(f"\n── {path.name}  {frame.size[0]}x{frame.size[1]}")
    dedup = (f"   复用 {n_reused}/{n_roi}，实跑 {fresh}（均值 {avg:.1f}ms/块）"
             if n_reused else f"   无复用，实跑 {fresh} 块（均值 {avg:.1f}ms/块）")
    print(f"   轮到我={turn}{detail}   ROI {frame.ms_roi:.0f}ms{dedup}")
    print(f"   庄位={d_str} {frame.ms_dealer:.0f}ms   合计 {frame.ms_total:.0f}ms")

    if not quiet:
        keys = list(frame.seats[0].fields) if frame.seats else []
        print("   " + f"{'座位':<7}" + "".join(f"{k:<{_CELL_W}}" for k in keys))
        for seat in frame.seats:
            mark = "*" if seat.seat_id == frame.hero_seat_id else " "
            row = f" {mark} {seat.seat_id:<7}" + "".join(cell(seat.fields[k]) for k in keys)
            print(row)

        print("   全局    " + "  ".join(f"{k}={cell(read)}"
                                      for k, read in frame.globals.items()))

    if dump_dir is not None and img is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        for seat in frame.seats:
            for k, read in seat.fields.items():
                _save_crop(img, read, dump_dir / f"{seat.seat_id}_{k}.png")
        for k, read in frame.globals.items():
            _save_crop(img, read, dump_dir / f"global_{k}.png")
        # hero_turn 的探针不在 seats/globals 里，单独存一张（判错时第一个要看它）
        turn_rect = frame.hero_turn_evidence.get("rect")
        if turn_rect:
            read = RoiRead("hero_turn_probe", tuple(turn_rect))
            _save_crop(img, read, dump_dir / "global_hero_turn_probe.png")


def _save_crop(img: np.ndarray, read, out: Path) -> None:
    """把这块 ROI 的裁剪存下来 —— 读数不对时，第一件事是看裁到了什么。

    裁剪坐标同时写成同名 .rect.txt：错读经常是「框偏了」而不是「识别器不行」，
    有坐标才能回去改配置。
    """
    x1, y1, x2, y2 = read.rect
    if (x2 - x1) <= 0 or (y2 - y1) <= 0:
        return
    cv2.imwrite(str(out), img[y1:y2, x1:x2])
    out.with_suffix(".rect.txt").write_text(f"{x1},{y1},{x2},{y2}\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--mode", default="raw", choices=["raw", "gray", "otsu"])
    ap.add_argument("--upscale", type=float, default=1.0)
    ap.add_argument("--pad", type=float, default=0.05)
    ap.add_argument("--fields", default="", help="逗号分隔，覆盖座位字段（默认 name,chip,bet）")
    ap.add_argument("--globals", default="", help="逗号分隔，覆盖全局槽位")
    ap.add_argument("--no-dealer", action="store_true")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每帧重复跑几次（模型预热后量耗时用）")
    ap.add_argument("--stream", action="store_true",
                    help="按命令行顺序当一段视频喂（M2.2 去重跨帧生效）")
    ap.add_argument("--no-dedup", action="store_true",
                    help="关掉去重（M2.1 的纯单帧行为），用来和 --stream 对拍")
    ap.add_argument("--max-distance", type=int, default=DEFAULT_MAX_DISTANCE,
                    help="dhash 距离 ≤ 它才算没变（默认 0：逐 bit 全同）")
    ap.add_argument("--mad-eps", type=float, default=DEFAULT_MAD_EPS,
                    help="逐像素平均绝对差 ≤ 它也算没变（默认 0.2；0 = 关闭这条兜底）")
    ap.add_argument("--tiles", type=int, default=TILES,
                    help="ROI 横向切几片各算一个 hash（默认 4；1 = 整块比）")
    ap.add_argument("--hash", default=f"{HASH_W}x{HASH_H}",
                    help="dhash 网格，如 17x16（默认）或 9x8")
    ap.add_argument("--quiet", action="store_true",
                    help="只打每帧的汇总行，不打每座明细（长帧流用）")
    ap.add_argument("--json", type=Path, help="把完整读数写到这个文件")
    ap.add_argument("--dump-crops", type=Path,
                    help="把每块 ROI 的裁剪存下来 + 存裁剪坐标（肉眼查错用）")
    args = ap.parse_args()

    try:
        hash_w, hash_h = (int(v) for v in args.hash.lower().split("x"))
    except ValueError:
        sys.exit(f"--hash 要写成 WxH，收到 {args.hash!r}")

    profile = Profile.load(args.profile)
    engine = OcrEngine()
    detector = None if args.no_dealer else build_detector(profile, None, engine)

    print(f"profile={args.profile.name}  参考尺寸={profile.reference_size}  "
          f"preprocess={args.mode} upscale={args.upscale} pad={args.pad}")
    if args.stream:
        print(f"模式=抽帧流  去重=on（dhash {hash_w}x{hash_h} 每块横切 {args.tiles} 片，"
              f"max_distance={args.max_distance}，mad_eps={args.mad_eps}）")
    else:
        print("模式=单帧（每张图独立读，不跨帧复用；要看去重效果加 --stream）")

    kwargs: dict = {"mode": args.mode, "upscale": args.upscale, "pad": args.pad,
                    "dealer_detector": detector}
    if args.fields:
        kwargs["seat_fields"] = tuple(args.fields.split(","))
    if args.globals:
        kwargs["global_fields"] = tuple(args.globals.split(","))

    # 去重只在 --stream 下开：单帧模式下每张图都跟上一个**不同的牌局**比，
    # 「没变就跳过」在那里没有意义，还会把「复用」这件事说得像是常态
    dedup_on = args.stream and not args.no_dedup
    stream = (RoiStream(profile, engine, detector, max_distance=args.max_distance,
                        mad_eps=args.mad_eps, tiles=args.tiles,
                        hash_w=hash_w, hash_h=hash_h,
                        seat_fields=kwargs.get("seat_fields", SEAT_FIELDS),
                        global_fields=kwargs.get("global_fields", GLOBAL_FIELDS),
                        mode=args.mode, upscale=args.upscale, pad=args.pad,
                        with_dealer=not args.no_dealer)
              if dedup_on else None)

    out, ms_all = [], []
    for path in args.images:
        img = imread(path)
        if img is None:
            print(f"\n── {path.name}  读不出来（路径或格式问题），跳过")
            continue
        frame = None
        for i in range(max(1, args.repeat)):
            t0 = time.perf_counter()
            if stream is not None:
                frame = stream.read(img, frame_id=path.stem)
            else:
                frame = read_frame(img, profile, engine, frame_id=path.stem, **kwargs)
            ms_all.append((time.perf_counter() - t0) * 1000)
            if args.repeat > 1:
                print(f"   [{path.name}] 第 {i + 1} 次: {ms_all[-1]:.0f}ms")
        assert frame is not None
        dump = args.dump_crops / path.stem if args.dump_crops else None
        print_frame(frame, path, img, dump, quiet=args.quiet)
        out.append(frame.to_dict())

    if ms_all:
        print(f"\n合计 {len(ms_all)} 帧：均值 {sum(ms_all) / len(ms_all):.0f}ms，"
              f"最慢 {max(ms_all):.0f}ms，最快 {min(ms_all):.0f}ms")
    if stream is not None:
        print(f"去重累计：{stream.cache.to_debug()}  {stream.stats.to_debug()}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整读数已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
