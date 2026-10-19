#!/usr/bin/env python3
"""整段视频 → 变化触发的桌面状态 JSON（M2.3 + M2.4）。

    .venv/bin/python tools/video_to_states.py images/video/video1.mp4 --every 0.5 \
        --out /tmp/video1.states.json

规则（用户定的）：

  1. 按固定间隔抽帧（默认 0.5s），**顺序解码**，不做时间戳 seek。
  2. 状态和上一份记录相比没有变化 → 不记录；有变化 → 记一条。
  3. 一帧里只要有字段**无法精准识别**（底池只有标签、筹码胶囊被摊牌标签盖住、
     排行榜面板盖住座位、庄位找不到…）→ 整帧丢弃，原因计数。
  4. 盲注是**慢变量**：横幅被「All-in 全场禁言中…」盖住时继承上一帧
     （输出里 `blinds.source = "carried"`）；底池不继承。

两个防抖（都是为了让输出能看，不是掩盖问题）：

  · `--stable-frames 2`：新状态要连续出现 2 个被接受的帧才记录。
    OCR 的单帧抖动（`Y→YKY`）就不会变成一条假记录。首帧立即记录当基线。
  · `--max-vanish 3`：上一份状态里在场的人突然整座空白，先当遮挡丢帧；
    连续 3 帧都空才认定「人走了」。否则一个弹窗就能伪造一次人员变动。

输出是**一个 JSON**：`{video, interval_s, ..., discarded: {...}, states: [...]}`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.live import AspectFit      # noqa: E402
from poker_ocr.ocr import OcrEngine       # noqa: E402
from poker_ocr.profile import Profile     # noqa: E402
from poker_ocr.state_stream import StateStream  # noqa: E402

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--every", type=float, default=0.5, help="抽帧间隔，秒")
    ap.add_argument("--out", type=Path, default=None,
                    help="输出 JSON（默认 <视频名>.states.json）")
    ap.add_argument("--stable-frames", type=int, default=2,
                    help="新状态连续出现几帧才记录（默认 2）")
    ap.add_argument("--max-vanish", type=int, default=3,
                    help="在场玩家整座空白连续几帧才认定离开（默认 3）")
    ap.add_argument("--name-confirm", type=int, default=3,
                    help="名字连续确认几帧才换（默认 3；防 OCR 抖动）")
    ap.add_argument("--max-frames", type=int, default=0, help="最多处理多少抽帧（调试用）")
    ap.add_argument("--no-dedup", action="store_true", help="关掉 ROI 去重")
    ap.add_argument("--no-fold", action="store_true", help="不读头像（省 9 块 ROI）")
    ap.add_argument("--quiet", action="store_true", help="只打记录和最终汇总")
    args = ap.parse_args()

    out_path = args.out or args.video.with_suffix(".states.json")
    profile = Profile.load(args.profile)
    engine = OcrEngine()
    state_stream = StateStream(
        profile, engine,
        detect_fold=not args.no_fold,
        dedup=not args.no_dedup,
        stable_frames=args.stable_frames,
        max_vanish=args.max_vanish,
        name_confirm=args.name_confirm,
    )

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        sys.exit(f"打不开视频：{args.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0:
        sys.exit("读不到帧率")
    step = max(1, round(fps * args.every))
    fit = AspectFit.for_size((w, h), profile)
    print(f"{args.video.name}: {w}x{h} {fps:.2f}fps {total} 帧 ≈ {total / fps:.1f}s；"
          f"每 {step} 帧取 1（{step / fps:.2f}s）")
    if fit.resized:
        print(f"比例：{w}x{h}（{fit.aspect:.4f}）→ {fit.out_size[0]}x{fit.out_size[1]}"
              f"（参考 {fit.reference_aspect:.4f}），x/y 缩放 "
              f"{fit.scale[0]:.4f}/{fit.scale[1]:.4f}")

    states: list[dict] = []
    idx = 0
    t0 = time.perf_counter()

    while True:
        ok, img = cap.read()
        if not ok:
            break
        if idx % step:
            idx += 1
            continue
        if args.max_frames and state_stream.sampled >= args.max_frames:
            break
        t = idx / fps
        img = fit.apply(img)

        outcome = state_stream.ingest(
            img, frame_id=f"f{idx:05d}_{t:06.2f}s", t=t)
        state = outcome.state

        if outcome.kind == "discard":
            if not args.quiet:
                print(f"  [{t:7.2f}s] 丢弃 {','.join(outcome.errors)}"
                      f"  (复用 {outcome.reused}/{outcome.n_roi})")
        elif outcome.record is not None:
            states.append(outcome.record)
            if not args.quiet:
                players = " ".join(
                    f"{p.seat_id}:{p.name or '-'}={p.stack}"
                    f"{'/' + str(p.bet) if p.bet else ''}"
                    f"{'(' + p.action + ')' if p.action else ''}"
                    for p in state.players if p.in_hand)
                print(f"  [{t:7.2f}s] ✅ 记录 #{outcome.record['index']}  "
                      f"pot={state.pot} pbs={state.pot_before_street} "
                      f"轮到我={state.hero_turn}  {players}")
        elif not args.quiet:
            print(f"  [{t:7.2f}s] 无变化 (复用 {outcome.reused}/{outcome.n_roi})")

        idx += 1

    cap.release()
    elapsed = time.perf_counter() - t0
    summary = state_stream.summary()

    payload = {
        "video": args.video.name,
        "interval_s": args.every,
        "profile": profile.path.name,
        "table_seats": len(profile.seats),
        "sampled_frames": summary["sampled_frames"],
        "accepted_frames": summary["accepted_frames"],
        "discarded_frames": summary["discarded_frames"],
        "discarded_reasons": summary["discarded_reasons"],
        "record_count": summary["record_count"],
        "processing_seconds": round(elapsed, 1),
        "roi_reuse": summary["roi_reuse"],
        "states": states,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print(f"\n抽样 {summary['sampled_frames']} 帧，接受 {summary['accepted_frames']}，"
          f"丢弃 {summary['discarded_frames']}，记录 {summary['record_count']} 条；"
          f"耗时 {elapsed:.0f}s")
    if summary["discarded_reasons"]:
        print("丢弃原因：" + "  ".join(
            f"{k}={v}" for k, v in
            sorted(summary["discarded_reasons"].items(), key=lambda kv: -kv[1])))
    print("ROI 去重：" + str(summary["roi_reuse"]))
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
