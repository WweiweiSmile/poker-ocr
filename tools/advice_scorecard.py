#!/usr/bin/env python3
"""建议评分卡：按「决策节点 × 提示词版本」统计 AI 建议的动作组合与结果。

背景（2026-11-04）：用户的实盘基本按建议执行，所以「他的打法」≈「建议的
打法」。要研究的是建议本身，不是人。这个工具把 advice 记录（含 prompt
版本）按节点聚合成一张表，回答三个问题：

  1. 这个节点建议在做什么（bet/raise/check/call/fold 的比例）；
  2. 和这个池子的目标差多少（目标写在 TARGETS，来源见 docs/复盘-…-全量.md）；
  3. 换了提示词版本之后，动作组合变了没有（`--replay` 对齐同一批决策）。

节点口径：street × 是否面对下注 × 池型（limped/single/3bet+/bomb）× 牌力
档（strong/top_pair/mid/draw/air）。牌力档由 hero_cards + 当街公共牌重算，
不依赖 advice.facts（老记录里大多是空值）。

用法：
    .venv/bin/python tools/advice_scorecard.py
    .venv/bin/python tools/advice_scorecard.py --since 2026-11-04T20:00
    .venv/bin/python tools/advice_scorecard.py --replay /tmp/advice-ab.json
    .venv/bin/python tools/advice_scorecard.py --json /tmp/scorecard.json
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from poker_ocr.coach import evaluate  # noqa: E402

RANKS = "23456789TJQKA"
STREETS = ("preflop", "flop", "turn", "river")
POSTFLOP = ("flop", "turn", "river")
KIND_ZH = {"bet": "下注", "raise": "加注", "check": "过牌",
           "call": "跟注", "fold": "弃牌", "allin": "全下"}
AGGRESSIVE = ("bet", "raise", "allin")

# 节点目标（无人下注节点的下注率 / 河牌强牌下注率）。来源：
# docs/复盘-2026-11-04-紧弱与攻击力-全量.md；改目标也改这里。
TARGETS = {
    ("postflop", "no_bet"): 40.0,
    ("river", "strong"): 60.0,
}


def _card(text):
    text = (text or "").strip().upper().replace("10", "T")
    if len(text) != 2:
        return None
    return (RANKS.index(text[0]), text[1].lower())


def _cards(items):
    return [c for c in (_card(x) for x in items or []) if c]


POS_BUCKET = {"UTG": "EP", "UTG+1": "EP", "UTG+2": "EP", "LJ": "MP",
              "HJ": "MP", "CO": "LP", "BTN": "LP",
              "SB": "盲注", "BB": "盲注"}


def _pos_bucket(pos):
    return POS_BUCKET.get((pos or "").strip().upper(), "?")


def _strength(hole, board):
    """strong / top_pair / mid / draw / air（与复盘表同一个口径）。"""
    hole, board = _cards(hole), _cards(board)
    if len(hole) < 2 or len(board) < 3:
        return "air"
    cat, _ = evaluate(hole + board)
    if len(board) >= 5:
        bcat, _ = evaluate(board)
        if bcat >= cat and bcat >= 1:
            return "air"
    if cat >= 2:
        return "strong"
    if cat == 1:
        br = sorted({r for r, _ in board}, reverse=True)
        hr = [r for r, _ in hole]
        if any(r == br[0] for r in hr):
            return "top_pair"
        return "mid"
    # 听牌：4 张同花或单张成顺
    suits = collections.Counter(s for _, s in hole + board)
    if suits.most_common(1)[0][1] >= 4:
        return "draw"
    used = {s for _, s in hole + board}
    spade = next((s for s in "shdc" if s not in used), "s")
    have = {r for r, _ in hole + board}
    for rank in range(len(RANKS)):
        if rank in have:
            continue
        cat6, _ = evaluate(hole + board + [(rank, spade)])
        if cat6 == 4:
            return "draw"
    return "air"


def _pot_type(hand, pre_actions):
    """limped / single / 3bet+ / bomb / unknown，只看翻前。"""
    if not pre_actions:
        return "bomb"
    raises = sum(1 for a in pre_actions if a.get("action") in ("raise", "bet"))
    if raises >= 2:
        return "3bet+"
    if raises == 1:
        return "single"
    calls = sum(1 for a in pre_actions if a.get("action") == "call")
    return "limped" if calls else "unknown"


def _kind(option_id: str, to_call: float) -> str:
    oid = (option_id or "").lower()
    if oid.startswith("fold"):
        return "fold"
    if oid.startswith("allin"):
        return "allin"
    if oid.startswith("raise") or oid.startswith("open"):
        return "raise"
    if oid.startswith("call"):
        return "check" if (to_call or 0) <= 0 else "call"
    if oid.startswith("check"):
        return "check"
    if oid.startswith("bet"):
        return "bet"
    return oid or "?"


def _node(street, to_call, pot_type, strength):
    facing = "facing" if (to_call or 0) > 0 else "no_bet"
    return (street, facing, pot_type, strength)


def _load(path: Path):
    advices, hands = [], {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("type")
            if kind == "advice":
                advices.append(rec)
            elif kind == "hand":
                hands[rec.get("hand_id")] = rec
    return advices, hands


def _preflop_actions(hand):
    for street in hand.get("streets") or []:
        if street.get("street") == "preflop":
            return street.get("actions") or []
    return []


def _hero_action(hand, street):
    """该街 hero 的第一次动作 + 其后对手的反应。"""
    for rec in hand.get("streets") or []:
        if rec.get("street") != street:
            continue
        acts = rec.get("actions") or []
        for i, a in enumerate(acts):
            if not (a.get("is_hero") or a.get("seat_id") == "hero"):
                continue
            resp = collections.Counter()
            for b in acts[i + 1:]:
                if b.get("is_hero") or b.get("seat_id") == "hero":
                    break
                if b.get("action") in ("fold", "call", "raise"):
                    resp[b["action"]] += 1
                if b.get("action") == "raise":
                    break
            return a.get("action"), dict(resp)
    return None, {}


def _hand_net(hand, nets):
    return nets.get(hand.get("hand_id"))


def _load_nets(path: Path):
    try:
        import hand_leak_report as hlr
    except ImportError:
        return {}
    hands, _ = hlr.load(path)
    rows = hlr.build(hands)
    nets, _ = hlr.attach_nets(rows)
    return {r["hand"].get("hand_id"): nets.get(i) for i, r in enumerate(rows)}


def collect(advices, hands, nets):
    """每条建议 → 一行结构化记录。"""
    rows = []
    first_seen = set()
    for adv in advices:
        hid = adv.get("hand_id")
        hand = hands.get(hid) or {}
        street = adv.get("street")
        if street not in STREETS:
            continue
        pre = _preflop_actions(hand)
        pot_type = _pot_type(hand, pre)
        n_cards = {"flop": 3, "turn": 4, "river": 5}.get(street, 0)
        board = list(hand.get("board") or [])[:n_cards]
        if street == "preflop":
            strength = _pos_bucket(hand.get("hero_position"))
        else:
            strength = _strength(adv.get("hero_cards") or [], board)
        to_call = float(adv.get("to_call") or 0.0)
        kind = _kind((adv.get("option") or {}).get("id"), to_call)
        actual, resp = _hero_action(hand, street)
        followed = None
        if actual:
            followed = _kind(actual, to_call) == kind
        net = None
        if street != "preflop" and hid not in first_seen:
            first_seen.add(hid)
            net = _hand_net(hand, nets)
        rows.append(dict(
            hand=hid, ts=adv.get("ts"), street=street,
            version=adv.get("prompt_version") or "?",
            node=_node(street, to_call, pot_type, strength),
            kind=kind, option_id=(adv.get("option") or {}).get("id"),
            actual=actual, followed=followed,
            resp=resp, net=net,
            cards="".join(adv.get("hero_cards") or []),
            board="".join(board),
            reason=adv.get("reason") or "",
        ))
    return rows


def _mix(rows):
    c = collections.Counter(r["kind"] for r in rows)
    n = sum(c.values())
    return c, n


def _fmt_mix(c, n):
    if not n:
        return "—"
    order = ("bet", "raise", "call", "check", "fold", "allin")
    parts = [f"{KIND_ZH[k]} {c[k] / n * 100:.0f}%" for k in order if c.get(k)]
    return "｜".join(parts)


def _aggr_pct(rows):
    if not rows:
        return None
    return sum(1 for r in rows if r["kind"] in AGGRESSIVE) / len(rows) * 100


def _target(node):
    street, facing, _pot, strength = node
    if street == "river" and strength == "strong":
        return TARGETS[("river", "strong")]
    if facing == "no_bet" and street in POSTFLOP:
        return TARGETS[("postflop", "no_bet")]
    return None


def render(rows, replay_rows=None, min_n=8):
    out = []
    out.append("# 建议评分卡")
    versions = collections.Counter(r["version"] for r in rows)
    out.append("")
    out.append("提示词版本：" + "｜".join(
        f"{v} {n}" for v, n in versions.most_common()))
    out.append("")

    for street in STREETS:
        sub = [r for r in rows if r["street"] == street]
        if not sub:
            continue
        c, n = _mix(sub)
        aggr = _aggr_pct(sub)
        out.append(f"## {street}（n={n}）")
        out.append("")
        out.append(f"- 动作：{_fmt_mix(c, n)}")
        if aggr is not None:
            out.append(f"- 激进率（bet+raise）：{aggr:.0f}%")
        out.append("")

    out.append("## 节点（n≥%d）" % min_n)
    out.append("")
    out.append("| 街 | 面对下注 | 池型 | 牌力/位置 | n | 动作组合 | 激进率 | 目标 |")
    out.append("|---|---|---|---|---|---|---|---|")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["node"]].append(r)
    for node in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        sub = groups[node]
        if len(sub) < min_n:
            continue
        street, facing, pot, strength = node
        c, n = _mix(sub)
        aggr = _aggr_pct(sub)
        target = _target(node)
        out.append(
            f"| {street} | {facing} | {pot} | {strength} | {n} | "
            f"{_fmt_mix(c, n)} | {aggr:.0f}% | "
            f"{'—' if target is None else f'{target:.0f}%'} |")
    out.append("")

    # 对手反应：只统计「实际动作 = 建议动作」且建议是激进的节点。
    out.append("## 建议激进时，对手实际反应（只算跟做了的）")
    out.append("")
    out.append("| 街 | 池型 | 牌力 | n | 被弃 | 被跟 | 被加 |")
    out.append("|---|---|---|---|---|---|---|")
    resp_groups = collections.defaultdict(lambda: collections.Counter())
    cnt = collections.Counter()
    for r in rows:
        if r["kind"] not in AGGRESSIVE or not r["followed"] or not r["resp"]:
            continue
        key = (r["street"], r["node"][2], r["node"][3])
        cnt[key] += 1
        for k, v in r["resp"].items():
            resp_groups[key][k] += v
    for key in sorted(cnt, key=lambda k: -cnt[k]):
        if cnt[key] < min_n:
            continue
        tot = sum(resp_groups[key].values()) or 1
        c = resp_groups[key]
        out.append(f"| {key[0]} | {key[1]} | {key[2]} | {cnt[key]} | "
                   f"{c['fold'] / tot * 100:.0f}% | {c['call'] / tot * 100:.0f}% | "
                   f"{c['raise'] / tot * 100:.0f}% |")
    out.append("")

    # 手级净值：每手只在「第一条翻后建议」的节点上计一次，避免重复。
    out.append("## 手级净值（每手只计一次；单手 σ≈16bb，n<100 只看方向）")
    out.append("")
    out.append("| 街 | 面对下注 | 池型 | 牌力 | n | 净值 bb | 每手 |")
    out.append("|---|---|---|---|---|---|---|")
    net_groups = collections.defaultdict(list)
    for r in rows:
        if r["net"] is None:
            continue
        net_groups[r["node"]].append(r["net"])
    for node in sorted(net_groups, key=lambda k: -len(net_groups[k])):
        vals = net_groups[node]
        if len(vals) < min_n:
            continue
        out.append(f"| {node[0]} | {node[1]} | {node[2]} | {node[3]} | "
                   f"{len(vals)} | {sum(vals):+.1f} | {sum(vals) / len(vals):+.2f} |")
    out.append("")

    if replay_rows:
        out.extend(_render_ab(rows, replay_rows, min_n))
    return "\n".join(out)


def _render_ab(rows, replay_rows, min_n):
    """同一批决策：记录里的建议（旧）vs 复跑建议（新）。"""
    by_key = collections.defaultdict(list)
    for r in rows:
        by_key[(r["hand"], r["street"])].append(r)
    new_by_key = collections.defaultdict(list)
    for r in replay_rows:
        new_by_key[(r.get("hand"), r.get("street"))].append(r)

    # 复跑会跳过陈旧帧，按顺序 zip 会错位：用「旧建议 id」做一次对齐。
    pairs = []
    for key, old_rows in by_key.items():
        pool = list(new_by_key.get(key) or [])
        for o in old_rows:
            hit = next((i for i, n in enumerate(pool)
                        if str(n.get("old") or "") == str(o.get("option_id") or o.get("kind"))), None)
            if hit is None:
                hit = next((i for i, n in enumerate(pool)
                            if _kind(str(n.get("old") or ""), float(n.get("to_call") or 0))
                            == o["kind"]), None)
            if hit is None:
                continue
            n = pool.pop(hit)
            kind_new = _kind((n.get("new") or ""), float(n.get("to_call") or 0))
            pairs.append((o, kind_new))
    if not pairs:
        return ["", "## 版本对比：没有能对齐的决策点", ""]

    out = ["", "## 版本对比（同一批决策点：记录建议 → 复跑建议）", ""]
    old_c = collections.Counter(o["kind"] for o, _ in pairs)
    new_c = collections.Counter(k for _, k in pairs)
    n = len(pairs)
    out.append(f"- n={n}：旧 {_fmt_mix(old_c, n)}")
    out.append(f"- n={n}：新 {_fmt_mix(new_c, n)}")
    out.append(f"- 激进率：旧 {sum(old_c[k] for k in AGGRESSIVE) / n * 100:.0f}% → "
               f"新 {sum(new_c[k] for k in AGGRESSIVE) / n * 100:.0f}%")
    out.append("")
    out.append("| 街 | 面对下注 | 牌力 | n | 旧激进率 | 新激进率 |")
    out.append("|---|---|---|---|---|---|")
    groups = collections.defaultdict(list)
    for (o, k), (old_r, _) in zip(pairs, pairs):
        groups[(o["street"], o["node"][1], o["node"][3])].append((o["kind"], k))
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        g = groups[key]
        if len(g) < min_n:
            continue
        old_a = sum(1 for a, _ in g if a in AGGRESSIVE) / len(g) * 100
        new_a = sum(1 for _, b in g if b in AGGRESSIVE) / len(g) * 100
        out.append(f"| {key[0]} | {key[1]} | {key[2]} | {len(g)} | "
                   f"{old_a:.0f}% | {new_a:.0f}% |")
    out.append("")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="按节点统计 AI 建议的动作组合")
    ap.add_argument("--log", type=Path, default=Path("logs/live.states.jsonl"))
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--replay", type=Path, default=None,
                    help="replay_live_decisions.py --json 的输出，用来做版本对比")
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    advices, hands = _load(args.log)
    if args.since:
        advices = [a for a in advices if str(a.get("ts") or "") >= args.since]
    if args.until:
        advices = [a for a in advices if str(a.get("ts") or "") < args.until]
    nets = _load_nets(args.log)
    rows = collect(advices, hands, nets)

    replay_rows = None
    if args.replay and args.replay.exists():
        replay_rows = json.loads(args.replay.read_text(encoding="utf-8"))

    print(render(rows, replay_rows, args.min_n))
    if args.json:
        payload = [dict(node=list(r["node"]), **{k: v for k, v in r.items()
                                                if k != "node"}) for r in rows]
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"\n写出 {args.json}（{len(payload)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
