#!/usr/bin/env python3
"""confidence 可靠性曲线：模型自评置信度 vs 实际摊牌结果。

置信度只有配上可靠性表才有意义：0.7 到底代表多少次「对」。本工具把
`logs/live.states.jsonl` 里的 advice 记录和同一手的摊牌信息（`shows`）
按 hand_id 关联，用「这手最后谁赢」当代理标签，输出分桶命中率。

标签口径（代理，不是 GTO 正确性）：

  · 建议弃牌 → 这手最后 Hero 输 = 命中（弃对了）；
  · 建议跟注/过牌/下注/加注 → 这手最后 Hero 赢 = 命中。

只在「Hero 底牌 + 公共牌 5 张 + 至少一名对手摊牌」齐全时才计入，
缺数据的建议直接跳过（不猜）。用 `--min-samples` 过滤样本过少的桶。

用法：

    nv tools/confidence_calibration.py
    nv tools/confidence_calibration.py logs/live.mac.jsonl --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from export_live_hands import load_hands  # noqa: E402
from poker_ocr.coach import evaluate, parse_cards  # noqa: E402
from poker_ocr.opponent_profile import showdown_reads  # noqa: E402

DEFAULT_INPUT = Path("logs/live.states.jsonl")
BUCKETS = ((0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001))


def _hand_key(cards: list[str]) -> tuple[int, list[int]] | None:
    """五张/七张牌 → 可比较的 (category, kickers)。读不出返回 None。"""
    text = "".join(str(card) for card in cards)
    try:
        return evaluate(parse_cards(text))
    except (ValueError, IndexError):
        return None


def _showdown_result(hand: dict) -> bool | None:
    """Hero 是否赢下摊牌；数据不齐返回 None（不猜）。"""
    hole = hand.get("hero_cards") or []
    board = hand.get("board") or []
    # #20：先过 clean 闸门 —— 弃牌座位上的亮牌是串到下一手的残影。
    clean = {r.seat_id for r in showdown_reads(hand) if r.clean}
    shows = [s for s in hand.get("shows") or []
             if str(s.get("seat_id") or "") in clean]
    if len(hole) != 2 or len(board) != 5 or not shows:
        return None
    hero = _hand_key([*hole, *board])
    if hero is None:
        return None
    for shown in shows:
        cards = shown.get("cards") or []
        if len(cards) != 2:
            continue
        theirs = _hand_key([*cards, *board])
        if theirs is None:
            continue
        if theirs > hero:
            return False
    return True


def _bucket(confidence: float) -> tuple[float, float]:
    for low, high in BUCKETS:
        if low <= confidence < high:
            return low, high
    return BUCKETS[-1]


def _is_fold(option: dict) -> bool:
    return str(option.get("kind") or option.get("id") or "") == "fold"


def collect(input_path: Path) -> tuple[list[dict], dict]:
    """读日志，返回 (每条可用建议的 {confidence, hit}, 统计)。"""
    hands, _stats, _bad = load_hands(
        input_path, include_partial=True, allow_mid_hand=True)
    winners: dict[str, bool] = {}
    for hand in hands:
        result = _showdown_result(hand)
        if result is not None:
            winners[hand.get("hand_id")] = result

    rows: list[dict] = []
    stats = {"advice_rows": 0, "with_confidence": 0,
             "joined_showdown": 0, "skipped_no_confidence": 0,
             "skipped_no_result": 0, "skipped_fallback": 0}

    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "advice":
                continue
            stats["advice_rows"] += 1
            if record.get("source") != "model":
                stats["skipped_fallback"] += 1
                continue
            confidence = record.get("confidence")
            if not isinstance(confidence, (int, float)):
                stats["skipped_no_confidence"] += 1
                continue
            stats["with_confidence"] += 1
            hero_won = winners.get(record.get("hand_id"))
            if hero_won is None:
                stats["skipped_no_result"] += 1
                continue
            option = record.get("option") or {}
            hit = (not hero_won) if _is_fold(option) else hero_won
            rows.append({"confidence": float(confidence), "hit": bool(hit)})
            stats["joined_showdown"] += 1
    return rows, stats


def bucket_table(rows: list[dict], *, min_samples: int = 1) -> list[dict]:
    out: list[dict] = []
    for low, high in BUCKETS:
        bucket = [r for r in rows if low <= r["confidence"] < high]
        if len(bucket) < min_samples:
            continue
        hits = sum(1 for r in bucket if r["hit"])
        mean_conf = sum(r["confidence"] for r in bucket) / len(bucket)
        out.append({
            "bucket": f"{low:.1f}–{min(high, 1.0):.1f}",
            "n": len(bucket),
            "hit_rate": round(hits / len(bucket), 3),
            "mean_confidence": round(mean_conf, 3),
            "gap": round(mean_conf - hits / len(bucket), 3),
        })
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="confidence 分桶可靠性表")
    ap.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--json", type=Path, default=None,
                    help="把分桶表写成 JSON（默认只打印表格）")
    ap.add_argument("--min-samples", type=int, default=1,
                    help="样本少于该值的桶不显示（默认 1）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.exists():
        print(f"找不到日志：{args.input}", file=sys.stderr)
        return 2
    rows, stats = collect(args.input)
    table = bucket_table(rows, min_samples=max(1, args.min_samples))
    print(f"日志：{args.input}")
    print(f"建议记录 {stats['advice_rows']} 条｜有 confidence "
          f"{stats['with_confidence']}｜能关联摊牌 {stats['joined_showdown']}"
          f"｜无结果跳过 {stats['skipped_no_result']}")
    if not table:
        print("没有可校准的样本（需要 confidence + 摊牌结果都齐全）。")
        return 0
    print(f"\n{'置信度桶':<12}{'样本':>6}{'命中率':>9}{'平均置信':>10}{'偏差':>9}")
    for row in table:
        print(f"{row['bucket']:<12}{row['n']:>6}{row['hit_rate']:>9.1%}"
              f"{row['mean_confidence']:>10.1%}{row['gap']:>+9.1%}")
    print("\n偏差 = 平均置信度 − 命中率；正得越多说明模型越过度自信。")
    if args.json is not None:
        args.json.write_text(json.dumps(
            {"stats": stats, "buckets": table}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"已写出：{args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
