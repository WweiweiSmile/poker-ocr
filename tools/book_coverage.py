#!/usr/bin/env python3
"""replay 书卡覆盖统计：按卡归因命中，并检查每张卡能否落到本桌 options（票 #83）。

    nv tools/book_coverage.py                     # 默认 flop + turn
    nv tools/book_coverage.py --streets flop,river
    nv tools/book_coverage.py --streets all --json logs/book_coverage.json

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

默认只统计 flop + turn：这是转牌缺口卡（#83）要看的范围；翻前旧入口
`tools/preflop_book_coverage.py` 是 `--streets preflop` 的薄包装。
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
from poker_ocr.playbook.options import action_available  # noqa: E402
from poker_ocr.playbook.prompt import _all_cards  # noqa: E402
from poker_ocr.situation import build_situation  # noqa: E402
from replay.config import Settings  # noqa: E402
from replay.db import Database  # noqa: E402
from replay.decisions import snapshot_from_json  # noqa: E402

STREETS = ("preflop", "flop", "turn", "river")

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


def _bucket(spot: Spot) -> tuple:
    """统计桶：翻前看节点/面对，翻后看街/牌力/听牌/面对。"""
    if spot.street == "preflop":
        return "preflop", (spot.preflop_node, spot.facing)
    return spot.street, (spot.made_hand, spot.draw, spot.facing)


def _parse_streets(raw: str) -> tuple[str, ...]:
    if raw.strip().lower() == "all":
        return STREETS
    wanted = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    bad = [street for street in wanted if street not in STREETS]
    if bad or not wanted:
        raise SystemExit(
            f"--streets 只能是 {','.join(STREETS)} 或 all，收到: {raw!r}")
    return wanted


def main(default_streets: str = "flop,turn") -> int:
    parser = argparse.ArgumentParser(description="replay 书卡覆盖统计")
    parser.add_argument("--streets", default=default_streets,
                        help=f"逗号分隔的街，或 all（默认 {default_streets}）")
    parser.add_argument("--json", type=Path, default=None,
                        help="把统计写成 JSON（含逐卡命中）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只看前 N 个去重后的决策点（0 = 全部）")
    args = parser.parse_args()
    streets = _parse_streets(args.streets)

    settings = Settings.from_env()
    db = Database(settings)
    try:
        rows = db.query(_SQL)
    finally:
        db.close()

    cards = _all_cards()
    selected = [row for row in _dedup(rows) if row["street"] in streets]
    if args.limit:
        selected = selected[: args.limit]

    total = 0
    matched = 0
    build_errors = 0
    by_street: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    buckets: dict[tuple, list[int]] = collections.defaultdict(lambda: [0, 0])
    hits: collections.Counter = collections.Counter()
    missing_action: collections.Counter = collections.Counter()
    pick_failed: collections.Counter = collections.Counter()

    for row in selected:
        total += 1
        street = row["street"]
        by_street[street][0] += 1
        try:
            snap = snapshot_from_json(row["snapshot_json"])
            sit = build_situation(snap, include_strength=False)
        except Exception:                     # noqa: BLE001 - 统计不因坏行中断
            build_errors += 1
            continue
        spot = Spot.from_situation(sit)
        bucket_street, bucket_key = _bucket(spot)
        buckets[(bucket_street, bucket_key)][0] += 1
        resolution = resolve(cards, spot)
        card = resolution.card
        if card is None:
            continue
        matched += 1
        by_street[street][1] += 1
        buckets[(bucket_street, bucket_key)][1] += 1
        hits[card.id] += 1
        match = choose_option(sit.options, card.plan,
                              pot_bb=float(getattr(snap, "pot", 0) or 0),
                              to_call_bb=float(getattr(snap, "to_call", 0) or 0))
        if match is not None:
            continue
        if action_available(sit.options, card.plan.action):
            pick_failed[card.id] += 1
        else:
            missing_action[card.id] += 1

    print(f"决策点（去重后，{','.join(streets)}）: {total}")
    print(f"命中书卡: {matched} ({matched / total:.1%})" if total else "命中书卡: 0")
    print(f"书中无此局面: {total - matched}，崩溃/坏行: {build_errors}")
    print()
    print("== 分街覆盖 ==")
    for street in streets:
        seen, hit = by_street.get(street, [0, 0])
        rate = f"{hit / seen:.1%}" if seen else "-"
        print(f"{street:<8} {hit:4d}/{seen:<4d} {rate}")
    print()
    print("== 桶覆盖 ==")
    for (street, key), (seen, hit) in sorted(
            buckets.items(), key=lambda item: (-item[1][0], item[0])):
        rate = f"{hit / seen:.1%}" if seen else "-"
        print(f"{street:<8} {'/'.join(str(part) for part in key):<40} "
              f"{hit:4d}/{seen:<4d} {rate}")
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
            "streets": list(streets),
            "total": total,
            "matched": matched,
            "unknown": total - matched,
            "build_errors": build_errors,
            "by_street": [
                {"street": street, "seen": seen, "hit": hit}
                for street, (seen, hit) in sorted(by_street.items())
            ],
            "buckets": [
                {"street": street, "key": list(key), "seen": seen, "hit": hit}
                for (street, key), (seen, hit) in sorted(buckets.items())
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
