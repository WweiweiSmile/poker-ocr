#!/usr/bin/env python3
"""回放 live 日志，按证据源审计对手行动线。

为什么需要它：
  `hand_track` 现在给每条行动记 `source`（badge / avatar_fold / bet_delta /
  ledger）和 `order_unknown`。行动线改完是否达标，不能只看单测 —— 要拿真实
  日志整手回放，按来源拆分推断动作，并检查盲注位有没有把强制投入写成行动。

用法：

    # 默认回放 logs/live.mac.jsonl 里的 state 记录
    nv tools/action_line_audit.py

    # 输出机器可读的完整结果
    nv tools/action_line_audit.py --json

    # 逐手明细（手牌 id / 动作数 / 推断数 / 弃牌推断 / 盲注误记 / 顺序未知）
    nv tools/action_line_audit.py --hands

验收三条线（#28）：弃牌 inferred 0、总 inferred ≤25%、盲注误记 0。
没达标时报告里写明差距；`--hands` 用来抽 5 手人工核对顺序。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import BOMB_ANTE_BB, LiveHandTracker  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402

DEFAULT_LOG = ROOT / "logs" / "live.mac.jsonl"
SOURCES = ("badge", "avatar_fold", "bet_delta", "ledger")
INFERRED_SOURCES = ("bet_delta", "ledger")


def player_from_dict(d: dict) -> PlayerState:
    return PlayerState(
        seat_id=d.get("seat_id"),
        name=d.get("name"),
        position=d.get("position"),
        stack=d.get("stack"),
        bet=d.get("bet"),
        action=d.get("action"),
        fold=d.get("fold"),
        in_hand=bool(d.get("in_hand")),
        all_in=bool(d.get("all_in")),
        status=d.get("status") or "away",
        name_score=d.get("name_score") or 0.0,
    )


def frame_from_state(rec: dict) -> FrameState:
    """把 live 日志里的 state 记录还原成 tracker 吃的 FrameState。

    日志只记 hero + opponents（在局座位），正好是 tracker 需要的名单；
    `bet_unknown` / `name_raw` 这类诊断字段没有落盘，按缺省值还原。
    """
    blinds = rec.get("blinds")
    return FrameState(
        frame_id=rec.get("frame") or f"seq{rec.get('seq')}",
        table_seats=rec.get("table_seats") or 0,
        players_in_hand=rec.get("players_in_hand") or 0,
        players=[player_from_dict(rec.get("hero") or {})]
        + [player_from_dict(x) for x in rec.get("opponents") or []],
        blinds=(Blinds(blinds.get("sb"), blinds.get("bb"),
                       blinds.get("ante"), blinds.get("raw"))
                if blinds else None),
        blinds_source=blinds.get("source") if blinds else "frame",
        dealer=rec.get("dealer"),
        hero_seat_id=(rec.get("hero") or {}).get("seat_id") or "hero",
        hero_turn=rec.get("hero_turn"),
        pot=rec.get("pot"),
        pot_before_street=rec.get("pot_before_street"),
        street_bet_total=rec.get("street_bet_total"),
        errors=list(rec.get("errors") or []),
    )


def replay(log: Path, *, limit: int | None = None) -> tuple[list[dict], int, int]:
    """回放 state 记录，返回 (已结束手牌, state 数, 开始手数)。"""
    tracker = LiveHandTracker(confirm_frames=1)
    hands: list[dict] = []
    states = 0
    starts = 0
    original_start = tracker._start_hand

    def counting_start(*args, **kwargs):
        nonlocal starts
        starts += 1
        return original_start(*args, **kwargs)

    tracker._start_hand = counting_start  # type: ignore[method-assign]
    for line in log.open(encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") != "state":
            continue
        states += 1
        tracker.observe(frame_from_state(rec),
                        rec.get("hero_cards") or [],
                        rec.get("board") or [])
        hand = tracker.take_finished()
        if hand is not None:
            hands.append(hand)
        if limit is not None and states >= limit:
            break
    return hands, states, starts


def _forced_blind(action: dict, hand: dict) -> float:
    blinds = hand.get("blinds") or {}
    if action.get("position") == "SB":
        return float(blinds.get("sb") or 0.0)
    if action.get("position") == "BB":
        return float(blinds.get("bb") or 0.0)
    return 0.0


def blind_misrecords(hands: list[dict]) -> list[dict]:
    """翻前落在盲注线上的 call/bet/raise —— 那是盲注本身，不是行动。"""
    out: list[dict] = []
    for hand in hands:
        blinds = hand.get("blinds") or {}
        ante = float(blinds.get("ante") or 0.0)
        forced_ante = ante if ante >= BOMB_ANTE_BB else 0.0
        for street in hand.get("streets") or []:
            if street.get("street") != "preflop":
                continue
            for a in street.get("actions") or []:
                if a.get("position") not in ("SB", "BB"):
                    continue
                if a.get("action") not in ("call", "bet", "raise"):
                    continue
                amount = a.get("amount")
                if amount is None:
                    continue
                limit = _forced_blind(a, hand) + forced_ante
                if amount <= limit + 1e-9:
                    out.append({"hand_id": hand.get("hand_id"),
                                "seat_id": a.get("seat_id"),
                                "position": a.get("position"),
                                "action": a.get("action"),
                                "amount": amount, "limit": limit,
                                "source": a.get("source")})
    return out


def summarize(hands: list[dict]) -> dict:
    total = 0
    inferred = 0
    sources: Counter[str] = Counter()
    inferred_by_source: Counter[str] = Counter()
    folds = 0
    fold_inferred = 0
    fold_exit = 0
    order_unknown = 0
    for hand in hands:
        for street in hand.get("streets") or []:
            for a in street.get("actions") or []:
                total += 1
                source = a.get("source") or ""
                sources[source] += 1
                if a.get("inferred") or source in INFERRED_SOURCES:
                    inferred += 1
                    inferred_by_source[source] += 1
                if a.get("action") == "fold":
                    folds += 1
                    # 街末「人已不在局」的弃牌按口径算观测，不算漏抓；
                    # 这类记录目前仍是 source=ledger，所以单列统计。
                    if source == "ledger":
                        fold_exit += 1
                    elif a.get("inferred") or source in INFERRED_SOURCES:
                        fold_inferred += 1
                if a.get("order_unknown"):
                    order_unknown += 1
    blind = blind_misrecords(hands)
    inferred_effective = inferred - fold_exit
    return {
        "hands": len(hands),
        "actions": total,
        "inferred": inferred,
        "inferred_ratio": (inferred / total) if total else 0.0,
        "inferred_effective": inferred_effective,
        "inferred_ratio_effective": ((inferred_effective / total)
                                     if total else 0.0),
        "sources": dict(sources),
        "inferred_by_source": dict(inferred_by_source),
        "folds": folds,
        "fold_inferred": fold_inferred,
        "fold_exit": fold_exit,
        "order_unknown": order_unknown,
        "blind_misrecords": blind,
    }


def hand_rows(hands: list[dict]) -> list[dict]:
    rows = []
    for hand in hands:
        acts = [a for s in hand.get("streets") or []
                for a in s.get("actions") or []]
        inferred = sum(1 for a in acts
                       if a.get("inferred")
                       or a.get("source") in INFERRED_SOURCES)
        folds = [a for a in acts if a.get("action") == "fold"]
        rows.append({
            "hand_id": hand.get("hand_id"),
            "actions": len(acts),
            "inferred": inferred,
            "fold_inferred": sum(1 for a in folds
                                 if a.get("source") != "ledger"
                                 and (a.get("inferred")
                                      or a.get("source") in INFERRED_SOURCES)),
            "blind": sum(1 for m in blind_misrecords([hand])),
            "order_unknown": sum(1 for a in acts if a.get("order_unknown")),
        })
    return rows


def print_report(summary: dict, rows: list[dict], *, show_hands: bool) -> None:
    print(f"回放手牌：{summary['hands']}")
    print(f"行动记录：{summary['actions']}（推断 {summary['inferred']}，"
          f"{summary['inferred_ratio']:.1%}；剔除街末不在局弃牌后 "
          f"{summary['inferred_effective']}，"
          f"{summary['inferred_ratio_effective']:.1%}）")
    print("按来源：" + "，".join(
        f"{s}={summary['sources'].get(s, 0)}"
        f"（推断 {summary['inferred_by_source'].get(s, 0)}）" for s in SOURCES))
    print(f"弃牌：{summary['folds']}（漏抓 {summary['fold_inferred']}，"
          f"街末不在局 {summary['fold_exit']}）")
    print(f"顺序未知：{summary['order_unknown']}")
    blind = summary["blind_misrecords"]
    print(f"盲注误记：{len(blind)}")
    for m in blind[:10]:
        print(f"  · {m['hand_id']} {m['seat_id']}/{m['position']} "
              f"{m['action']} @ {m['amount']}（≤{m['limit']}，{m['source']}）")
    if show_hands:
        print("\n逐手：")
        for row in rows:
            print(f"  {row['hand_id']}  动作 {row['actions']:>2}  "
                  f"推断 {row['inferred']:>2}  弃牌推断 {row['fold_inferred']:>2}  "
                  f"盲注 {row['blind']}  顺序未知 {row['order_unknown']}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG,
                    help="live JSONL（读 type=state 记录）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--hands", action="store_true", help="逐手明细")
    ap.add_argument("--limit", type=int, default=None, help="最多回放几个 state")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.log.exists():
        print(f"找不到日志：{args.log}", file=sys.stderr)
        return 2
    hands, states, starts = replay(args.log, limit=args.limit)
    summary = summarize(hands)
    rows = hand_rows(hands)
    if args.json:
        print(json.dumps({**summary, "states": states, "hand_starts": starts,
                          "hand_rows": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"state 记录：{states}，开手 {starts}，结束 {len(hands)}")
    print_report(summary, rows, show_hands=args.hands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
