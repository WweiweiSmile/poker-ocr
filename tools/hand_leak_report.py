#!/usr/bin/env python3
"""全量手牌复盘：把 live.states.jsonl 里的每一手算成「净值 + 上下文」，输出统计与清单。

口径（和 tools/hand_group_stats.py 的差异都是刻意的）：

  · **净值**用相邻两手的 `hero_stack_bb` 相减，但**只丢真正不可能的读数**：
    输超过手里筹码（delta < -start）。赢的上限用 `pot_estimate()`（记录的 pot 和
    「每条街起始底池 + 该街动作总和」取大）而不是自带筹码 —— 一手赢到的钱本来
    就可以超过自带筹码（对手的钱也在池里），老工具 `abs(delta) > start` 正好把
    +124.5 / +42.5 这类真实大胜丢了，把 -244 报成 -530。
  · **pot 串到下一手**（赢的比 pot 多、但不超过两倍）照赢算，标
    `win_pot_undercounted` 备查；**补码**（这手没有任何行动、正跳变 ≥20）单独列出，
    不计入净值。
  · **缺筹码读数的那一手**：把跨过缺口的差记在缺口前一手，标 `gap` ——
    否则整段总账会漏掉这一笔（本次 -53）。
  · 净值只在**同一个 session 内**相邻才有效；跨 session 是重买/换桌。
  · hero 的动作在日志里是**抽样**的（每 30 秒左右一帧，漏检多），所以 VPIP/PFR
    只当**下限**看；下注尺寸、大池里的动作线基本都能抓到。

用法：
    nv tools/hand_leak_report.py
    nv tools/hand_leak_report.py --json
    nv tools/hand_leak_report.py --top 20
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.opponent_profile import showdown_reads  # noqa: E402

DEFAULT = Path("logs/live.states.jsonl")
RANKS = "23456789TJQKA"
SUITS = "shdc"
RV = {r: i for i, r in enumerate(RANKS, start=2)}
CAT = {8: "同花顺", 7: "四条", 6: "葫芦", 5: "同花", 4: "顺子",
       3: "三条", 2: "两对", 1: "一对", 0: "高牌"}
POSITIONS = ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN", "SB", "BB"]


# ───────────────────────────── 牌力 ─────────────────────────────
def card(text):
    text = (text or "").strip().upper().replace("10", "T")
    if len(text) != 2:
        return None
    rank, suit = text[0], text[1].lower()
    if rank not in RANKS or suit not in SUITS:
        return None
    return (RV[rank], suit)


def clean_shows(hand):
    """只留过了 clean 闸门的亮牌（#20）：弃牌座位上的亮牌是串手的残影。"""
    clean = {r.seat_id for r in showdown_reads(hand) if r.clean}
    return [s for s in hand.get("shows") or []
            if str(s.get("seat_id") or "") in clean]


def cards(items):
    return [c for c in (card(x) for x in items or []) if c]


def score5(hand):
    ranks = sorted((c[0] for c in hand), reverse=True)
    flush = len({c[1] for c in hand}) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            straight = 5
    counts = Counter(ranks)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = tuple(g[1] for g in groups)
    kickers = tuple(g[0] for g in groups)
    if straight and flush:
        return (8, straight)
    if shape == (4, 1):
        return (7,) + kickers
    if shape == (3, 2):
        return (6,) + kickers
    if flush:
        return (5,) + tuple(ranks)
    if straight:
        return (4, straight)
    if shape == (3, 1, 1):
        return (3,) + kickers
    if shape == (2, 2, 1):
        return (2,) + kickers
    if shape == (2, 1, 1, 1):
        return (1,) + kickers
    return (0,) + tuple(ranks)


def best_hand(cards_):
    if len(cards_) < 5:
        return None
    return max(score5(c) for c in combinations(cards_, 5))


# ───────────────────────────── 读日志 ─────────────────────────────
def load(path):
    hands, starts, session = [], [], -1
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        if kind == "session_start":
            session += 1
            starts.append(rec.get("started_at"))
        elif kind == "hand":
            rec["_s"] = session
            hands.append(rec)
    return hands, starts


def hero_actions(hand, street=None):
    out = []
    for st in hand.get("streets") or []:
        if street and st.get("street") != street:
            continue
        for act in st.get("actions") or []:
            if act.get("is_hero") or act.get("seat_id") == "hero":
                out.append(act)
    return out


