"""手牌回放时间线：把一手牌的事实/决策重建成逐事件 frames。

瀑布流与牌桌回放共用这里的输出，避免两个视图各算一套。口径：

  · 一帧 = 一个语义事件（街开始 / 行动 / hero 决策 / 摊牌 / 手牌结束），
    不是视频帧。
  · action 的金额是「本街加到总额」（与 hand_track / advice 同口径）；
    delta 由该座本街已投入反推。
  · 决策帧优先用 `DecisionPoint.snapshot` 的 pot/to_call；动作帧用
    street.potStart + delta 推算。任何缺失/对账失败都标 `derived`，不猜。
  · 座位筹码只在已知起始筹码时推算；未知就写 null，前端显示「—」。
"""

from __future__ import annotations

from typing import Any, Iterable

STREETS = ("preflop", "flop", "turn", "river")
STREET_LABELS = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
AGGRESSIVE = ("bet", "raise", "allin")
TIMELINE_VERSION = "timeline-1"


def _cards(text: Any) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if len(raw) % 2 == 1:
        raw = raw[:-1]
    return [raw[i:i + 2] for i in range(0, len(raw), 2)]


def _rows(row: dict) -> list[dict]:
    roster = row.get("table_roster")
    if isinstance(roster, list) and roster:
        return [dict(x) for x in roster if isinstance(x, dict)]
    # 旧记录没有 table_roster：用 villains（位置聚合）兜底，让牌桌至少能显示
    # 本手出现过的对手。座位 id 用位置代替，前端按 position 映射。
    out: list[dict] = [{
        "seat_id": "hero", "name": None,
        "position": row.get("hero_position"), "in_hand": True,
    }]
    for villain in row.get("villains") or []:
        if not isinstance(villain, dict):
            continue
        position = villain.get("position")
        if not position:
            continue
        out.append({
            "seat_id": position,
            "name": villain.get("name"),
            "position": position,
            "stack": villain.get("stackBb"),
            "in_hand": True,
        })
    return out


def _meta(roster: list[dict], actor: str | None,
          hero_position: str | None) -> dict:
    actor = str(actor or "")
    if actor == "hero":
        for item in roster:
            if item.get("is_hero") or item.get("seat_id") == "hero":
                return {
                    "seatId": item.get("seat_id") or "hero",
                    "name": item.get("name"),
                    "position": item.get("position") or hero_position,
                }
        return {"seatId": "hero", "name": None, "position": hero_position}
    for item in roster:
        if actor and actor in (str(item.get("seat_id") or ""),
                               str(item.get("position") or ""),
                               str(item.get("name") or "")):
            return {
                "seatId": item.get("seat_id") or actor,
                "name": item.get("name"),
                "position": item.get("position"),
            }
    return {"seatId": actor or None, "name": None,
            "position": None if actor == "hero" else (actor or None)}


