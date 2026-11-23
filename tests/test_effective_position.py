#!/usr/bin/env python3
"""#113 有效位置（身后人数）+ 少于 7 人硬告警 的验收。

    nv tests/test_effective_position.py

覆盖：
  1. `pot_math.seats_behind()`：按**本街真实行动顺序**数身后还在局的对手，
     翻前 SB/BB 算身后、翻后不算；位置读不出时 fail open 返回 None；
  2. `hand_class.effective_position_bucket()`：座位静态档与人数档取更晚，
     SB/BB 恒为盲注档，behind=None 时退回静态档；
  3. 7/8/9 人桌 × 全部座位 × 翻前/翻后 的档位表（与 issue #113 验收逐项对照）；
  4. 有效档真的改变书卡匹配（9-max UTG+2 翻前从 early 卡换到 middle 卡）；
  5. 少于 7 人：硬告警同时进 facts["warnings"] 与提示词数据质量段；
  6. 提示词的位置事实带有效档，且不新增整段。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dataclasses import replace  # noqa: E402

from poker_ocr.advice import (  # noqa: E402
    _facts_snapshot, build_messages, build_situation, render_facts_zh,
)
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    HandSnapshot, LiveHandTracker, OpponentSnapshot,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.positions import POSITIONS_BY_TABLE_SIZE  # noqa: E402
from poker_ocr.playbook.hand_class import (  # noqa: E402
    effective_position_bucket, position_bucket,
)
from poker_ocr.playbook.prompt import resolve_for_situation  # noqa: E402
from poker_ocr.pot_math import seats_behind  # noqa: E402
from poker_ocr.profile import DEFAULT_PREFLOP_MIN_OPEN_BB  # noqa: E402
from poker_ocr.prompt.user.quality import _summarize_gaps  # noqa: E402

SHORT_TABLE_WARNING = "少于 7 人，第一轮未校准，建议离桌"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def snap(*, table_size: int, hero: str, street: str = "preflop",
         folded: tuple[str, ...] = (),
         unknown_positions: tuple[str, ...] = ()):
    """最小快照：只有一个完整座位环上的对手，位置就是座位名。

    `unknown_positions` 里的座位在局但位置读不出来（None）；`folded` 里的
    座位已弃牌。与 `_snapshot` 的口径一致：opponents 只含 hero 之外的座位。
    """
    order = POSITIONS_BY_TABLE_SIZE[table_size]
    opponents = tuple(
        OpponentSnapshot(
            seat_id=f"seat-{pos}", name=None,
            position=None if pos in unknown_positions else pos,
            stack=100.0, bet=0.0, folded=pos in folded,
        )
        for pos in order if pos != hero
    )
    return _Snapshot(table_size=table_size, hero=hero, street=street,
                     opponents=opponents)


class _Snapshot:
    """只带 seats_behind 需要的字段；表格型 dict 访问不到的位置就露馅。"""

    def __init__(self, *, table_size, hero, street, opponents):
        self.table_size = table_size
        self.hero_position = hero
        self.street = street
        self.opponents = opponents


# ───────────────── 1. seats_behind：本街行动顺序 ─────────────────
# 期望值来自 `positions.POSITIONS_BY_TABLE_SIZE` 的契约（按翻前行动顺序排）
# 与 issue #113 的文字规则，不调用被测函数反推。

PREFLOP_BEHIND = {
    9: {"UTG": 8, "UTG+1": 7, "UTG+2": 6, "LJ": 5, "HJ": 4,
        "CO": 3, "BTN": 2, "SB": 1, "BB": 0},
    8: {"UTG": 7, "UTG+1": 6, "LJ": 5, "HJ": 4,
        "CO": 3, "BTN": 2, "SB": 1, "BB": 0},
    7: {"UTG": 6, "LJ": 5, "HJ": 4, "CO": 3, "BTN": 2, "SB": 1, "BB": 0},
}

POSTFLOP_BEHIND = {
    9: {"SB": 8, "BB": 7, "UTG": 6, "UTG+1": 5, "UTG+2": 4, "LJ": 3,
        "HJ": 2, "CO": 1, "BTN": 0},
    8: {"SB": 7, "BB": 6, "UTG": 5, "UTG+1": 4, "LJ": 3, "HJ": 2,
        "CO": 1, "BTN": 0},
    7: {"SB": 6, "BB": 5, "UTG": 4, "LJ": 3, "HJ": 2, "CO": 1, "BTN": 0},
}


def test_seats_behind_preflop_full_ring():
    for size, expected in PREFLOP_BEHIND.items():
        for position, want in expected.items():
            got = seats_behind(
                snap(table_size=size, hero=position, street="preflop"))
            check(got == want,
                  f"{size}-max 翻前 {position} 身后应 {want} 人，实际 {got}")


def test_seats_behind_postflop_full_ring():
    """翻后盲注先行动 → SB/BB 不计入身后（不是「所有人都在身后」）。"""
    for size, expected in POSTFLOP_BEHIND.items():
        for position, want in expected.items():
            got = seats_behind(
                snap(table_size=size, hero=position, street="flop"))
            check(got == want,
                  f"{size}-max 翻后 {position} 身后应 {want} 人，实际 {got}")


def test_seats_behind_counts_only_in_hand_opponents():
    """弃牌的不算；身后的人弃牌后档位只会更晚，不会是 None。"""
    folded = ("UTG+2",)
    got = seats_behind(
        snap(table_size=9, hero="UTG+1", street="flop", folded=folded))
    check(got == 4, f"UTG+2 弃牌后 UTG+1 身后应 4 人，实际 {got}")
    got = seats_behind(
        snap(table_size=9, hero="SB", street="flop", folded=("UTG+2",)))
    check(got == 7, f"SB 身后应 7 人（UTG+2 弃牌），实际 {got}")
    # 弃牌的人即使位置读不出来，也不该让整张表变 None。
    got = seats_behind(
        snap(table_size=9, hero="UTG", street="flop",
             folded=("UTG+2",), unknown_positions=("UTG+2",)))
    check(got == 5, f"弃牌座位的未知位置不该阻塞计数，实际 {got}")


def test_seats_behind_fails_open_on_unreadable_positions():
    """位置表缺 / 英雄位置未知 / 在局对手位置未知 → None（不猜）。"""
    base = snap(table_size=9, hero="UTG")
    base.hero_position = None
    check(seats_behind(base) is None, "英雄位置未知时应返回 None")

    base = snap(table_size=9, hero="UTG")
    base.hero_position = "MP"
    check(seats_behind(base) is None, "英雄座位名不在位置表时应返回 None")

    base = snap(table_size=9, hero="UTG", unknown_positions=("HJ",))
    check(seats_behind(base) is None, "在局对手位置读不出时应返回 None")

    base = snap(table_size=9, hero="UTG")
    base.table_size = 10
    check(seats_behind(base) is None, "没有 10 人桌位置表时应返回 None")


def test_seats_behind_street_override():
    """翻前快照上按翻后顺序数：盲注从身后变到身前。"""
    base = snap(table_size=9, hero="UTG", street="preflop")
    check(seats_behind(base) == 8, "缺省用快照的 street（翻前 8）")
    check(seats_behind(base, "flop") == 6,
          "显式 street='flop' 时盲注不在身后（6）")


# ───────────────── 2. 有效档：座位档与人数档取更晚 ─────────────────

def test_effective_bucket_thresholds():
    """身后 ≥7 早位 / 5–6 中位 / ≤4 晚位。用静态早位的座位看三段边界。"""
    for behind, want in ((8, "early"), (7, "early"), (6, "middle"),
                         (5, "middle"), (4, "late"), (0, "late")):
        got = effective_position_bucket("UTG", behind)
        check(got == want, f"UTG 身后 {behind} 人应 {want}，实际 {got}")


def test_effective_bucket_count_only_widens():
    """人数只用来放宽，不会把座位判得比座位名更早。"""
    # 中位座位身后再多人也不能退回早位；晚位同理。
    for position, want in (("LJ", "middle"), ("HJ", "middle"),
                           ("CO", "late"), ("BTN", "late")):
        got = effective_position_bucket(position, 8)
        check(got == want, f"{position} 身后 8 人应留在 {want}，实际 {got}")
    # 静态早位、身后不多时照人数走晚；静态晚位、身后很多也留在晚。
    check(effective_position_bucket("UTG+2", 8) == "early",
          "UTG+2 身后 8 人仍是早位")
    check(effective_position_bucket("BTN", 7) == "late",
          "BTN 身后很多人仍是晚位")


def test_effective_bucket_blinds_stay_blind():
    """SB/BB 恒为盲注档，不参与人数映射。"""
    for position in ("SB", "BB"):
        for behind in (None, 0, 4, 7, 8):
            got = effective_position_bucket(position, behind)
            check(got == "blind",
                  f"{position} 身后 {behind} 人应恒为 blind，实际 {got}")


def test_effective_bucket_fails_open_to_static():
    """behind=None（算不出）退回静态档；座位名未知返回 None，不崩。"""
    for position in ("UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"):
        check(effective_position_bucket(position, None)
              == position_bucket(position),
              f"{position} behind=None 应退回静态档")
    check(effective_position_bucket(None, 4) is None, "英雄位置 None → None")
    check(effective_position_bucket("MP", 4) is None, "未知座位名 → None")
    check(effective_position_bucket("", 7) is None, "空座位名 → None")


def test_snapshot_unreadable_opponent_falls_back_to_static():
    """端到端 fail open：任一在局对手位置读不出 → 静态档 + 静态卡，不崩。"""
    base = full_snapshot(table_size=9, hero="UTG+2", cards=("8h", "8d"))
    opponents = tuple(
        replace(o, position=None) if o.position == "HJ" else o
        for o in base.opponents)
    sit = build_situation(
        replace(base, opponents=opponents), table_dynamics="")
    check(sit.seats_behind is None,
          f"对手位置未知时身后人数应为 None，实际 {sit.seats_behind}")
    check(sit.effective_position_bucket == "early",
          f"应退回座位静态档 early，实际 {sit.effective_position_bucket}")
    spot, card = resolve_for_situation(sit)
    check(spot.position_bucket == "early",
          f"书卡档应退回 early，实际 {spot.position_bucket}")
    check(card is not None and card.id == "preflop.rfi.low_pair.early",
          f"应按早位卡出建议，实际 {getattr(card, 'id', None)}")
    user = render_facts_zh(sit)
    line = next((ln for ln in user.splitlines() if "有效位置" in ln), "")
    effective = line.split("｜")[0] if line else ""
    check(effective == "有效位置：前位（UTG+2）",
          f"位置事实只报静态档、不编身后人数：{line}")


# ───────────────── 3. 快照 → Situation → 书卡 ─────────────────

def full_snapshot(*, table_size: int, hero: str, street: str = "preflop",
                  folded: tuple[str, ...] = (),
                  cards: tuple[str, str] = ("Ah", "Kd")):
    """真实 `HandSnapshot`：对手铺满整个座位环，位置就是座位名。"""
    order = POSITIONS_BY_TABLE_SIZE[table_size]
    opponents = tuple(
        OpponentSnapshot(f"seat-{pos}", None, pos, 100.0, 0.0, pos in folded)
        for pos in order if pos != hero
    )
    flop = street != "preflop"
    return HandSnapshot(
        hand_id=f"eff-{table_size}-{hero}-{street}", hand_index=0,
        street=street, board=("2c", "7d", "Jh") if flop else (),
        hero_cards=cards, hero_position=hero,
        hero_stack=100.0, hero_bet=0.0, hero_invested=0.0,
        to_call=0.0 if flop else 1.0, second_bet=None, pot=6.0,
        pot_before_street=0.0, table_size=table_size,
        blinds=Blinds(0.5, 1.0, None, "1/2"),
        opponents=opponents, streets=(),
        preflop_shapes=() if flop else ("unopened",),
        hero_is_pfr=False, cbet_spot=False,
        decision_key=("eff", street, 0, 6.0, 0.0),
        warnings=(), bomb_pot=False, players_dealt=table_size,
        preflop_min_open_bb=DEFAULT_PREFLOP_MIN_OPEN_BB,
    )


# issue #113 验收表：7/8/9 人 × 全部座位 × 翻前/翻后。
EXPECTED_BUCKETS = {
    "preflop": {
        9: {"UTG": "early", "UTG+1": "early", "UTG+2": "middle",
            "LJ": "middle", "HJ": "late", "CO": "late", "BTN": "late",
            "SB": "blind", "BB": "blind"},
        8: {"UTG": "early", "UTG+1": "middle", "LJ": "middle",
            "HJ": "late", "CO": "late", "BTN": "late",
            "SB": "blind", "BB": "blind"},
        7: {"UTG": "middle", "LJ": "middle", "HJ": "late",
            "CO": "late", "BTN": "late", "SB": "blind", "BB": "blind"},
    },
    "flop": {
        9: {"SB": "blind", "BB": "blind", "UTG": "middle",
            "UTG+1": "middle", "UTG+2": "late", "LJ": "late",
            "HJ": "late", "CO": "late", "BTN": "late"},
        8: {"SB": "blind", "BB": "blind", "UTG": "middle",
            "UTG+1": "late", "LJ": "late", "HJ": "late",
            "CO": "late", "BTN": "late"},
        7: {"SB": "blind", "BB": "blind", "UTG": "late", "LJ": "late",
            "HJ": "late", "CO": "late", "BTN": "late"},
    },
}


def test_acceptance_table_preflop_and_postflop():
    for street, table in EXPECTED_BUCKETS.items():
        for size, expected in table.items():
            for position, want in expected.items():
                sit = build_situation(
                    full_snapshot(table_size=size, hero=position,
                                  street=street), table_dynamics="")
                got = sit.effective_position_bucket
                check(got == want,
                      f"{size}-max {street} {position} 应 {want}，实际 {got}")
                spot, _card = resolve_for_situation(sit)
                check(spot.position_bucket == want,
                      f"{size}-max {street} {position} 书卡档应 {want}，"
                      f"实际 {spot.position_bucket}")


def test_postflop_counts_fewer_players_only_later():
    """同一手翻前→翻后、身后有人弃牌：档位只会更晚，不会更早。"""
    rank = {"early": 0, "middle": 1, "late": 2, "blind": 3}
    for size in (7, 8, 9):
        for position in POSITIONS_BY_TABLE_SIZE[size]:
            before = build_situation(
                full_snapshot(table_size=size, hero=position,
                              street="preflop"), table_dynamics="")
            after = build_situation(
                full_snapshot(table_size=size, hero=position,
                              street="flop"), table_dynamics="")
            check(rank[after.effective_position_bucket]
                  >= rank[before.effective_position_bucket],
                  f"{size}-max {position} 翻后档不能比翻前早："
                  f"{before.effective_position_bucket} → "
                  f"{after.effective_position_bucket}")

    # 9-max 翻后 UTG+1：UTG+2 弃牌 → 身后 5 → 4，中位 → 后位。
    bent = build_situation(
        full_snapshot(table_size=9, hero="UTG+1", street="flop",
                      folded=("UTG+2",)), table_dynamics="")
    check(bent.seats_behind == 4, f"UTG+2 弃牌后身后应 4 人，实际 {bent.seats_behind}")
    check(bent.effective_position_bucket == "late",
          f"减员后应更晚（late），实际 {bent.effective_position_bucket}")


def test_effective_bucket_changes_book_card():
    """有效档真的换了卡：9-max UTG+2 翻前 88，早位卡（跟）→ 中位卡（加）。"""
    utg = build_situation(full_snapshot(table_size=9, hero="UTG",
                                        cards=("8h", "8d")), table_dynamics="")
    utg2 = build_situation(full_snapshot(table_size=9, hero="UTG+2",
                                         cards=("8h", "8d")), table_dynamics="")
    check(utg.effective_position_bucket == "early",
          f"UTG 有效档应 early，实际 {utg.effective_position_bucket}")
    check(utg2.effective_position_bucket == "middle",
          f"UTG+2 有效档应 middle，实际 {utg2.effective_position_bucket}")
    _spot_u, card_u = resolve_for_situation(utg)
    _spot_2, card_2 = resolve_for_situation(utg2)
    check(card_u is not None and card_2 is not None, "两处都应命中卡片")
    check(card_u.id != card_2.id,
          f"有效档应改变命中卡：UTG={card_u.id} UTG+2={card_2.id}")
    check(card_u.id == "preflop.rfi.low_pair.early",
          f"UTG 应命中早位 88 卡，实际 {card_u.id}")
    check(card_2.id == "preflop.rfi.low_pair.middle",
          f"UTG+2 应命中中位 88 卡，实际 {card_2.id}")
    check(card_u.plan.action == "call" and card_2.plan.action == "raise",
          f"早位跟/中位加应换过来：{card_u.plan.action} → {card_2.plan.action}")


# ───────────────── 4. 少于 7 人：硬告警 ─────────────────

def player(seat_id: str, *, position: str | None = None,
           stack: float | None = 100.0, bet: float | None = 0.0,
           name: str | None = None) -> PlayerState:
    return PlayerState(seat_id=seat_id, name=name, position=position,
                       stack=stack, bet=bet, action=None, fold=None,
                       in_hand=True, all_in=False, status="playing",
                       left=False, bet_unknown=False)


def observed_point(*, table_seats: int):
    """走真实手牌跟踪：两帧确认后在我这里出一个决策点。"""
    players = (player("hero", position="BTN", stack=100.0),
               player("a", position="BB", bet=1.0, name="老王"),
               player("b", position="SB", bet=0.5, name="小李"))
    state = FrameState(
        frame_id="f", table_seats=table_seats, players_in_hand=3,
        players=list(players), blinds=Blinds(0.5, 1.0, None, "1/2"),
        dealer="a", hero_seat_id="hero", hero_turn=False, pot=2.0,
        pot_before_street=0.0, preflop_min_open_bb=DEFAULT_PREFLOP_MIN_OPEN_BB)
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state, ["Ah", "Kd"], [])
    point = tracker.observe(replace(state, hero_turn=True), ["Ah", "Kd"], [])
    check(point is not None, f"{table_seats} 人桌轮到我时应产出决策点")
    return point


def test_short_table_warning_in_snapshot_facts_and_prompt():
    """6 人桌：逐字硬告警同时进快照 warnings / facts / 数据质量段。"""
    point = observed_point(table_seats=6)
    if point is None:
        return
    check(SHORT_TABLE_WARNING in point.warnings,
          f"快照 warnings 要逐字带少于 7 人告警：{point.warnings}")

    sit = build_situation(point, table_dynamics="")
    check(SHORT_TABLE_WARNING in sit.warnings,
          f"Situation 要保留告警：{sit.warnings}")
    facts = _facts_snapshot(sit)
    check(SHORT_TABLE_WARNING in facts["warnings"],
          f'facts["warnings"] 要逐字带告警：{facts["warnings"]}')

    user = render_facts_zh(sit)
    check("## 数据质量" in user, "prompt 应有数据质量段")
    quality = user.split("## 数据质量", 1)[1]
    check(SHORT_TABLE_WARNING in quality,
          f"数据质量段要逐字带告警：{quality[:400]}")
    hard_lines = [line for line in quality.splitlines()
                  if line.startswith("- 硬缺口")]
    check(bool(hard_lines) and SHORT_TABLE_WARNING in hard_lines[0],
          f"少于 7 人应算硬缺口（走保守线）：{hard_lines}")
    _messages = build_messages(sit)
    check(SHORT_TABLE_WARNING in _messages[1],
          "发给模型的消息里也要有这条告警")

    # 硬缺口排在最前，四条上限也挤不掉它。
    summary = _summarize_gaps([
        "只建模前 2 名对手；另有 1 名未计入范围权益",
        "部分座位翻前动作线缺失，范围按未开池先验估计",
        "部分座位本街行动线缺失，范围按未行动处理（不是已过牌）",
        "某座位范围未知",
        SHORT_TABLE_WARNING,
    ])
    check(SHORT_TABLE_WARNING in summary,
          f"少于 7 人是硬缺口，必须活过 4 条 cap：{summary}")


def test_seven_or_more_players_has_no_short_table_warning():
    for seats in (7, 8, 9):
        point = observed_point(table_seats=seats)
        if point is None:
            continue
        check(SHORT_TABLE_WARNING not in point.warnings,
              f"{seats} 人桌不该有少于 7 人告警：{point.warnings}")


def test_six_max_still_gives_advice_without_raising():
    """6 人桌不因为告警而少选项/抛异常，照常能出建议卡所需事实。"""
    point = observed_point(table_seats=6)
    if point is None:
        return
    sit = build_situation(point, table_dynamics="")
    check(bool(sit.options), "6 人桌仍要有可选动作（不是空转）")
    check(sit.effective_position_bucket == "late",
          f"6 人桌 BTN 仍是后位，实际 {sit.effective_position_bucket}")
    facts = _facts_snapshot(sit)
    check(bool(facts["options"]), "建议卡事实里仍要有 options")


# ───────────────── 5. 提示词：位置事实带有效档 ─────────────────

def prompt_for(*, table_size: int, hero: str, street: str = "preflop"):
    sit = build_situation(
        full_snapshot(table_size=table_size, hero=hero, street=street),
        table_dynamics="")
    return render_facts_zh(sit)


def test_prompt_position_line_carries_effective_bucket():
    """9-max UTG+2 翻前：位置事实要写「有效位置：中位」，不新增整段。"""
    user = prompt_for(table_size=9, hero="UTG+2")
    check("有效位置：中位" in user, f"该有有效位置中位：{user[:600]}")
    check("## 有效位置" not in user, "不许为有效位置新开一段")
    lines = [line for line in user.splitlines() if "有效位置：" in line]
    check(len(lines) == 1, f"有效位置只该占一行：{lines}")
    check(any("翻后位置" in line for line in lines),
          f"有效位置要与既有位置事实同行：{lines}")

    # 同一手牌按当前街算：UTG 翻前早位、翻后中位。
    check("有效位置：前位" in prompt_for(table_size=9, hero="UTG"),
          "9-max UTG 翻前应前位（书里的 early）")
    check("有效位置：中位" in prompt_for(table_size=9, hero="UTG", street="flop"),
          "9-max UTG 翻后应中位（当前街口径）")
    # 7-max UTG 翻前（身后 6）放宽到中位。
    check("有效位置：中位" in prompt_for(table_size=7, hero="UTG"),
          "7-max UTG 翻前应中位")


def test_prompt_blind_bucket_does_not_map_seats():
    for position in ("SB", "BB"):
        user = prompt_for(table_size=9, hero=position)
        check("有效位置：盲注" in user,
              f"{position} 的有效位置应恒为盲注：{user[:300]}")


def test_prompt_position_unknown_does_not_crash_or_invent():
    snap = replace(full_snapshot(table_size=9, hero="UTG"), hero_position=None)
    sit = build_situation(snap, table_dynamics="")
    check(sit.effective_position_bucket is None,
          f"位置未知时有效档应为 None：{sit.effective_position_bucket}")
    user = render_facts_zh(sit)
    check("有效位置：" not in user, f"位置未知时不该编一个档：{user[:600]}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {test.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n✅ 有效位置 / 少于 7 人全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