def is_bomb(hand):
    pre = next((s for s in hand.get("streets") or []
                if s.get("street") == "preflop"), None)
    if pre is not None and pre.get("actions"):
        return False
    pot_start = (pre or {}).get("pot_start") or 0.0
    if pot_start >= 25 and pot_start % 5 == 0:
        return True
    return bool(hand.get("bomb_pot"))


def hand_bucket(hole):
    if len(hole) != 2:
        return "?"
    (r1, s1), (r2, s2) = hole
    hi, lo = max(r1, r2), min(r1, r2)
    suited, pair, gap = s1 == s2, r1 == r2, hi - lo
    if pair:
        return "口袋对"
    if suited and hi == 14:
        return "A同花"
    if suited and lo >= 10:
        return "同花大牌"
    if suited and gap <= 2 and hi <= 13:
        return "同花连张"
    if suited:
        return "弱同花"
    if hi == 14:
        return "A杂色"
    if lo >= 10:
        return "杂色大牌"
    if gap <= 2 and hi >= 11:
        return "杂色连张"
    return "杂牌"


def build(hands):
    """每手一条记录：净值、动作、底池、牌力。"""
    rows = []
    for i, hand in enumerate(hands):
        hole, board = cards(hand.get("hero_cards")), cards(hand.get("board"))
        pre = next((s for s in hand.get("streets") or []
                    if s.get("street") == "preflop"), None)
        pre_actions = (pre or {}).get("actions") or []
        hero_pre = hero_actions(hand, "preflop")
        voluntary = [a for a in hero_pre if a.get("action") in ("call", "raise", "bet")]
        raised = any(a.get("action") == "raise" for a in hero_pre)
        faced_raise = any(a.get("action") == "raise" for a in pre_actions)
        limped = any(a.get("action") == "call" for a in hero_pre) and not faced_raise
        cold = any(a.get("action") == "call" for a in hero_pre) and faced_raise
        flop_actions = [a for a in (next((s for s in hand.get("streets") or []
                                          if s.get("street") == "flop"), {}) or {}).get("actions") or []]
        hero_flop = [a for a in flop_actions
                     if a.get("is_hero") or a.get("seat_id") == "hero"]
        rows.append(dict(
            index=i, session=hand["_s"], ts=hand["ts"], hand_id=hand.get("hand_id"),
            hole=hole, pos=hand.get("hero_position"), board=board,
            pot=float(hand.get("pot") or 0.0), stack=hand.get("hero_stack_bb"),
            dealt=hand.get("players_dealt"),
            voluntary=bool(voluntary), raised=raised, limped=limped, cold=cold,
            saw_flop=bool(hero_flop),
            bets=[(a.get("amount") or 0.0) for a in
                  [x for st in hand.get("streets") or [] for x in st.get("actions") or []
                   if (x.get("is_hero") or x.get("seat_id") == "hero") and x.get("action") == "bet"]],
            calls=[(a.get("amount") or 0.0) for a in
                   [x for st in hand.get("streets") or [] for x in st.get("actions") or []
                    if (x.get("is_hero") or x.get("seat_id") == "hero") and x.get("action") == "call"]],
            cls=hand_bucket(hole),
            final=best_hand(hole + board) if len(board) == 5 else None,
            bomb=is_bomb(hand),
            shown=[best_hand(cards(s.get("cards")) + board)
                   for s in clean_shows(hand) if len(board) == 5],
            hand=hand))
    return rows


def pot_estimate(row):
    """这手「至少有多大」的底池。

    落库的 `pot` 字段偶尔会串到下一手（本次 4 处，出现 pot=5.5 而翻前底池 35 的
    情况），所以取「记录的 pot」和「每条街的起始底池 + 该街动作总和」的较大值。
    赢的上限用这个数，比用单个 pot 字段可靠。
    """
    best = row["pot"]
    for street in row["hand"].get("streets") or []:
        pot_start = street.get("pot_start")
        if pot_start is None:
            continue
        total = pot_start + sum(a.get("amount") or 0.0
                                for a in street.get("actions") or [])
        best = max(best, total)
    return best


