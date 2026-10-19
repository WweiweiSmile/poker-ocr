#!/usr/bin/env python3
"""全图 OCR 座位聚类探针（M3.2）。

    .venv/bin/python tools/full_table_probe.py table-8p.jpg --out /tmp/full.png

不依赖固定 ROI/profile：全图 OCR → 名字/筹码聚类 → D 庄位 → 位置。
输出 JSON + 一张叠加图（座位中心、名字、筹码、位置、庄位、桌心）。

调参用 `--config` 指向一份 JSON，字段与 `full_table.FullTableConfig` 同名。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.full_table import FullTableConfig, observe_full  # noqa: E402
from poker_ocr.ocr import OcrEngine  # noqa: E402


def load_config(path: Path | None) -> FullTableConfig:
    if path is None:
        return FullTableConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    valid = {f.name for f in dataclasses.fields(FullTableConfig)}
    unknown = set(raw) - valid
    if unknown:
        sys.exit(f"配置里有未知字段：{sorted(unknown)}")
    if "table_center" in raw and raw["table_center"] is not None:
        raw["table_center"] = tuple(raw["table_center"])
    if "region_y" in raw:
        raw["region_y"] = tuple(raw["region_y"])
    if "dealer_ref_size" in raw:
        raw["dealer_ref_size"] = tuple(raw["dealer_ref_size"])
    if "ui_words" in raw:
        raw["ui_words"] = frozenset(raw["ui_words"])
    return FullTableConfig(**raw)


def render(img, result) -> None:
    h, w = img.shape[:2]
    cx, cy = int(result.table_center[0] * w), int(result.table_center[1] * h)
    cv2.drawMarker(img, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 20, 2)
    cv2.putText(img, "table", (cx + 6, cy - 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
    for s in result.seats:
        x, y = int(s.center[0] * w), int(s.center[1] * h)
        color = (0, 255, 255) if s.is_dealer else (0, 255, 0)
        cv2.circle(img, (x, y), 8, color, 2)
        label = f"{s.seat_id} {s.position or '-'} {s.name or '?'}"
        cv2.putText(img, label, (x + 10, y - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        if s.stack is not None:
            cv2.putText(img, f"{s.stack:g}", (x + 10, y + 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        if s.angle:
            cv2.putText(img, f"{s.angle:.0f}", (x + 10, y + 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
    if result.dealer_center:
        dx, dy = int(result.dealer_center[0] * w), int(result.dealer_center[1] * h)
        cv2.drawMarker(img, (dx, dy), (255, 0, 255), cv2.MARKER_DIAMOND, 16, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", type=Path)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="叠加图输出路径")
    ap.add_argument("--json", type=Path, default=None, help="JSON 输出路径")
    ap.add_argument("--no-debug", action="store_true", help="JSON 不带原始文本框")
    ap.add_argument("--direction", choices=("cw", "ccw"), default=None)
    ap.add_argument("--table-center", default=None,
                    help="人工指定桌心 x,y（归一化，如 0.5,0.45）")
    args = ap.parse_args()

    img = cv2.imread(str(args.image))
    if img is None:
        sys.exit(f"读不了图片：{args.image}")
    cfg = load_config(args.config)
    if args.direction:
        cfg = dataclasses.replace(cfg, direction=args.direction)
    if args.table_center:
        x, y = (float(v) for v in args.table_center.split(","))
        cfg = dataclasses.replace(cfg, table_center=(x, y))

    engine = OcrEngine()
    result = observe_full(img, engine, cfg)
    payload = result.to_dict(include_debug=not args.no_debug)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")

    render(img, result)
    out = args.out or Path("full_table_overlay.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    print(f"\n叠加图 → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
