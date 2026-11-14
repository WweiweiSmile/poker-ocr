"""一次性原型：把手牌重放成「范围推导时间线」，生成只读 HTML。

用途：验证 docs/看板-范围推导-信息架构.md 的线框在真实数据上能不能
一屏回答四个验收问题，并暴露 trace schema 的缺口。不接入 live，不是产品代码。

    nv tools/proto_range_timeline.py            # 生成 docs/proto/range-timeline-prototype.html
    nv tools/proto_range_timeline.py --open     # 顺手用默认浏览器打开

数据来源：hands/1023.hands.json 的真实手牌（归档里没有筹码量，SPR 一律 99，
spr_bucket 退化，原型里标注为数据缺口）。模板在 tools/proto_range_timeline.html.tpl。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import field_ranges, hand_vs_ranges, range_flow, ranges  # noqa: E402
from poker_ocr.hand_vs_ranges import _as_cards  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, Blinds, HandSnapshot, OpponentSnapshot, StreetRecord)

STREETS = ("preflop", "flop", "turn", "river")
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}

# 选中的真实样本：决策点 = 当前街 hero 最后一条建议记录里的 pot。
PICKS = (
    ("h104", "flop", "三人 3-bet 池：AK 在 9s8sTs 面 c-bet 11.5，被加注到 52（面对 40.5）"),
    ("h4", "turn", "单挑多街：翻前加注、翻牌 1/3 下注被跟、转牌再打 1/2 后的抉择"),
    ("h8", "flop", "五家池：BB 满池 donk 进四家，SB 拿 T6o 中对的抉择"),
)


def hands() -> dict:
    out = {}
    for p in sorted(ROOT.glob("hands/*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for h in (data if isinstance(data, list) else data.get("hands", [])):
            if h.get("hand_id"):
                out[str(h["hand_id"])] = h
    return out


def _actions(hand: dict, street: str, cut=None):
    for st in hand.get("streets") or []:
        if st.get("street") != street:
            continue
        recs = [ActionRecord(
            street=street,
            seat_id=str(a.get("seat_id") or ""),
            name=a.get("name"),
            position=a.get("position"),
            action=str(a.get("action") or ""),
            amount=(float(a["amount"]) if a.get("amount") is not None else None),
            all_in=bool(a.get("all_in")),
            is_hero=bool(a.get("is_hero")) or a.get("seat_id") == "hero",
        ) for a in st.get("actions") or []]
        return tuple(recs[:cut] if cut is not None else recs)
    return ()


def _pot_after(pot: float, actions) -> float:
    level = 0.0
    committed = {}
    for a in actions:
        before = committed.get(a.seat_id, 0.0)
        amount = a.amount
        delta = max((amount if amount is not None else level) - before, 0.0)
        pot += delta
        if amount is not None:
            committed[a.seat_id] = max(before, amount)
        if a.action in ("bet", "raise"):
            level = max(level, committed.get(a.seat_id, level))
    return pot


def _cut_index(hand: dict, street: str, target_pot: float):
    """建议记录里的 pot 反推「当时街上有几个动作已经发生」。"""
    pot_start = 0.0
    for st in hand.get("streets") or []:
        if st.get("street") == street:
            pot_start = float(st.get("pot_start") or 0.0)
            break
    full = _actions(hand, street)
    best, best_pot = len(full), pot_start
    for cut in range(len(full) + 1):
        pot = _pot_after(pot_start, full[:cut])
        if abs(pot - target_pot) < 0.02:
            return cut, pot
        best, best_pot = cut, pot
    return best, best_pot


def snapshot(hand: dict, street: str, cut: int, advice: dict) -> HandSnapshot:
    streets = []
    for st in hand.get("streets") or []:
        name = str(st.get("street") or "")
        if name not in STREETS or STREETS.index(name) > STREETS.index(street):
            continue
        acts = _actions(hand, name, cut if name == street else None)
        streets.append(StreetRecord(name, st.get("pot_start"), acts))

    seats = {}
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
        shapes = ("limped",) if any(a.action == "call" for a in pre_actions
                                    if not a.is_hero) else ("unopened",)
    elif raises == 1:
        shapes = ("single-raise",)
    else:
        shapes = ("three-bet",)

    b = hand.get("blinds") or {}
    blinds = Blinds(float(b.get("sb") or 0.5), float(b.get("bb") or 1.0),
                    float(b["ante"]) if b.get("ante") else None,
                    str(b.get("raw") or ""))
    board = tuple((hand.get("board") or ())[:BOARD_LEN[street]])
    hero = tuple(hand.get("hero_cards") or ())
    pot_start = next((s.get("pot_start") for s in hand.get("streets") or []
                      if s.get("street") == street), None)
    return HandSnapshot(
        hand_id=str(hand.get("hand_id") or ""),
        hand_index=0,
        street=street,
        board=board,
        hero_cards=hero,
        hero_position=hand.get("hero_position"),
        hero_stack=None,
        hero_bet=0.0,
        hero_invested=None,
        to_call=float(advice.get("to_call") or 0.0),
        second_bet=None,
        pot=float(advice.get("pot") or 0.0) or None,
        pot_before_street=float(pot_start) if pot_start else None,
        table_size=int(hand.get("table_size") or (len(seats) + 1)),
        blinds=blinds,
        opponents=tuple(
            OpponentSnapshot(seat_id=sid, name=None, position=info["position"],
                             stack=None, bet=float(info["bet"] or 0.0),
                             folded=bool(info["folded"]))
            for sid, info in seats.items()),
        streets=tuple(streets),
        preflop_shapes=shapes,
        hero_is_pfr=hero_is_pfr,
        cbet_spot=False,
        decision_key=(str(hand.get("hand_id")), street, cut,
                      float(advice.get("pot") or 0.0),
                      float(advice.get("to_call") or 0.0), hero),
        players_dealt=len(seats) + 1,
    )


def _grid(weights: dict) -> dict:
    total = sum(w for w in weights.values() if w > 0)
    out = {}
    if total <= 0:
        return out
    for combo, w in weights.items():
        if w <= 0:
            continue
        key = ranges.key_of_combo(combo)
        out[key] = out.get(key, 0.0) + w / total
    return out


def _eff_width(weights: dict, mass: float = 0.90) -> float:
    live = sorted((w for w in weights.values() if w > 0), reverse=True)
    total = sum(live)
    if total <= 0:
        return 0.0
    acc = 0.0
    for i, w in enumerate(live):
        acc += w
        if acc >= total * mass:
            return (i + 1) / ranges.TOTAL_COMBOS * 100
    return len(live) / ranges.TOTAL_COMBOS * 100


def _combo_rows(weights: dict, board: str, before, dead: set) -> list:
    total = sum(w for w in weights.values() if w > 0) or 1.0
    before_total = (sum(w for w in before.values() if w > 0) if before else 0.0) or 1.0
    rows = []
    for combo, w in weights.items():
        if w <= 0:
            continue
        try:
            cls = range_flow.hand_class(combo, board)
        except Exception:  # noqa: BLE001
            cls = None
        share = w / total
        prev = (before.get(combo, 0.0) / before_total) if before else None
        rows.append({
            "combo": ranges.combo_text(combo),
            "key": ranges.key_of_combo(combo),
            "group": cls.group if cls else "",
            "made": cls.made if cls else "",
            "draw": cls.draw if cls else "",
            "share": round(share, 6),
            "delta": None if prev is None else round(share - prev, 6),
            "cut": bool(prev is not None and prev > 0 and share / prev < 0.05),
            "state": ("dead" if any(c in dead for c in combo) else "keep"),
        })
    return rows


def replay(prior, events, archetype: str, dead: set) -> dict:
    """与 range_flow.build_range 同构的一次重放，逐步留痕（原型专用）。"""
    support_floor = range_flow._env_support_floor()
    alpha_in = range_flow._env_alpha()
    alpha = range_flow.effective_alpha(min(max(alpha_in, 0.05), 2.0), len(events))
    weights = {c: float(w) for c, w in prior.normalized().items() if w > 0}
    steps = []

    def push(index, label, street, action, board, mass, dropped, before, note=""):
        rows = _combo_rows(weights, board, before, dead)
        lost = sorted((r for r in rows if r["delta"] is not None),
                      key=lambda r: r["delta"])[:12]
        gained = sorted((r for r in rows if r["delta"] is not None),
                        key=lambda r: -r["delta"])[:12]
        live = sum(1 for w in weights.values() if w > 0)
        steps.append({
            "index": index,
            "label": label,
            "street": street,
            "action": action,
            "board": board,
            "mass": round(mass, 6),
            "width": live,
            "width_pct": round(live / ranges.TOTAL_COMBOS * 100, 2),
            "eff_width_pct": round(_eff_width(weights), 2),
            "z": None,
            "dropped": dropped,
            "grid": {k: round(v, 6) for k, v in _grid(weights).items()},
            "top_lost": lost,
            "top_gained": gained,
            "note": note,
        })

    mass0 = sum(weights.values())
    push(0, "step0 · 先验 " + prior.id, "preflop", "", "", mass0, {}, None,
         prior.source or "")

    for i, event in enumerate(events, start=1):
        before = dict(weights)
        before_mass = sum(w for w in before.values() if w > 0)
        classes = range_flow._class_map(event.board)
        dropped = {"collision": 0.0, "dead": 0.0, "floor": 0.0}
        if classes:
            factors = {
                cls: max(range_flow.event_factor(event, cls, archetype=archetype),
                         range_flow._MIN_LIKELIHOOD) ** alpha
                for cls in {e[0] for e in classes.values()}}
            for combo in list(weights):
                entry = classes.get(combo)
                if entry is None:
                    dropped["collision"] += weights[combo]
                    weights[combo] = 0.0
                    continue
                weights[combo] *= factors.get(entry[0], 0.0)
        mass = range_flow._normalize(weights)
        if mass <= 0:
            steps.append({"index": i, "label": event.label(), "street": event.street,
                          "action": event.action, "board": event.board,
                          "mass": 0.0, "width": 0, "width_pct": 0.0,
                          "eff_width_pct": 0.0, "z": 0.0,
                          "dropped": {"collision": round(dropped["collision"], 6),
                                      "dead": 0.0, "floor": 0.0},
                          "grid": {}, "top_lost": [], "top_gained": [],
                          "note": "这一步把所有组合清零（build_range 会返回 None，退回旧口径）"})
            return {"steps": steps, "final": None, "alpha": alpha,
                    "support_floor": support_floor, "mass0": mass0}
        push(i, event.label() + " · " + event.seat_id, event.street,
             event.action, event.board, mass,
             {"collision": round(dropped["collision"], 6), "dead": 0.0,
              "floor": 0.0}, before)
        steps[-1]["z"] = round(mass / before_mass, 4) if before_mass > 0 else None

    before = dict(weights)
    dead_mass = sum(w for c, w in weights.items() if any(x in dead for x in c))
    for combo in list(weights):
        if any(c in dead for c in combo):
            weights[combo] = 0.0
    mass = range_flow._normalize(weights)
    peak = max((w for w in weights.values() if w > 0), default=0.0)
    floor = peak * support_floor
    floor_mass = sum(w for w in weights.values() if 0 < w < floor)
    for combo in list(weights):
        if weights[combo] < floor:
            weights[combo] = 0.0
    range_flow._normalize(weights)
    board_last = steps[-1]["board"] if steps else ""
    # 支撑集裁剪若一手没删，final 就是上一步，别多画一个空步
    if dead_mass > 0 or floor_mass > 0:
        push(len(events) + 1, "final · 支撑集裁剪", "river", "", board_last, mass,
             {"collision": 0.0, "dead": round(dead_mass, 6),
              "floor": round(floor_mass, 6)}, before,
             "支撑集下限 %.0f%% × 峰值；α=%.2f" % (support_floor * 100, alpha))
        steps[-1]["z"] = None   # final 步的 z 是留存量，不是似然比，别混进 Z 栏
    final_rows = _combo_rows(weights, board_last, None, dead)
    final_rows.sort(key=lambda r: -r["share"])
    live = sum(1 for w in weights.values() if w > 0)
    return {"steps": steps, "final": {
        "width": live,
        "width_pct": round(live / ranges.TOTAL_COMBOS * 100, 2),
        "eff_width_pct": round(_eff_width(weights), 2),
        "combos": final_rows[:400],
    }, "alpha": alpha, "support_floor": support_floor, "mass0": mass0}


def payload(hand: dict, street: str, title: str):
    advices = [a for a in hand.get("advices") or [] if a.get("street") == street]
    if not advices:
        return None
    advice = advices[-1]
    cut, pot = _cut_index(hand, street, float(advice.get("pot") or 0.0))
    if cut <= 0:
        return None
    snap = snapshot(hand, street, cut, advice)
    field = field_ranges.resolve(snap)
    if not field.specs:
        return None
    t0 = time.perf_counter()
    equity = hand_vs_ranges.lookup(snap)
    equity_ms = (time.perf_counter() - t0) * 1000
    dead = set(_as_cards(tuple(snap.hero_cards) + tuple(snap.board)))

    seats = []
    for spec in field.specs:
        prior = ranges.get(spec.prior_id) if spec.prior_id else None
        events = tuple(spec.events)
        entry = {
            "seat_id": spec.seat_id,
            "position": spec.position,
            "prior_id": spec.prior_id,
            "role": spec.role,
            "mode": spec.mode,
            "note": spec.note,
            "events": [{"label": e.label(), "street": e.street, "action": e.action,
                        "board": e.board, "facing": e.facing_bet,
                        "bucket": range_flow.bucket_of(e),
                        "spr": round(e.spr, 2), "pot": round(e.pot, 2),
                        "players_behind": e.players_behind,
                        "n_callers": e.n_callers} for e in events],
            # 没有翻后动作的座位，live 走 board_range 旧口径：宽度来自 edge，
            # 事件模型没有它的 trace（硬造一个「先验」会和 live 显示的数字打架）。
            "replay": (replay(prior, events, "unknown", dead)
                       if prior is not None and events else None),
        }
        edge = None
        if equity:
            edge = next((o for o in equity.opponents
                         if o.seat_id == spec.seat_id), None)
        if edge:
            entry["edge"] = {"width_pct": round(edge.width_pct, 2),
                             "eff_width_pct": round(edge.eff_width_pct, 2),
                             "behind": round(edge.behind, 4),
                             "tie": round(edge.tie, 4),
                             "ahead": round(edge.ahead, 4),
                             "combos": edge.combos_after_removal,
                             "bucket": edge.bucket, "jam": edge.jam}
            if not events:
                entry["legacy"] = {
                    "width_pct": round(edge.width_pct, 2),
                    "eff_width_pct": round(edge.eff_width_pct, 2),
                    "prior_id": spec.prior_id,
                    "prior_grid": ({k: round(v, 6) for k, v in _grid(
                        {c: float(w) for c, w in prior.normalized().items()}).items()}
                        if prior is not None else {}),
                    "reason": "该座位在决策点前没有翻后动作 → live 走 board_range 旧口径，"
                              "没有逐事件 trace",
                }
            if edge.composition:
                c = edge.composition
                entry["composition"] = {"value": round(c.value, 4),
                                        "semi": round(c.semi, 4),
                                        "bluff": round(c.bluff, 4),
                                        "made_hand": round(c.made_hand, 4),
                                        "draw": round(c.draw, 4),
                                        "air": round(c.air, 4)}
        seats.append(entry)

    facts = []
    for st in hand.get("streets") or []:
        if st.get("street") not in BOARD_LEN:
            continue
        acts = _actions(hand, st["street"], cut if st["street"] == street else None)
        for a in acts:
            facts.append({"street": a.street,
                          "who": "hero" if a.is_hero else a.seat_id,
                          "position": a.position, "action": a.action,
                          "amount": (None if a.amount is None else round(a.amount, 2)),
                          "all_in": a.all_in})
        if st["street"] == street:
            break

    return {
        "hand_id": hand.get("hand_id"),
        "title": title,
        "facts": facts,
        "board": "".join(snap.board),
        "street": street,
        "hero": "".join(snap.hero_cards),
        "hero_position": snap.hero_position,
        "table_size": snap.table_size,
        "pot": float(advice.get("pot") or 0.0),
        "to_call": float(advice.get("to_call") or 0.0),
        "advice": {"option": advice.get("option"), "reason": advice.get("reason"),
                   "source": advice.get("source"), "model": advice.get("model"),
                   "duration_ms": advice.get("duration_ms")},
        "equity": None if not equity else {
            "equity": round(equity.equity, 4),
            "ci": round(equity.equity_ci, 4),
            "behind_any": round(equity.behind_any, 4),
            "tie_any": round(equity.tie_any, 4),
            "samples": equity.samples,
            "context": equity.context,
            "source": equity.source,
            "random_opponents": equity.random_opponents,
            "ms": round(equity_ms, 1),
        },
        "warnings": list(dict.fromkeys(
            list(field.warnings) + list(equity.warnings if equity else ()))),
        "seats": seats,
        "steps": cut,
        "meta": {"stacks": "归档无筹码量 → SPR=99、spr_bucket 退化",
                 "board_len": BOARD_LEN[street]},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs/proto/range-timeline-prototype.html"))
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    all_hands = hands()
    cases = []
    for hid, street, title in PICKS:
        hand = all_hands.get(hid)
        if not hand:
            print("skip " + hid + ": not found", file=sys.stderr)
            continue
        case = payload(hand, street, title)
        if case:
            cases.append(case)
            print("ok %s %s: %d seats, %d pre-decision actions, equity %.0f%% in %.0f ms"
                  % (hid, street, len(case["seats"]), case["steps"],
                     case["equity"]["equity"] * 100 if case["equity"] else 0,
                     case["equity"]["ms"] if case["equity"] else 0))
        else:
            print("skip " + hid + " " + street + ": no specs/advice", file=sys.stderr)
    if not cases:
        print("no cases generated", file=sys.stderr)
        return 1

    tpl = Path(__file__).with_suffix(".html.tpl")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = tpl.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps({"generated_at": time.strftime("%Y-%m-%d %H:%M"),
                                "cases": cases}, ensure_ascii=False))
    out.write_text(html, encoding="utf-8")
    print("wrote %s (%.0f KB)" % (out, out.stat().st_size / 1024))
    if args.open:
        import webbrowser
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
