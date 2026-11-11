#!/usr/bin/env python3
"""按手牌 outbox 重放指定决策点（不依赖 live state 帧）。

为什么需要它
────────────
`replay_live_decisions.py` 用 `logs/live.states.jsonl` 的 state 帧重建快照。
这有两个硬边界：

  · 有些手牌只有 hand 记录、没有 state 帧（2026-10-30 的 acf4 / a334）；
  · 默认 `--hands hands.json` 已经不存在，翻前/之前街的行动线会整段丢失，
    翻后底池类型因此被算成「无人入池」。

本工具直接读 `data/hand_outbox/pending/<hand_id>.json`：

  · 用 advice 的 `action_index` 截断行动线（翻前 + 之前街完整保留）；
  · 用 hand 的 `street_active` 判断谁已经弃牌；
  · 用 `seat_meta` 的起始筹码 + 已投入额算后手；
  · 底池/需跟/底牌/牌面以 advice 记录为准（模型当时真正看到的数）；
  · 可回填 advice.context 的桌况统计与逐座位画像。

    # 只看事实层（不调模型）
    nv tools/replay_hand_outbox.py --hand 20261030-acf43ea34f

    # 真跑模型
    nv tools/replay_hand_outbox.py --hand 20261030-acf43ea34f --model
    nv tools/replay_hand_outbox.py --all --model --json /tmp/rerun.json

注意：outbox 的 `advices` 是**当时**的建议，不是 hand 记录里的实际动作；
`action_index` 是「下一个动作」的全局下标（0 起）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    AISettings, advise, build_situation, render_facts_zh,
)
from poker_ocr.env import load_env  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.opponent_profile import (  # noqa: E402
    OpponentRead, PooledStats, load_profile_store,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.table_dynamics import load_table_dynamics  # noqa: E402

STREETS = ("preflop", "flop", "turn", "river")
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
AGGRESSIVE = ("bet", "raise", "allin")


def load_hand(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(hand: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for rec in hand.get("streets") or []:
        street = str(rec.get("street") or "")
        if street not in STREETS:
            continue
        for action in rec.get("actions") or []:
            out.append((street, action))
    return out


def _seat(action: dict) -> str:
    return "hero" if action.get("is_hero") else str(action.get("position") or "")


def _preflop_shapes(actions: list[tuple[str, dict]]) -> tuple[str, ...]:
    pre = [a for s, a in actions if s == "preflop"]
    raises = sum(1 for a in pre
                 if a.get("action") in ("raise", "bet")
                 or (a.get("action") == "call" and a.get("all_in")))
    if raises == 0:
        limped = any(a.get("action") == "call" and not a.get("is_hero")
                     for a in pre)
        return ("limped",) if limped else ("unopened",)
    if raises == 1:
        return ("single-raise",)
    return ("three-bet",)


def _find_index(hand: dict, advice: dict) -> int:
    """没有 action_index 时，用 (街, 底池, 需跟) 找最接近的截断点。"""
    street = str(advice.get("street") or "")
    want_pot = float(advice.get("pot") or 0.0)
    want_call = float(advice.get("to_call") or 0.0)
    actions = _flatten(hand)
    candidates = [i for i, (s, _) in enumerate(actions) if s == street]
    if not candidates:
        return len(actions)
    # 街起点本身也是一个候选（第一手动作之前）。
    first = candidates[0]
    candidates = [first] + [i + 1 for i in candidates]
    best: tuple[float, int] | None = None
    for idx in candidates:
        snap = _snap_at(hand, advice, idx)
        score = (abs((snap.pot or 0.0) - want_pot)
                 + abs((snap.to_call or 0.0) - want_call))
        if best is None or score < best[0]:
            best = (score, idx)
    return best[1] if best else first


def _snap_at(hand: dict, advice: dict, idx: int) -> HandSnapshot:
    street = str(advice.get("street") or "")
    actions = _flatten(hand)
    before = actions[:idx]
    board_len = BOARD_LEN.get(street, len(hand.get("board") or ()))

    blinds_raw = hand.get("blinds") or {}
    sb = float(blinds_raw.get("sb") or 0.0)
    bb = float(blinds_raw.get("bb") or 1.0)
    ante = float(blinds_raw.get("ante") or 0.0)
    bomb = bool(hand.get("bomb_pot"))
    hero_pos = str(hand.get("hero_position") or "")
    seats = [s for s in hand.get("seat_meta") or []
             if s.get("position") and s.get("seat_id") != "hero"]
    start = {str(s.get("position")): s.get("stack") for s in seats}
    hero_start = hand.get("hero_stack_bb")
    hero_start = float(hero_start) if hero_start is not None else None

    # 起始投入：普通前注每个在局玩家都交；SB/BB 再交盲注。
    # 炸弹池的 5BB 强制前注不在 stack 里扣（outbox 的 hero_stack_bb 已经是
    # 扣完炸弹前注后的数，再扣一次会把 5c1b 的 182.5 算成 177.5）。
    invested: dict[str, float] = {str(s.get("position")): ante for s in seats}
    invested["hero"] = ante
    # 盲注已经在牌桌上，无论当前是哪一街都要从后手/本手投入里扣掉。
    invested["SB"] = invested.get("SB", 0.0) + sb
    invested["BB"] = invested.get("BB", 0.0) + bb
    if hero_pos in ("SB", "BB"):
        invested["hero"] += sb if hero_pos == "SB" else bb
    street_commit: dict[str, float] = {}
    level = 0.0
    if street == "preflop":
        street_commit = {"SB": sb, "BB": bb}
        level = bb

    folded: set[str] = set()
    current = ""
    street_delta = 0.0
    for action_street, action in before:
        if action_street != current:
            current = action_street
            street_commit = ({"SB": sb, "BB": bb}
                             if current == "preflop" else {})
            level = bb if current == "preflop" else 0.0
            street_delta = 0.0
        seat = _seat(action)
        if not seat:
            continue
        act = action.get("action")
        amount = action.get("amount")
        if act in AGGRESSIVE and amount is not None:
            amount = float(amount)
            delta = max(amount - street_commit.get(seat, 0.0), 0.0)
            street_commit[seat] = amount
            level = max(level, amount)
            invested[seat] = invested.get(seat, 0.0) + delta
            street_delta += delta
        elif act == "call":
            delta = max(level - street_commit.get(seat, 0.0), 0.0)
            street_commit[seat] = level
            invested[seat] = invested.get(seat, 0.0) + delta
            street_delta += delta
        elif act == "fold":
            folded.add(seat)

    # 当前街还没有动作时，street_commit 仍停留在上一条街（翻前）的投入；
    # 不能把它当成「本街已下注」，否则翻牌的有效筹码/SPR 会把翻前跟注
    # 再加一遍（acf4 的 UTG+1 会从 142.5 变成 146.5）。
    if current != street:
        street_commit = {}
        level = 0.0
        street_delta = 0.0

    active = set(hand.get("street_active", {}).get(street) or ())
    if not active:
        active = {str(s.get("seat_id")) for s in hand.get("seat_meta") or []}

    def is_folded(pos: str) -> bool:
        # street_active 是「这一街还在手」的座位；不在其中就是弃牌。
        seat_id = next((str(s.get("seat_id")) for s in seats
                        if str(s.get("position")) == pos), "")
        if seat_id and active:
            return seat_id not in active
        return pos in folded

    opponents = []
    for s in seats:
        pos = str(s.get("position"))
        stack = s.get("stack")
        stack = (float(stack) - invested.get(pos, 0.0)
                 if stack is not None else None)
        opponents.append(OpponentSnapshot(
            seat_id=str(s.get("seat_id") or pos), name=s.get("name"),
            position=pos, stack=round(stack, 2) if stack is not None else None,
            bet=float(street_commit.get(pos, 0.0) or 0.0),
            folded=is_folded(pos)))

    hero_bet = float(street_commit.get("hero", 0.0) or 0.0)
    to_call = advice.get("to_call")
    to_call = (float(to_call) if to_call is not None
               else max(level - hero_bet, 0.0))
    pot = advice.get("pot")
    pot = float(pot) if pot is not None else None
    if pot is None:
        start_pot = next((r.get("pot_start") for r in hand.get("streets") or []
                          if r.get("street") == street), None)
        pot = float(start_pot or 0.0) + street_delta
    pot_start = next((r.get("pot_start") for r in hand.get("streets") or []
                      if r.get("street") == street), None)
    if pot_start is None:
        pot_start = max(pot - street_delta, 0.0)
    else:
        pot_start = float(pot_start)

    hero_stack = (hero_start - invested.get("hero", 0.0)
                  if hero_start is not None else None)
    hero_cards = tuple(advice.get("hero_cards")
                       or hand.get("hero_cards") or ())
    board = tuple(advice.get("board")
                  or (hand.get("board") or ())[:board_len])
    all_before = _flatten(hand)
    pre_actions = [
        (s, a) for i, (s, a) in enumerate(all_before)
        if s == "preflop" and (street != "preflop" or i < idx)
    ]
    shapes = _preflop_shapes(pre_actions)
    hero_is_pfr = any(a.get("is_hero") and a.get("action") in ("raise", "bet")
                      for _, a in pre_actions)

    return HandSnapshot(
        hand_id=str(hand.get("hand_id") or hand.get("uid") or ""),
        hand_index=0, street=street, board=board, hero_cards=hero_cards,
        hero_position=hero_pos or None,
        hero_stack=round(hero_stack, 2) if hero_stack is not None else None,
        hero_bet=hero_bet,
        hero_invested=round(invested.get("hero", 0.0), 2),
        to_call=round(to_call, 2), second_bet=None,
        pot=round(pot, 2), pot_before_street=round(pot_start, 2),
        table_size=int(hand.get("table_size") or 0),
        blinds=Blinds(sb, bb, ante or None,
                      str(blinds_raw.get("raw") or "")),
        opponents=tuple(opponents),
        streets=tuple(_street_records(hand, idx, street)),
        preflop_shapes=shapes, hero_is_pfr=hero_is_pfr,
        cbet_spot=False,
        decision_key=(str(hand.get("hand_id")), street, idx,
                      round(pot, 2), round(to_call, 2), hero_cards),
        players_dealt=int(hand.get("players_dealt") or 0),
        bomb_pot=bomb,
    )


def _street_records(hand: dict, idx: int,
                    current_street: str) -> list[StreetRecord]:
    """把行动线截断到 idx；之前街保留完整，当前街只保留已发生的动作。"""
    records: list[StreetRecord] = []
    seen = 0
    for rec in hand.get("streets") or []:
        street = str(rec.get("street") or "")
        if street not in STREETS:
            continue
        actions = rec.get("actions") or []
        if street == current_street:
            actions = actions[:max(idx - seen, 0)]
        elif STREETS.index(street) > STREETS.index(current_street):
            continue
        rows = tuple(
            ActionRecord(
                street=street, seat_id=str(a.get("seat_id") or ""),
                name=a.get("name"),
                position=(str(hand.get("hero_position") or "")
                          if a.get("is_hero") else a.get("position")),
                action=str(a.get("action") or ""),
                amount=a.get("amount"),
                all_in=bool(a.get("all_in")),
                is_hero=bool(a.get("is_hero")),
                inferred=bool(a.get("inferred")))
            for a in actions)
        records.append(StreetRecord(street, rec.get("pot_start"), rows))
        seen += len(rec.get("actions") or [])
    return records


def build_snap(hand: dict, advice: dict) -> HandSnapshot:
    idx = advice.get("action_index")
    idx = int(idx) if idx is not None else _find_index(hand, advice)
    return _snap_at(hand, advice, idx)


def _table_stats(advice: dict) -> PooledStats | None:
    raw = (advice.get("context") or {}).get("table_stats")
    if not isinstance(raw, dict):
        return None
    fields = set(PooledStats.__dataclass_fields__)
    try:
        return PooledStats(**{k: v for k, v in raw.items() if k in fields})
    except TypeError:
        return None


def _reads(hand: dict, advice: dict, snap: HandSnapshot,
           store) -> tuple[OpponentRead, ...]:
    if store is None:
        return ()
    current = store.read_for([o for o in snap.opponents if not o.folded])
    logged = {str(r.get("seat_id")): r
              for r in advice.get("opponent_reads") or []}
    out: list[OpponentRead] = []
    for seat_id, read in current.items():
        log = logged.get(str(seat_id))
        if log:
            read = replace(
                read,
                display_name=log.get("display_name") or read.display_name,
                archetype=log.get("archetype") or read.archetype,
                confidence=float(log.get("confidence") or read.confidence),
                n=int(log.get("n") or read.n),
                gate=str(log.get("gate") or read.gate),
            )
        out.append(read)
    return tuple(out)


def _old(advice: dict) -> str:
    opt = advice.get("option") or {}
    return str(opt.get("id") or opt.get("label") or "?")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按手牌 outbox 重放 AI 决策点")
    ap.add_argument("--hand", action="append", default=[],
                    help="手牌 id；可重复。省略时配合 --all 使用")
    ap.add_argument("--all", action="store_true", help="重放 outbox 里全部有 advices 的手")
    ap.add_argument("--dir", type=Path, default=Path("data/hand_outbox/pending"))
    ap.add_argument("--model", action="store_true", help="真的调模型")
    ap.add_argument("--facts", action="store_true", help="打印完整事实块")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--table-dynamics", type=Path, default=None)
    ap.add_argument("--no-table-dynamics", action="store_true")
    ap.add_argument("--no-reads", action="store_true",
                    help="不注入画像（只看事实层时更快）")
    args = ap.parse_args(argv)

    paths: list[Path]
    if args.all:
        paths = sorted(args.dir.glob("*.json"))
    elif args.hand:
        paths = []
        for hid in args.hand:
            path = args.dir / f"{hid}.json"
            if not path.exists():
                print(f"找不到 {path}", file=sys.stderr)
                return 2
            paths.append(path)
    else:
        ap.error("需要 --hand 或 --all")

    dynamics = ""
    if not args.no_table_dynamics:
        dyn = load_table_dynamics(args.table_dynamics)
        if dyn.error:
            print(f"牌桌动态读取失败：{dyn.error}", file=sys.stderr)
        elif dyn.found:
            dynamics = dyn.text
            print(f"牌桌动态：{dyn.path}（{len(dynamics)} 字符）")

    store = None
    if not args.no_reads:
        try:
            store = load_profile_store()
        except Exception as exc:                    # noqa: BLE001
            print(f"画像读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    settings = None
    if args.model:
        env = load_env()
        source = "dotenv" if "AI_SERVER_API_KEY" in env.applied else "env"
        settings = AISettings.from_env(source=source)
        if settings is None:
            print("没有 AI 凭据，--model 用不了", file=sys.stderr)
            return 1
        if args.model_name:
            settings = replace(settings, model=args.model_name)
        print(f"模型: {settings.model}（thinking={settings.thinking}，"
              f"reasoning={settings.reasoning}）\n")

    rows: list[dict] = []
    for path in paths:
        hand = load_hand(path)
        advices = hand.get("advices") or []
        if not advices:
            print(f"\n{hand.get('hand_id')}: 没有 advice 记录，跳过")
            continue
        print(f"\n{'=' * 100}\n{hand.get('hand_id')}  "
              f"{len(advices)} 个决策点")
        for advice in advices:
            snap = build_snap(hand, advice)
            sit = build_situation(
                snap, table_dynamics=dynamics,
                opponent_reads=_reads(hand, advice, snap, store),
                table_stats=_table_stats(advice))
            row = {
                "hand": snap.hand_id, "street": snap.street,
                "cards": " ".join(snap.hero_cards),
                "board": " ".join(snap.board),
                "position": snap.hero_position,
                "pot": snap.pot, "to_call": snap.to_call,
                "stack": snap.hero_stack,
                "n_opp": sit.opponents_in_hand,
                "old": _old(advice),
                "old_reason": advice.get("reason") or "",
                "facts": render_facts_zh(sit),
                "sit": sit,
            }
            rows.append(row)
            print(f"\n· {snap.street}  {row['cards']} / {row['board'] or '—'}  "
                  f"pot={snap.pot} call={snap.to_call} "
                  f"stack={snap.hero_stack} opp={sit.opponents_in_hand}")
            print(f"  旧: {row['old']} — {row['old_reason']}")
            if args.facts:
                print(row["facts"])
            if args.model:
                t0 = time.monotonic()
                try:
                    new = advise(sit, settings, timeout=args.timeout)
                    row["new"] = new.option.id
                    row["new_reason"] = new.reason
                except Exception as exc:            # noqa: BLE001
                    row["new"] = "ERROR"
                    row["new_reason"] = f"{type(exc).__name__}: {exc}"
                row["ms"] = int((time.monotonic() - t0) * 1000)
                print(f"  新: {row['new']} — {row['new_reason']} "
                      f"({row['ms']}ms)")

    if args.model:
        print(f"\n共 {len(rows)} 个决策点；"
              f"与旧建议不同 {sum(1 for r in rows if r.get('new') != r['old'])} 个")
    if args.json:
        dump = []
        for r in rows:
            sit = r["sit"]
            dump.append({
                **{k: v for k, v in r.items() if k not in ("sit",)},
                "options": [o.id for o in sit.options],
                "hand_ranges": bool(sit.hand_ranges),
                "fold_eq": bool(sit.fold_eq),
            })
        Path(args.json).write_text(
            json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"写出 {args.json}（{len(dump)} 个决策点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
