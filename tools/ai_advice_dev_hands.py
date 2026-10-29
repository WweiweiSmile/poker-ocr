#!/usr/bin/env python3
"""把 dev 库导出的复盘手牌喂给 AI 模型（JEV/chat），和**实际动作**逐决策点对比。

`replay_dev_hands.py` 是同一批手牌喂给 coach **规则引擎**的版本；本脚本喂给
AI 模型（advice.py 的 `advise`）。模型名决定协议：名字里带 jev 走 OpenRouter
decisions，其余走 OpenAI 兼容的 /chat/completions，凭据都来自 .env 的 AI_SERVER_*。

用法：
    # 1) 从 dev 库导出（在 ~/codes/call-back 目录下）
    export $(grep -E '^DB_' .env | xargs)
    mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -N -e "
      SELECT JSON_OBJECT('id',id,'heroPosition',hero_position,'heroCards',hero_cards,
        'heroStackBb',hero_stack_bb,'sb',small_blind_bb,'bb',big_blind_bb,'ante',ante_bb,
        'bombPot',bomb_pot_bb,'board',IFNULL(board,''),'villainCount',villain_count,
        'villains',IFNULL(villains, JSON_ARRAY()),'streets',streets)
      FROM review_hands WHERE deleted_at IS NULL;" > /tmp/hands.jsonl

    # 2) 跑对比。每个决策点 = 一次真实模型调用，按额度计费，默认上限 --limit 20
    .venv/bin/python tools/ai_advice_dev_hands.py /tmp/hands.jsonl

## 与 replay_dev_hands.py 相同的口径

- 底池是**重建**的，不是库里读的（库里 potStartBb 多为空），误差 ≤1BB。
- allin 的 amountBb 语义不确定（"加到多少"还是"这一手下多少"），一律当"加到"。
- 只评**翻后**决策：advice 不覆盖翻前。
- 对手信息（松弱/紧凶…）库里没有，模型按「未知」跑。

## 对比口径（三档）

    一致     模型动作 == 实际动作（激进的两边再看尺度差有没有超 1/4 池）
    尺度差   两边都是 bet/raise/allin，只是下注量不同
    动作不同 动作类别就不一样（如实际 call、模型说 fold）

模型不可用（fallback）或调用出错的决策点记「跳过」，不计入三档。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    AISettings, advise, build_situation, is_jev_model, render_facts_zh,
)
from poker_ocr.env import load_env  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

POSTFLOP = ("flop", "turn", "river")
BOARD_LEN = {"flop": 3, "turn": 4, "river": 5}
AGGRESSIVE = ("bet", "raise")


def cards_of(board: str) -> list[str]:
    return [board[i:i + 2] for i in range(0, len(board) - 1, 2)]


def normalize(h: dict) -> dict:
    """兼容两种导出：本仓 tools/export_hands.sql 的 camelCase，和原始 DB 行的 snake_case。

    streets / villains 内部都是 camelCase（amountBb / stackBb），不用管。
    """
    def g(*names, default=None):
        for n in names:
            if n in h and h[n] is not None:
                return h[n]
        return default

    return {
        "id": g("id"),
        "heroCards": g("heroCards", "hero_cards", default=""),
        "heroPosition": g("heroPosition", "hero_position", default=""),
        "heroStackBb": g("heroStackBb", "hero_stack_bb"),
        "villainCount": g("villainCount", "villain_count", default=1),
        "board": g("board", default=""),
        "sb": g("sb", "small_blind_bb", default=0),
        "bb": g("bb", "big_blind_bb", default=0),
        "ante": g("ante", "ante_bb", default=0),
        "bombPot": g("bombPot", "bomb_pot_bb", default=0),
        "villains": g("villains", default=[]),
        "streets": g("streets", default=[]),
    }


def _snapshot(hand: dict, street: str, pot: float, pot_start: float,
              level: float, commit: dict[str, float], hero_invested: float,
              players: int, streets: tuple[StreetRecord, ...],
              idx: int) -> HandSnapshot:
    """把一个决策点（轮到我这一动之前）拼成 HandSnapshot。

    commit 是「本街各座已投入」，level 是当前最高下注；两者相减就是需跟额。
    """
    hero_pos = hand.get("heroPosition") or None
    hero_bet = commit.get("hero", 0.0)
    to_call = max(level - hero_bet, 0.0)
    vals = sorted(commit.values(), reverse=True)
    second = vals[1] if len(vals) > 1 else None

    start = hand.get("heroStackBb")
    remaining = (start - hero_invested) if start is not None else None
    board = cards_of(hand.get("board") or "")[:BOARD_LEN[street]]

    opps = tuple(
        OpponentSnapshot(seat_id=v["position"], name=v.get("name"),
                         position=v["position"], stack=v.get("stackBb"),
                         bet=commit.get(v["position"], 0.0), folded=False)
        for v in (hand.get("villains") or []) if v.get("position")
    )

    raises = sum(1 for s in streets if s.street == "preflop"
                 for a in s.actions if a.action in ("raise", "bet"))
    shapes = (("unopened",) if raises == 0
              else ("single-raise",) if raises == 1 else ("three-bet",))
    pfr = any(a.is_hero and a.action in ("raise", "bet")
              for s in streets if s.street == "preflop" for a in s.actions)

    sb, bb, ante = (hand.get("sb") or 0.0, hand.get("bb") or 0.0,
                    hand.get("ante") or 0.0)
    blinds = Blinds(sb, bb, ante or None, f"{sb}/{bb}({ante})") if bb > 0 else None

    return HandSnapshot(
        hand_id=str(hand.get("id")),
        hand_index=0,
        street=street,
        board=tuple(board),
        hero_cards=tuple(cards_of(hand.get("heroCards") or "")),
        hero_position=hero_pos,
        hero_stack=round(remaining, 2) if remaining is not None else None,
        hero_bet=hero_bet,
        hero_invested=round(hero_invested, 2),
        to_call=round(to_call, 2),
        second_bet=second,
        pot=round(pot, 2),
        pot_before_street=round(pot_start, 2),
        table_size=players,
        blinds=blinds,
        opponents=opps,
        streets=tuple(streets),
        preflop_shapes=shapes,
        hero_is_pfr=pfr,
        cbet_spot=False,
        decision_key=(str(hand.get("id")), street, idx),
    )


def walk_snapshots(hand: dict) -> list[dict]:
    """按街重放，返回 hero 的翻后决策点。

    每个决策点带：`snap`（轮到我这一动之前的快照）、`actual` / `actual_amt`
    （库里记录的实际动作）、`pot`（行动前的底池）。hero 在同一街行动多次时，
    每一次都会单独成一个决策点（跟注后被加注、再轮到 hero）。
    """
    players = (hand.get("villainCount") or 1) + 1
    sb = hand.get("sb") or 0.0
    bb = hand.get("bb") or 0.0
    ante = hand.get("ante") or 0.0
    bomb = hand.get("bombPot") or 0.0
    pot = sb + bb + ante * players + bomb * players
    hero_pos = hand.get("heroPosition") or "HERO"
    hero_invested = 0.0
    villains = {v["position"]: v
                for v in (hand.get("villains") or []) if v.get("position")}

    built: list[StreetRecord] = []
    decisions: list[dict] = []

    for rec in hand.get("streets") or []:
        street = rec.get("street")
        if street not in ("preflop",) + POSTFLOP:
            continue
        if rec.get("potStartBb") is not None:
            pot = float(rec["potStartBb"])
        pot_start = pot
        level = bb if street == "preflop" else 0.0
        commit: dict[str, float] = {}
        cur_acts: list[ActionRecord] = []
        for a in rec.get("actions") or []:
            actor = a.get("actor")
            act = a.get("action")
            amt = a.get("amountBb")
            is_hero = actor == "hero"
            if is_hero and street in POSTFLOP:
                streets = tuple(built) + (
                    StreetRecord(street, pot_start, tuple(cur_acts)),)
                snap = _snapshot(hand, street, pot, pot_start, level, commit,
                                 hero_invested, players, streets, len(decisions))
                decisions.append({"snap": snap, "actual": act,
                                  "actual_amt": amt, "pot": pot})
            cur_acts.append(ActionRecord(
                street=street, seat_id=actor,
                name=(villains.get(actor) or {}).get("name") if not is_hero else None,
                position=(hero_pos if is_hero else actor),
                action=act, amount=amt, all_in=(act == "allin"), is_hero=is_hero))
            delta = 0.0
            if act in ("bet", "raise", "allin") and amt is not None:
                level = amt
                delta = max(amt - commit.get(actor, 0.0), 0.0)
                commit[actor] = amt
            elif act == "call":
                delta = max(level - commit.get(actor, 0.0), 0.0)
                commit[actor] = level
            pot += delta
            if is_hero:
                hero_invested += delta
        built.append(StreetRecord(street, pot_start, tuple(cur_acts)))

    return decisions


def classify(engine: str, actual: str, sizing: float | None,
             amt: float | None, pot: float) -> str:
    """把一次对比归到 一致 / 尺度差 / 动作不同。与 replay_dev_hands 同口径。"""
    actual_eff = "raise" if actual == "allin" else actual
    if engine == actual_eff:
        if engine in AGGRESSIVE and sizing is not None and amt is not None:
            if abs(sizing - amt) > 0.25 * pot:
                return "尺度差"
        return "一致"
    if engine in AGGRESSIVE and actual_eff in AGGRESSIVE:
        return "尺度差"
    return "动作不同"


def _signature(h: dict) -> str:
    """内容指纹：底牌 + 公共牌 + 全部行动。用来去掉重复手牌。"""
    return json.dumps([h.get("heroCards"), h.get("board"), h.get("streets")],
                      sort_keys=True, ensure_ascii=False)


def _fmt(kind: str, amount: float | None) -> str:
    if kind in AGGRESSIVE and amount is not None:
        return f"{kind} {amount}"
    return kind


def main(path: str, limit: int, timeout: float, verbose: bool = False,
         json_path: str | None = None) -> int:
    load_env()
    settings = AISettings.from_env()
    if settings is None:
        print("没配 AI_SERVER_*：先 cp .env.example .env 填 Key", file=sys.stderr)
        return 2

    raw = Path(path).read_text().strip()
    if raw.startswith("["):                    # 整个文件是一个 JSON 数组
        hands = [normalize(h) for h in json.loads(raw)]
    else:                                      # JSONL：一行一手
        hands = [normalize(json.loads(l)) for l in raw.splitlines() if l.strip()]

    proto = "JEV decisions" if is_jev_model(settings.model) else "chat/completions"
    print(f"模型: {settings.model}（{proto}）")

    seen_sig: set[str] = set()
    dupes = 0
    buckets = {"一致": 0, "尺度差": 0, "动作不同": 0}
    skipped = 0
    rows = []
    processed = 0

    for h in hands:
        sig = _signature(h)
        if sig in seen_sig:
            dupes += 1
            continue
        seen_sig.add(sig)

        board = cards_of(h.get("board") or "")
        for d in walk_snapshots(h):
            if limit and processed >= limit:
                break
            snap = d["snap"]
            n = BOARD_LEN[snap.street]
            if len(board) < n or not h.get("heroCards"):
                skipped += 1
                continue
            processed += 1

            try:
                sit = build_situation(snap)
                res = advise(sit, settings, timeout=timeout)
            except Exception as exc:            # noqa: BLE001 — 批量重放，不能因一手坏数据死
                skipped += 1
                rows.append((h["id"], d, None, None, None, "跳过",
                             f"{type(exc).__name__}: {exc}", None))
                continue

            if res.source != "model":
                skipped += 1
                rows.append((h["id"], d, sit.hand_label, res.option.kind,
                             res.option.amount, "跳过", res.error or res.reason,
                             sit))
                continue
            bucket = classify(res.option.kind, d["actual"], res.option.amount,
                              d["actual_amt"], d["pot"])
            buckets[bucket] += 1
            rows.append((h["id"], d, sit.hand_label, res.option.kind,
                         res.option.amount, bucket, res.reason, sit))

    print(f"{'手':>7} {'街':<6} {'持牌':<8} {'AI':<14} {'实际':<14} {'判定':<8} 底池")
    print("─" * 96)
    for hid, d, label, kind, amt, bucket, reason, sit in rows:
        ai = _fmt(kind, amt) if kind else "—"
        act = _fmt(d["actual"], d["actual_amt"])
        print(f"{hid:>7} {d['snap'].street:<6} {(label or '—'):<8} "
              f"{ai:<14} {act:<14} {bucket:<8} {d['pot']:.1f}")
        if bucket in ("动作不同", "跳过"):
            print(f"    └ {reason}")
        if verbose and sit is not None and bucket in ("动作不同", "尺度差"):
            print("    ── 牌况 ──")
            print("    " + render_facts_zh(sit).replace("\n", "\n    "))

    total = sum(buckets.values())
    print("─" * 96)
    print(f"决策点 {total} 个（跳过 {skipped} 个无法评估；另有 {dupes} 手重复已去重）")
    if total:
        for k in ("一致", "尺度差", "动作不同"):
            print(f"  {k}: {buckets[k]}  ({buckets[k] / total:.0%})")

        def aggr(v: str) -> bool:
            return v in ("bet", "raise", "allin")

        diff = [(kind, d["actual"])
                for _, d, _, kind, _, b, _, _ in rows if b == "动作不同"]
        ai_pass = sum(1 for e, a in diff if not aggr(e) and aggr(a))
        ai_aggr = sum(1 for e, a in diff if aggr(e) and not aggr(a))
        # 第三项不是「两边都激进」——那种情况 classify 会归到尺度差，
        # 落到动作不同的只会是 call/check/fold 之间的错位（两边都保守）
        print(f"  动作不同的方向：AI 保守/玩家激进 {ai_pass} ｜ "
              f"AI 激进/玩家保守 {ai_aggr} ｜ 两边都保守 {len(diff) - ai_pass - ai_aggr}")

    if json_path:
        import datetime

        def row_dict(hid, d, label, kind, amt, bucket, reason, sit):
            snap = d["snap"]
            return {
                "hand_id": hid,
                "street": snap.street,
                "hero_position": snap.hero_position,
                "hero_cards": "".join(snap.hero_cards),
                "board": "".join(snap.board),
                "table_size": snap.table_size,
                "hand_label": label,
                "pot": round(d["pot"], 2),
                "to_call": round(snap.to_call, 2),
                "hero_stack": snap.hero_stack,
                "hero_invested": snap.hero_invested,
                "ai": None if kind is None else {
                    "kind": kind, "amount": amt,
                    "label": next((o.label for o in sit.options if o.id == kind), kind)
                    if sit else kind,
                },
                "actual": {"action": d["actual"], "amount": d["actual_amt"]},
                "bucket": bucket,
                "reason": reason,
                "hand_facts": render_facts_zh(sit) if sit is not None else None,
            }

        dump = {
            "model": settings.model,
            "protocol": proto,
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source": str(path),
            "summary": {
                "decisions": total,
                "skipped": skipped,
                "dupes": dupes,
                **buckets,
            },
            "rows": [row_dict(*r) for r in rows],
        }
        Path(json_path).write_text(
            json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入 {json_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="把 dev 库导出的复盘手牌喂给 AI 模型，逐决策点对比实际动作")
    ap.add_argument("path", nargs="?", default="/tmp/hands.jsonl",
                    help="导出的 JSONL / JSON 文件（默认 /tmp/hands.jsonl）")
    ap.add_argument("--limit", type=int, default=20,
                    help="最多跑多少个决策点（0 = 不限制；默认 20）")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="单次模型调用超时秒数（默认 20）")
    ap.add_argument("--verbose", action="store_true",
                    help="对「动作不同/尺度差」的决策点，展开打印完整牌况")
    ap.add_argument("--json", metavar="PATH", default=None,
                    help="把逐决策点结果（含完整牌况）写成 JSON，供报告工具消费")
    args = ap.parse_args()
    sys.exit(main(args.path, args.limit, args.timeout, args.verbose, args.json))
