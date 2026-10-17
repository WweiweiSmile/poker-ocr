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
from collections import Counter, defaultdict
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.dealer import build_detector          # noqa: E402
from poker_ocr.frame_state import (                  # noqa: E402
    FrameState, NameTracker, build_state, signature,
)
from poker_ocr.ocr import OcrEngine                  # noqa: E402
from poker_ocr.profile import Profile                # noqa: E402
from poker_ocr.roi_dedup import RoiStream            # noqa: E402
from poker_ocr.roi_reader import read_slot           # noqa: E402

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"


def read_avatars(img, profile: Profile, engine: OcrEngine, stream: RoiStream,
                 detect_fold: bool) -> dict[str, tuple[str, float]]:
    """每座的头像区读一遍，用于弃牌 / 正在带入检测（best-effort）。"""
    if not detect_fold:
        return {}
    out: dict[str, tuple[str, float]] = {}
    size = (img.shape[1], img.shape[0])
    for seat in profile.seats:
        read = read_slot(img, profile, engine, seat.avatar_slot, "avatar", size,
                         pad=0.0, cache=stream.cache,
                         cache_key=f"{seat.seat_id}.@avatar")
        out[seat.seat_id] = (read.text, read.score)
    return out


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
    detector = build_detector(profile, None, engine)
    stream = RoiStream(profile, engine, detector, with_dealer=True)

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
    print(f"{args.video.name}: {w}x{h} {fps:.2f}fps {total} 帧 ≈ {total / fps:.1f}s；"
          f"每 {step} 帧取 1（{step / fps:.2f}s）")

    discarded: Counter = Counter()
    vanish: dict[str, int] = defaultdict(int)
    states: list[dict] = []
    prev: FrameState | None = None       # 上一份被接受的状态（vanish / 盲注继承用）
    # 全下状态要跨「下注 pill 被收走」保持：同一手内只增不减，庄位轮转（新手）时清空。
    # 不能每帧重算 —— 某一帧把全下玩家读成「筹码 0、无下注」就会把他误判成离桌。
    prev_all_in: set[str] = set()
    prev_dealer: str | None = None
    names = NameTracker(confirm=args.name_confirm)   # 跨帧的名字稳定器
    last_sig: tuple | None = None
    cand_sig: tuple | None = None
    cand_n = 0
    sampled = accepted = idx = 0
    t0 = time.perf_counter()

    while True:
        ok, img = cap.read()
        if not ok:
            break
        if idx % step:
            idx += 1
            continue
        sampled += 1
        t = idx / fps
        if args.max_frames and sampled > args.max_frames:
            break

        frame = stream.read(img, frame_id=f"f{idx:05d}_{t:06.2f}s")
        avatars = read_avatars(img, profile, engine, stream, not args.no_fold)

        # 庄位轮转 = 新的一手：上一手的全下状态不能带进来
        dealer_now = (frame.dealer.seat_id
                      if frame.dealer and frame.dealer.status == "found" else None)
        if dealer_now and prev_dealer and dealer_now != prev_dealer:
            prev_all_in = set()

        state = build_state(frame, profile,
                            prev_blinds=prev.blinds if prev else None,
                            avatar_reads=avatars,
                            name_tracker=names,
                            prev_all_in=prev_all_in)
        if dealer_now:
            prev_dealer = dealer_now
        prev_all_in |= {p.seat_id for p in state.players if p.in_hand and p.all_in}

        # ── 座位整座消失：先当遮挡，连续 max_vanish 帧才认定人走了 ──
        if prev is not None:
            for p_prev in prev.players:
                if not p_prev.in_hand:
                    continue
                p_now = next(p for p in state.players if p.seat_id == p_prev.seat_id)
                if p_now.in_hand:
                    vanish[p_prev.seat_id] = 0
                else:
                    vanish[p_prev.seat_id] += 1
                    if vanish[p_prev.seat_id] <= args.max_vanish:
                        state.errors.append(f"vanished:{p_prev.seat_id}")

        if not state.ok:
            for e in state.errors:
                discarded[e.split(":", 1)[0]] += 1
            if not args.quiet:
                n_reused = frame.n_reused
                print(f"  [{t:7.2f}s] 丢弃 {','.join(state.errors)}"
                      f"  (复用 {n_reused}/{frame.n_roi})")
            idx += 1
            continue

        accepted += 1
        sig = signature(state)
        record = None
        if last_sig is None:
            record = state                    # 首帧：直接当基线
        elif sig == cand_sig:
            cand_n += 1
            if cand_n >= args.stable_frames and sig != last_sig:
                record = state
        else:
            cand_sig, cand_n = sig, 1

        if record is not None:
            payload = record.to_debug()
            payload.pop("frame_id", None)
            payload = {"index": len(states) + 1, "t": round(t, 2),
                       "frame": state.frame_id, **payload}
            states.append(payload)
            last_sig = sig
            if not args.quiet:
                players = " ".join(
                    f"{p.seat_id}:{p.name or '-'}={p.stack}"
                    f"{'/' + str(p.bet) if p.bet else ''}"
                    f"{'(' + p.action + ')' if p.action else ''}"
                    for p in record.players if p.in_hand)
                print(f"  [{t:7.2f}s] ✅ 记录 #{len(states)}  pot={record.pot} "
                      f"pbs={record.pot_before_street} 轮到我={record.hero_turn}  "
                      f"{players}")
        elif not args.quiet:
            print(f"  [{t:7.2f}s] 无变化 (复用 {frame.n_reused}/{frame.n_roi})")

        prev = state
        idx += 1

    cap.release()
    elapsed = time.perf_counter() - t0

    payload = {
        "video": args.video.name,
        "interval_s": args.every,
        "profile": profile.path.name,
        "table_seats": len(profile.seats),
        "sampled_frames": sampled,
        "accepted_frames": accepted,
        "discarded_frames": sampled - accepted,
        "discarded_reasons": dict(discarded),
        "record_count": len(states),
        "processing_seconds": round(elapsed, 1),
        "roi_reuse": stream.cache.to_debug(),
        "states": states,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print(f"\n抽样 {sampled} 帧，接受 {accepted}，丢弃 {sampled - accepted}，"
          f"记录 {len(states)} 条；耗时 {elapsed:.0f}s")
    if discarded:
        print("丢弃原因：" + "  ".join(f"{k}={v}" for k, v in discarded.most_common()))
    print("ROI 去重：" + str(stream.cache.to_debug()))
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
