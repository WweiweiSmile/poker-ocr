#!/usr/bin/env python3
"""摊牌字段体检：现有手牌记录够不够判「牌型族 × 局面」（只读，不写产品数据）。

回答三个问题：

1. `shows` / `streets` / `seat_meta` / `board` / `hero_cards` / `advices`
   里**实际**有哪些字段、类型、空值率；
2. `opponent_profile.showdown_read` 与 `observations_from_hand` 实际取用了
   哪些字段，按它们的口径复算会丢掉多少亮牌；
3. 判「牌型族 / 街 / 面对下注 / 位置 / 多人池 / 牌面纹理 / 是否赢家」分别
   需要什么，现有记录里是「有 / 能推导 / 必须补采」。

默认读 `data/hand_outbox/{pending,sent,discarded}/*.json`：

    nv tools/showdown_field_audit.py
    nv tools/showdown_field_audit.py --hands data/hand_outbox/sent/*.json
    nv tools/showdown_field_audit.py --json > /tmp/showdown-audit.json

口径说明：

- 「非空率」= 键存在且值不是 None/空列表/空字符串/空 dict 的手牌占比；
- 「可判族」= 2 张亮牌 + 牌面 ≥3 张（与 `showdown_read` 同门槛），另外单独报
  「1 张亮牌」——它能排除一张牌，但判不了族；
- 「赢家」没有字段，只能按「最后一条街的活跃座位 + 亮牌集合」推导，本脚本
  报告**可推导的覆盖面**与不可判定的部分，不假装知道真实赢家。
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.coach import evaluate, parse_cards  # noqa: E402
from poker_ocr.opponent_profile import (  # noqa: E402
    SHOWDOWN_DROP_REASONS, observations_from_hand, showdown_reads,
)

DEFAULT_GLOB = [
    "data/hand_outbox/pending/*.json",
    "data/hand_outbox/sent/*.json",
    "data/hand_outbox/discarded/*.json",
]
STREETS = ("preflop", "flop", "turn", "river")
POSTFLOP = ("flop", "turn", "river")
FAMILY_LABELS = {
    0: "高牌", 1: "一对", 2: "两对", 3: "三条", 4: "顺子",
    5: "同花", 6: "葫芦", 7: "四条", 8: "同花顺",
}

# 两个产品的取用口径（读源码后抄下来的清单，改口径时同步这里）。
SHOWDOWN_FIELDS = {
    "shows[].seat_id": "按座位匹配亮牌",
    "shows[].cards": "要求恰好 2 张，否则整条丢弃",
    "board": "要求 >=3 张才 evaluate",
}
OBSERVATION_FIELDS = {
    "shows[].seat_id / shows[].cards": "showdown_read：clean 判定 + 牌型 + 摊牌前是否主动",
    "seat_meta[].seat_id / name / position": "对手键、位置、limp 的 BB 判定",
    "start_stacks[].seat_id": "旧记录推断「被发过牌」",
    "streets[].street / actions[].seat_id / action / amount / all_in / inferred":
        "VPIP/PFR/3bet/开池/c-bet/面对下注/亮牌前主动性",
    "board": "牌型判定 + 最后一街推断",
    "street_active": "补推弃牌时判断该座位是否还在局",
    "partial / data_quality": "被截断的手牌不补推弃牌",
    "table_size / blinds": "位置排序与池子键",
    "hero_seat_id / hero_cards": "区分 hero 与对手",
}


# ─────────────────────────── 载入 ───────────────────────────

def load_hands(patterns: list[str]) -> tuple[list[dict], list[Path]]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(Path(p) for p in sorted(glob.glob(pattern)))
    hands: list[dict] = []
    seen: set[str] = set()
    for path in files:
        try:
            hand = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"跳过 {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(hand, dict):
            continue
        uid = str(hand.get("uid") or hand.get("hand_id") or path.stem)
        if uid in seen:            # 同一手牌可能同时留在 pending 与 sent
            continue
        seen.add(uid)
        hands.append(hand)
    return hands, files


# ─────────────────────────── 小工具 ───────────────────────────

def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _types(values) -> str:
    kinds = sorted({type(v).__name__ for v in values})
    return "/".join(kinds) if kinds else "—"


def coverage(hands: list[dict], getter) -> dict:
    """非空率 + 类型。getter(hand) -> 值。"""
    present = 0
    values = []
    for hand in hands:
        try:
            value = getter(hand)
        except (KeyError, TypeError, IndexError):
            value = None
        if not _is_empty(value):
            present += 1
        values.append(value)
    return {
        "n": len(hands),
        "non_empty": present,
        "pct": present / len(hands) if hands else 0.0,
        "types": _types(values),
    }


def row(label: str, stat: dict) -> str:
    return f"| {label} | {stat['non_empty']}/{stat['n']} | {stat['pct']:.1%} | {stat['types']} |"


def _last_street(hand: dict) -> str | None:
    for street in reversed(STREETS):
        if any(str(s.get("street")) == street for s in hand.get("streets") or []):
            return street
    return None


def _active(hand: dict, street: str) -> list[str]:
    seats = (hand.get("street_active") or {}).get(street)
    return [str(s) for s in seats] if isinstance(seats, list) else []


# ─────────────────────────── 各段体检 ───────────────────────────

def audit_top(hands: list[dict]) -> list[tuple[str, dict]]:
    keys = [
        "type", "schema", "uid", "ts", "table_signature", "table_roster",
        "table_size", "blinds", "players_dealt", "hero_seat_id",
        "hero_position", "hero_cards", "hero_stack_bb", "start_stacks",
        "board", "pot", "streets", "street_active", "seat_meta",
        "data_quality", "data_warnings", "cards_warnings", "cards_unreliable",
        "bomb_pot", "all_ins", "shows", "advices",
    ]
    return [(key, coverage(hands, lambda h, k=key: h.get(k))) for key in keys]


def audit_shows(hands: list[dict]) -> dict:
    entries = []
    hand_stats = Counter()
    per_hand_showers = Counter()
    show_key_sets = Counter()
    card_counts = Counter()
    board_x_cards = Counter()
    name_null = position_null = 0
    hero_shows = 0
    for hand in hands:
        shows = hand.get("shows") or []
        if not shows:
            continue
        hand_stats["hands_with_shows"] += 1
        per_hand_showers[len(shows)] += 1
        board_len = len(hand.get("board") or [])
        hand_stats[f"board_{board_len}"] += 1
        for show in shows:
            if not isinstance(show, dict):
                hand_stats["non_dict_entry"] += 1
                continue
            entries.append((hand, show))
            show_key_sets[",".join(sorted(show))] += 1
            cards = show.get("cards") or []
            card_counts[len(cards)] += 1
            board_x_cards[(board_len, len(cards))] += 1
            if _is_empty(show.get("name")):
                name_null += 1
            if _is_empty(show.get("position")):
                position_null += 1
            if str(show.get("seat_id")) == str(hand.get("hero_seat_id")):
                hero_shows += 1
    return {
        "hands_with_shows": hand_stats["hands_with_shows"],
        "entries": len(entries),
        "card_counts": dict(card_counts),
        "board_x_cards": {f"{b}x{c}": v for (b, c), v in sorted(board_x_cards.items())},
        "show_key_sets": dict(show_key_sets),
        "name_null": name_null,
        "position_null": position_null,
        "hero_shows": hero_shows,
        "per_hand_showers": dict(sorted(per_hand_showers.items())),
        "hands_by_board_len": {
            f"board_{b}": hand_stats[f"board_{b}"] for b in (0, 3, 4, 5)
            if hand_stats[f"board_{b}"]
        },
        "has_street_key": sum(1 for _h, s in entries if "street" in s),
        "has_winner_key": sum(
            1 for _h, s in entries
            if any(k in s for k in ("winner", "won", "is_winner", "payout"))),
        "has_timing_key": sum(
            1 for _h, s in entries
            if any(k in s for k in ("street", "timing", "shown_at", "mucked"))),
    }


def audit_streets(hands: list[dict]) -> dict:
    street_variants = Counter()
    street_hands = Counter()
    pot_start_missing = Counter()
    action_keys = Counter()
    amount_null = Counter()
    action_kinds = Counter()
    inferred = total_actions = 0
    actions_per_street = defaultdict(list)
    for hand in hands:
        streets = hand.get("streets") or []
        seen = set()
        for street in streets:
            if not isinstance(street, dict):
                continue
            name = str(street.get("street"))
            street_variants[name] += 1
            seen.add(name)
            actions = street.get("actions") or []
            actions_per_street[name].append(len(actions))
            total_actions += len(actions)
            if _is_empty(street.get("pot_start")):
                pot_start_missing[name] += 1
            for action in actions:
                if not isinstance(action, dict):
                    continue
                for key in action:
                    action_keys[key] += 1
                kind = str(action.get("action"))
                action_kinds[kind] += 1
                if _is_empty(action.get("amount")):
                    amount_null[kind] += 1
                if action.get("inferred"):
                    inferred += 1
        for name in seen:
            street_hands[name] += 1
    return {
        "street_variants": dict(street_variants),
        "hands_by_street": dict(street_hands),
        "pot_start_missing": dict(pot_start_missing),
        "action_keys": dict(action_keys),
        "action_kinds": dict(action_kinds),
        "amount_null": dict(amount_null),
        "total_actions": total_actions,
        "inferred": inferred,
        "inferred_pct": inferred / total_actions if total_actions else 0.0,
        "actions_per_street": {
            k: {"min": min(v), "max": max(v), "avg": sum(v) / len(v)}
            for k, v in actions_per_street.items() if v
        },
    }


def audit_meta(hands: list[dict]) -> dict:
    entries = 0
    name_null = position_null = 0
    stack_entries = 0
    stack_null = 0
    per_hand = Counter()
    positions = Counter()
    for hand in hands:
        meta = hand.get("seat_meta") or []
        per_hand[len(meta)] += 1
        for item in meta:
            if not isinstance(item, dict):
                continue
            entries += 1
            if _is_empty(item.get("name")):
                name_null += 1
            if _is_empty(item.get("position")):
                position_null += 1
            positions[str(item.get("position"))] += 1
        for item in hand.get("start_stacks") or []:
            if not isinstance(item, dict):
                continue
            stack_entries += 1
            if _is_empty(item.get("stack")):
                stack_null += 1
    return {
        "entries": entries,
        "name_null": name_null,
        "name_null_pct": name_null / entries if entries else 0.0,
        "position_null": position_null,
        "position_null_pct": position_null / entries if entries else 0.0,
        "positions": dict(positions.most_common()),
        "per_hand": dict(sorted(per_hand.items())),
        "stack_entries": stack_entries,
        "stack_null": stack_null,
        "stack_null_pct": stack_null / stack_entries if stack_entries else 0.0,
    }


def audit_board(hands: list[dict]) -> dict:
    lengths = Counter(len(hand.get("board") or []) for hand in hands)
    hero_lengths = Counter(len(hand.get("hero_cards") or []) for hand in hands)
    return {
        "board_lengths": dict(sorted(lengths.items())),
        "hero_card_lengths": dict(sorted(hero_lengths.items())),
    }


def audit_advices(hands: list[dict]) -> dict:
    with_advices = 0
    total = 0
    keys = Counter()
    street_dist = Counter()
    has_facts = has_context = has_reads = 0
    for hand in hands:
        advices = hand.get("advices") or []
        if advices:
            with_advices += 1
        for advice in advices:
            if not isinstance(advice, dict):
                continue
            total += 1
            for key in advice:
                keys[key] += 1
            street_dist[str(advice.get("street"))] += 1
            if not _is_empty(advice.get("facts")):
                has_facts += 1
            if not _is_empty(advice.get("context")):
                has_context += 1
            if not _is_empty(advice.get("opponent_reads")):
                has_reads += 1
    return {
        "hands_with_advices": with_advices,
        "advices": total,
        "keys": dict(keys),
        "street_dist": dict(street_dist),
        "with_facts": has_facts,
        "with_context": has_context,
        "with_opponent_reads": has_reads,
    }


def audit_showdown(hands: list[dict]) -> dict:
    """按 `showdown_read` 的口径复算，并统计丢掉的原因（与 witness 同一份判定）。"""
    stats = Counter()
    hands_with_two_card = 0
    hands_eligible = 0
    for hand in hands:
        board = [str(c) for c in (hand.get("board") or [])]
        shows = [s for s in (hand.get("shows") or []) if isinstance(s, dict)]
        if not shows:
            continue
        two = [s for s in shows if len(s.get("cards") or []) == 2]
        one = [s for s in shows if len(s.get("cards") or []) == 1]
        if two:
            hands_with_two_card += 1
        if two and len(board) >= 3:
            hands_eligible += 1
        for read in showdown_reads(hand):
            if read.reason:
                stats[f"dropped_{read.reason}"] += 1
            else:
                stats["kept"] += 1
                stats[f"aggressive_{bool(read.aggressive)}"] += 1
    # observations_from_hand 的产出规模
    obs_total = 0
    obs_with_showdown = 0
    obs_hands = 0
    for hand in hands:
        try:
            obs = observations_from_hand(hand)
        except Exception as exc:      # noqa: BLE001 —— 体检要把异常也报出来
            stats[f"obs_error:{type(exc).__name__}"] += 1
            continue
        if obs:
            obs_hands += 1
        obs_total += len(obs)
        obs_with_showdown += sum(
            1 for o in obs if o.showdown_category is not None)
    stats["hands_with_two_card_show"] = hands_with_two_card
    stats["hands_showdown_eligible"] = hands_eligible
    stats["observations"] = obs_total
    stats["observations_with_showdown"] = obs_with_showdown
    stats["hands_with_observations"] = obs_hands
    return dict(stats)


def audit_derivability(hands: list[dict]) -> dict:
    """判局面/赢家的可推导性（只统计，不猜真值）。"""
    out = Counter()
    for hand in hands:
        board = hand.get("board") or []
        shows = [s for s in (hand.get("shows") or []) if isinstance(s, dict)]
        last = _last_street(hand)
        active = _active(hand, last) if last else []
        active_flop = _active(hand, "flop")
        out[f"last_street_{last}"] += 1
        if board:
            out[f"last_street_vs_board_{last}_{len(board)}"] += 1
        if len(board) >= 3:
            out["has_flop_board"] += 1
        if active_flop:
            out[f"flop_active_{min(len(active_flop), 4)}"] += 1
        if not shows:
            continue
        out["show_hands"] += 1
        # 亮牌座位是否还在最后一条街的活跃集合里
        shown_seats = {str(s.get("seat_id")) for s in shows}
        if active:
            if shown_seats <= set(active):
                out["show_seats_all_active"] += 1
            elif shown_seats & set(active):
                out["show_seats_partially_active"] += 1
            else:
                out["show_seats_none_active"] += 1
        # 赢家推导：最后只剩 1 人 → 动作线可定赢家；>=2 人 → 只能靠亮牌集合
        if len(active) == 1:
            out["winner_by_action_single_active"] += 1
        elif len(active) >= 2:
            out["winner_needs_showdown"] += 1
            out[f"showdown_showers_{min(len(shows), 3)}"] += 1
            if len(shows) == 1:
                out["showdown_single_shower"] += 1
                if len(board) >= 5:
                    out["single_shower_full_board"] += 1
        # 亮牌发生在最后一条街之前的证据：最终牌面不到 5 张
        if len(board) < 5:
            out["show_with_board_lt5"] += 1
            out[f"show_board_{len(board)}"] += 1
        if len(shows) == 1 and len(shows[0].get("cards") or []) == 1:
            out["single_card_show"] += 1
    return dict(out)


def _last_action_by_seat(hand: dict) -> dict[str, dict]:
    """每个座位在本手的最后一条动作（用来识别「已弃牌却亮牌」）。"""
    last: dict[str, dict] = {}
    for street in hand.get("streets") or []:
        for action in street.get("actions") or []:
            last[str(action.get("seat_id"))] = action
    return last


def audit_witness(hands: list[dict]) -> dict:
    """按 clean 闸门的口径统计样本量（判定与 `showdown_reads` 共用一处）。

    clean = 2 张牌 + 牌面 >=3 + 该座位最后动作不是 fold + 仍在最后一街的
    在局集合里。判定逻辑现在住在 `poker_ocr.opponent_profile.showdown_read`
    （#20 之前这里是第二份重复实现，两边会漂）。
    """
    out: Counter = Counter()
    per: dict[str, Counter] = defaultdict(Counter)
    families: Counter = Counter()
    for hand in hands:
        board = [str(c) for c in (hand.get("board") or [])]
        shows = {str(s.get("seat_id")): s
                 for s in (hand.get("shows") or [])
                 if isinstance(s, dict) and s.get("seat_id")}
        full_board = len(board) == 5
        river_active = set(_active(hand, "river")) if full_board else set()
        if full_board and len(river_active) >= 2:
            out["showdown_hands"] += 1
        last = _last_action_by_seat(hand)
        clean: list[tuple[str, int, bool]] = []
        hand_clean = False
        for read in showdown_reads(hand):
            if read.reason:
                out[f"drop_{read.reason}"] += 1
                continue
            # clean 口径下不该再出现「最后动作是 fold」；出现就是闸门漏了。
            if last.get(read.seat_id, {}).get("action") == "fold":
                out["clean_fold_contradiction"] += 1
            name = str((shows.get(read.seat_id) or {}).get("name")
                       or read.seat_id)
            per[name][(read.category, read.aggressive)] += 1
            families[FAMILY_LABELS.get(read.category, str(read.category))] += 1
            clean.append((read.seat_id, read.category, read.aggressive))
            out["clean_entries"] += 1
            hand_clean = True
        if hand_clean:
            out["clean_hands"] += 1
        if full_board and len(river_active) >= 2:
            showers = [c for c in clean if c[0] in river_active]
            if not showers:
                out["winner_unknown"] += 1
            else:
                out["winner_derivable"] += 1
                out[f"winner_showers_{min(len(showers), 4)}"] += 1
    counts = sorted((sum(c.values()) for c in per.values()), reverse=True)
    out["players"] = len(per)
    out["per_player_hist"] = dict(sorted(Counter(counts).items()))
    for threshold in (3, 5, 8, 10):
        out[f"players_ge{threshold}"] = sum(1 for n in counts if n >= threshold)
        out[f"players_family_cell_ge{threshold}"] = sum(
            1 for cells in per.values()
            if max((n for (cat, _aggr), n in cells.items()), default=0) >= threshold)
        out[f"players_family_x_aggr_cell_ge{threshold}"] = sum(
            1 for cells in per.values() if max(cells.values(), default=0) >= threshold)
    # 零值也写进 JSON：验收要按 reason 对总数，缺键会被误读成「没有这个 reason」。
    for reason in SHOWDOWN_DROP_REASONS:
        out.setdefault(f"drop_{reason}", 0)
    out.setdefault("clean_fold_contradiction", 0)
    out["families"] = dict(families.most_common())
    out["family_aggressive"] = dict(Counter(
        f"{FAMILY_LABELS.get(cat, cat)}/{'aggr' if aggr else 'passive'}"
        for cells in per.values() for (cat, aggr), n in cells.items() for _ in range(n)))
    return dict(out)


# ─────────────────────────── 报告 ───────────────────────────

def _print(title: str) -> None:
    print(f"\n## {title}\n")


def print_human(result: dict) -> None:
    _print("语料")
    print(f"- 手牌 {result['corpus']['hands']} 手，文件 {result['corpus']['files']} 个，"
          f"schema {result['corpus']['schemas']}")
    print(f"- 目录分布：{result['corpus']['dirs']}")

    _print("顶层字段覆盖率")
    print("| 字段 | 非空 | 覆盖率 | 类型 |")
    print("|---|---:|---:|---|")
    for label, stat in result["top"]:
        print(row(label, stat))

    shows = result["shows"]
    _print("shows（亮牌）")
    print(f"- 有亮牌的手牌 **{shows['hands_with_shows']}** 手，亮牌条目 **{shows['entries']}** 条")
    print(f"- 每条字段集合：{shows['show_key_sets']}")
    print(f"- 张数分布：{shows['card_counts']}；"
          f"含 street/时机键的条目：{shows['has_timing_key']}；"
          f"含 winner 键的条目：{shows['has_winner_key']}")
    print(f"- name 空 {shows['name_null']}，position 空 {shows['position_null']}，"
          f"hero 自己的亮牌 {shows['hero_shows']}")
    print(f"- 每手亮牌人数：{shows['per_hand_showers']}")
    print(f"- 有亮牌手牌的最终牌面长度：{shows['hands_by_board_len']}")
    print(f"- 牌面长度 × 亮牌张数：{shows['board_x_cards']}")

    streets = result["streets"]
    _print("streets（逐街动作）")
    print(f"- 街值：{streets['street_variants']}；有该街的手牌数：{streets['hands_by_street']}")
    print(f"- 动作总数 {streets['total_actions']}，inferred {streets['inferred']} "
          f"({streets['inferred_pct']:.1%})")
    print(f"- 动作字段：{streets['action_keys']}")
    print(f"- 动作类型：{streets['action_kinds']}")
    print(f"- amount 空值（按动作）：{streets['amount_null']}")
    print(f"- pot_start 缺失（按街）：{streets['pot_start_missing']}")
    print(f"- 每街动作数：{streets['actions_per_street']}")

    meta = result["meta"]
    _print("seat_meta / start_stacks")
    print(f"- seat_meta 条目 {meta['entries']}，name 空 {meta['name_null_pct']:.1%}，"
          f"position 空 {meta['position_null_pct']:.1%}")
    print(f"- position 取值：{meta['positions']}")
    print(f"- 每手座位数：{meta['per_hand']}")
    print(f"- start_stacks 条目 {meta['stack_entries']}，stack 空 {meta['stack_null_pct']:.1%}")

    board = result["board"]
    _print("board / hero_cards")
    print(f"- board 长度：{board['board_lengths']}")
    print(f"- hero_cards 长度：{board['hero_card_lengths']}")

    adv = result["advices"]
    _print("advices")
    print(f"- 有建议的手牌 {adv['hands_with_advices']}，建议条目 {adv['advices']}")
    print(f"- 街分布：{adv['street_dist']}")
    print(f"- 带 facts {adv['with_facts']}，带 context {adv['with_context']}，"
          f"带 opponent_reads {adv['with_opponent_reads']}")
    print(f"- 字段：{sorted(adv['keys'])}")

    sd = result["showdown"]
    _print("按 `showdown_read` 口径复算")
    print(f"- 2 张亮牌手牌 {sd.get('hands_with_two_card_show', 0)}，"
          f"其中牌面 >=3 可判族 {sd.get('hands_showdown_eligible', 0)}")
    drops = "，".join(f"{r} {sd.get('dropped_' + r, 0)}"
                     for r in SHOWDOWN_DROP_REASONS)
    print(f"- 亮牌条目：保留 {sd.get('kept', 0)}；丢弃（按 reason）：{drops}")
    print(f"- 观察数 {sd.get('observations', 0)}，其中有摊牌牌型 "
          f"{sd.get('observations_with_showdown', 0)}，覆盖手牌 "
          f"{sd.get('hands_with_observations', 0)}")

    der = result["derivability"]
    _print("判局面 / 赢家的可推导性")
    keys = [
        "has_flop_board", "show_hands", "show_seats_all_active",
        "show_seats_partially_active", "show_seats_none_active",
        "winner_by_action_single_active", "winner_needs_showdown",
        "showdown_single_shower", "single_shower_full_board",
        "single_card_show", "show_with_board_lt5",
        "flop_active_1", "flop_active_2", "flop_active_3", "flop_active_4",
    ]
    for key in keys:
        if key in der:
            print(f"- {key}: {der[key]}")

    wit = result["witness"]
    _print("可当 witness 的亮牌样本量（2 张 + 牌面>=3 + 未弃牌 + 最后街仍在局）")
    print(f"- clean 亮牌条目 **{wit['clean_entries']}** 条，覆盖 "
          f"**{wit.get('clean_hands', 0)}** 手 / **{wit.get('players', 0)}** 名玩家")
    drops = "，".join(f"{r} {wit.get('drop_' + r, 0)}"
                     for r in SHOWDOWN_DROP_REASONS)
    print(f"- 丢弃（按 reason）：{drops}")
    print(f"- clean 口径 fold×亮牌矛盾：{wit.get('clean_fold_contradiction', 0)}"
          f"（应为 0）")
    print(f"- 每人 clean 摊牌数分布：{wit.get('per_player_hist')}")
    print(f"- 玩家数 >=3/5/8/10 次：{wit.get('players_ge3')}/{wit.get('players_ge5')}/"
          f"{wit.get('players_ge8')}/{wit.get('players_ge10')}")
    print(f"- 单族格 >=3/5/8 的玩家：{wit.get('players_family_cell_ge3')}/"
          f"{wit.get('players_family_cell_ge5')}/{wit.get('players_family_cell_ge8')}；"
          f"族x主动性格 >=3/5/8：{wit.get('players_family_x_aggr_cell_ge3')}/"
          f"{wit.get('players_family_x_aggr_cell_ge5')}/{wit.get('players_family_x_aggr_cell_ge8')}")
    print(f"- 族分布：{wit.get('families')}")
    print(f"- 满河 5 张 / 河牌起始 >=2 人的摊牌手：{wit.get('showdown_hands')}；"
          f"可由亮牌排序推出赢家 {wit.get('winner_derivable')}，"
          f"无 active 亮牌不可推 {wit.get('winner_unknown')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hands", nargs="+", default=DEFAULT_GLOB,
                        help="手牌 JSON glob（可多个），默认 data/hand_outbox/*/*.json")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()

    hands, files = load_hands(args.hands)
    if not hands:
        sys.exit("没有读到任何手牌记录")

    dirs = Counter(path.parent.name for path in files)
    result = {
        "corpus": {
            "hands": len(hands),
            "files": len(files),
            "schemas": dict(Counter(str(h.get("schema")) for h in hands)),
            "dirs": dict(dirs),
        },
        "top": audit_top(hands),
        "shows": audit_shows(hands),
        "streets": audit_streets(hands),
        "meta": audit_meta(hands),
        "board": audit_board(hands),
        "advices": audit_advices(hands),
        "showdown": audit_showdown(hands),
        "derivability": audit_derivability(hands),
        "witness": audit_witness(hands),
        "contract": {
            "showdown_reads": SHOWDOWN_FIELDS,
            "observation_reads": OBSERVATION_FIELDS,
        },
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_human(result)


if __name__ == "__main__":
    main()