def attach_nets(rows):
    """相邻两个**有筹码读数**的手之间做差 = 这手的净赢输。

    中间缺读数时（换手、全下遮住筹码），把跨过缺口的差记在**缺口前那一手**上，
    并标 `gap=True`：整段总账不会因为缺一帧就少算钱。返回 (净值字典, 异常清单)。
    """
    nets, odd = {}, []
    prev = None
    for i, cur in enumerate(rows):
        if cur["stack"] is None:
            continue
        if prev is None or rows[prev]["session"] != cur["session"]:
            prev = i
            continue
        start, end = rows[prev]["stack"], cur["stack"]
        delta = round(end - start, 2)
        gap = i - prev > 1
        pot = pot_estimate(rows[prev])
        allin = any((a.get("seat_id") == "hero") for a in
                    (rows[prev]["hand"].get("all_ins") or []))
        entry = dict(index=prev, delta=delta, pot=round(pot, 1),
                     hand_id=rows[prev]["hand_id"], ts=rows[prev]["ts"], gap=gap)
        if -start <= delta <= pot + 1:
            nets[prev] = delta
            if gap:
                odd.append(dict(entry, kind="gap"))
        elif delta > pot + 1 and rows[prev]["hand"].get("streets") and delta <= 2 * pot + 1:
            # 赢的比底池字段还多、但没超过两倍：多半是落库的 pot 串到了下一手
            # （一次下注/加注没记进 pot），不是补码 —— 照赢算，标出来备查。
            nets[prev] = delta
            odd.append(dict(entry, kind="win_pot_undercounted"))
        elif delta > pot + 1 and not allin and delta >= 20:
            odd.append(dict(entry, kind="topup"))
        else:
            odd.append(dict(entry, kind="impossible"))
        prev = i
    for r in rows:
        r["net"] = nets.get(r["index"])
    return nets, odd


