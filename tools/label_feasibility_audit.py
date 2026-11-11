#!/usr/bin/env python3
"""打法标签的数据可得性体检（只读，不写任何产品数据）。

回答一个问题：**在现有手牌记录里，每个候选标签有多少玩家能达到最小机会数？**
标签的门槛是 `n>=8`（与 `opponent_profile.TENDENCY_MIN_OPPS` 同口径），
达不到的标签不该做——不是代码难，是数据撑不住。

    # 默认读实时日志
    nv tools/label_feasibility_audit.py

    # 读导出的手牌库 / 指定门槛
    nv tools/label_feasibility_audit.py \
        --hands hands/1023.hands.json --min-opps 8

    # 机器可读，方便攒够样本后对比
    nv tools/label_feasibility_audit.py --json

统计口径**直接复用** `poker_ocr.opponent_profile.observations_from_hand`：
标签最终就是从这些观察聚合出来的，体检用另一套逻辑算出来的数字没有意义。
只有 iso-raise（面对溜入的隔离加注）和 BB 走牌（walk）目前不在
`HandObservation` 里，由本脚本单独解析，并已在文档里标为「待补采」。

输出两列关键数字：
  · ≥N 人数：有多少玩家的机会数达到门槛（决定标签能不能做）
  · 每百手：该局面在每 100 手里出现多少次（决定要攒多久）
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.opponent_profile import (  # noqa: E402
    observations_from_hand, position_bucket,
)

DEFAULT_HANDS = Path("logs/live.states.jsonl")
VOLUNTARY = ("call", "bet", "raise")


# ─────────────────────────── 载入 ───────────────────────────

def load_hands(path: Path) -> list[dict]:
    """JSONL（只取 type==hand）或 `{"hands": [...]}` / 顶层数组。"""
    if not path.exists():
        sys.exit(f"找不到 {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "hand":
                out.append(record)
        return out
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("hands") or []
    return [h for h in data if isinstance(h, dict)]


# ─────────────────────── 模块外的两个局面 ───────────────────────

def _seat_positions(hand: dict) -> dict[str, str]:
    return {str(e.get("seat_id")): str(e.get("position") or "").upper()
            for e in (hand.get("seat_meta") or []) if e.get("position")}


def _first_action_index(actions: list[dict]) -> dict[str, int]:
    first: dict[str, int] = {}
    for i, action in enumerate(actions):
        first.setdefault(str(action.get("seat_id")), i)
    return first


def iso_raise_spot(hand: dict) -> dict[tuple[str, str], tuple[int, bool]]:
    """面对溜入的隔离加注：前面有人跟注、且还没有人加注。

    这是「抢底池」在松桌里最常出现的形态（本池 28 次/百手），
    但 `HandObservation` 目前没有这个字段。
    """
    streets = {s.get("street"): (s.get("actions") or [])
               for s in (hand.get("streets") or [])}
    preflop = streets.get("preflop") or []
    if not preflop:
        return {}
    first = _first_action_index(preflop)
    positions = _seat_positions(hand)
    out: dict[tuple[str, str], tuple[int, bool]] = {}
    for action in preflop:
        seat = str(action.get("seat_id"))
        if seat == "hero" or not action.get("name"):
            continue
        index = first.get(seat, len(preflop))
        prior = preflop[:index]
        limps = sum(1 for a in prior if a.get("action") == "call")
        raised = any(a.get("action") in ("bet", "raise") for a in prior)
        if limps < 1 or raised:
            continue
        position = positions.get(seat, "")
        bucket = ("盲注" if position in ("SB", "BB")
                  else position_bucket(position))
        hit = action.get("action") in ("bet", "raise")
        out[(str(action["name"]), bucket)] = (1, hit)
    return out


def bb_walk_seats(hand: dict) -> set[str]:
    """全员弃牌到 BB、BB 没有行动 —— 这不是「开池机会」，是走牌。

    现有 `open_opp` 会把它算成「有机会没开」，系统性拉低 BB 的开池率。
    """
    streets = {s.get("street"): (s.get("actions") or [])
               for s in (hand.get("streets") or [])}
    preflop = streets.get("preflop") or []
    if not preflop:
        return set()
    positions = _seat_positions(hand)
    actors = {str(a.get("seat_id")) for a in preflop}
    out = set()
    for seat, position in positions.items():
        if position != "BB" or seat in actors:
            continue
        if all(a.get("action") == "fold" for a in preflop):
            out.add(seat)
    return out


# ─────────────────────────── 统计 ───────────────────────────

def audit(hands: list[dict], min_opps: int) -> dict:
    # spot → 玩家 → [机会, 命中]
    spots: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0]))
    hands_by_player: Counter[str] = Counter()
    misc: Counter[str] = Counter()

    def add(spot: str, name: str, hit: bool) -> None:
        if not name:
            return
        cell = spots[spot][name]
        cell[0] += 1.0
        if hit:
            cell[1] += 1.0

    for hand in hands:
        misc["hands"] += 1
        observations = observations_from_hand(hand)
        if not observations:
            misc["no_preflop"] += 1
        for obs in observations:
            name = obs.opponent_key
            hands_by_player[name] += 1
            position = (obs.position or "")
            bucket = ("盲注" if position.upper() in ("SB", "BB")
                      else position_bucket(position))
            if obs.open_opp:
                add("preflop.unopened.any", name, obs.opened)
                add(f"preflop.unopened.{position.upper()}", name, obs.opened)
                add(f"preflop.unopened.{bucket}", name, obs.opened)
            if obs.threebet_opp:
                add("preflop.facing_raise.3bet", name, obs.threebet)
            if obs.limp_opp:
                add("preflop.unopened.limp", name, obs.limped)
            if obs.cbet_opp:
                add("postflop.cbet", name, obs.cbet)
            if obs.barrel_opp:
                add("postflop.barrel", name, obs.barrel)
            if obs.stab_opp:
                add("postflop.stab.any", name, obs.stab)
            if obs.check_raise_opp:
                add("postflop.check_raise", name, obs.check_raise)
            if obs.ftc_opp:
                add("postflop.fold_to_cbet", name, obs.ftc_fold)
                add("postflop.call_to_cbet", name, obs.ftc_call)
            if obs.f3b_opp:
                add("preflop.fold_to_3bet", name, obs.f3b_fold)
            for street in ("flop", "turn", "river"):
                if obs.face_opp.get(street):
                    add(f"postflop.facing_bet.{street}", name,
                        bool(obs.face_fold.get(street)))
            if obs.showdown_category is not None:
                add("showdown.any", name, True)
                if obs.aggressive_showdown:
                    # 弱牌激进摊牌：主动下注但摊牌只有高牌/一对
                    add("showdown.weak_aggressive", name,
                        obs.showdown_category <= 1)

        # 模块外的两个局面
        for (name, bucket), (_one, hit) in iso_raise_spot(hand).items():
            add("preflop.facing_limp.iso", name, hit)
        for seat in bb_walk_seats(hand):
            misc["bb_walk"] += 1
            observations_by_seat = {o.seat_id: o for o in observations}
            obs = observations_by_seat.get(seat)
            if obs is not None:
                add("preflop.unopened.walk", obs.opponent_key, True)

    return {"spots": spots, "hands_by_player": hands_by_player, "misc": misc}


def summarize(result: dict, min_opps: int) -> list[dict]:
    spots = result["spots"]
    hands_by_player = result["hands_by_player"]
    rows: list[dict] = []
    for spot in sorted(spots):
        per = spots[spot]
        opps = [cell[0] for cell in per.values() if cell[0] > 0]
        if not opps:
            continue
        # 每百手出现次数：只取 >=50 手的玩家，避免小样本把中位数拉爆
        rates = [cell[0] / hands_by_player[name] * 100
                 for name, cell in per.items()
                 if hands_by_player[name] >= 50 and cell[0] > 0]
        rate = statistics.median(rates) if rates else 0.0
        rows.append({
            "spot": spot,
            "players": len(per),
            "median_opps": statistics.median(opps),
            "players_ge": sum(1 for o in opps if o >= min_opps),
            "players_ge_6": sum(1 for o in opps if o >= 6),
            "players_ge_15": sum(1 for o in opps if o >= 15),
            "per_100_hands": round(rate, 1),
            "hands_for_min": (round(min_opps / rate * 100)
                              if rate > 0 else None),
        })
    return rows


def print_report(rows: list[dict], result: dict, min_opps: int) -> None:
    misc = result["misc"]
    print(f"手牌 {misc['hands']} 手｜无翻前 {misc['no_preflop']} 手｜"
          f"BB 走牌 {misc['bb_walk']} 手｜"
          f"玩家 {len(result['hands_by_player'])} 人")
    print(f"\n{'局面键':34s}{'玩家':>5s}{'中位机会':>9s}"
          f"{'≥6':>5s}{'≥N':>5s}{'≥15':>5s}{'每百手':>8s}{'攒N次需':>9s}")
    print("-" * 88)
    for row in rows:
        need = (f"{row['hands_for_min']}手" if row["hands_for_min"]
                else "—")
        print(f"{row['spot']:34s}{row['players']:5d}"
              f"{row['median_opps']:9.1f}{row['players_ge_6']:5d}"
              f"{row['players_ge']:5d}{row['players_ge_15']:5d}"
              f"{row['per_100_hands']:8.1f}{need:>9s}")
    print(f"\nN = {min_opps}（标签最小机会数，与 TENDENCY_MIN_OPPS 同口径）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hands", type=Path, default=DEFAULT_HANDS,
                        help=f"手牌 JSONL/JSON（默认 {DEFAULT_HANDS}）")
    parser.add_argument("--min-opps", type=int, default=8,
                        help="标签最小机会数（默认 8）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    result = audit(load_hands(args.hands), args.min_opps)
    rows = summarize(result, args.min_opps)
    if args.json:
        print(json.dumps({"min_opps": args.min_opps,
                          "misc": dict(result["misc"]),
                          "spots": rows}, ensure_ascii=False, indent=1))
        return
    print_report(rows, result, args.min_opps)


if __name__ == "__main__":
    main()
