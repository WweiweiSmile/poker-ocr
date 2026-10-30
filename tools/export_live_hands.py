#!/usr/bin/env python3
"""从实时识别日志 live.states.jsonl 里筛出完整手牌，写成 hands.json。

用法：

    .venv/bin/python tools/export_live_hands.py
    .venv/bin/python tools/export_live_hands.py logs/live.states.jsonl -o hands.json
    .venv/bin/python tools/export_live_hands.py --require-hero-cards --require-actions
    .venv/bin/python tools/export_live_hands.py --mid-hand --include-partial

默认的「完整」口径（同时满足）：

  1. `type == "hand"`；
  2. 没有 `"partial": true` —— partial 是退出时最后一手还没换手、被截断的记录；
  3. 第一条街是 `preflop` —— 从翻前开始观察到的一手。程序中途接管、第一条街
     已经是 flop/turn/river 的记录不算完整；确实要时加 `--mid-hand`。

每条记录原样保留，只追加两个字段：

  · `session`：第几个 `session_start`（1 起；日志里没有 session_start 时为 0）
  · `uid`：`s{session:02d}-{hand_id}`。`hand_id` 现在是「日期 + 名单摘要」，
    跨会话通常已经唯一；再加 session 前缀是为了兼容旧日志/极端碰撞。

输出是 JSON 数组（不是 JSONL），保留原字段与原顺序。没有 hand 记录时输出 `[]`。
注意：`--no-ai-advice` 时不会写 hand 记录，这个脚本自然也只能得到空数组。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_INPUT = Path("logs/live.states.jsonl")
DEFAULT_OUTPUT = Path("hands.json")

# 「接管/中途加入」的判定：第一条街不是 preflop。翻前开始的空行动手（走牌/
# 全部弃牌）仍然是完整一手，不能因为 actions 为空就丢掉。
FIRST_STREET = "preflop"


def _streets(hand: dict) -> list:
    streets = hand.get("streets")
    return streets if isinstance(streets, list) else []


def _starts_at_preflop(hand: dict) -> bool:
    streets = _streets(hand)
    return bool(streets) and streets[0].get("street") == FIRST_STREET


def _has_actions(hand: dict) -> bool:
    return any(street.get("actions") for street in _streets(hand))


def _hero_card_count(hand: dict) -> int:
    cards = hand.get("hero_cards")
    return len(cards) if isinstance(cards, list) else 0


def _hand_is_complete(hand: dict, *, include_partial: bool,
                      allow_mid_hand: bool) -> bool:
    if hand.get("partial") and not include_partial:
        return False
    if not allow_mid_hand and not _starts_at_preflop(hand):
        return False
    return True


def load_hands(path: Path, *, include_partial: bool = False,
               allow_mid_hand: bool = False,
               require_hero_cards: bool = False,
               require_actions: bool = False
               ) -> tuple[list[dict], dict, list[tuple[int, str]]]:
    """读 JSONL，返回 (完整手牌, 统计, 坏行)。坏行 = [(行号, 原因)]。"""
    hands: list[dict] = []
    bad_lines: list[tuple[int, str]] = []
    stats = {
        "hand_records": 0,
        "sessions": 0,
        "skipped_partial": 0,
        "skipped_mid_hand": 0,
        "skipped_no_hero": 0,
        "skipped_no_actions": 0,
        "kept": 0,
        "kept_no_hero": 0,
        "kept_no_actions": 0,
    }
    session = 0

    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                bad_lines.append((lineno, f"JSON 解析失败：{exc.msg}"))
                continue
            if not isinstance(record, dict):
                bad_lines.append((lineno, "不是 JSON 对象"))
                continue

            kind = record.get("type")
            if kind == "session_start":
                session += 1
                stats["sessions"] += 1
                continue
            if kind != "hand":
                continue

            stats["hand_records"] += 1
            if record.get("partial") and not include_partial:
                stats["skipped_partial"] += 1
                continue
            if not allow_mid_hand and not _starts_at_preflop(record):
                stats["skipped_mid_hand"] += 1
                continue
            if require_hero_cards and _hero_card_count(record) != 2:
                stats["skipped_no_hero"] += 1
                continue
            if require_actions and not _has_actions(record):
                stats["skipped_no_actions"] += 1
                continue

            hand = dict(record)
            hand["session"] = session
            # 新日志已经带全局唯一的 uid（= hand_id）；旧日志没有 uid，
            # 才补 session 前缀，避免跨会话 h1 重复。
            hand["uid"] = (str(record.get("uid") or "").strip()
                           or f"s{session:02d}-{record.get('hand_id') or '?'}")
            hands.append(hand)
            stats["kept"] += 1
            if _hero_card_count(record) != 2:
                stats["kept_no_hero"] += 1
            if not _has_actions(record):
                stats["kept_no_actions"] += 1

    return hands, stats, bad_lines


def write_hands(path: Path, hands: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(hands, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="从 live.states.jsonl 筛选完整手牌并写成 hands.json")
    ap.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT,
                    help=f"输入的 JSONL 日志（默认 {DEFAULT_INPUT}）")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUTPUT,
                    help=f"输出的 JSON 文件（默认 {DEFAULT_OUTPUT}）")
    ap.add_argument("--include-partial", action="store_true",
                    help="也保留退出时被截断的 partial 手牌（默认不保留）")
    ap.add_argument("--mid-hand", action="store_true",
                    help="也保留从 flop/turn/river 开始的手（默认只保留翻前开始）")
    ap.add_argument("--require-hero-cards", action="store_true",
                    help="只保留读到自己 2 张手牌的记录")
    ap.add_argument("--require-actions", action="store_true",
                    help="只保留至少有一条行动记录的手（走牌/全弃牌会被去掉）")
    return ap


def _print_summary(input_path: Path, out_path: Path, hands: list[dict],
                   stats: dict, bad_lines: list[tuple[int, str]]) -> None:
    print(f"读取 {input_path}")
    print(f"  hand 记录 {stats['hand_records']} 条，"
          f"保留 {stats['kept']} 条")
    skipped = []
    if stats["skipped_partial"]:
        skipped.append(f"partial {stats['skipped_partial']}")
    if stats["skipped_mid_hand"]:
        skipped.append(f"中途接管 {stats['skipped_mid_hand']}")
    if stats["skipped_no_hero"]:
        skipped.append(f"没读到手牌 {stats['skipped_no_hero']}")
    if stats["skipped_no_actions"]:
        skipped.append(f"没有行动 {stats['skipped_no_actions']}")
    if skipped:
        print(f"  跳过：{'、'.join(skipped)}")
    print(f"写入 {out_path}（{len(hands)} 手，"
          f"日志内 {stats['sessions']} 个 session）")

    if hands:
        print(f"  时间范围：{hands[0].get('ts')} → {hands[-1].get('ts')}")
    if stats["kept_no_hero"]:
        print(f"  提示：其中 {stats['kept_no_hero']} 手没读到 hero 手牌，"
              "可加 --require-hero-cards 过滤")
    if stats["kept_no_actions"]:
        print(f"  提示：其中 {stats['kept_no_actions']} 手没有任何行动记录，"
              "可加 --require-actions 过滤")
    if bad_lines:
        print(f"  [警告] 跳过 {len(bad_lines)} 行坏数据"
              f"（首个：第 {bad_lines[0][0]} 行，{bad_lines[0][1]}）")
    if not hands:
        print("  [警告] 没有可导出的手牌：确认日志里存在 type=hand 记录，"
              "且启动时没有关闭 --ai-advice")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.is_file():
        print(f"找不到日志：{args.input}", file=sys.stderr)
        return 1
    hands, stats, bad_lines = load_hands(
        args.input,
        include_partial=args.include_partial,
        allow_mid_hand=args.mid_hand,
        require_hero_cards=args.require_hero_cards,
        require_actions=args.require_actions)
    try:
        write_hands(args.out, hands)
    except OSError as exc:
        print(f"写不了 {args.out}：{exc}", file=sys.stderr)
        return 1
    _print_summary(args.input, args.out, hands, stats, bad_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
