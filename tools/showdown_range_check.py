#!/usr/bin/env python3
"""摊牌反核范围：用他亮出来的手牌，检查 `range_flow` 收窄后的范围准不准。

为什么这条路可行
────────────────
弃牌时你永远看不到他的手牌（所以 `P(fold|tier)` 不可识别）。但**到摊牌的手牌是可见的**
—— 本地 573 手里有 341 条两手亮牌、288 条在河牌上可定档。于是可以问一个能回答的问题：

    模型在河牌给他的范围，和他真正亮出来的手牌，对得上吗？

三个指标
────────
1. **档位构成**：模型预测的 nut/strong/medium/weak/air 占比 vs 亮牌实测占比；
2. **百分位校准（PIT）**：把亮出的那一手放回模型的范围里，算「比它大的权重占比」。
   如果模型范围是对的，这个百分位应该**均匀分布**（每档 10%）。如果实测集中在高位
   （比如 80–100%），说明模型范围太松、留了太多弱牌；
3. **命中率**：亮出的组合是否还在模型的支撑集里。不在 = 模型把这个合法的组合排除了。

口径与偏差（重要）
──────────────────
  · 样本只包含**走到摊牌**的手 → 天然偏强（空气只有 7%）。所以它校准的是
    「到摊牌的条件分布」，不是全范围；
  · 对手的行动线来自录像回放，翻前形态可能缺失 → `field_ranges` 给不出先验时跳过
    （不猜）；跳过比例会打出来；
  · 多人池里亮牌的座位逐个算，样本不独立。

用法
────
    .venv/bin/python tools/showdown_range_check.py
    STRATEGY_TABLES_PROFILE=sourced .venv/bin/python tools/showdown_range_check.py
    .venv/bin/python tools/showdown_range_check.py --html reports/showdown-range-2026-11-05.html
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import field_ranges, range_flow, ranges, strategy_tables  # noqa: E402
from poker_ocr.coach import evaluate, parse_cards  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

TIERS = ("nut", "strong", "medium", "weak", "air")
TIER_ZH = {"nut": "坚果/近坚果", "strong": "强成牌", "medium": "中等",
           "weak": "弱", "air": "空气"}
STREETS = ("preflop", "flop", "turn", "river")


@dataclass(frozen=True)
class Sample:
    hand_id: str
    seat_id: str
    tier: str
    percentile: float      # 比它强的权重占比（0 = 最强，1 = 最弱）
    in_range: bool
    width: int             # 该座位模型范围的组合数
    street: str
    predicted: dict         # 模型在该节点的档位构成（加权占比）
    prior_id: str = ""
    miss: str = ""          # 不在范围时的原因：prior_absent / cut_by_events
    hand_key: str = ""      # 169 键（如 "T9s"），缺口统计用


def _hands() -> list[dict]:
    """本地手牌：hands/*.json + outbox。"""
    import glob
    out: list[dict] = []
    for p in glob.glob(str(ROOT / "hands" / "*.json")):
        out += json.loads(Path(p).read_text(encoding="utf-8"))
    for sub in ("pending", "discarded"):
        for p in glob.glob(str(ROOT / "data" / "hand_outbox" / sub / "*.json")):
            try:
                out.append(json.loads(Path(p).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return out


def _to_streets(hand: dict) -> tuple[StreetRecord, ...]:
    recs = []
    for st in hand.get("streets") or []:
        street = str(st.get("street") or "")
        if street not in STREETS:
            continue
        actions = tuple(ActionRecord(
            street=street,
            seat_id=str(a.get("seat_id") or a.get("actor") or ""),
            name=a.get("name"),
            position=a.get("position"),
            action=str(a.get("action") or ""),
            amount=(float(a.get("amount", a.get("amountBb")))
                    if a.get("amount", a.get("amountBb")) is not None else None),
            all_in=bool(a.get("all_in")),
            is_hero=bool(a.get("is_hero")) or str(a.get("seat_id")) == "hero",
        ) for a in st.get("actions") or ())
        recs.append(StreetRecord(street, st.get("pot_start", st.get("potStartBb")),
                                 actions))
    return tuple(recs)


def _snap(hand: dict) -> HandSnapshot | None:
    board = tuple(hand.get("board") or ())
    hero = tuple(hand.get("hero_cards") or ())
    if len(board) < 5 or len(hero) != 2:
        return None
    streets = _to_streets(hand)
    if not streets:
        return None

    seats: dict[str, dict] = {}
    for rec in streets:
        for a in rec.actions:
            if a.is_hero or not a.seat_id:
                continue
            info = seats.setdefault(a.seat_id, {"position": a.position,
                                                "folded": False, "bet": 0.0})
            if a.position and not info["position"]:
                info["position"] = a.position
            if a.action == "fold":
                info["folded"] = True
            if a.amount:
                info["bet"] = float(a.amount)

    pre = next((r for r in streets if r.street == "preflop"), None)
    pre_actions = pre.actions if pre else ()
    raises = sum(1 for a in pre_actions if a.action in ("raise", "bet"))
    hero_is_pfr = any(a.is_hero and a.action in ("raise", "bet") for a in pre_actions)
    if raises == 0:
        limped = any(a.action == "call" for a in pre_actions if not a.is_hero)
        shapes = ("limped",) if limped else ("unopened",)
    elif raises == 1:
        shapes = ("single-raise",)
    else:
        shapes = ("three-bet",)

    b = hand.get("blinds") or {}
    try:
        blinds = Blinds(float(b.get("sb") or 0.5), float(b.get("bb") or 1.0),
                        float(b["ante"]) if b.get("ante") else None,
                        str(b.get("raw") or ""))
    except (TypeError, ValueError):
        blinds = None

    river = next((r for r in streets if r.street == "river"), None)
    return HandSnapshot(
        hand_id=str(hand.get("hand_id") or ""),
        hand_index=0,
        street="river",
        board=board[:5],
        hero_cards=hero,
        hero_position=hand.get("hero_position"),
        hero_stack=hand.get("hero_stack_bb"),
        hero_bet=0.0,
        hero_invested=None,
        to_call=0.0,
        second_bet=None,
        pot=float(hand.get("pot") or 0.0) or None,
        pot_before_street=(float(river.pot_start) if river and river.pot_start else None),
        table_size=int(hand.get("table_size") or (len(seats) + 1)),
        blinds=blinds,
        opponents=tuple(
            OpponentSnapshot(seat_id=sid, name=None,
                             position=info["position"], stack=None,
                             bet=float(info["bet"] or 0.0),
                             folded=bool(info["folded"]))
            for sid, info in seats.items()),
        streets=streets,
        preflop_shapes=shapes,
        hero_is_pfr=hero_is_pfr,
        cbet_spot=False,
        decision_key=(str(hand.get("hand_id")), "river", 0, 0.0, 0.0, hero),
        players_dealt=len(seats) + 1,
    )


def _percentile(built, dead, board, cards) -> tuple[float, bool]:
    """亮出的这手在模型范围里的位置：比它强的权重占比。"""
    combo = tuple(cards)
    combo_key = frozenset(combo)          # combo 的规范序在 make_range 里会排序，
    score = evaluate(list(cards) + list(board))   # 所以比较要按「无落差集合」
    total = 0.0
    better = 0.0
    present = False
    for cb, w in built.combo_weights.items():
        if w <= 0:
            continue
        if cb[0] in dead or cb[1] in dead:
            continue
        same = frozenset(cb) == combo_key
        total += w
        if same:
            present = True
        try:
            s = evaluate(list(cb) + list(board))
        except ValueError:
            continue
        if s > score:
            better += w
        elif s == score and not same:
            better += w / 2.0          # 同强度按半计入，避免平局堆积在 0 或 1
    if total <= 0:
        return 0.0, False
    return better / total, present


def _tier_shares(built, dead, board) -> dict[str, float]:
    """模型范围在本节点的档位构成（按权重）。"""
    acc = {t: 0.0 for t in TIERS}
    total = 0.0
    for cb, w in built.combo_weights.items():
        if w <= 0 or cb[0] in dead or cb[1] in dead:
            continue
        acc[range_flow.hand_class(cb, board).made] += w
        total += w
    if total <= 0:
        return {}
    return {t: v / total for t, v in acc.items() if v > 0}


def collect(limit: int = 0) -> tuple[list[Sample], dict]:
    stats = Counter()
    samples: list[Sample] = []
    for hand in _hands():
        snap = _snap(hand)
        if snap is None:
            stats["no_snapshot"] += 1
            continue
        board = tuple(parse_cards("".join(snap.board)))
        dead = set(board) | set(parse_cards("".join(snap.hero_cards)))
        try:
            field = field_ranges.resolve(snap)
        except Exception:                                  # noqa: BLE001
            stats["field_error"] += 1
            continue
        if not field.specs:
            stats["no_specs"] += 1
            stats["warn:" + (field.warnings[0][:40] if field.warnings else "")]
            continue
        for show in hand.get("shows") or ():
            cards = show.get("cards") or ()
            if len(cards) != 2:
                continue
            seat = str(show.get("seat_id") or "")
            spec = next((s for s in field.specs if s.seat_id == seat), None)
            if spec is None or not spec.prior_id:
                stats["seat_not_modeled"] += 1
                continue
            prior = ranges.get(spec.prior_id)
            if prior is None:
                stats["prior_missing"] += 1
                continue
            built = range_flow.build_range(prior, spec.events or (), dead=dead)
            if built is None:
                stats["range_none"] += 1
                continue
            combo = tuple(parse_cards("".join(cards)))
            if len(combo) != 2:
                continue
            pct, present = _percentile(built, dead, board, combo)
            pred = _tier_shares(built, dead, board)
            width = sum(1 for w in built.combo_weights.values() if w > 0)
            if not present:
                stats["out_of_range"] += 1
            stats["tier:" + range_flow.hand_class(combo, board).made] += 1
            reason = ""
            if not present:                       # 为什么不在：先验没有 vs 被后手砍掉
                in_prior = any(cb == combo or (cb[0] == combo[1] and cb[1] == combo[0])
                               for cb, w in prior.combo_weights.items() if w > 0)
                reason = "cut_by_events" if in_prior else "prior_absent"
                stats["miss:" + reason] += 1
            samples.append(Sample(snap.hand_id, seat,
                                  range_flow.hand_class(combo, board).made,
                                  pct, present, width, "river", pred,
                                  spec.prior_id, reason,
                                  ranges.key_of_combo(combo)))
            if limit and len(samples) >= limit:
                return samples, dict(stats)
    return samples, dict(stats)


def report(samples: list[Sample], stats: dict) -> None:
    n = len(samples)
    print(f"profile: {strategy_tables.profile()}")
    print(f"可用样本 {n}｜跳过的原因：{ {k: v for k, v in stats.items() if not k.startswith('tier:')} }")
    if not n:
        return
    inr = sum(1 for s in samples if s.in_range)
    print(f"亮出的组合仍在模型支撑集：{inr}/{n} = {inr / n:.0%}"
          f"（不在 = 模型把它排除了，属于「范围太窄」的硬证据）")

    obs = Counter(s.tier for s in samples)
    pred = {t: (sum(s.predicted.get(t, 0.0) for s in samples) / n) for t in TIERS}
    print(f"\n{'档位':10s}{'实测占比':>10s}{'样本':>8s}{'模型预测':>12s}{'差':>8s}")
    for t in TIERS:
        share = obs.get(t, 0) / n
        print(f"{TIER_ZH[t]:10s}{share:10.1%}{obs.get(t, 0):8d}"
              f"{pred[t]:12.1%}{share - pred[t]:+8.1%}")

    miss = Counter(s.miss for s in samples if not s.in_range)
    if miss:
        print("  不在范围的原因：" + "，".join(f"{k} {v}" for k, v in miss.items()))
        by_prior = Counter(s.prior_id for s in samples if not s.in_range)
        by_tier = Counter(s.tier for s in samples if not s.in_range)
        print("  按先验：" + "，".join(f"{k} {v}" for k, v in by_prior.most_common(5)))
        print("  按档位：" + "，".join(f"{TIER_ZH[t]} {by_tier.get(t, 0)}"
                                for t in TIERS if by_tier.get(t)))
    inside = [s for s in samples if s.in_range]
    buckets = [0] * 10
    for s in inside:
        buckets[min(9, max(0, int(s.percentile * 10)))] += 1
    n_in = len(inside)
    print(f"\n百分位校准（只算仍在支撑集里的 {n_in} 个；均匀 = 模型范围与实测一致）")
    print(f"{'百分位':>12s}{'样本':>8s}{'占比':>8s}{'期望':>8s}")
    for i, c in enumerate(buckets):
        print(f"{f'{i * 10}-{(i + 1) * 10}%':>12s}{c:8d}"
              f"{(c / n_in if n_in else 0):8.1%}{0.10:8.1%}")
    mean = (sum(s.percentile for s in inside) / n_in) if n_in else 0.0
    print(f"百分位均值 {mean:.3f}（均匀应 ≈0.50；>0.5 = 他的牌比模型预期更弱/模型太松，"
          f"<0.5 = 模型太紧）")
    print(f"模型中位宽度 {sorted(s.width for s in samples)[n // 2]} 组合")


def main() -> int:
    ap = argparse.ArgumentParser(description="摊牌反核范围")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--html", default=None)
    args = ap.parse_args()
    samples, stats = collect(args.limit)
    report(samples, stats)
    if args.html and samples:
        _write_html(args.html, samples, stats)
        print(f"\n已写入 {args.html}")
    return 0


def _write_html(path: str, samples: list[Sample], stats: dict) -> None:
    n = len(samples)
    miss = Counter(s.miss for s in samples if not s.in_range)
    if miss:
        print("  不在范围的原因：" + "，".join(f"{k} {v}" for k, v in miss.items()))
        by_prior = Counter(s.prior_id for s in samples if not s.in_range)
        by_tier = Counter(s.tier for s in samples if not s.in_range)
        print("  按先验：" + "，".join(f"{k} {v}" for k, v in by_prior.most_common(5)))
        print("  按档位：" + "，".join(f"{TIER_ZH[t]} {by_tier.get(t, 0)}"
                                for t in TIERS if by_tier.get(t)))
    inside = [s for s in samples if s.in_range]
    n = len(inside) or 1
    buckets = [0] * 10
    for s in inside:
        buckets[min(9, max(0, int(s.percentile * 10)))] += 1
    W, H, PAD = 720, 320, 48
    bw = (W - 2 * PAD) / 10
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="-apple-system,sans-serif">',
             f'<rect width="{W}" height="{H}" fill="#fff"/>']
    mx = max(max(buckets), 1)
    for i in range(5):
        y = H - PAD - (H - 2 * PAD) * i / 4
        parts.append(f'<line x1="{PAD}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{PAD - 8}" y="{y + 4:.1f}" font-size="11" fill="#888" '
                     f'text-anchor="end">{mx * i // 4}</text>')
    exp = (H - 2 * PAD) * (n / 10) / mx
    parts.append(f'<line x1="{PAD}" y1="{H - PAD - exp:.1f}" x2="{W - PAD}" '
                 f'y2="{H - PAD - exp:.1f}" stroke="#d9480f" stroke-dasharray="5 4"/>')
    for i, c in enumerate(buckets):
        h = (H - 2 * PAD) * c / mx
        x = PAD + i * bw + 6
        parts.append(f'<rect x="{x:.1f}" y="{H - PAD - h:.1f}" width="{bw - 12:.1f}" '
                     f'height="{h:.1f}" fill="#1c7ed6" opacity="0.85"/>')
        parts.append(f'<text x="{x + (bw - 12) / 2:.1f}" y="{H - PAD + 16:.1f}" '
                     f'font-size="11" fill="#666" text-anchor="middle">{i * 10}</text>')
    parts.append(f'<text x="{W / 2:.0f}" y="{H - 8}" font-size="12" fill="#555" '
                 f'text-anchor="middle">亮出的手牌在模型范围里的百分位（n={n}，'
                 f'虚线 = 均匀期望）</text>')
    parts.append("</svg>")
    html = ("<!doctype html><meta charset='utf-8'><title>摊牌反核范围</title>"
            "<body style='margin:24px;font-family:-apple-system,sans-serif'>"
            f"<h2>摊牌反核范围（n={n}）</h2>" + "".join(parts) +
            "<p style='color:#666;font-size:13px;max-width:740px'>"
            "每根柱 = 亮出的手牌落在模型范围的哪个百分位。均匀分布 = 模型范围与实测一致；"
            "柱子偏向右侧（80–100%）说明模型留了太多弱牌，偏向左侧说明模型太窄。</p>"
            "</body>")
    Path(path).write_text(html, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
