#!/usr/bin/env python3
"""用固定 ROI profile 批量识别图片，输出 JSON 报告 + 带框叠加图。

    # 验证裁剪后的实时抓帧
    .venv/bin/python tools/validate_roi.py logs/live_frames_cropped \
        --profile config/table_wpk_1080x2400.json \
        --out-dir logs/roi_validation

产物：

    logs/roi_validation/<原文件名>.roi.jpg   带 ROI 框 + 识别文本的叠加图（3x 放大）
    logs/roi_validation/report.json           每帧原始读数、解析后的状态、错误
    stdout                                    各字段读取率 / 错误汇总

用途：槽位到底偏没偏、哪一块读不出来，先看叠加图；再看 JSON 里的
`rect`（裁到了哪）和 `text/score`（读成了什么）。**只看识别率不看框，
分不清「框偏了」还是「OCR 不行」。**

`--crop auto` 可对原始（未裁黑边的）帧先做一遍和实时链路相同的
`plan_crop + AspectFit`；输入已经是 `logs/live_frames_cropped/` 时保持默认 `none`。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.dealer import build_detector                   # noqa: E402
from poker_ocr.frame_state import build_state                 # noqa: E402
from poker_ocr.live import AspectFit, plan_crop               # noqa: E402
from poker_ocr.ocr import OcrEngine                           # noqa: E402
from poker_ocr.parse import parse_blinds                      # noqa: E402
from poker_ocr.profile import Profile                         # noqa: E402
from poker_ocr.roi_reader import read_avatars, read_frame     # noqa: E402

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"
SUFFIXES = {".jpg", ".jpeg", ".png"}

# 各类槽位的颜色（RGB）。同一个座位的 name/chip/bet 用不同颜色，眼睛好分。
COLORS = {
    "name": (80, 220, 80),        # 绿
    "chip": (0, 220, 255),        # 青
    "bet": (255, 220, 0),         # 黄
    "avatar": (160, 160, 160),    # 灰（只画框，不标文本）
    "pot": (255, 80, 255),        # 品红
    "pot_before_street": (255, 140, 0),
    "blinds": (255, 60, 60),
    "hero_turn_probe": (80, 120, 255),
}
FONT_CANDIDATES = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
)


def _font(size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_overlay(img, profile: Profile, frame, state, scale: int):
    """把座位/全局槽位框和识别文本画到放大后的图上。"""
    from PIL import Image, ImageDraw

    h, w = img.shape[:2]
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb).resize((w * scale, h * scale),
                                      Image.LANCZOS)
    draw = ImageDraw.Draw(pil)
    font = _font(max(10, 5 * scale))

    def rect(px_rect, color, label):
        x1, y1, x2, y2 = px_rect
        box = (x1 * scale, y1 * scale, x2 * scale, y2 * scale)
        draw.rectangle(box, outline=color, width=max(1, scale // 2))
        if label:
            tx, ty = box[0], max(0, box[1] - 6 * scale)
            draw.text((tx, ty), label, fill=color, font=font)

    def draw_read(read, slot, color, label):
        """有实际读数就按**真正裁剪的 rect** 画（可能来自备选槽），否则画配置槽位。"""
        if read is not None and read.rect[2] > read.rect[0]:
            used = tuple(read.rect)
            text = read.text.strip()
            if text:
                label += f"={text[:12]}"
            if read.note and "chip_slot_alt" in read.note:
                label += "→alt"
        else:
            used = profile.slot_to_px(slot, (w, h))
        rect(used, color, label)

    for seat in profile.seats:
        sid = seat.seat_id
        for key, slot in (("name", seat.name_slot), ("chip", seat.chip_slot),
                          ("bet", seat.bet_slot), ("avatar", seat.avatar_slot)):
            if slot is None:
                continue
            draw_read(frame.seat(sid).fields.get(key), slot,
                      COLORS[key], f"{sid}.{key}")

    for key, color in COLORS.items():
        if key in ("name", "chip", "bet", "avatar"):
            continue
        slot = profile.global_slots.get(key)
        if slot is None:
            continue
        draw_read(frame.globals.get(key), slot, color, key)

    return pil


def _frame_record(name: str, frame, state) -> dict:
    def read_debug(read):
        if read is None:
            return None
        return {"rect": list(read.rect), "text": read.text,
                "score": round(read.score, 4)}

    return {
        "frame": name,
        "size": list(frame.size),
        "hero_turn": frame.hero_turn,
        "dealer": frame.dealer.to_debug() if frame.dealer else None,
        "globals": {k: read_debug(v) for k, v in frame.globals.items()},
        "seats": {
            s.seat_id: {k: read_debug(v) for k, v in s.fields.items()}
            for s in frame.seats
        },
        "state": state.to_debug(),
        "errors": list(state.errors),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path, help="图片目录")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--out-dir", type=Path, default=Path("logs/roi_validation"))
    ap.add_argument("--crop", choices=("auto", "left", "none"), default="none",
                    help="输入未裁黑边时用 auto；默认 none")
    ap.add_argument("--scale", type=int, default=3,
                    help="叠加图放大倍数（默认 3，方便看小字）")
    ap.add_argument("--no-dealer", action="store_true", help="跳过庄位检测")
    ap.add_argument("--no-avatars", action="store_true",
                    help="不读头像（省 9 块 ROI；会失去弃牌检测）")
    ap.add_argument("--initial-blinds", default=None,
                    help="盲注横幅读不到时的首帧盲注，如 1/20（之后走继承）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少张")
    ap.add_argument("--no-overlay", action="store_true",
                    help="不写叠加图（只统计，跑大目录时快很多）")
    ap.add_argument("--quiet", action="store_true", help="只打汇总")
    args = ap.parse_args()

    initial_blinds = None
    if args.initial_blinds:
        initial_blinds = parse_blinds(args.initial_blinds, max_ratio=50.0)
        if initial_blinds is None:
            ap.error(f"--initial-blinds 解析不了：{args.initial_blinds}")

    if not args.src.is_dir():
        ap.error(f"不是目录：{args.src}")
    files = sorted(p for p in args.src.iterdir()
                   if p.is_file() and p.suffix.lower() in SUFFIXES)
    if not files:
        ap.error(f"{args.src} 里没有 jpg/png")
    if args.limit:
        files = files[:args.limit]

    profile = Profile.load(args.profile)
    engine = OcrEngine()
    detector = None if args.no_dealer else build_detector(profile, None, engine)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"profile={profile.path.name} 参考={profile.reference_size} "
          f"座位={len(profile.seats)} crop={args.crop} "
          f"dealer={'off' if args.no_dealer else 'on'} "
          f"frames={len(files)}")

    records: list[dict] = []
    # 统计：每个字段读到了几帧
    field_seen: Counter = Counter()
    field_total: Counter = Counter()
    error_counts: Counter = Counter()
    dealer_found = 0
    accepted = 0

    for path in files:
        img = cv2.imread(str(path))
        if img is None:
            print(f"  ⚠️ 读不出来：{path.name}")
            continue
        if args.crop != "none":
            plan = plan_crop(img.shape[:2], profile, args.crop, image=img)
            img = plan.apply(img)
            fit = AspectFit.for_size((img.shape[1], img.shape[0]), profile)
            img = fit.apply(img)

        frame = read_frame(img, profile, engine, frame_id=path.stem,
                           dealer_detector=detector, with_dealer=not args.no_dealer)
        avatars = read_avatars(img, profile, engine,
                               detect_fold=not args.no_avatars)
        state = build_state(frame, profile, avatar_reads=avatars,
                            prev_blinds=initial_blinds)
        records.append(_frame_record(path.stem, frame, state))

        if frame.dealer and frame.dealer.status == "found":
            dealer_found += 1
        if state.ok:
            accepted += 1
        for e in state.errors:
            error_counts[e.split(":", 1)[0]] += 1

        for s in frame.seats:
            for key, read in s.fields.items():
                if key == "avatar":
                    continue
                field_total[f"seat.{key}"] += 1
                field_seen[f"seat.{key}"] += bool(read.text)
        for key in ("pot", "pot_before_street", "blinds"):
            read = frame.globals.get(key)
            field_total[key] += 1
            field_seen[key] += bool(read and read.text)

        if not args.no_overlay:
            out_path = out_dir / f"{path.stem}.roi.jpg"
            pil = _draw_overlay(img, profile, frame, state, args.scale)
            pil.save(out_path, quality=90)
        if not args.quiet:
            status = "✅" if state.ok else "丢弃 " + ",".join(state.errors)
            print(f"  {path.name}: {status}  dealer="
                  f"{frame.dealer.seat_id if frame.dealer else None}  "
                  f"pot={state.pot} hero_turn={frame.hero_turn}")

    report = {
        "profile": profile.path.name,
        "reference_size": list(profile.reference_size),
        "frames": len(records),
        "dealer_found": dealer_found,
        "state_ok": accepted,
        "errors": dict(error_counts),
        "field_reads": {
            k: {"read": field_seen[k], "total": field_total[k],
                "rate": round(field_seen[k] / field_total[k], 3)
                if field_total[k] else 0.0}
            for k in sorted(field_total)
        },
        "by_frame": records,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print(f"\n帧={len(records)}  状态可用={accepted}  "
          f"庄位找到={dealer_found}/{len(records)}")
    print("字段读取率：")
    for key in sorted(field_total):
        print(f"  {key:22} {field_seen[key]:3}/{field_total[key]:3} "
              f"({field_seen[key] / field_total[key] * 100:5.1f}%)")
    if error_counts:
        print("丢弃原因：" + "  ".join(f"{k}={v}" for k, v in
                                     error_counts.most_common()))
    print(f"叠加图 → {out_dir}")
    print(f"报告   → {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