def _start_stacks(roster: list[dict], hero_stack: float | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in roster:
        stack = item.get("stack")
        if stack is None:
            stack = item.get("start_stack")
        try:
            if stack is not None:
                out[str(item.get("seat_id") or item.get("position") or "")] = float(stack)
        except (TypeError, ValueError):
            continue
    if hero_stack is not None:
        out["hero"] = float(hero_stack)
    return out


def _decision_snapshot(decision) -> dict:
    snap = getattr(decision, "snap", None)
    if snap is None:
        return {}
    return {
        "pot": snap.pot,
        "toCall": round(float(snap.to_call or 0.0), 2),
        "heroStack": snap.hero_stack,
        "heroBet": snap.hero_bet,
        "potBeforeStreet": snap.pot_before_street,
        "opponentsInHand": getattr(snap, "opponents_in_hand", None),
    }


def _advice_public(advice: dict | None) -> dict | None:
    if not isinstance(advice, dict):
        return None
    option = advice.get("option") if isinstance(advice.get("option"), dict) else {}
    return {
        "adviceId": advice.get("id"),
        "optionId": option.get("id") or advice.get("option_id"),
        "kind": option.get("kind") or advice.get("option_kind"),
        "amount": (option.get("amount_bb")
                   if option.get("amount_bb") is not None
                   else option.get("amount")),
        "label": option.get("label") or advice.get("option_label"),
        "reason": advice.get("reason"),
        "reasonFull": advice.get("reason_full"),
        "source": advice.get("source"),
        "model": advice.get("model"),
        "promptSha256": advice.get("prompt_sha256"),
        "promptMissing": bool(advice.get("prompt_missing")),
        "durationMs": advice.get("duration_ms"),
        "confidence": advice.get("confidence"),
    }


def build_timeline(row: dict, analysis,
                   advices: Iterable[dict] | None = None,
                   decision_ids: dict[tuple[str, int], int] | None = None) -> dict:
    """返回 `ReplayTimeline` 字典（可直接 JSON 化）。

    `analysis` 是 replay.decisions.HandAnalysis；`advices` 是
    replay_live_advices 的行（可含 prompt 字段）。
    """
    roster = _rows(row)
    hero_cards = _cards(row.get("hero_cards"))
    board = _cards(row.get("board"))
    hero_position = row.get("hero_position")
    blinds = row.get("blinds")
    if not isinstance(blinds, dict):
        blinds = {
            "sb": row.get("small_blind_bb"),
            "bb": row.get("big_blind_bb"),
            "ante": row.get("ante_bb"),
        }
    hero_stack = row.get("hero_stack_bb")
    try:
        hero_stack = float(hero_stack) if hero_stack is not None else None
    except (TypeError, ValueError):
        hero_stack = None

    advices_by_key: dict[tuple[str, int], dict] = {}
    for advice in advices or []:
        if not isinstance(advice, dict):
            continue
        key = (str(advice.get("street") or ""),
               int(advice.get("action_index") or 0))
        advices_by_key[key] = advice

    decisions_by_index: dict[int, list] = {}
    for decision in getattr(analysis, "decisions", []) or []:
        decisions_by_index.setdefault(int(getattr(decision, "action_index", 0)),
                                      []).append(decision)

    frames: list[dict] = []
    seq = 0
    warnings: list[str] = []

    def add(frame: dict) -> None:
        nonlocal seq
        frame["seq"] = seq
        seq += 1
        frames.append(frame)

    add({
        "type": "hand_start",
        "street": "preflop",
        "ts": row.get("source_created_at") or row.get("created_at"),
        "table": {
            "size": int(row.get("table_size") or 0),
            "blinds": {
                "sb": blinds.get("sb"),
                "bb": blinds.get("bb"),
                "ante": blinds.get("ante"),
            },
            "bombPot": bool((row.get("bomb_pot_bb") or 0) or 0),
        },
        "hero": {
            "seatId": "hero",
            "cards": hero_cards,
            "position": hero_position,
            "stack": hero_stack,
        },
        "roster": [
            {
                "seatId": item.get("seat_id"),
                "name": item.get("name"),
                "position": item.get("position"),
                "stack": item.get("stack"),
                "status": item.get("status"),
                "inHand": bool(item.get("in_hand", True)),
            }
            for item in roster
        ],
    })

    streets = row.get("streets")
    if not isinstance(streets, list):
        streets = []
    # 规范化：只保留四个合法街，并给每条街一个全局动作起点。
    normalized: list[dict] = []
    global_index = 0
    for rec in streets:
        if not isinstance(rec, dict):
            continue
        street = str(rec.get("street") or "").lower()
        if street not in STREETS:
            continue
        actions = []
        for action in rec.get("actions") or []:
            if not isinstance(action, dict):
                continue
            item = dict(action)
            item["_global"] = global_index
            global_index += 1
            actions.append(item)
        normalized.append({"street": street, "potStart": rec.get("potStartBb"),
                           "actions": actions})

    pot = 0.0
    bets: dict[str, float] = {}
    committed: dict[str, float] = {}
    stacks = _start_stacks(roster, hero_stack)
    dealt_decisions: set[int] = set()

    def actor_key(actor: str | None) -> str:
        meta = _meta(roster, actor, hero_position)
        return str(meta.get("seatId") or actor or "hero")

    def add_decisions_at(index: int) -> None:
        pending = decisions_by_index.get(index) or []
        for decision in pending:
            if id(decision) in dealt_decisions:
                continue
            dealt_decisions.add(id(decision))
            snap = getattr(decision, "snap", None)
            street = getattr(decision, "street", None) or "preflop"
            action_index = int(getattr(decision, "action_index", 0))
            advice = advices_by_key.get((street, action_index))
            decision_id = (decision_ids or {}).get((street, action_index))
            frame = {
                "type": "decision",
                "street": street,
                "decisionId": decision_id,
                "actionIndex": action_index,
                "decisionKey": [row.get("external_uid") or row.get("external_id"),
                                street, action_index],
                "snapshot": _decision_snapshot(decision),
                "actual": {
                    "action": getattr(decision, "actual", None),
                    "amountBb": getattr(decision, "actual_amount", None),
                },
                "advice": _advice_public(advice),
                "quality": getattr(decision, "quality", None),
                "warnings": list(getattr(decision, "warnings", []) or []),
            }
            if advice is not None:
                frame["promptSha256"] = advice.get("prompt_sha256")
                frame["promptMissing"] = bool(advice.get("prompt_missing"))
            add(frame)

    for street_rec in normalized:
        street = street_rec["street"]
        bets = {}
        committed = {}
        # 翻前盲注不是 action，但 already 在 potStart 里；把它们记成初始
        # committed，跟注的 delta 才不会把盲注重复加一次。
        if street == "preflop":
            try:
                sb = float(blinds.get("sb") or 0.0)
            except (TypeError, ValueError):
                sb = 0.0
            try:
                bb = float(blinds.get("bb") or 0.0)
            except (TypeError, ValueError):
                bb = 0.0
            for item in roster:
                key = str(item.get("seat_id") or item.get("position") or "")
                position = str(item.get("position") or "")
                blind = sb if position == "SB" else bb if position == "BB" else 0.0
                if blind:
                    committed[key] = blind
                    bets[key] = blind
                    if key in stacks:
                        stacks[key] = max(stacks[key] - blind, 0.0)
        # 街首底池：优先记录值；没有就沿用上一街末，并标 derived。
        street_pot = street_rec.get("potStart")
        derived_start = False
        try:
            if street_pot is not None:
                pot = float(street_pot)
            else:
                derived_start = True
        except (TypeError, ValueError):
            derived_start = True
        board_now = board[:BOARD_LEN[street]]
        add({
            "type": "street_start",
            "street": street,
            "label": STREET_LABELS[street],
            "board": board_now,
            "potStart": round(pot, 2),
            "derived": derived_start,
            **({"warnings": ["该街起始底池缺失，沿用上一街末"]}
               if derived_start else {}),
        })
        for action in street_rec["actions"]:
            add_decisions_at(int(action["_global"]))
            actor = str(action.get("actor") or "")
            kind = str(action.get("action") or "").lower()
            key = actor_key(actor)
            amount = action.get("amountBb")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None
            delta = 0.0
            action_derived = bool(action.get("inferred"))
            if kind in ("bet", "raise", "allin", "call") and amount is not None:
                previous = float(committed.get(key, 0.0) or 0.0)
                delta = max(amount - previous, 0.0)
                committed[key] = max(amount, previous)
                bets[key] = amount
                pot += delta
                if key in stacks:
                    stacks[key] = max(stacks[key] - delta, 0.0)
            elif kind in ("fold", "check"):
                if kind == "check":
                    bets.setdefault(key, 0.0)
            else:
                action_derived = True
            if amount is None and kind in ("bet", "raise", "allin", "call"):
                action_derived = True
                if "该动作金额缺失" not in warnings:
                    warnings.append("该动作金额缺失")
            frame = {
                "type": "action",
                "street": street,
                "actor": _meta(roster, actor, hero_position),
                "action": kind,
                "amount": amount,
                "amountMode": "street_total",
                "potAfter": round(pot, 2),
                "betsAfter": {k: round(v, 2) for k, v in bets.items()},
                "stacksAfter": {k: round(v, 2) for k, v in stacks.items()},
                "allIn": bool(action.get("all_in")),
                "inferred": bool(action.get("inferred")),
                "derived": bool(action_derived),
            }
            if action_derived:
                frame["warnings"] = ["由起始筹码/行动额推算"]
            add(frame)
        # 街末还没有插入的决策（例如没有后续动作）挂到街末
        for index in sorted(decisions_by_index):
            if any(getattr(d, "street", None) == street
                   for d in decisions_by_index[index]):
                add_decisions_at(index)

    # 所有尚未插入的决策（异常记录）追加到末尾，保证不丢。
    for index in sorted(decisions_by_index):
        add_decisions_at(index)

    add({
        "type": "hand_end",
        "result": {
            "heroNetBb": row.get("result_amount"),
            "outcome": row.get("result") or "unknown",
            "source": row.get("result_source") or "unknown",
        },
        "quality": getattr(analysis, "quality", "invalid"),
        "warnings": list(getattr(analysis, "warnings", []) or []) + warnings,
    })

    return {
        "version": TIMELINE_VERSION,
        "handUid": row.get("external_uid") or row.get("external_id"),
        "recordSchema": row.get("record_schema") or "hand.v2",
        "quality": getattr(analysis, "quality", "invalid"),
        "frames": frames,
    }
