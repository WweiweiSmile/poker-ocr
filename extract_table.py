#!/usr/bin/env python3
"""9 人牌桌状态提取。

一张截图 → 一圈座位状态（名称 / 筹码 / 是否庄位），从自己开始沿行动方向绕一圈。

用法：
    # 纯几何自检：只画槽位不跑 OCR，用来确认坐标配得对（改配置后先跑这个）
    python extract_table.py slots table.jpg

    # 完整提取，人工指定庄位
    python extract_table.py run table.jpg --dealer-seat upL

    # 落盘证据：槽位叠加图 + 9 个名字的接触表
    python extract_table.py run table.jpg --dealer-seat upL --dump-slots --dump-seats out/

识别结果**每次 run 都会写进 rois.json**（可用 --out 改路径），内容和打印出来的一致。

字段语义：
    seats_ring_from_hero  座位环序，不是下注顺序。翻牌前 UTG 先动、翻牌后 SB 先动，
                          hero 未必是第一个。
    status                occupied / empty / uncertain。空座位 stack 返回 0，
                          但「有人筹码为 0」（全下/刚输光）是 uncertain，两者不混。
    dealer.status         found / not_found / ambiguous。「9 席恰有 1 个庄位」是牌局
                          的事实，不是检测器的能力 —— 找不到就报找不到，不猜。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import cv2

from poker_ocr.dealer import build_detector
from poker_ocr.ocr import OcrEngine
from poker_ocr.pipeline import extract, render_slots, render_contact_sheet
from poker_ocr.profile import DEFAULT_PROFILE, Profile, detect_canvas

PROJECT_ROOT = Path(__file__).resolve().parent

# 识别结果的落盘位置。**这个文件以前是旧扫描脚本的 ROI 配置**，
# 现在改作结果文件 —— 几何配置已经搬到 config/table_default.json，
# rois.json 里那份旧坐标是可重建的，覆盖掉不心疼。
# 注意：覆盖后 scan.py run 会因为找不到 fields 而报错退出（它自己有兜底提示，
# 不会崩），scan.py calibrate 不受影响。
DEFAULT_OUT = PROJECT_ROOT / "rois.json"


def load_profile(args) -> Profile:
    profile = Profile.load(args.profile)
    if args.hero_seat:
        try:
            profile.seat(args.hero_seat)
        except KeyError:
            sys.exit(f"--hero-seat {args.hero_seat} 不在配置的座位里："
                     f"{[s.seat_id for s in profile.seats]}")
        profile = dataclasses.replace(profile, hero_seat_id=args.hero_seat)
    return profile


def load_image(path: Path):
    img = cv2.imread(str(path))
    if img is None:
        sys.exit(f"读不了这张图：{path}")
    return img


def maybe_detect_canvas(img, profile: Profile, force: bool) -> Profile:
    """截图宽高比和参考尺寸明显不符时才找 letterbox 黑边。

    正常铺满屏幕的截图直接跳过 —— 误判会让所有坐标整体偏移，宁可不动。
    """
    if profile.canvas is not None:
        return profile
    h, w = img.shape[:2]
    rw, rh = profile.reference_size
    if not force and abs((w / h) - (rw / rh)) / (rw / rh) < 0.02:
        return profile

    canvas = detect_canvas(img)
    if canvas is None:
        print(f"注意：截图尺寸 {w}x{h} 和参考 {rw}x{rh} 不一致，但没找到 letterbox 黑边，"
              f"按铺满处理。坐标可能整体偏移。", file=sys.stderr)
        return profile
    print(f"检测到游戏画面区域 y {canvas[1]:.3f}-{canvas[3]:.3f}，按此映射坐标。",
          file=sys.stderr)
    return dataclasses.replace(profile, canvas=canvas)


# ─────────────────────────── 子命令 ───────────────────────────

def cmd_slots(args) -> None:
    profile = load_profile(args)
    img = load_image(args.image)
    profile = maybe_detect_canvas(img, profile, args.force_canvas)

    vis = render_slots(img, profile)
    out = Path(args.out)
    cv2.imwrite(str(out), vis)

    size = (img.shape[1], img.shape[0])
    print(f"原图 {size[0]}x{size[1]}，游戏区 {profile.content_rect(size)}")
    print(f"槽位叠加图已存到 {out}")
    print("请肉眼确认 9 组绿框（名字）、黄框（筹码）、品红框（头像）是否严丝合缝。\n")
    for seat in profile.seats_in_ring_order:
        nx1, ny1, nx2, ny2 = profile.slot_to_px(seat.name_slot, size)
        cx1, cy1, cx2, cy2 = profile.slot_to_px(seat.chip_slot, size)
        print(f"  {seat.ring_index} {seat.seat_id:6s} "
              f"name=({nx1},{ny1})-({nx2},{ny2})  chip=({cx1},{cy1})-({cx2},{cy2})")


def cmd_run(args) -> None:
    profile = load_profile(args)
    img = load_image(args.image)
    profile = maybe_detect_canvas(img, profile, args.force_canvas)

    if args.dump_slots:
        cv2.imwrite(args.dump_slots, render_slots(img, profile))
        print(f"槽位叠加图已存到 {args.dump_slots}", file=sys.stderr)

    engine = OcrEngine(det_side_len=args.det_side_len)
    if args.dealer_seat:
        # 打错字要立刻报错，不能静默降级成「庄位未找到」—— 那和「这一帧真的没庄位」
        # 是两回事，混在一起会让人以为检测失败
        try:
            profile.seat(args.dealer_seat)
        except KeyError:
            sys.exit(f"--dealer-seat {args.dealer_seat} 不在配置的座位里："
                     f"{[s.seat_id for s in profile.seats]}")
    detector = build_detector(profile, args.dealer_seat, engine=engine)
    result = extract(img, profile, engine, detector,
                     frame_id=Path(args.image).name, use_ladder=not args.no_ladder)

    if args.dump_seats:
        render_contact_sheet(img, profile, result, Path(args.dump_seats))
        print(f"座位证据图已存到 {args.dump_seats}/", file=sys.stderr)

    payload = result.to_dict(include_debug=not args.no_debug)
    text = json.dumps(payload, indent=2, ensure_ascii=False)

    # 每次 run 都落盘。写文件和打印用同一份内容，免得两边不一致
    out_path = Path(args.out)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"结果已写入 {out_path}", file=sys.stderr)

    print(text)

    # direction 不在这里唠叨：它是环序的方向、当前按「屏幕顺时针」假定，
    # 输出里的 direction_verified 字段已经把这个不确定性标出来了。
    # 行动顺序目前不记录，所以不再提示去验它。
    review = [s.seat_id for s in result.seats if s.name_review]
    if review:
        print(f"\n⚠️  名字需要复核：{', '.join(review)}", file=sys.stderr)

    if result.violations:
        for v in result.violations:
            print(f"⚠️  {v}", file=sys.stderr)

    if args.strict and not result.valid:
        sys.exit(1)


# ─────────────────────────── CLI ───────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="9 人牌桌状态提取（名称 / 筹码 / 庄位）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("image", type=Path, help="截图路径")
        p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                       help=f"牌桌几何配置（默认 {DEFAULT_PROFILE}）")
        p.add_argument("--hero-seat", help="覆盖配置里的 hero 座位 id")
        p.add_argument("--force-canvas", action="store_true",
                       help="强制做 letterbox 检测（默认只在宽高比不符时才做）")

    slots = sub.add_parser("slots", help="只画槽位不跑 OCR，用来核对坐标")
    common(slots)
    slots.add_argument("--out", default="slots.png", help="输出图路径")
    slots.set_defaults(func=cmd_slots)

    run = sub.add_parser("run", help="完整提取一遍")
    common(run)
    run.add_argument("--dealer-seat", help="人工指定庄位座位 id（如 upL）")
    run.add_argument("--det-side-len", type=int,
                     help="把图缩到这个长边再送检测，再换算回原图坐标。"
                          "不指定就用引擎默认")
    run.add_argument("--no-ladder", action="store_true",
                     help="名字只认 L0 原始结果，不跑放大阶梯")
    run.add_argument("--out", type=Path, default=DEFAULT_OUT, metavar="PATH",
                     help=f"识别结果落盘位置（默认 {DEFAULT_OUT.name}，每次 run 都会写）")
    run.add_argument("--no-debug", action="store_true", help="输出里不带 debug 字段")
    run.add_argument("--dump-slots", nargs="?", const="slots.png", metavar="PATH",
                     help="把槽位画在原图上存下来")
    run.add_argument("--dump-seats", metavar="DIR",
                     help="把每个座位的名字裁剪和接触表存下来（复核用）")
    run.add_argument("--strict", action="store_true",
                     help="valid=false 时以非零码退出")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
