#!/usr/bin/env python3
"""从手牌记录重建对手画像（`data/opponents/profiles.json`）。

设计见 `docs/对手类型识别-设计.md` 7.3。手牌记录是唯一事实来源，画像只是派生
缓存；分类规则/阈值改动后，用这个脚本从头重算，保证可复现。

    # 从导出的 hands JSON + 实时 JSONL 重建（两者重叠的 hand_uid 会自动去重）
    .venv/bin/python tools/rebuild_opponent_profiles.py \\
        --hands hands/1023.hands.json logs/live.states.jsonl \\
        --out data/opponents/profiles.json

    # 只看现有画像，不重建
    .venv/bin/python tools/rebuild_opponent_profiles.py --list

    # 人工修正：pin 永远优先于自动分类；--unpin 清除
    .venv/bin/python tools/rebuild_opponent_profiles.py \\
        --pin "芒果与西米=maniac" --pin "好运常在ok=station"
    .venv/bin/python tools/rebuild_opponent_profiles.py --unpin "芒果与西米"

    # 第一次全量重建时清掉旧数据（默认就会重算，--append 才追加）
    .venv/bin/python tools/rebuild_opponent_profiles.py --hands ... --reset

输入可以是：
  · JSON 数组（`export_live_hands.py` 的产物）；
  · `{"hands": [...]}` 形式的对象；
  · JSONL（`logs/live.states.jsonl`，只取 `type == "hand"`，自动补 session/uid）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.hand_track import LiveHandTracker  # noqa: E402
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.opponent_profile import (  # noqa: E402
    ARCHETYPES, DEFAULT_PROFILE_PATH, ProfileStore,
)
from poker_ocr.parse import Blinds  # noqa: E402

DEFAULT_HANDS = Path("hands/1023.hands.json")
DEFAULT_LIVE = Path("logs/live.states.jsonl")


def load_jsonl_hands(path: Path) -> list[dict]:
    """读 JSONL，只保留 hand 记录；补 session/uid（与 export_live_hands 同口径）。"""
    hands: list[dict] = []
    session = 0
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"  ! {path}:{lineno} JSON 解析失败：{exc.msg}", file=sys.stderr)
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "session_start":
                session += 1
                continue
            if record.get("type") != "hand":
                continue
            hand = dict(record)
            if "session" not in hand:
                hand["session"] = session
            if not hand.get("uid"):
                hand["uid"] = (f"raw:{path.name}:"
                               f"s{session:02d}-{hand.get('hand_id', '')}")
            hands.append(hand)
    return hands


def _player_from_record(raw: dict) -> PlayerState:
    return PlayerState(
        seat_id=str(raw.get("seat_id") or ""),
        name=raw.get("name"),
        position=raw.get("position"),
        stack=raw.get("stack"),
        bet=raw.get("bet"),
        action=raw.get("action"),
        fold=raw.get("fold"),
        in_hand=bool(raw.get("in_hand")),
        all_in=bool(raw.get("all_in")),
        equity=raw.get("equity"),
        status=str(raw.get("status") or "away"),
    )


def _state_from_record(raw: dict) -> FrameState:
    """把 live.states.jsonl 的 state 记录还原成 FrameState。"""
    hero = raw.get("hero") or {}
    players = [_player_from_record(hero)] if hero else []
    players.extend(_player_from_record(o) for o in raw.get("opponents") or [])
    b = raw.get("blinds") or {}
    blinds = None
    if b and b.get("sb") is not None and b.get("bb") is not None:
        blinds = Blinds(sb=float(b["sb"]), bb=float(b["bb"]),
                        ante=b.get("ante"), raw=str(b.get("raw") or ""))
    return FrameState(
        frame_id=str(raw.get("frame") or raw.get("ts") or ""),
        table_seats=int(raw.get("table_seats") or len(players) or 9),
        players_in_hand=int(raw.get("players_in_hand") or 0),
        players=players,
        blinds=blinds,
        dealer=raw.get("dealer"),
        hero_seat_id="hero",
        hero_turn=raw.get("hero_turn"),
        pot=raw.get("pot"),
        pot_before_street=raw.get("pot_before_street"),
        bomb_pot=bool(raw.get("bomb_pot")),
        errors=[],
    )


def load_hands_file(path: Path) -> list[dict]:
    """按扩展名/内容分派 JSON 数组 / 对象 / JSONL / state 帧重放。"""
    if path.suffix.lower() == ".jsonl":
        return _load_jsonl(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 后缀不是 .jsonl 但内容是逐行 JSON：按 JSONL 再试一次
        try:
            return _load_jsonl(path)
        except OSError:
            raise
    if isinstance(data, list):
        return [h for h in data if isinstance(h, dict)
                and (h.get("type") in (None, "hand"))]
    if isinstance(data, dict):
        hands = data.get("hands")
        if isinstance(hands, list):
            return [h for h in hands if isinstance(h, dict)]
        if data.get("type") == "hand":
            return [data]
    return []


def _load_jsonl(path: Path) -> list[dict]:
    """JSONL：含 state 帧就重放（P0 补全）；否则只取 hand 记录。"""
    has_state = False
    session = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "state":
                has_state = True
                break
            if isinstance(record, dict) and record.get("type") == "session_start":
                session += 1
    if has_state:
        return _replay_jsonl(path)
    return load_jsonl_hands(path)


def _replay_jsonl(path: Path) -> list[dict]:
    """重放时保留每条 state 所属的 session，交给 replay_states。

    这样跨 session 的手牌 id 不会串（旧日志的 hand_id 每次启动都从 h1
    重新计数；新日志的 hand_id 已经是日期 + 名单摘要）。
    """
    records: list[dict] = []
    session = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = record.get("type")
            if kind == "session_start":
                session += 1
            elif kind == "state":
                records.append({**record, "_session": session or 1})
            elif kind == "hand":
                records.append({**record, "_session": session or 1})
    # 日志里已经带落库的成品手牌时，**直接用**，不要拿 state 帧重放。
    #
    # 重放版只有实时链路当场看到的动作；成品在街末做过底池对账，补过跟注和
    # 加注。用重放版会让依赖完整行动线的统计系统性偏低（实测 fold-to-cbet
    # 的机会数 24 → 16，摊牌与跟注整段丢失）。
    stored = [r for r in records if r.get("type") == "hand"]
    if stored:
        out: list[dict] = []
        for record in stored:
            hand = dict(record)
            sid = int(hand.get("_session") or 1)
            hand["session"] = sid
            hand["uid"] = (f"state:{path.name}:s{sid:02d}-"
                           f"{hand.get('hand_id')}")
            out.append(hand)
        return out

    # 只有 state 帧的旧日志：退回重放。
    return _replay_records(records, path)


def _replay_records(records: list[dict], path: Path) -> list[dict]:
    original: dict[tuple[int, str], dict] = {}
    for record in records:
        if record.get("type") == "hand":
            sid = int(record.get("_session") or 1)
            original[(sid, str(record.get("hand_id") or ""))] = record
    hands: list[dict] = []
    tracker = LiveHandTracker(confirm_frames=1)
    current_session = 1
    prefix = f"state:{path.name}"

    def flush(hand: dict | None) -> None:
        if hand is None:
            return
        hand = dict(hand)
        hand["session"] = current_session
        hid = str(hand.get("hand_id") or "")
        matching = original.get((current_session, hid))
        if matching:
            # 落库的手牌记录是**成品**：动作线已经在街末对账时补全过，而重放
            # 出来的版本只有实时链路当场看到的那几条动作。用重放版会让依赖
            # 完整行动线的统计系统性偏低 —— 实测 fold-to-cbet 的 24 次机会
            # 被压成 8 次，摊牌/跟注整段丢失。优先用成品，只把 uid 换成本次
            # 重建的前缀。
            hand = dict(matching)
            hand["session"] = current_session
        hand["uid"] = f"{prefix}:s{current_session:02d}-{hid}"
        hands.append(hand)

    for record in records:
        if record.get("type") != "state":
            continue
        sid = int(record.get("_session") or 1)
        if sid != current_session:
            flush(tracker.take_current())
            flush(tracker.take_finished())
            tracker = LiveHandTracker(confirm_frames=1)
            current_session = sid
        tracker.observe(
            _state_from_record(record),
            record.get("hero_cards") or [],
            record.get("board") or [],
            cards_reliable=bool(record.get("cards_reliable", True)),
            cards_warnings=tuple(record.get("cards_warnings") or ()),
        )
        flush(tracker.take_finished())
    flush(tracker.take_finished())
    flush(tracker.take_current())
    return hands


def _print_report(store: ProfileStore, *, top: int = 0) -> None:
    rows = store.report(limit=top or None)
    if not rows:
        print("（还没有任何对手样本）")
        return
    headers = ("玩家", "n", "手数", "VPIP", "PFR", "AF", "3bet", "翻后激",
               "摊牌", "爆炸", "被动", "类型", "置信", "来源", "门控")
    widths = (20, 5, 6, 6, 6, 5, 6, 7, 6, 6, 6, 8, 6, 7, 10)
    def fmt(value) -> str:
        return "-" if value is None else str(value)
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        values = (
            row["name"][:20], row["n"], row["hands"], f"{row['vpip']:g}",
            f"{row['pfr']:g}", fmt(row["af"]), fmt(row["threebet"]),
            fmt(row["postflop_aggr"]), row["showdowns"],
            row["bomb_pot_hands"], row["passive_entry_hands"],
            row["archetype"], f"{row['confidence']:.2f}", row["source"],
            row["gate"],
        )
        print("  ".join(str(v).ljust(w) for v, w in zip(values, widths)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="从手牌记录重建本地对手画像（纯规则、不调模型）")
    ap.add_argument("--hands", nargs="*", type=Path, default=None,
                    help=f"手牌来源（默认 {DEFAULT_HANDS} 和 {DEFAULT_LIVE}；"
                         "不存在就跳过）")
    ap.add_argument("--out", type=Path, default=DEFAULT_PROFILE_PATH,
                    help=f"画像输出（默认 {DEFAULT_PROFILE_PATH}）")
    ap.add_argument("--pool-key", default=None,
                    help="牌桌池键，如 0.5/1-9max；默认从手牌盲注推导")
    ap.add_argument("--append", action="store_true",
                    help="在现有画面上追加（默认先清空旧统计再重算，pin 保留）")
    ap.add_argument("--reset", action="store_true",
                    help="清空现有画像（包括 pin）后再重建")
    ap.add_argument("--pin", action="append", default=[], metavar="NAME=TYPE",
                    help="人工 pin，可重复；TYPE ∈ station/lag/maniac/nit/tag")
    ap.add_argument("--unpin", action="append", default=[], metavar="NAME",
                    help="清除人工 pin，可重复")
    ap.add_argument("--list", action="store_true", help="只打印画像，不重建")
    ap.add_argument("--top", type=int, default=0, help="报告只显示前 N 人（0=全部）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    store = ProfileStore(args.out, args.pool_key, enabled=True)
    store.load()
    if store.load_error and not args.quiet:
        print(f"! 现有画像读取失败（将重建）：{store.load_error}", file=sys.stderr)

    # pin 保留：重建统计不该丢掉人工修正。
    pins = {key: stats.manual_pin for key, stats in store.players.items()
            if stats.manual_pin}

    if not args.list:
        sources = args.hands
        if sources is None:
            sources = [p for p in (DEFAULT_HANDS, DEFAULT_LIVE) if p.exists()]
        if not sources:
            ap.error("没有可用的 --hands 输入；用 --list 只看现有画像")

        if not args.append:
            store.players.clear()
            store.aliases.clear()
            store.seat_keys.clear()
            store.seen_hands = []
            store._seen = set()
        if args.reset:
            store.players.clear()
            store.aliases.clear()
            store.seat_keys.clear()
            store.seen_hands = []
            store._seen = set()
            pins.clear()

        seen_files = 0
        total_hands = 0
        total_obs = 0
        for path in sources:
            if not path.exists():
                print(f"! 跳过不存在的输入：{path}", file=sys.stderr)
                continue
            hands = load_hands_file(path)
            seen_files += 1
            total_hands += len(hands)
            for hand in hands:
                total_obs += len(store.update_hand(hand))
            if not args.quiet:
                print(f"  {path}: {len(hands)} 手")

        # 重新应用 pin（以及命令行新增的 pin）
        for key, archetype in pins.items():
            try:
                store.pin(key, archetype)
            except ValueError:
                pass

    for raw in args.pin:
        if "=" not in raw:
            ap.error(f"--pin 需要 NAME=TYPE：{raw!r}")
        name, archetype = raw.rsplit("=", 1)
        name, archetype = name.strip(), archetype.strip().lower()
        if archetype not in ARCHETYPES[1:]:
            ap.error(f"未知类型 {archetype!r}（可选："
                     f"{', '.join(ARCHETYPES[1:])}）")
        try:
            store.pin(name, archetype)
        except ValueError as exc:
            ap.error(str(exc))
    for name in args.unpin:
        try:
            store.pin(name, None)
        except ValueError as exc:
            ap.error(str(exc))

    if not args.list or args.pin or args.unpin:
        ok = store.save(force=True)
        if not ok:
            print(f"! 写入失败：{store.last_save_error}", file=sys.stderr)
            return 1

    if not args.quiet:
        print()
        if not args.list:
            print(f"处理 {total_hands} 手（{seen_files} 个文件），"
                  f"产生 {total_obs} 条对手观察；"
                  f"画像 {len(store.players)} 人 → {store.path}")
        rows = store.report()
        unknown = sum(1 for r in rows if r["archetype"] == "unknown")
        tiny = sum(1 for r in rows if 0 < r["n"] < 8)
        passive = sum(r["passive_entry_hands"] for r in rows)
        print(f"玩家 {len(rows)}：unknown {unknown}，n<8 {tiny}，"
              f"pin {sum(1 for s in store.players.values() if s.manual_pin)}，"
              f"爆炸池被动入池 {passive} 手（不计 VPIP 分母）")
        print()
        _print_report(store, top=args.top)
    else:
        store.save(force=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
