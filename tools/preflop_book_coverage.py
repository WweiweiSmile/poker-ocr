#!/usr/bin/env python3
"""翻前 replay 覆盖统计：按卡归因命中，并检查每张卡能否落到本桌 options（票 #87）。

    nv tools/preflop_book_coverage.py
    nv tools/preflop_book_coverage.py --json logs/preflop_coverage.json

只读 replay 库，不调模型、不跑 A/B：

  · 决策点按 (hand_id, street, seq) 去重，同一决策点优先取 quality=exact、其次
    取 id 最大的快照（不是复盘控制台 `_dedupe_source_decisions` 的源优先级
    口径——这里只统计「书卡能命中多少点」，不参与复盘评分）；
  · 局面用 `Spot.from_situation` 归桶（与提示词同一条链路）；
  · 命中数按卡 id 统计，「新增卡可归因」直接看这张表；
  · 每张命中的卡都过 `choose_option`：返回 None 说明卡的动作在本桌 options
    里不存在。这类点分成两种，不混算：
      - options 快照缺动作：options 里根本没有该动作档（采集/OCR 缺口，
        与卡无关，单独计数）；
      - 选档失败：options 有该动作档却仍选不出（真 bug，必须为 0）。
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "replay-console"
for path in (str(ROOT), str(CONSOLE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from poker_ocr.playbook import Spot, choose_option, resolve  # noqa: E402
from poker_ocr.playbook.options import kinds_for_action  # noqa: E402
from poker_ocr.playbook.prompt import _all_cards  # noqa: E402
from poker_ocr.situation import build_situation  # noqa: E402
from replay.config import Settings  # noqa: E402
from replay.db import Database  # noqa: E402
from replay.decisions import snapshot_from_json  # noqa: E402

_SQL = """
    SELECT id, hand_id, street, seq, quality, snapshot_json
      FROM replay_decisions
     ORDER BY id
"""

def _dedup(rows) -> list[dict]:
    grouped: dict[tuple, list[dict]] = collections.defaultdict(list)
    for row in rows:
        grouped[(str(row["hand_id"]), row["street"],
                 int(row["seq"] or 0))].append(row)
    return [sorted(items, key=lambda r: (r["quality"] != "exact", -int(r["id"])))[0]
            for items in grouped.values()]


def _action_available(options, action: str) -> bool:
    """本桌 options 里有没有能承载该动作的档（判定规则与 choose_option 同源）。"""
    wanted = kinds_for_action(action)
    if not wanted:
        return True
    kinds = {getattr(option, "kind", None) for option in options or ()}
    return bool(kinds & set(wanted))


def main() -> int:
    parser = argparse.ArgumentParser(description="翻前 replay 书卡覆盖统计")
    parser.add_argument("--json", type=Path, default=None,
                        help="把统计写成 JSON（含逐卡命中）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只看前 N 个去重后的翻前决策点（0 = 全部）")
    args = parser.parse_args()

    settings = Settings.from_env()
    db = Database(settings)
    try:
        rows = db.query(_SQL)
    finally:
        db.close()

    cards = _all_cards()
    preflop = [row for row in _dedup(rows) if row["street"] == "preflop"]
    if args.limit:
        preflop = preflop[: args.limit]

    total = 0
    matched = 0
    build_errors = 0
    buckets: dict[tuple, list[int]] = collections.defaultdict(lambda: [0, 0])
    hits: collections.Counter = collections.Counter()
    missing_action: collections.Counter = collections.Counter()
    pick_failed: collections.Counter = collections.Counter()

    for row in preflop:
        total += 1
        try:
            snap = snapshot_from_json(row["snapshot_json"])
            sit = build_situation(snap, include_strength=False)
        except Exception:                     # noqa: BLE001 - 统计不因坏行中断
            build_errors += 1
            continue
        spot = Spot.from_situation(sit)
        bucket = (spot.preflop_node, spot.facing)
        buckets[bucket][0] += 1
        resolution = resolve(cards, spot)
        card = resolution.card
        if card is None:
            continue
        matched += 1
        buckets[bucket][1] += 1
        hits[card.id] += 1
        match = choose_option(sit.options, card.plan,
                              pot_bb=float(getattr(snap, "pot", 0) or 0),
                              to_call_bb=float(getattr(snap, "to_call", 0) or 0))
        if match is not None:
            continue
        if _action_available(sit.options, card.plan.action):
            pick_failed[card.id] += 1
        else:
            missing_action[card.id] += 1

    print(f"翻前决策点（去重后）: {total}")
    print(f"命中书卡: {matched} ({matched / total:.1%})" if total else "命中书卡: 0")
    print(f"书中无此局面: {total - matched}，崩溃/坏行: {build_errors}")
    print()
    print("== 主桶覆盖 ==")
    for (node, facing), (seen, hit) in sorted(
            buckets.items(), key=lambda item: -item[1][0]):
        rate = f"{hit / seen:.1%}" if seen else "-"
        print(f"{node}/{facing:<5} {hit:4d}/{seen:<4d} {rate}")
    print()
    print("== 命中归因（按卡） ==")
    for card_id, count in hits.most_common():
        print(f"{count:4d}  {card_id}")
    print()
    print(f"选项里没有该动作档（采集缺口，不计卡错）: {sum(missing_action.values())}")
    for card_id, count in missing_action.most_common():
        print(f"  {count:4d}  {card_id}")
    print(f"选档失败（必须为 0）: {sum(pick_failed.values())}")
    for card_id, count in pick_failed.most_common():
        print(f"  {count:4d}  {card_id}")

    if args.json:
        payload = {
            "total": total,
            "matched": matched,
            "unknown": total - matched,
            "build_errors": build_errors,
            "buckets": [
                {"node": node, "facing": facing, "seen": seen, "hit": hit}
                for (node, facing), (seen, hit) in sorted(buckets.items())
            ],
            "hits": dict(hits),
            "missing_action": dict(missing_action),
            "pick_failed": dict(pick_failed),
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print(f"JSON -> {args.json}")

    return 0 if not pick_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
