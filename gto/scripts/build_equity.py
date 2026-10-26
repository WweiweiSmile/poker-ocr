#!/usr/bin/env python3
"""牌面范围权益表（Monte Carlo，不需要求解器）。

回答的是多人池最需要的三个数：
  · PFR 范围对 1 / 2 / 3 个跟注者的**范围权益**
  · PFR 的**坚果占比**（唯一最大）
  · 随人数增加，牌面更偏向谁

用法：
    python gto/scripts/build_equity.py --samples 1200
    python gto/scripts/build_equity.py --boards 48 --samples 2000
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
from pathlib import Path

GTO = Path(__file__).resolve().parents[1]
ROOT = GTO.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.coach import STRAIGHT, TWO_PAIR, evaluate  # noqa: E402
from poker_ocr.preflop_ranges import expand  # noqa: E402

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = [(r, s) for r in range(13) for s in SUITS]


def combos_of(key: str) -> list[tuple[tuple[int, str], tuple[int, str]]]:
    a, b = key[0], key[1]
    ra, rb = RANKS.index(a), RANKS.index(b)
    if a == b:
        return [((ra, s1), (rb, s2)) for s1, s2 in itertools.combinations(SUITS, 2)]
    if key.endswith("s"):
        return [((ra, s), (rb, s)) for s in SUITS]
    return [((ra, s1), (rb, s2)) for s1 in SUITS for s2 in SUITS if s1 != s2]


def range_combos(text: str) -> list[tuple[tuple[int, str], tuple[int, str]]]:
    out = []
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        for key in expand(tok):
            out.extend(combos_of(key))
    # 去重
    return list({tuple(sorted(c)): c for c in out}.values())


def sample(rng: random.Random, combos, used: set) -> tuple:
    for _ in range(200):
        h = rng.choice(combos)
        if h[0] not in used and h[1] not in used:
            used.add(h[0])
            used.add(h[1])
            return h
    raise RuntimeError("cannot sample a non-conflicting hand")


def run_board(rng, board, pfr, caller, n_callers, samples):
    """PFR 对 n 个跟注者的范围权益。"""
    eq = 0.0
    board_cards = [(RANKS.index(c[0]), c[1]) for c in board]
    for _ in range(samples):
        used: set = set(board_cards)
        hands = [sample(rng, pfr, used)]
        for _ in range(n_callers):
            hands.append(sample(rng, caller, used))
        deck = [c for c in DECK if c not in used]
        runout = rng.sample(deck, 5 - len(board_cards))
        full = board_cards + runout
        scores = [evaluate(list(h) + full) for h in hands]
        best = max(scores)
        winners = [i for i, s in enumerate(scores) if s == best]
        if 0 in winners:
            eq += 1.0 / len(winners)
    return eq / samples


def strength_share(combos, board_cards):
    """当前牌面的强牌密度：两对+ / 顺子+ 各占范围多少。"""
    strong = 0
    nuts = 0
    for h in combos:
        cat, _ = evaluate(list(h) + board_cards)
        if cat >= TWO_PAIR:
            strong += 1
        if cat >= STRAIGHT:
            nuts += 1
    n = max(len(combos), 1)
    return strong / n, nuts / n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(GTO / "specs"))
    ap.add_argument("--out", default=str(GTO / "data" / "equity.json"))
    ap.add_argument("--boards", type=int, default=0, help="只取前 N 个牌面（0=全部）")
    ap.add_argument("--samples", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--pfr", default="", help="只算这个 PFR 位置（默认全部 SRP spot）")
    args = ap.parse_args()

    specs = Path(args.specs)
    spots = json.loads((specs / "spots.json").read_text(encoding="utf-8"))["spots"]
    boards = json.loads((specs / "boards.json").read_text(encoding="utf-8"))["boards"]
    if args.boards:
        boards = boards[: args.boards]
    if args.pfr:
        spots = [s for s in spots if s["ip_pos"] == args.pfr]
    print(f"spots {len(spots)} | boards {len(boards)} | samples {args.samples}")

    rng = random.Random(args.seed)
    out: dict = {}
    t0 = time.time()
    for spot in spots:
        pfr = range_combos(spot["ip_range"])
        caller = range_combos(spot["oop_range"])
        rows = {}
        for b in boards:
            flop = b["flop"]
            row = {}
            board_cards = [(RANKS.index(flop[i]), flop[i + 1]) for i in (0, 2, 4)]
            for n in (1, 2, 3):
                row[str(n)] = round(run_board(rng, [flop[0:2], flop[2:4], flop[4:6]], pfr, caller, n, args.samples), 4)
            row["pfr_strong"], row["pfr_nuts"] = [round(x, 4) for x in strength_share(pfr, board_cards)]
            row["caller_strong"], row["caller_nuts"] = [round(x, 4) for x in strength_share(caller, board_cards)]
            rows[flop] = row
        out[spot["id"]] = rows
        print(f"  {spot['id']}  {len(pfr)} combos  {time.time()-t0:.1f}s")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "version": 1,
        "samples": args.samples,
        "caller": "BB",
        "seed": args.seed,
        "spots": out,
    }, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {args.out} ({len(out)} spots × {len(boards)} boards, {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
