"""把 `review_hands` 的一行重放成 hero 决策点。

这段逻辑来自 `tools/compare_review_hands.py` 的 `build_decisions()`，控制台把它
扩展成可持久化、可审计的版本：

  · 手牌指纹（只包含会影响建议的字段）
  · 每个决策点的重建质量（exact / approx / invalid）
  · `HandSnapshot` 的稳定 JSON 序列化（任务创建时冻结，worker 不再读库重建）
  · 每个决策点在行动线中的序号（供详情页定位）
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from poker_ocr.hand_track import (
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds

from .util import BOARD_LEN, STREETS, cards_of, json_dumps, json_load, sha256_text

POSTFLOP = ("flop", "turn", "river")
INVALID = "invalid"
APPROX = "approx"
EXACT = "exact"
QUALITY_RANK = {INVALID: 0, APPROX: 1, EXACT: 2}


def call_amount(to_call: float | None, hero_stack: float | None) -> float:
    """跟注实际要付的筹码：后手不足时只能跟到全下。

    与 `poker_ocr.advice.call_amount()` 同一口径。控制台展示「需跟」时不能
    直接贴 `snap.to_call`：对手 all-in 完全 cover 我时，那个数是本街最高
    下注（例如 471.5），而牌桌上我只需要跟到自己的后手（168）。
    """
    to_call = round(float(to_call or 0.0), 2)
    if hero_stack is not None and 0 < float(hero_stack) < to_call:
        return round(float(hero_stack), 2)
    return to_call


@dataclass
class DecisionPoint:
    hand_id: str
    street: str
    seq: int
    action_index: int
    snap: HandSnapshot
    actual: str | None
    actual_amount: float | None
    pot: float
    quality: str
    warnings: list[str] = field(default_factory=list)
    position_index: int = 0
    # 线上建议上下文快照（table_stats/opponent_reads/table_dynamics）。
    # runner 从 replay_live_advices 按 (street, action_index) 挂上来；
    # 旧手牌没有快照时为空，重跑退回 none 模式。
    context: dict = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.hand_id, self.street, self.seq

    @property
    def snapshot_json(self) -> str:
        return snapshot_to_json(self.snap)

    def snapshot(self) -> dict:
        snap = self.snap
        return {
            "handId": snap.hand_id,
            "street": snap.street,
            "board": list(snap.board),
            "heroCards": list(snap.hero_cards),
            "heroPosition": snap.hero_position,
            "heroStack": snap.hero_stack,
            "pot": snap.pot,
            "toCall": snap.to_call,
            "tableSize": snap.table_size,
            "playersDealt": snap.players_dealt,
            "opponentsInHand": snap.opponents_in_hand,
            "preflopShapes": list(snap.preflop_shapes),
            "bombPot": bool(snap.bomb_pot),
        }


@dataclass
class HandAnalysis:
    hand_id: int
    fingerprint: str
    decisions: list[DecisionPoint]
    quality: str
    warnings: list[str]
    invalid_reason: str | None = None
    bomb: bool = False
    players: int = 0

    @property
    def decision_count(self) -> int:
        return len(self.decisions)

    @property
    def approx_count(self) -> int:
        return sum(1 for d in self.decisions if d.quality == APPROX)

    @property
    def quality_rank(self) -> int:
        return QUALITY_RANK.get(self.quality, 0)


def quality_of(decision: DecisionPoint) -> str:
    return decision.quality


def worst_quality(values: list[str]) -> str:
    if not values:
        return INVALID
    return min(values, key=lambda q: QUALITY_RANK.get(q, 0))


def _players(h: dict) -> int:
    return int(h.get("tableSize") or (h.get("villainCount") or 1) + 1)


def _bomb_pot(h: dict, ante: float) -> bool:
    """爆炸底池：显式标记，或「翻前没有任何动作 + 每人 5BB 强制前注」。

    手牌记录里的 `bombPotBb` 可能存的是 1（标记）也可能真是 5（金额）；
    更可靠的信号是**翻前一条行动都没有** —— 普通牌局不可能没人行动就发翻牌。
    最后再用「翻牌起始底池 = 5BB × 人数」兜底，兼容没带标记的旧记录。
    """
    flag = h.get("bombPotBb")
    if flag is None:
        flag = h.get("bombPot")
    if flag is not None and float(flag) > 0:
        return True

    preflop_actions = [
        action
        for street in h.get("streets") or []
        if street.get("street") == "preflop"
        for action in street.get("actions") or []
    ]
    if preflop_actions:
        return False
    if ante >= 5:                       # 炸弹池的前注就是 5BB/人
        return True

    first = next((street for street in h.get("streets") or []
                  if street.get("street") in ("flop", "turn", "river")), None)
    pot_start = first.get("potStartBb") if first else None
    players = _players(h)
    return (pot_start is not None and players > 0
            and abs(float(pot_start) - 5.0 * players) <= 0.5)


def _normalize_streets(raw: Any) -> list[dict]:
    out = []
    for rec in raw or []:
        if not isinstance(rec, dict):
            continue
        street = rec.get("street")
        if street not in STREETS:
            continue
        actions = []
        for a in rec.get("actions") or []:
            if not isinstance(a, dict):
                continue
            actions.append({
                "actor": a.get("actor"),
                "action": a.get("action"),
                "amountBb": a.get("amountBb"),
            })
        out.append({
            "street": street,
            "potStartBb": rec.get("potStartBb"),
            "actions": actions,
        })
    return out


def hand_fingerprint(h: dict) -> str:
    """只包含「会影响建议」的内容。改标题/输赢/复盘结论不应该失效。"""
    villains = sorted(
        ((v.get("position"), v.get("stackBb"))
         for v in (h.get("villains") or []) if isinstance(v, dict)),
        key=lambda x: (str(x[0]), str(x[1])),
    )
    payload = [
        h.get("heroCards") or "",
        h.get("board") or "",
        int(h.get("tableSize") or 0),
        h.get("heroPosition") or None,
        h.get("heroStackBb"),
        h.get("smallBlindBb") if h.get("smallBlindBb") is not None else h.get("sb"),
        h.get("bigBlindBb") if h.get("bigBlindBb") is not None else h.get("bb"),
        h.get("anteBb") if h.get("anteBb") is not None else h.get("ante"),
        h.get("bombPotBb") if h.get("bombPotBb") is not None else h.get("bombPot"),
        villains,
        _normalize_streets(h.get("streets")),
    ]
    return sha256_text(json_dumps(payload))


def hand_to_input(row: dict) -> dict:
    """DB 行 → build 函数使用的 camelCase 字典。"""
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "tableSize": row.get("table_size") or row.get("tableSize") or 0,
        "heroPosition": row.get("hero_position") or row.get("heroPosition"),
        "heroCards": row.get("hero_cards") or row.get("heroCards") or "",
        "heroStackBb": (row.get("hero_stack_bb") if row.get("hero_stack_bb") is not None
                        else row.get("heroStackBb")),
        "smallBlindBb": row.get("small_blind_bb") if row.get("small_blind_bb") is not None
        else row.get("smallBlindBb", row.get("sb")),
        "bigBlindBb": row.get("big_blind_bb") if row.get("big_blind_bb") is not None
        else row.get("bigBlindBb", row.get("bb")),
        "anteBb": row.get("ante_bb") if row.get("ante_bb") is not None
        else row.get("anteBb", row.get("ante")),
        "bombPotBb": row.get("bomb_pot_bb") if row.get("bomb_pot_bb") is not None
        else row.get("bombPotBb", row.get("bombPot")),
        "board": row.get("board") or "",
        "villainCount": row.get("villain_count") or row.get("villainCount") or 0,
        "villains": json_load(row.get("villains"), []) or [],
        "streets": json_load(row.get("streets"), []) or [],
        "potType": row.get("pot_type") or row.get("potType"),
        "result": row.get("result"),
        "resultAmount": (row.get("result_amount")
                         if row.get("result_amount") is not None
                         else row.get("resultAmount")),
        "heroTags": json_load(row.get("hero_tags") or row.get("heroTags"), []) or [],
    }


def _base_warnings(h: dict, *, players: int, bb: float, hero_cards: tuple[str, ...],
                   hero_start: float | None) -> list[str]:
    warnings: list[str] = []
    streets = h.get("streets") or []
    villains = [v for v in (h.get("villains") or []) if isinstance(v, dict)]
    if not villains:
        warnings.append("缺对手位置/筹码，决策点按聚合对手重建")
    elif any(v.get("stackBb") is None for v in villains):
        warnings.append("部分对手筹码缺失")
    if hero_start is None:
        warnings.append("缺 hero 起始筹码")
    # 设计口径：首街是 preflop 且行动线完整时，底池可以从盲注 + 行动精确重建，
    # 没有 potStartBb 不算 approx。只有「手牌从翻后开始」或行动金额缺失才需要提示。
    first = next((r.get("street") for r in streets if r.get("street")), None)
    if first and first != "preflop":
        warnings.append(f"手牌从 {first} 开始，缺翻前记录/起始底池，底池按行动线推算")
    # hero 在后面的街还有决策，但中间某条街完全没有 hero 动作 → 行动线缺口。
    hero_indices = [i for i, rec in enumerate(streets)
                    if any((a.get("actor") == "hero") for a in (rec.get("actions") or []))]
    if hero_indices:
        last_hero = max(hero_indices)
        for i, rec in enumerate(streets[:last_hero]):
            street_name = rec.get("street")
            actions = rec.get("actions") or []
            if street_name == "preflop" or not actions:
                continue
            if not any(a.get("actor") == "hero" for a in actions):
                warnings.append(f"{street_name} 缺 hero 动作，底池变化按对账推算")
    for rec in streets:
        street = rec.get("street")
        for a in rec.get("actions") or []:
            actor = a.get("actor")
            if actor in ("villain", "other"):
                warnings.append("行动线使用旧数据聚合口径（villain/other），对手位置不可还原")
                break
            if a.get("action") in ("bet", "raise", "allin") and a.get("amountBb") is None:
                warnings.append(f"{street} 的 {a.get('action')} 缺金额，底池为推算值")
                break
    # 老数据可能把行动者写成 villain/other，去重后只保留一条提示
    seen: set[str] = set()
    unique: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique


def _validate_hand(h: dict) -> tuple[list[str], int, float, tuple[str, ...]]:
    reasons: list[str] = []
    players = _players(h)
    bb = float(h.get("bigBlindBb") or h.get("bb") or 0.0)
    hero_cards = cards_of(h.get("heroCards"))
    if bb <= 0:
        reasons.append("大盲 bb<=0")
    if len(hero_cards) != 2:
        reasons.append("hero 底牌不是 2 张")
    if not h.get("streets"):
        reasons.append("行动线为空")
    return reasons, players, bb, hero_cards


def build_decisions(h: dict) -> tuple[list[DecisionPoint], bool, list[str], int]:
    """返回 (决策点, 是否爆炸底池, 重建警告, 参与人数)。

    数据不足（bb<=0 / 底牌 / 空行动线）时返回空决策点列表；调用方把整手牌
    标成 invalid。这里不抛异常，因为列表页需要把「为什么不可重跑」展示出来。
    """
    players = _players(h)
    sb = float(h.get("smallBlindBb") or h.get("sb") or 0.0)
    bb = float(h.get("bigBlindBb") or h.get("bb") or 0.0)
    ante = float(h.get("anteBb") or h.get("ante") or 0.0)
    hero_cards = cards_of(h.get("heroCards"))
    hero_pos = h.get("heroPosition") or None
    hero_start_raw = h.get("heroStackBb")
    hero_start = float(hero_start_raw) if hero_start_raw is not None else None

    warnings = _base_warnings(h, players=players, bb=bb, hero_cards=hero_cards,
                              hero_start=hero_start)
    if bb <= 0 or len(hero_cards) != 2 or not h.get("streets"):
        return [], False, warnings, players

    bomb = _bomb_pot(h, ante)
    pot = 5.0 * players if bomb else sb + bb + ante * players
    hero_invested = ante if bomb else ante + ({"SB": sb, "BB": bb}.get(hero_pos or "", 0.0))

    villains = {
        v.get("position"): v for v in (h.get("villains") or [])
        if isinstance(v, dict) and v.get("position")
    }
    folded = {pos: False for pos in villains}
    built: list[StreetRecord] = []
    decisions: list[DecisionPoint] = []
    seen_actions: list[ActionRecord] = []
    idx = 0

    for rec in h.get("streets") or []:
        if not isinstance(rec, dict):
            continue
        street = rec.get("street")
        if street not in STREETS:
            continue
        if rec.get("potStartBb") is not None:
            pot = float(rec["potStartBb"])
            # 实时日志的 `pot_before_street` 在翻前只记了前注（9 人 × 0.5 =
            # 4.5BB），盲注那 1.5BB 没在里面 —— 同一帧的实时建议用的却是
            # 真实底池 6.0BB。翻前不补盲注，所有翻前决策点的底池、赔率门槛
            # 和客户端底池档位都会偏小（h39：4.5 vs 6.0）。
            if (street == "preflop" and not bomb
                    and pot <= ante * players + 1e-9):
                pot += sb + bb
        pot_start = pot

        commit: dict[str, float] = {}
        level = 0.0
        if street == "preflop":
            commit = {"SB": sb, "BB": bb}
            level = bb
            if hero_pos in ("SB", "BB"):
                commit["hero"] = commit[hero_pos]

        cur: list[ActionRecord] = []
        for action_index, a in enumerate(rec.get("actions") or []):
            if not isinstance(a, dict):
                continue
            actor = a.get("actor")
            act = a.get("action")
            amt = a.get("amountBb")
            is_hero = actor == "hero"

            if is_hero:
                opps: list[OpponentSnapshot] = []
                listed: set[str] = set()
                for pos, v in villains.items():
                    if pos == hero_pos or pos in listed:
                        continue
                    listed.add(pos)
                    opps.append(OpponentSnapshot(
                        seat_id=pos, name=v.get("name"), position=pos,
                        stack=v.get("stackBb"),
                        bet=float(commit.get(pos, 0.0) or 0.0),
                        folded=bool(folded.get(pos))))
                for pos in {x.seat_id for x in seen_actions + cur if x.seat_id}:
                    if pos in ("hero", hero_pos) or pos in listed:
                        continue
                    listed.add(pos)
                    opps.append(OpponentSnapshot(
                        seat_id=pos, name=None, position=pos, stack=None,
                        bet=float(commit.get(pos, 0.0) or 0.0),
                        folded=bool(folded.get(pos))))

                streets = tuple(built) + (
                    StreetRecord(street, pot_start, tuple(cur)),)
                pre_acts = [x for r in streets if r.street == "preflop"
                            for x in r.actions]
                raises = sum(1 for x in pre_acts
                             if x.action in ("raise", "bet")
                             or (x.action == "call" and x.all_in))
                if raises == 0:
                    limped = any(x.action == "call" and not x.is_hero
                                 for x in pre_acts)
                    shapes = ("limped",) if limped else ("unopened",)
                elif raises == 1:
                    shapes = ("single-raise",)
                else:
                    shapes = ("three-bet",)
                hero_pfr = any(x.is_hero and x.action in ("raise", "bet")
                               for x in pre_acts)

                remaining = (max(hero_start - hero_invested, 0.0)
                             if hero_start is not None else None)
                to_call = round(max(level - float(commit.get("hero", 0.0) or 0.0), 0.0), 2)
                board = cards_of(h.get("board"))[:BOARD_LEN[street]]
                snap = HandSnapshot(
                    hand_id=str(h.get("id")), hand_index=0, street=street,
                    board=board, hero_cards=hero_cards,
                    hero_position=hero_pos,
                    hero_stack=round(remaining, 2) if remaining is not None else None,
                    hero_bet=float(commit.get("hero", 0.0) or 0.0),
                    hero_invested=round(hero_invested, 2),
                    to_call=to_call, second_bet=None,
                    pot=round(pot, 2), pot_before_street=round(pot_start, 2),
                    table_size=players,
                    blinds=Blinds(sb, bb, ante or None, f"{sb}/{bb}({ante})"),
                    opponents=tuple(opps), streets=streets,
                    preflop_shapes=shapes, hero_is_pfr=hero_pfr,
                    cbet_spot=False,
                    decision_key=(str(h.get("id")), street, idx,
                                  round(pot, 2), to_call),
                    players_dealt=players, bomb_pot=bomb,
                )
                decision_warnings = list(warnings)
                if remaining is None and "缺 hero 起始筹码" not in decision_warnings:
                    decision_warnings.append("hero 剩余筹码未知")
                # 把重建警告放进快照：build_situation 会把它带进模型事实和
                # replay_decisions.snapshot_json，任务详情才能解释「为什么可疑」。
                if decision_warnings:
                    snap = dataclasses.replace(snap, warnings=tuple(decision_warnings))
                decisions.append(DecisionPoint(
                    hand_id=str(h.get("id")), street=street, seq=idx,
                    action_index=action_index, snap=snap,
                    actual=act,
                    actual_amount=(float(amt) if amt is not None
                                   else (to_call if act == "call" else None)),
                    pot=round(pot, 2),
                    quality=APPROX if decision_warnings else EXACT,
                    warnings=decision_warnings,
                ))
                idx += 1

            cur.append(ActionRecord(
                street=street, seat_id=str(actor),
                name=(villains.get(actor) or {}).get("name") if actor != "hero" else None,
                position=(hero_pos if is_hero else actor),
                action=act, amount=amt, all_in=(act == "allin"), is_hero=is_hero))

            delta = 0.0
            if act in ("bet", "raise", "allin") and amt is not None:
                level = float(amt)
                delta = max(level - float(commit.get(actor, 0.0) or 0.0), 0.0)
                commit[actor] = level
            elif act == "call":
                delta = max(level - float(commit.get(actor, 0.0) or 0.0), 0.0)
                commit[actor] = level
            pot += delta
            if actor != "hero" and act == "fold":
                folded[actor] = True
            if is_hero:
                hero_invested += delta

        built.append(StreetRecord(street, pot_start, tuple(cur)))
        seen_actions += cur

    # 每个决策点的最终质量：只要有任意推算/旧口径提示就是 approx
    for decision in decisions:
        decision.quality = APPROX if decision.warnings else EXACT
    return decisions, bomb, warnings, players


def analyze_hand(row: dict) -> HandAnalysis:
    h = hand_to_input(row)
    hand_id = int(h.get("id") or 0)
    fingerprint = hand_fingerprint(h)
    invalid_reasons, players, bb, hero_cards = _validate_hand(h)
    warnings = _base_warnings(h, players=players, bb=bb, hero_cards=hero_cards,
                              hero_start=(float(h["heroStackBb"])
                                          if h.get("heroStackBb") is not None else None))
    if invalid_reasons:
        # 即使这手牌不能重跑（缺 bb/底牌/行动线），爆炸底池标记仍要显示：
        # 手牌库里「翻前无动作」本身就是一条有用的信息。
        return HandAnalysis(
            hand_id=hand_id, fingerprint=fingerprint, decisions=[],
            quality=INVALID, warnings=invalid_reasons, invalid_reason="；".join(invalid_reasons),
            bomb=_bomb_pot(h, float(h.get("anteBb") or 0.0)), players=players,
        )
    decisions, bomb, build_warnings, players = build_decisions(h)
    if not decisions:
        reason = "没有 hero 决策点"
        return HandAnalysis(
            hand_id=hand_id, fingerprint=fingerprint, decisions=[],
            quality=INVALID, warnings=[reason], invalid_reason=reason,
            bomb=bomb, players=players,
        )
    quality = worst_quality([d.quality for d in decisions])
    return HandAnalysis(
        hand_id=hand_id, fingerprint=fingerprint, decisions=decisions,
        quality=quality, warnings=build_warnings, bomb=bomb, players=players,
    )


# ─────────────── HandSnapshot 序列化（任务创建时冻结） ───────────────

def _action_to_dict(a: ActionRecord) -> dict:
    return {
        "street": a.street, "seatId": a.seat_id, "name": a.name,
        "position": a.position, "action": a.action, "amount": a.amount,
        "allIn": a.all_in, "isHero": a.is_hero, "inferred": a.inferred,
    }


def _action_from_dict(d: dict) -> ActionRecord:
    return ActionRecord(
        street=d.get("street"), seat_id=d.get("seatId") or "",
        name=d.get("name"), position=d.get("position"),
        action=d.get("action") or "", amount=d.get("amount"),
        all_in=bool(d.get("allIn")), is_hero=bool(d.get("isHero")),
        inferred=bool(d.get("inferred")),
    )


def snapshot_to_dict(snap: HandSnapshot) -> dict:
    return {
        "handId": snap.hand_id,
        "handIndex": snap.hand_index,
        "street": snap.street,
        "board": list(snap.board),
        "heroCards": list(snap.hero_cards),
        "heroPosition": snap.hero_position,
        "heroStack": snap.hero_stack,
        "heroBet": snap.hero_bet,
        "heroInvested": snap.hero_invested,
        "toCall": snap.to_call,
        "secondBet": snap.second_bet,
        "pot": snap.pot,
        "potBeforeStreet": snap.pot_before_street,
        "tableSize": snap.table_size,
        "blinds": ({"sb": snap.blinds.sb, "bb": snap.blinds.bb,
                    "ante": snap.blinds.ante, "raw": snap.blinds.raw}
                   if snap.blinds else None),
        "opponents": [{
            "seatId": o.seat_id, "name": o.name, "position": o.position,
            "stack": o.stack, "bet": o.bet, "folded": o.folded,
        } for o in snap.opponents],
        "streets": [{
            "street": s.street, "potStart": s.pot_start,
            "actions": [_action_to_dict(a) for a in s.actions],
        } for s in snap.streets],
        "preflopShapes": list(snap.preflop_shapes),
        "heroIsPfr": snap.hero_is_pfr,
        "cbetSpot": snap.cbet_spot,
        "decisionKey": list(snap.decision_key),
        "warnings": list(snap.warnings),
        "bombPot": snap.bomb_pot,
        "playersDealt": snap.players_dealt,
    }


def snapshot_to_json(snap: HandSnapshot) -> str:
    return json_dumps(snapshot_to_dict(snap))


def snapshot_from_dict(d: dict) -> HandSnapshot:
    blinds_data = d.get("blinds")
    blinds = None
    if blinds_data:
        blinds = Blinds(
            sb=float(blinds_data.get("sb") or 0.0),
            bb=float(blinds_data.get("bb") or 0.0),
            ante=blinds_data.get("ante"),
            raw=blinds_data.get("raw") or "",
        )
    return HandSnapshot(
        hand_id=str(d.get("handId") or ""),
        hand_index=int(d.get("handIndex") or 0),
        street=d.get("street") or "preflop",
        board=tuple(d.get("board") or ()),
        hero_cards=tuple(d.get("heroCards") or ()),
        hero_position=d.get("heroPosition"),
        hero_stack=d.get("heroStack"),
        hero_bet=d.get("heroBet"),
        hero_invested=d.get("heroInvested"),
        to_call=float(d.get("toCall") or 0.0),
        second_bet=d.get("secondBet"),
        pot=d.get("pot"),
        pot_before_street=d.get("potBeforeStreet"),
        table_size=int(d.get("tableSize") or 0),
        blinds=blinds,
        opponents=tuple(OpponentSnapshot(
            seat_id=o.get("seatId") or "", name=o.get("name"),
            position=o.get("position"), stack=o.get("stack"),
            bet=o.get("bet"), folded=bool(o.get("folded")),
        ) for o in (d.get("opponents") or [])),
        streets=tuple(StreetRecord(
            street=s.get("street"), pot_start=s.get("potStart"),
            actions=tuple(_action_from_dict(a) for a in (s.get("actions") or [])),
        ) for s in (d.get("streets") or [])),
        preflop_shapes=tuple(d.get("preflopShapes") or ()),
        hero_is_pfr=bool(d.get("heroIsPfr")),
        cbet_spot=bool(d.get("cbetSpot")),
        decision_key=tuple(d.get("decisionKey") or ()),
        warnings=tuple(d.get("warnings") or ()),
        bomb_pot=bool(d.get("bombPot")),
        players_dealt=int(d.get("playersDealt") or 0),
    )


def snapshot_from_json(text: str) -> HandSnapshot:
    return snapshot_from_dict(json_load(text, {}) or {})


def decision_facts(sit) -> dict:
    """该版本实际看到的 Situation 事实。桌风会影响选项与桌风快照。

    旧版会把 `value_directive` 写进 `directive` 字段；中文 lean 上线后该字段
    不再生成（旧记录里的值保持只读）。
    """
    gt = getattr(sit, "ground_truth", None)
    hs = getattr(sit, "hand_strength", None)
    return {
        "handTier": sit.hand_tier,
        "handLabel": sit.hand_label,
        "handLabelEn": sit.hand_label_en,
        "category": sit.category,
        "texture": sit.texture,
        "textureEn": sit.texture_en,
        "equityPct": sit.equity_pct,
        "beatPct": sit.beat_pct,
        "beatAtLeastPct": sit.beat_at_least_pct,
        "heroIsNuts": getattr(gt, "hero_is_nuts", None) if gt else None,
        "bestFive": ([_card(c) for c in gt.made.best_five]
                     if gt is not None and getattr(gt, "made", None) else None),
        "madeName": (gt.made.name if gt is not None and getattr(gt, "made", None) else None),
        "madeNameCn": (gt.made.name_cn if gt is not None and getattr(gt, "made", None) else None),
        "handStrength": ({
            "kind": hs.kind, "size": hs.size,
            "behindAny": round(hs.behind_any, 4),
            "equity": round(hs.equity, 4), "n": hs.n,
        } if hs is not None else None),
        "options": [o.to_dict() for o in sit.options],
        "opponentsInHand": sit.opponents_in_hand,
        "tableDynamics": bool(sit.table_dynamics),
        "potPerPlayer": sit.pot_per_player,
        "explosivePot": sit.explosive_pot,
        "rangeVerdict": sit.range_verdict,
        "warnings": list(sit.warnings),
    }


def _card(card) -> str:
    # ground_truth 的牌是 (rank_int, suit_char) 元组；兼容 str / 带 code 的对象
    if isinstance(card, str):
        return card
    try:
        from poker_ocr.ground_truth import card_text
        return card_text(card)
    except (TypeError, ValueError, IndexError):
        return str(card)
