#!/usr/bin/env python3
"""用一批真实帧重新标注 ROI 槽位（全图检测框 → 中位数 → 新 profile）。

    .venv/bin/python tools/refit_profile.py logs/live_frames_cropped \
        --profile config/table_wpk_1080x2400.json \
        --out config/table_wpk_live.json --step 2

做法：

1. 每帧跑一次全图 det+rec，得到所有文本框。
2. 对每个现有槽位，在**原槽位外扩一圈**的搜索区里按字段类型找候选：
   - chip / bet / pot / pot_before_street：必须是金额形（`123` / `4.5BB`）；
   - name：`looks_like_name`；
   - blinds：`1/2...` 形状；
   - avatar / hero_turn_probe：没有文字，保持原值。
3. 同一槽位在多帧里取坐标**中位数**，再加留白。命中帧数不足 `--min-hits`
   就不改它（宁可保留旧值，也不拿两三帧的噪声去改配置）。
4. 输出新 profile（`reference_size` = 这批帧的尺寸），并附上每个槽位的
   命中数、旧值、新值，方便 review。

**中位数而不是平均**：弹窗、showdown 手牌、动画帧会产生离群框；
中位数对少量离群值不敏感。异常帧里根本没有对应文字时也不会进候选。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.fields import looks_like_chip, looks_like_name  # noqa: E402
from poker_ocr.ocr import OcrEngine                           # noqa: E402
from poker_ocr.profile import Profile                         # noqa: E402

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"
SUFFIXES = {".jpg", ".jpeg", ".png"}

# 检测框不是“槽位”，是文字紧框。以下最小尺寸沿用配置里的搜索区经验值，
# 防止短名字/窄金额把槽位缩得比字还小。
MIN_W = {"name": 0.09, "chip": 0.11, "bet": 0.10, "pot": 0.09,
         "pot_before_street": 0.09, "blinds": 0.10}
MIN_H = {"name": 0.010, "chip": 0.014, "bet": 0.014, "pot": 0.014,
         "pot_before_street": 0.014, "blinds": 0.012}

_BLINDS_RE = re.compile(r"^\s*\d{1,3}\s*/\s*\d{1,3}")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*B{1,2}", re.IGNORECASE)


def _is_amount(text: str) -> bool:
    """标注用的宽松金额判定：允许 BB→B/8B 这类误读，只要能看出是数字+单位。"""
    return looks_like_chip(text) or bool(_AMOUNT_RE.search(text))


def _is_name(text: str) -> bool:
    return looks_like_name(text)


def _is_blinds(text: str) -> bool:
    return bool(_BLINDS_RE.match(text))


def _cost(cx: float, cy: float, slot) -> float:
    """归一化中心距：以槽位自身尺寸为单位。"""
    x1, y1, x2, y2 = slot
    sw, sh = max(1e-6, x2 - x1), max(1e-6, y2 - y1)
    return (((cx - (x1 + x2) / 2) / sw) ** 2
            + ((cy - (y1 + y2) / 2) / sh) ** 2) ** 0.5


# 不同字段的搜索范围：筹码槽本来就贴着胶囊，收窄防止吃到旁边的下注 pill；
# 下注 pill 放得离座位稍远，放宽一点。
EXPAND_FACTOR = {"name": 0.35, "chip": 0.25, "bet": 0.60,
                 "pot": 0.40, "pot_before_street": 0.40, "blinds": 0.50}


def _pick(boxes, slot, kind: str, expand: float, exclude: set[int] | None = None):
    """在槽位外扩的搜索区里，选类型匹配且中心最近的框。

    `exclude` 装的是已经分配给别的槽位的框 id —— 典型用途：下注槽不能把
    同一座位的筹码胶囊再算一遍。
    """
    x1, y1, x2, y2 = slot
    fx = expand * EXPAND_FACTOR.get(kind, 1.0)
    dw, dh = (x2 - x1) * fx, (y2 - y1) * fx
    region = (x1 - dw, y1 - dh, x2 + dw, y2 + dh)
    pred = {"name": _is_name, "blinds": _is_blinds}.get(kind, _is_amount)
    exclude = exclude or set()
    best = None
    for b in boxes:
        if id(b) in exclude or not pred(b["text"]):
            continue
        cx, cy = b["cx"], b["cy"]
        if not (region[0] <= cx <= region[2] and region[1] <= cy <= region[3]):
            continue
        c = _cost(cx, cy, slot)
        if best is None or c < best[0]:
            best = (c, b)
    if best is None:
        return None, None
    cost, b = best
    # 搜索区外扩不到 2 个槽宽以内才接受；再远就是隔壁座位/其他字段了。
    if cost > 2.0:
        return None, None
    return b, cost


def _new_slot(boxes, prior, kind: str):
    """多帧框 → 中位数中心 + **保留原槽位尺寸**。

    尺寸不动是刻意的：原槽位是「搜索区域」，已经按最长名字/最大金额留过白；
    实测把它放宽会让全下玩家的裸 `0`（显示成 `。`/`O`）反而读成空 ——
    画面没变，只是 rec 对裁剪边距敏感。这里只修正**整体偏移**。
    """
    med = [statistics.median(v[i] for v in boxes) for i in range(4)]
    cx, cy = (med[0] + med[2]) / 2, (med[1] + med[3]) / 2
    pw, ph = prior[2] - prior[0], prior[3] - prior[1]
    # 极小槽位仍然兜一个最小尺寸（老配置里没有的情况）
    w = max(pw, MIN_W.get(kind, 0.0))
    h = max(ph, MIN_H.get(kind, 0.0))
    return (max(0.0, cx - w / 2), max(0.0, cy - h / 2),
            min(1.0, cx + w / 2), min(1.0, cy + h / 2))


def _fmt_slot(slot) -> list[float]:
    return [round(v, 6) for v in slot]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path, help="裁剪后的帧目录")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--out", type=Path, default=Path("config/table_wpk_live.json"))
    ap.add_argument("--step", type=int, default=1,
                    help="每 N 帧取 1 帧做标注（默认 1；大目录可设 2/3）")
    ap.add_argument("--min-hits", type=int, default=5,
                    help="同一槽位至少命中几帧才更新（默认 5）")
    ap.add_argument("--expand", type=float, default=0.5,
                    help="在原槽位外扩多少（按槽位尺寸，默认 0.5）")
    ap.add_argument("--no-dealer-tune", action="store_true",
                    help="不调整庄位 blob 的亮度门槛")
    ap.add_argument("--report", type=Path, default=None,
                    help="标注明细 JSON（默认与 --out 同名的 .refit.json）")
    args = ap.parse_args()

    if not args.src.is_dir():
        ap.error(f"不是目录：{args.src}")
    files = sorted(p for p in args.src.iterdir()
                   if p.is_file() and p.suffix.lower() in SUFFIXES)
    if args.step > 1:
        files = files[::args.step]
    if not files:
        ap.error(f"{args.src} 里没有 jpg/png")

    prior = Profile.load(args.profile)
    engine = OcrEngine()

    # 待标注的槽位：座椅字段 + 全局字段
    targets: list[tuple[str, str, tuple]] = []
    for seat in prior.seats:
        for key, slot in (("name", seat.name_slot), ("chip", seat.chip_slot),
                          ("bet", seat.bet_slot)):
            if slot is not None:
                targets.append((seat.seat_id, key, slot))
    for key in ("pot", "pot_before_street", "blinds"):
        slot = prior.global_slots.get(key)
        if slot is not None:
            targets.append(("", key, slot))

    hits: dict[tuple[str, str], list[tuple[float, float, float, float]]] = {
        (sid, key): [] for sid, key, _ in targets
    }
    costs: dict[tuple[str, str], list[float]] = {k: [] for k in hits}
    sizes: Counter = Counter()
    print(f"frames={len(files)}（每 {args.step} 帧取 1）")
    print(f"待标注槽位={len(targets)}  min_hits={args.min_hits}")

    for i, path in enumerate(files):
        img = cv2.imread(str(path))
        if img is None:
            continue
        h, w = img.shape[:2]
        sizes[(w, h)] += 1
        boxes = engine.read_boxes(img)
        norm = [{"text": b.text.strip(),
                 "cx": b.cx / w, "cy": b.cy / h,
                 "x1": b.x1 / w, "y1": b.y1 / h,
                 "x2": b.x2 / w, "y2": b.y2 / h}
                for b in boxes if b.text.strip()]
        # 先筹码、再名字、最后下注：下注槽要排除本帧已经归给筹码/其他字段的框，
        # 否则 topL/topR 的下注槽会把正下方的筹码胶囊当成下注 pill。
        order = ([t for t in targets if t[1] == "chip"]
                 + [t for t in targets if t[1] == "name"]
                 + [t for t in targets if t[1] == "bet"]
                 + [t for t in targets if t[1] in ("pot", "pot_before_street")]
                 + [t for t in targets if t[1] == "blinds"])
        used: set[int] = set()
        for sid, key, slot in order:
            exclude = used if key in ("chip", "bet", "pot", "pot_before_street") else set()
            box, cost = _pick(norm, slot, key, args.expand, exclude)
            if box is None:
                continue
            hits[(sid, key)].append((box["x1"], box["y1"],
                                     box["x2"], box["y2"]))
            costs[(sid, key)].append(cost)
            used.add(id(box))
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(files)}")

    changes = []
    new_seats = []
    for seat in prior.seats:
        payload = {
            "seat_id": seat.seat_id,
            "ring_index": seat.ring_index,
            "name_slot": _fmt_slot(seat.name_slot),
            "chip_slot": _fmt_slot(seat.chip_slot),
            "avatar_slot": _fmt_slot(seat.avatar_slot),
            "bet_slot": _fmt_slot(seat.bet_slot) if seat.bet_slot else None,
            "action_slot": _fmt_slot(seat.action_slot) if seat.action_slot else None,
        }
        for key, old in (("name_slot", seat.name_slot),
                         ("chip_slot", seat.chip_slot),
                         ("bet_slot", seat.bet_slot)):
            if old is None:
                continue
            kind = key.split("_")[0]
            found = hits[(seat.seat_id, kind)]
            if len(found) < args.min_hits:
                continue
            new = _new_slot(found, old, kind)
            payload[key] = _fmt_slot(new)
            changes.append({
                "seat": seat.seat_id, "field": key,
                "hits": len(found), "old": _fmt_slot(old), "new": _fmt_slot(new),
                "median_cost": round(statistics.median(costs[(seat.seat_id, kind)]), 3),
            })
        if seat.chip_slot_alt is not None:
            payload["chip_slot_alt"] = _fmt_slot(seat.chip_slot_alt)
        new_seats.append(payload)

    new_globals = {}
    for key in ("pot", "pot_before_street", "blinds"):
        old = prior.global_slots.get(key)
        if old is None:
            continue
        found = hits[("", key)]
        if len(found) >= args.min_hits:
            new = _new_slot(found, old, key)
            new_globals[key] = _fmt_slot(new)
            changes.append({
                "seat": None, "field": key, "hits": len(found),
                "old": _fmt_slot(old), "new": _fmt_slot(new),
                "median_cost": round(statistics.median(costs[("", key)]), 3),
            })
        else:
            new_globals[key] = _fmt_slot(old)

    (w, h), _ = sizes.most_common(1)[0]
    raw = json.loads(prior.path.read_text(encoding="utf-8"))
    payload = dict(raw)
    payload["reference_size"] = [w, h]
    payload["canvas"] = None
    payload["seats"] = new_seats
    payload["global_slots"] = new_globals
    payload["_provenance"] = {
        "source": str(args.src),
        "method": ("全图 det+rec 的文本框按字段类型匹配到原槽位，"
                   "多帧取坐标中位数后加留白"),
        "frames": len(files),
        "step": args.step,
        "min_hits": args.min_hits,
        "changed_slots": len(changes),
        "date": "2026-10-19",
    }
    if not args.no_dealer_tune and payload.get("dealer", {}).get("method") == "blob":
        blob = payload["dealer"].setdefault("blob", {})
        # 这批帧的 D 圆片偏暗（白底不再是 >195），实测 170/60 能稳定检出；
        # 面积门槛按新旧参考面积比折算，保持归一化口径不变。
        old_w, old_h = prior.reference_size
        k2 = (w * h) / float(old_w * old_h)
        blob["v_min"] = 170
        blob["s_max"] = 60
        blob.setdefault("area_min", 675)
        blob.setdefault("area_max", 1462)
        if prior.reference_size != (w, h):
            blob["area_min"] = round(blob["area_min"] * k2, 1)
            blob["area_max"] = round(blob["area_max"] * k2, 1)
        blob["_comment"] = (
            f"白底圆片 + 深色 D。area_min/max 是**本配置参考分辨率 {w}x{h}** 上的"
            f"绝对像素，运行时按整图面积归一化。v_min/s_max 由 2026-10-19 抓帧标定。")
        blob["_derivation"] = (
            f"原 profile 在 1080x2400 上量得 area=[675,1462]；"
            f"按面积比 k²={k2:.4f} 折算到 {w}x{h} → "
            f"[{blob['area_min']}, {blob['area_max']}]。")
        payload["_dealer_tune"] = (
            f"v_min/s_max 由 195/45 放宽到 170/60；面积门槛按参考面积比 "
            f"{k2:.4f} 折算到 {w}x{h}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    report_path = args.report or args.out.with_name(args.out.stem + ".refit.json")
    report_path.write_text(json.dumps({
        "frames": len(files), "size": [w, h], "changes": changes,
        "hit_counts": {f"{sid}.{key}" if sid else key: len(v)
                       for (sid, key), v in hits.items()},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n改了 {len(changes)}/{len(targets)} 个槽位 → {args.out}")
    for c in changes:
        name = f"{c['seat']}.{c['field']}" if c["seat"] else c["field"]
        print(f"  {name:20} hits={c['hits']:3}  {c['old']} → {c['new']}")
    print(f"明细 → {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
