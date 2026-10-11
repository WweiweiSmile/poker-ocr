#!/usr/bin/env python3
"""把 dev 库导出的复盘手牌喂给 coach 规则引擎，和**实际动作**逐决策点对比。

用法：
    # 1) 从 dev 库导出（在 ~/codes/call-back 目录下）
    export $(grep -E '^DB_' .env | xargs)
    mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -N -e "
      SELECT JSON_OBJECT('id',id,'heroPosition',hero_position,'heroCards',hero_cards,
        'heroStackBb',hero_stack_bb,'sb',small_blind_bb,'bb',big_blind_bb,'ante',ante_bb,
        'bombPot',bomb_pot_bb,'board',IFNULL(board,''),'villainCount',villain_count,
        'streets',streets,'heroThought',hero_thought)
      FROM review_hands WHERE deleted_at IS NULL;" > /tmp/hands.jsonl

    # 2) 跑对比
    .venv/bin/python tools/replay_dev_hands.py /tmp/hands.jsonl

## 底池是**重建**的，不是库里读的

库里没存逐步底池（potStartBb 多为空），这里从盲注 + 各街 amountBb 累加出来。
盲注认领不精确（不知道谁是大盲），**误差 ≤1BB**。看的是动作口径，够用；
每条结果会打印用到的底池，可疑的能一眼看出来。

## 对比口径（三档）

    一致     引擎动作 == 实际动作（激进的再看尺度差有没有超 1/4 池）
    尺度差   两边都是 bet/raise/allin，只是下注量不同
    动作不同 动作类别就不一样（如实际 call、引擎说 fold）

## 已知的粗糙处

- allin 的 amountBb 语义不确定（是"加到多少"还是"这一手下多少"），一律当"加到"处理。
- 对手信息（松弱/紧凶…）库里没有，引擎按「未知」跑。
- 只评**翻后**决策：引擎不覆盖翻前。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.coach import advise  # noqa: E402

POSTFLOP = ("flop", "turn", "river")
BOARD_LEN = {"flop": 3, "turn": 4, "river": 5}
AGGRESSIVE = ("bet", "raise")


def cards_of(board: str) -> list[str]:
    return [board[i:i + 2] for i in range(0, len(board) - 1, 2)]


def normalize(h: dict) -> dict:
    """兼容两种导出：本仓 tools/export_hands.sql 的 camelCase，和原始 DB 行的 snake_case。

    streets 内部两种都是 camelCase（amountBb / potStartBb），不用管。
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
        "streets": g("streets", default=[]),
    }


def walk(hand: dict) -> tuple[list[dict], str]:
    """按街重放，返回 (hero 的翻后决策点, hero 在行动序列里的标识)。

    每个决策点带：行动前的底池、hero 已投入、截至此刻的行动序列。
    """
    players = (hand.get("villainCount") or 1) + 1
    pot = ((hand.get("sb") or 0) + (hand.get("bb") or 0)
           + (hand.get("ante") or 0) * players + (hand.get("bombPot") or 0) * players)
    hero_token = hand.get("heroPosition") or "HERO"

    decisions: list[dict] = []
    seen: list[dict] = []
    hero_invested = 0.0

    for rec in hand.get("streets") or []:
        street = rec.get("street")
        if street not in ("preflop",) + POSTFLOP:
            continue
        level = (hand.get("bb") or 0) if street == "preflop" else 0.0
        commit: dict[str, float] = {}

        # 库里存了该街起始底池就直接用（比重建准）；没有才靠累加
        if rec.get("potStartBb") is not None:
            pot = float(rec["potStartBb"])

        for a in rec.get("actions") or []:
            actor, act = a.get("actor"), a.get("action")
            amt = a.get("amountBb")
            is_hero = actor == "hero"
            if is_hero and street in POSTFLOP:
                decisions.append({"street": street, "pot": pot,
                                  "hero_invested": hero_invested,
                                  "actions": list(seen),
                                  "actual": act, "actual_amt": amt})

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
            seen.append({"street": street, "actor": hero_token if is_hero else actor,
                         "action": act, "amount": amt})

    return decisions, hero_token


def classify(engine, actual, sizing, amt, pot) -> str:
    """把一次对比归到 一致 / 尺度差 / 动作不同。"""
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


def main(path: str) -> int:
    raw = Path(path).read_text().strip()
    if raw.startswith("["):                    # 整个文件是一个 JSON 数组（生产导出）
        hands = [normalize(h) for h in json.loads(raw)]
    else:                                      # JSONL：一行一手
        hands = [normalize(json.loads(l)) for l in raw.splitlines() if l.strip()]
    seen_sig: set[str] = set()
    dupes = 0
    buckets = {"一致": 0, "尺度差": 0, "动作不同": 0}
    skipped = 0
    rows = []

    for h in hands:
        sig = _signature(h)
        if sig in seen_sig:
            dupes += 1
            continue
        seen_sig.add(sig)

        board = cards_of(h.get("board") or "")
        decisions, hero_token = walk(h)
        for d in decisions:
            n = BOARD_LEN[d["street"]]
            if len(board) < n or not h.get("heroCards"):
                skipped += 1
                continue
            res = advise(hero_cards=h["heroCards"], board="".join(board[:n]),
                         street=d["street"], actions=d["actions"], pot=d["pot"],
                         hero_position=hero_token, opponents=h.get("villainCount"),
                         stack_bb=h.get("heroStackBb"),
                         hero_invested_bb=d["hero_invested"])
            label = res.hand.label if res.hand else "—"
            if not res.ok:
                skipped += 1
                rows.append((h["id"], d, label, res.action, res.sizing_bb, "跳过", res.reason))
                continue
            bucket = classify(res.action, d["actual"], res.sizing_bb, d["actual_amt"], d["pot"])
            buckets[bucket] += 1
            rows.append((h["id"], d, label, res.action, res.sizing_bb, bucket, res.reason))

    print(f"{'手':>7} {'街':<6} {'引擎持牌':<12} {'引擎':<12} {'实际':<12} {'判定':<8} 底池")
    print("─" * 92)
    for hid, d, label, action, sizing, bucket, reason in rows:
        eng = (action + (f" {sizing}" if sizing else "")) if action else "—"
        act = f"{d['actual']}" + (f" {d['actual_amt']}" if d.get("actual_amt") else "")
        print(f"{hid:>7} {d['street']:<6} {label:<12} {eng:<12} {act:<12} {bucket:<8} {d['pot']:.1f}")
        if bucket in ("动作不同", "跳过"):
            print(f"    └ {reason}")

    total = sum(buckets.values())
    print("─" * 92)
    print(f"决策点 {total} 个（跳过 {skipped} 个无法评估；另有 {dupes} 手重复已去重）")
    if total:
        for k in ("一致", "尺度差", "动作不同"):
            print(f"  {k}: {buckets[k]}  ({buckets[k] / total:.0%})")

        def aggr(v: str) -> bool:
            return v in ("bet", "raise", "allin")

        diff = [(e, d["actual"]) for _, d, _, e, _, b, _ in rows if b == "动作不同"]
        eng_pass = sum(1 for e, a in diff if not aggr(e) and aggr(a))   # 引擎保守、玩家激进
        eng_aggr = sum(1 for e, a in diff if aggr(e) and not aggr(a))   # 引擎激进、玩家保守
        print(f"  动作不同的方向：引擎保守/玩家激进 {eng_pass} ｜ 引擎激进/玩家保守 {eng_aggr} "
              f"｜ 两边都激进 {len(diff) - eng_pass - eng_aggr}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hands.jsonl"))