def summarize(rows, nets, odd, starts):
    vals = [nets[i] for i in nets]
    sessions = defaultdict(list)
    for i, d in nets.items():
        sessions[rows[i]["session"]].append(d)

    daily = defaultdict(list)
    for s, ds in sessions.items():
        day = (starts[s] or "")[5:10]
        daily[day].extend(ds)

    def group(key):
        out = {}
        for name in sorted({r[key] for r in rows}):
            g = [r for r in rows if r[key] == name]
            gn = [r for r in g if r["net"] is not None]
            out[name] = dict(n=len(g), net=round(sum(r["net"] for r in gn), 1),
                             per_hand=round(sum(r["net"] for r in gn) / max(len(gn), 1), 2),
                             voluntary=round(sum(r["voluntary"] for r in g) / max(len(g), 1), 3),
                             raised=round(sum(r["raised"] for r in g) / max(len(g), 1), 3))
        return out

    buckets = [(-9999, -80), (-80, -40), (-40, -20), (-20, -10),
               (-10, -2), (-2, 0), (0, 9999)]
    loss_buckets = []
    for lo, hi in buckets:
        g = [d for d in vals if lo < d <= hi]
        loss_buckets.append(dict(lo=lo, hi=hi, n=len(g), total=round(sum(g), 1),
                                 mean=round(sum(g) / max(len(g), 1), 2)))

    by_cat = {}
    for cat in range(8, -1, -1):
        g = [r for r in rows if r["final"] and r["final"][0] == cat and r["net"] is not None]
        if g:
            by_cat[CAT[cat]] = dict(n=len(g), won=sum(1 for r in g if r["net"] > 0),
                                    net=round(sum(r["net"] for r in g), 1))

    # 摊牌：有对手亮牌 + 有净值
    showdown = [r for r in rows if r["shown"] and r["net"] is not None]
    sd_win = [r for r in showdown if r["net"] > 0]

    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return dict(
        hands=len(rows),
        hands_with_net=len(vals),
        net=round(sum(vals), 1),
        mean=round(statistics.mean(vals), 3) if vals else 0.0,
        stdev=round(stdev, 2),
        se=round(stdev / len(vals) ** 0.5, 2) if vals else 0.0,
        wins=len([v for v in vals if v > 0]),
        losses=len([v for v in vals if v < 0]),
        gross_win=round(sum(v for v in vals if v > 0), 1),
        gross_loss=round(sum(v for v in vals if v < 0), 1),
        odd=odd,
        sessions={s: dict(n=len(ds), net=round(sum(ds), 1), day=(starts[s] or "")[5:16])
                  for s, ds in sorted(sessions.items())},
        days={d: dict(n=len(v), net=round(sum(v), 1)) for d, v in sorted(daily.items())},
        by_position=group("pos"),
        by_class=group("cls"),
        loss_buckets=loss_buckets,
        by_category=by_cat,
        showdown=dict(n=len(showdown), won=len(sd_win),
                      net=round(sum(r["net"] for r in showdown), 1)),
        bomb=dict(n=sum(1 for r in rows if r["bomb"]),
                  net=round(sum(r["net"] for r in rows if r["bomb"] and r["net"] is not None), 1)),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="全量手牌复盘（净值 + 上下文统计）")
    ap.add_argument("source", nargs="?", type=Path, default=DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    hands, starts = load(args.source)
    rows = build(hands)
    nets, odd = attach_nets(rows)
    rep = summarize(rows, nets, odd, starts)
    rep["source"] = str(args.source)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    print(f"来源 {args.source}")
    print(f"手牌 {rep['hands']} 手（有净值 {rep['hands_with_net']}），"
          f"净 {rep['net']:+.1f}bb，均值 {rep['mean']:+.3f}bb/手，"
          f"标准差 {rep['stdev']}，标准误 ±{rep['se']}")
    print(f"赢 {rep['wins']} 手 / 合计 {rep['gross_win']:+.1f}；"
          f"输 {rep['losses']} 手 / 合计 {rep['gross_loss']:+.1f}")

    print("\n按天：")
    for day, d in rep["days"].items():
        print(f"  {day}  n={d['n']:>3}  net={d['net']:>+8.1f}")

    print("\n按 session：")
    for s, d in rep["sessions"].items():
        print(f"  s{s:02d} {d['day']}  n={d['n']:>3}  net={d['net']:>+8.1f}")

    print("\n按位置：")
    print(f"{'位置':<7}{'手数':>5}{'主动入池':>9}{'加注':>7}{'净值':>10}{'每手':>8}")
    for pos in POSITIONS:
        d = rep["by_position"].get(pos)
        if not d:
            continue
        print(f"{pos:<7}{d['n']:>5}{d['voluntary']:>9.0%}{d['raised']:>7.0%}"
              f"{d['net']:>+10.1f}{d['per_hand']:>+8.2f}")

    print("\n按起手牌型：")
    for name, d in rep["by_class"].items():
        print(f"  {name:<6} n={d['n']:>3} 入池={d['voluntary']:>4.0%} "
              f"net={d['net']:>+8.1f} per={d['per_hand']:>+6.2f}")

    print("\n按单手盈亏分桶：")
    for b in rep["loss_buckets"]:
        lo = "-∞" if b["lo"] < -999 else f"{b['lo']:.0f}"
        print(f"  ({lo},{b['hi']:.0f}]  n={b['n']:>4}  合计={b['total']:>+9.1f}  均值={b['mean']:>+7.2f}")

    print("\n按成牌（有 5 张公共牌）：")
    for name, d in rep["by_category"].items():
        print(f"  {name:<4} n={d['n']:>3} 赢={d['won']:>3} net={d['net']:>+8.1f}")

    print(f"\n炸弹池：{rep['bomb']['n']} 手，net={rep['bomb']['net']:+.1f}")
    print(f"摊牌（对手亮牌）：{rep['showdown']['n']} 次，赢 {rep['showdown']['won']} 次，"
          f"net={rep['showdown']['net']:+.1f}")

    print("\n读数异常（补码/全下/缺帧）：")
    for o in rep["odd"]:
        print(f"  {o}")

    print(f"\n最大的 {args.top} 手亏损：")
    losers = sorted([r for r in rows if r["net"] is not None], key=lambda r: r["net"])[:args.top]
    for r in losers:
        line = []
        for st in r["hand"].get("streets") or []:
            acts = " ".join(
                ("H:" if (a.get("is_hero") or a.get("seat_id") == "hero") else "") +
                f"{a.get('action')}{'' if not a.get('amount') else round(a['amount'], 1)}"
                for a in st.get("actions") or [])
            line.append(f"{st['street'][:2]}({st.get('pot_start')}) {acts}")
        board = " ".join(r["hand"].get("board") or [])
        shows = [(s.get("position"), " ".join(s.get("cards") or []))
                 for s in r["hand"].get("shows") or []]
        print(f"  {r['net']:+8.1f} {r['ts'][5:16]} {r['hand_id']:<22} "
              f"{r['pos']:<5} pot={r['pot']:>6.1f} {board}")
        print(f"          {' | '.join(line)[:300]}")
        if shows:
            print(f"          亮牌 {shows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
