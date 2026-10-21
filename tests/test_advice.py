#!/usr/bin/env python3
"""实时建议链路的测试：行动线累积、决策点触发、按街加载技能、选项与模型调用。

    .venv/bin/python tests/test_advice.py

全部是纯逻辑：手工拼 FrameState，不连 RTSP、不加载 OCR、不真的调模型
（模型调用用注入的 `complete` 桩）。期望值对着 call-back/services/skills/ 的规则。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unittest.mock import patch  # noqa: E402

import poker_ocr.advice as advice_mod  # noqa: E402
from poker_ocr import skills  # noqa: E402
from poker_ocr.advice import (  # noqa: E402
    AISettings, AIError, AdviceRunner, OPTION_ORDER, advise,
    build_messages, build_options, build_situation, complete_jev, complete_json,
    is_jev_model, jev_decisions, jev_headers, jev_payload, parse_json_content,
    render_hand_facts, value_directive,
)
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker, street_of_board  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.table_dynamics import (  # noqa: E402
    MAX_CHARS, load_table_dynamics, parse_table_dynamics, value_heavy_aggression,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


BLINDS = Blinds(sb=0.5, bb=1.0, ante=None, raw="1/2")


def player(seat_id: str, *, name: str | None = None, position: str | None = None,
           stack: float | None = 100.0, bet: float | None = 0.0,
           action: str | None = None, fold: bool | None = None,
           in_hand: bool = True, all_in: bool = False,
           status: str = "playing") -> PlayerState:
    return PlayerState(seat_id=seat_id, name=name, position=position, stack=stack,
                       bet=bet, action=action, fold=fold, in_hand=in_hand,
                       all_in=all_in, status=status)


def state(*players: PlayerState, pot: float = 3.0, pbs: float = 3.0,
          dealer: str = "a", hero_turn: bool | None = False,
          hero_seat: str = "hero", errors: list[str] | None = None,
          table_seats: int | None = None,
          in_hand: int | None = None) -> FrameState:
    return FrameState(
        frame_id="f",
        table_seats=len(players) if table_seats is None else table_seats,
        players_in_hand=len(players) if in_hand is None else in_hand,
        players=list(players), blinds=BLINDS, dealer=dealer,
        hero_seat_id=hero_seat, hero_turn=hero_turn, pot=pot,
        pot_before_street=pbs, errors=errors or [])


def feed(tracker: LiveHandTracker, st: FrameState,
         hero_codes: list[str], board: list[str]):
    return tracker.observe(st, hero_codes, board)


# ─────────────────────────── 行动线 ───────────────────────────

def test_street_of_board() -> None:
    cases = {0: "preflop", 3: "flop", 4: "turn", 5: "river", 1: None, 2: None}
    for n, want in cases.items():
        check(street_of_board(n) == want, f"{n} 张公共牌应 {want}，实际 {street_of_board(n)}")


def test_tracker_collects_actions_and_dedupes_frames() -> None:
    """同一个徽章连续多帧只记一次；中间夹了别人的动作就是新的一次。"""
    tr = LiveHandTracker(confirm_frames=1)
    pre = state(
        player("hero", position="BTN", stack=100.0),
        player("a", name="老王", position="SB", bet=0.5, stack=99.5),
        player("b", name="小李", position="BB", bet=1.0, stack=99.0),
        pot=1.5, pbs=0.0)
    feed(tr, pre, ["Ah", "Qd"], [])

    # 老王跟注：徽章出现 3 帧（真机上会持续几帧）
    for _ in range(3):
        s = state(player("hero", position="BTN"),
                  player("a", position="SB", bet=1.0, action="call", name="老王"),
                  player("b", position="BB", bet=1.0, name="小李"),
                  pot=2.5, pbs=0.0)
        feed(tr, s, ["Ah", "Qd"], [])

    acts = tr.hand.street_actions("preflop")
    calls = [a for a in acts if a.seat_id == "a" and a.action == "call"]
    check(len(calls) == 1, f"老王连跟 3 帧应只记 1 次跟注，实际 {len(calls)}")
    check(calls[0].amount == 1.0, f"跟注金额应是本街总额 1.0，实际 {calls[0].amount}")

    # 小李过牌、老王也过牌（翻后）：同一个动作在不同街不合并
    flop = state(player("hero", position="BTN"),
                 player("a", position="SB", action="check", name="老王"),
                 player("b", position="BB", action="check", name="小李"),
                 pot=3.0, pbs=3.0)
    feed(tr, flop, ["Ah", "Qd"], ["Ks", "7d", "2c"])
    check(tr.hand.street == "flop", f"公共牌 3 张应推进到翻牌，实际 {tr.hand.street}")
    flop_acts = tr.hand.street_actions("flop")
    check([(a.seat_id, a.action) for a in flop_acts]
          == [("a", "check"), ("b", "check")],
          f"翻牌行动线不对：{[(a.seat_id, a.action) for a in flop_acts]}")


def test_tracker_infers_actions_when_badges_are_missed() -> None:
    """徽章读不到时，用本街下注额的变化反推 bet/raise/call；盲注不算行动。"""
    tr = LiveHandTracker(confirm_frames=1)
    feed(tr, state(player("hero", position="BTN"),
                   player("a", position="BB", bet=1.0),
                   player("b", position="CO", bet=0.0),
                   pot=1.5, pbs=0.0), ["Ah", "Kd"], [])
    check(tr.hand.street_actions("preflop") == [],
          f"盲注不该被记成行动：{tr.hand.street_actions('preflop')}")

    # a 加注到 3、b 跟注 3，两个都没有徽章
    feed(tr, state(player("hero", position="BTN"),
                   player("a", position="BB", bet=3.0),
                   player("b", position="CO", bet=3.0),
                   pot=7.5, pbs=0.0), ["Ah", "Kd"], [])
    acts = [(x.seat_id, x.action, x.amount) for x in tr.hand.street_actions("preflop")]
    check(("a", "raise", 3.0) in acts, f"下注额变大应推成加注：{acts}")
    check(("b", "call", 3.0) in acts, f"跟到当前水位应推成跟注：{acts}")

    # 翻牌：下注额归零，a 下注 2（无徽章）→ bet 而不是 raise
    feed(tr, state(player("hero", position="BTN"),
                   player("a", position="BB", bet=2.0),
                   player("b", position="CO", bet=0.0),
                   pot=9.5, pbs=7.5), ["Ah", "Kd"], ["Ks", "7d", "2c"])
    acts = [(x.seat_id, x.action, x.amount) for x in tr.hand.street_actions("flop")]
    check(("a", "bet", 2.0) in acts, f"新街无人下注时应推成下注：{acts}")


def test_tracker_ignores_stale_badge_after_others_acted() -> None:
    """老王的加注徽章在别人行动后仍然停留，不能记成第二次加注。"""
    tr = LiveHandTracker(confirm_frames=1)
    feed(tr, state(player("hero", position="BTN"),
                   player("a", position="SB", action="raise", bet=3.0, name="老王"),
                   player("b", position="BB", bet=1.0, name="小李"),
                   pot=4.5, pbs=0.0), ["Ah", "Qd"], [])

    # 老王加注 → 小李跟注（3），同一帧/下一帧老王的加注徽章还在
    for _ in range(2):
        feed(tr, state(player("hero", position="BTN"),
                       player("a", position="SB", action="raise", bet=3.0,
                              name="老王"),
                       player("b", position="BB", action="call", bet=3.0,
                              name="小李"),
                       pot=7.5, pbs=0.0), ["Ah", "Qd"], [])

    pre = tr.hand.street_actions("preflop")
    check([(a.seat_id, a.action, a.amount) for a in pre]
          == [("a", "raise", 3.0), ("b", "call", 3.0)],
          f"老王的旧徽章不该重复记录："
          f"{[(a.seat_id, a.action, a.amount) for a in pre]}")

    # 小李再加注到 9，老王跟注 9：同一个「跟注」动作但金额变了，必须记第二次
    feed(tr, state(player("hero", position="BTN"),
                   player("a", position="SB", action="call", bet=9.0, name="老王"),
                   player("b", position="BB", action="raise", bet=9.0,
                          name="小李"),
                   pot=16.5, pbs=0.0), ["Ah", "Qd"], [])
    acts = [(a.seat_id, a.action, a.amount)
            for a in tr.hand.street_actions("preflop")]
    check(("a", "call", 9.0) in acts,
          f"跟注 3 之后被加注再跟 9，应记两次跟注：{acts}")
    check(acts.count(("a", "raise", 3.0)) == 1,
          f"第一次加注不该被覆盖或重复：{acts}")


def _flop_scene(turn_pbs: float, hero_call_before_raise: bool = False):
    """复现「最后位置秒跟、视频流漏抓」：SB 在翻牌下注 22，hero(CO) 与
    BTN 跟注后直接进入转牌（下注 pill 已被底池收走）。返回喂完转牌的 tracker。

    `hero_call_before_raise=True` 时，在 SB 下注后多喂一帧 hero 跟注 22，
    用于测试「加注帧漏了、但加注前的跟注帧抓到了」的线路。
    """
    tr = LiveHandTracker(confirm_frames=1)
    feed(tr, state(
        player("a", position="SB", bet=0.5, name="老王"),
        player("b", position="BB", bet=1.0, name="小李"),
        player("hero", position="CO", stack=100.0),
        player("c", position="BTN", name="阿强"),
        pot=1.5, pbs=0.0, dealer="c"), ["Ah", "Qd"], [])

    # 翻牌还没人下注：pot_before_street = 翻前结算后的 1.5
    feed(tr, state(
        player("a", position="SB", name="老王"),
        player("b", position="BB", fold=True, status="folded", name="小李"),
        player("hero", position="CO", stack=100.0),
        player("c", position="BTN", name="阿强"),
        pot=1.5, pbs=1.5, dealer="c"), ["Ah", "Qd"], ["Ks", "7d", "2c"])

    # SB 下注 22（这一帧抓到了）
    feed(tr, state(
        player("a", position="SB", bet=22.0, action="bet", name="老王"),
        player("b", position="BB", fold=True, status="folded", name="小李"),
        player("hero", position="CO", stack=100.0),
        player("c", position="BTN", name="阿强"),
        pot=23.5, pbs=1.5, dealer="c"), ["Ah", "Qd"], ["Ks", "7d", "2c"])

    if hero_call_before_raise:
        # 加注之前 hero 跟注 22 的这一帧抓到了（加注帧仍然漏掉）
        feed(tr, state(
            player("a", position="SB", bet=22.0, action="bet", name="老王"),
            player("b", position="BB", fold=True, status="folded", name="小李"),
            player("hero", position="CO", bet=22.0, action="call", stack=78.0),
            player("c", position="BTN", name="阿强"),
            pot=45.5, pbs=1.5, dealer="c"), ["Ah", "Qd"], ["Ks", "7d", "2c"])

    # 转牌：底池 1.5+66，大家的本街下注已清零 —— 中间两帧跟注没抓到
    feed(tr, state(
        player("a", position="SB", name="老王"),
        player("b", position="BB", fold=True, status="folded", name="小李"),
        player("hero", position="CO",
               stack=78.0 if hero_call_before_raise else 100.0),
        player("c", position="BTN", name="阿强"),
        pot=turn_pbs, pbs=turn_pbs, dealer="c"),
        ["Ah", "Qd"], ["Ks", "7d", "2c", "Th"])
    return tr


def test_tracker_backfills_missed_calls_at_street_end() -> None:
    """街末用底池对账补上漏抓的跟注：SB bet 22，hero 和 BTN 的跟注没抓到，
    转牌底池 +66 说明三人都投了 22 —— 补 hero/BTN 各一条跟注 22。"""
    tr = _flop_scene(turn_pbs=67.5)
    acts = [(a.seat_id, a.action, a.amount, a.inferred)
            for a in tr.hand.street_actions("flop")]
    check(acts == [("a", "bet", 22.0, False),
                   ("hero", "call", 22.0, True),
                   ("c", "call", 22.0, True)],
          f"漏抓的跟注应按行动顺序补在 SB 下注之后：{acts}")

    # 弃牌的 BB 不该被补跟注
    check(all(a.seat_id != "b" for a in tr.hand.street_actions("flop")),
          "弃牌的人不应补记跟注")

    # 补完就收账：翻牌的 pot_start 仍是 1.5，转牌没有凭空多出行动
    check(tr.hand.pot_start.get("flop") == 1.5,
          f"翻牌起始底池应保持 1.5，实际 {tr.hand.pot_start.get('flop')}")
    check(tr.hand.street_actions("turn") == [], "转牌不该有行动")

    rec = tr._hand_dict(tr.hand)
    flop = next(s for s in rec["streets"] if s["street"] == "flop")
    check("inferred" not in flop["actions"][0], "正常记录不应带 inferred")
    check(flop["actions"][1].get("inferred") is True, "补记的跟注应标 inferred")


def test_tracker_backfills_missed_raise_at_street_end() -> None:
    """SB bet 22、hero raise 66、BTN/SB 秒跟，加注和两个跟注帧全漏掉：
    转牌底池 +198 = 3×66 —— 反解最终水位 66，按行动顺序补成
    hero raise 66 → BTN call 66 → SB call 66。"""
    tr = _flop_scene(turn_pbs=199.5)   # 1.5 + 3×66
    acts = [(a.seat_id, a.action, a.amount, a.inferred)
            for a in tr.hand.street_actions("flop")]
    check(acts == [("a", "bet", 22.0, False),
                   ("hero", "raise", 66.0, True),
                   ("c", "call", 66.0, True),
                   ("a", "call", 66.0, True)],
          f"漏抓的加注线应按行动顺序补齐：{acts}")


def test_tracker_backfills_raise_after_recorded_call() -> None:
    """加注前 hero 跟注 22 的那一帧抓到了：加注者仍按「最后一个已记录动作
    之后最早能行动的人」推出来（BTN），hero 的记录保留为 call 22。"""
    tr = _flop_scene(turn_pbs=199.5, hero_call_before_raise=True)
    acts = [(a.seat_id, a.action, a.amount, a.inferred)
            for a in tr.hand.street_actions("flop")]
    check(acts == [("a", "bet", 22.0, False),
                   ("hero", "call", 22.0, False),
                   ("c", "raise", 66.0, True),
                   ("a", "call", 66.0, True),
                   ("hero", "call", 66.0, True)],
          f"加注前的跟注应保留、加注者应从它之后取：{acts}")


def test_tracker_does_not_guess_when_pot_does_not_match() -> None:
    """底池差额对不上（中间可能有加注/读错）时宁可留空，不乱补。"""
    tr = _flop_scene(turn_pbs=89.5)   # 1.5 + 88：不是 3×22
    acts = [(a.seat_id, a.action, a.amount)
            for a in tr.hand.street_actions("flop")]
    check(acts == [("a", "bet", 22.0)],
          f"对不上账时只应保留已抓到的 SB 下注：{acts}")


def test_tracker_new_hand_on_dealer_change() -> None:
    tr = LiveHandTracker(confirm_frames=1)
    feed(tr, state(player("hero", position="BTN"),
                   player("a", position="SB"), player("b", position="BB"),
                   dealer="a"), ["Ah", "Qd"], [])
    first = tr.hand.hand_id
    feed(tr, state(player("hero", position="CO"),
                   player("a", position="BB"), player("b", position="BTN"),
                   dealer="b"), ["7c", "7s"], [])
    check(tr.hand.hand_id != first, "庄位变了应开新手牌")
    check(tr.last_finished is not None and tr.last_finished.hand_id == first,
          "上一手应被保留下来供落库")


def test_tracker_board_drop_needs_two_frames() -> None:
    """单帧公共牌读空是遮挡，连续两帧才是新的一手。"""
    tr = LiveHandTracker(confirm_frames=1)
    players = (player("hero", position="BTN"), player("a", position="BB"))
    feed(tr, state(*players, dealer="a"), ["Ah", "Kd"],
         ["Ks", "7d", "2c", "Qh", "9s"])
    first = tr.hand.hand_id
    feed(tr, state(*players, dealer="a"), ["Ah", "Kd"], [])
    check(tr.hand.hand_id == first, "单帧读空不该换手")
    feed(tr, state(*players, dealer="a"), ["Ah", "Kd"], [])
    check(tr.hand.hand_id != first, "连续两帧读空应换手")


def test_tracker_decision_point_confirmed_and_deduped() -> None:
    tr = LiveHandTracker(confirm_frames=2)
    players = (player("hero", position="BTN"),
               player("a", position="SB", action="call", bet=1.0, name="老王"),
               player("b", position="BB", bet=1.0, name="小李"))
    feed(tr, state(*players, pot=2.5, pbs=0.0), ["Ah", "Qd"], [])
    # 第一帧 true 不满足连续 2 帧
    check(feed(tr, state(*players, pot=2.5, pbs=0.0, hero_turn=True),
               ["Ah", "Qd"], []) is None,
          "hero_turn 只出现 1 帧不应给决策点")
    point = feed(tr, state(*players, pot=2.5, pbs=0.0, hero_turn=True),
                 ["Ah", "Qd"], [])
    check(point is not None, "连续 2 帧轮到我应给决策点")
    # 同一个回合的后续帧不重复给
    again = feed(tr, state(*players, pot=2.5, pbs=0.0, hero_turn=True),
                 ["Ah", "Qd"], [])
    check(again is None, "同一个决策点不应重复给")
    # SB 跟注、BB 补齐后，BTN 的 hero 还要补 1BB 才能看翻牌
    check(point.street == "preflop" and point.to_call == 1.0,
          f"前面只跟注时 hero 应需补 1BB，实际 {point.to_call}")

    # 同一手牌、不需要 hero_turn 出现假帧的第二个决策点：翻牌 hero 过牌，
    # 对手下注，按钮可能全程都在 —— 行动条数变了就必须再给一次
    flop_players = (player("hero", position="BTN"),
                    player("a", position="SB", action="bet", bet=2.0, name="老王"),
                    player("b", position="BB", name="小李"))
    p2 = feed(tr, state(*flop_players, pot=5.0, pbs=3.0, hero_turn=True),
              ["Ah", "Qd"], ["Ks", "7d", "2c"])
    check(p2 is not None and p2.street == "flop" and p2.to_call == 2.0,
          f"转街后的新决策点应再给一次，实际 {p2}")


def test_snapshot_to_call_and_preflop_shape() -> None:
    tr = LiveHandTracker(confirm_frames=1)
    players = (player("hero", position="BTN", stack=100.0, bet=0.0),
               player("a", position="CO", action="raise", bet=3.0, name="老王"),
               player("b", position="BB", bet=1.0, name="小李"))
    feed(tr, state(*players, pot=4.5, pbs=1.0), ["Ah", "Qd"], [])
    point = feed(tr, state(*players, pot=4.5, pbs=1.0, hero_turn=True),
                 ["Ah", "Qd"], [])
    check(point.to_call == 3.0, f"面对 3BB 加注应需跟 3BB，实际 {point.to_call}")
    check(point.preflop_shapes == ("single-raise",),
          f"一次加注应判 single-raise，实际 {point.preflop_shapes}")
    check(not point.hero_is_pfr, "hero 没加注不应是 PFR")


def test_snapshot_marks_limped_pot() -> None:
    """有人溜入、没人加注应判 limped —— 否则会命中「第一个入池必须加注」。"""
    tr = LiveHandTracker(confirm_frames=1)
    players = (player("hero", position="UTG+1", stack=100.0),
               player("a", position="UTG", bet=1.0, action="call", name="老王"),
               player("b", position="BB", bet=1.0, name="小李"))
    feed(tr, state(*players, pot=2.5, pbs=0.0), ["4s", "4h"], [])
    point = feed(tr, state(*players, pot=2.5, pbs=0.0, hero_turn=True),
                 ["4s", "4h"], [])
    check(point is not None and point.preflop_shapes == ("limped",),
          f"一次溜入应判 limped，实际 {getattr(point, 'preflop_shapes', None)}")


def test_table_size_uses_seats_not_players_in_hand() -> None:
    """9 人桌打到只剩 2 人在局，桌型仍是 9-handed。

    修复前 table_size 取的是 players_in_hand（弃牌后会一路变小），
    提示词会写成「Table: 2-handed」，让模型误判桌型。
    """
    tr = LiveHandTracker(confirm_frames=1)
    players = (player("hero", position="BTN"),
               player("a", position="BB", name="老王"))
    feed(tr, state(*players, pot=1.5, pbs=0.0, table_seats=9, in_hand=2),
         ["Jd", "Jh"], [])
    point = feed(tr, state(*players, pot=1.5, pbs=0.0, table_seats=9,
                           in_hand=2, hero_turn=True), ["Jd", "Jh"], [])
    check(point is not None, "轮到我时应给决策点")
    check(point.table_size == 9,
          f"桌型应按座位数 9，实际 {point.table_size}")
    check(tr._hand_dict(tr.hand)["table_size"] == 9,
          "落库的手牌记录也应保存座位数")
    user = render_hand_facts(build_situation(point))
    check("Table: 9-handed" in user, f"提示词桌型不对：\n{user}")


# ─────────────────────────── 技能路由 ───────────────────────────

def context(**kw) -> skills.SkillContext:
    base = dict(street="flop", hero_position="BTN", table_size=6, opponents=1,
                preflop_shapes=("single-raise",), villains_known=2)
    base.update(kw)
    return skills.SkillContext(**base)


def codes(selected) -> list[str]:
    return [s.code for s in selected]


def test_skill_library_loads_all_files() -> None:
    all_ = skills.all_skills()
    check(len(all_) == 15, f"应有 15 篇技能，实际 {len(all_)}")
    check(all(s.body.strip() for s in all_), "每篇都应有正文")
    core = next(s for s in all_ if s.code == "core-stance")
    check("Core Stance" in core.body, "core-stance 正文不对")


def test_skill_text_matches_call_back_copy() -> None:
    """正文是 call-back 那边维护的单一事实源。两边同机时逐字比对，尽早发现漂移。"""
    external = ROOT.parent / "call-back" / "services" / "skills"
    if not external.is_dir():
        return                     # 没装 call-back 的机器上跳过
    local = ROOT / "poker_ocr" / "skills_md"
    for path in sorted(local.glob("*.md")):
        ext = external / path.name
        check(ext.exists(), f"call-back 里没有对应文件：{path.name}")
        if ext.exists():
            check(path.read_text(encoding="utf-8")
                  == ext.read_text(encoding="utf-8"),
                  f"{path.name} 与 call-back 的正文字不一致，请同步")


def test_skill_selection_is_street_scoped() -> None:
    """只加载**当前街**的技能：翻牌不展开河牌/翻前专项。"""
    flop = skills.select_skills(context(street="flop", tier="强成牌",
                                        category=2, plays_board=False))
    got = codes(flop)
    check("core-stance" in got and "odds-table" in got, f"常驻技能必须展开：{got}")
    check("river-value" not in got, f"翻牌不该展开河牌技能：{got}")
    check("preflop-open" not in got, f"翻牌不该展开翻前技能：{got}")
    check("postflop-made-hand" in got, f"两对以上应展开成牌打法：{got}")

    river = skills.select_skills(context(street="river", tier="强成牌",
                                          category=2, plays_board=False))
    river_codes = codes(river)
    check("river-value" in river_codes,
          f"河牌强牌应展开河牌价值下注：{river_codes}")
    check("postflop-made-hand" in river_codes,
          f"成牌技能锚定三条街，河牌也该展开：{river_codes}")


def test_skill_selection_preflop_shapes() -> None:
    open_ = codes(skills.select_skills(context(
        street="preflop", preflop_shapes=("unopened",))))
    check("preflop-open" in open_ and "preflop-vs-open" not in open_,
          f"无人加注应只展开开池：{open_}")
    vs = codes(skills.select_skills(context(
        street="preflop", preflop_shapes=("single-raise",))))
    check("preflop-vs-open" in vs and "preflop-open" not in vs,
          f"面对一次加注应只展开 vs-open：{vs}")
    three = codes(skills.select_skills(context(
        street="preflop", preflop_shapes=("three-bet",))))
    check("preflop-3bet-pot" in three, f"3bet 底池应展开：{three}")

    limped = codes(skills.select_skills(context(
        street="preflop", preflop_shapes=("limped",))))
    check("preflop-limp" in limped and "preflop-open" not in limped,
          f"溜入池应展开抽 set 技能，而不是「第一个入池」：{limped}")


def test_skill_selection_draw_and_multiway_and_cbet() -> None:
    draw = codes(skills.select_skills(context(has_draw=True, tier="强听")))
    check("postflop-draw" in draw, f"有听牌应展开听牌技能：{draw}")
    no_draw = codes(skills.select_skills(context(has_draw=False, tier="强听")))
    check("postflop-draw" not in no_draw, f"没听牌不该展开：{no_draw}")

    multi = codes(skills.select_skills(context(opponents=3, tier="强成牌",
                                               category=2)))
    check("postflop-multiway" in multi, f"多人池应展开铁律：{multi}")

    cbet = codes(skills.select_skills(context(street="flop", cbet_spot=True)))
    check("postflop-cbet" in cbet, f"cbet 点应展开持续下注：{cbet}")
    check("postflop-cbet" not in codes(
        skills.select_skills(context(street="turn", cbet_spot=True))),
        "持续下注只发生在翻牌，转牌不该展开")


def test_plays_board_excludes_made_hand_skills() -> None:
    got = codes(skills.select_skills(context(tier="打牌面", category=2,
                                             plays_board=True)))
    check("postflop-made-hand" not in got, f"打牌面不该按成牌打法给建议：{got}")


def test_catalog_marks_expanded() -> None:
    all_ = skills.all_skills()
    selected = skills.select_skills(context(street="flop"))
    catalog = skills.render_catalog(all_, selected)
    for s in selected:
        check(f"★ {s.code}" in catalog, f"已展开的技能应标 ★：{s.code}")
    check("Coach Skill Catalog" in catalog, "目录应有标题")


# ─────────────────────────── 选项 ───────────────────────────

def make_point(**kw):
    """造一个最小可用的 HandSnapshot。"""
    from poker_ocr.hand_track import (HandSnapshot, OpponentSnapshot,
                                      StreetRecord)
    base = dict(
        hand_id="h1", hand_index=1, street="flop",
        board=("Ks", "7d", "2c"), hero_cards=("Ah", "Kd"),
        hero_position="BTN", hero_stack=97.0, hero_bet=0.0, hero_invested=3.0,
        to_call=0.0, second_bet=None, pot=6.0, pot_before_street=6.0,
        table_size=6, blinds=BLINDS,
        opponents=(OpponentSnapshot("a", "老王", "BB", 97.0, 0.0, False),),
        streets=(StreetRecord("preflop", 0.0, ()),
                 StreetRecord("flop", 6.0, ())),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=True,
        decision_key=("h1", "flop", 0, 6.0, 0.0, ("Ah", "Kd")),
    )
    base.update(kw)
    return HandSnapshot(**base)


def test_options_checked_to_hero() -> None:
    opts = build_options(make_point())
    ids = [o.id for o in opts]
    check(ids == [i for i in OPTION_ORDER if i in ids], f"选项顺序不对：{ids}")
    check("fold" not in ids, f"没人下注不该有弃牌：{ids}")
    check(opts[0].id == "call" and opts[0].kind == "check",
          f"没人下注时 call 这个 id 是过牌：{opts[0]}")
    by = {o.id: o for o in opts}
    check(by["raise-1/2"].amount == 3.0, f"1/2 池应 3.0，实际 {by['raise-1/2'].amount}")
    check(by["raise-1"].amount == 6.0, f"1 倍池应 6.0，实际 {by['raise-1'].amount}")
    check(by["raise-1.2"].amount == 7.2, f"1.2 倍池应 7.2，实际 {by['raise-1.2'].amount}")


def test_options_no_bet_are_labelled_bets() -> None:
    """没人下注时是下注不是加注：标签和 kind 都要说 Bet。"""
    by = {o.id: o for o in build_options(make_point())}
    check(by["raise-1/2"].label == "Bet 1/2 pot",
          f"无人下注应标 Bet：{by['raise-1/2'].label}")
    check(by["raise-1/2"].kind == "bet",
          f"无人下注 kind 应为 bet：{by['raise-1/2'].kind}")
    facing = {o.id: o for o in build_options(
        make_point(to_call=4.0, pot=10.0, hero_stack=90.0))}
    check(facing["raise-1/2"].label == "Raise to 1/2 pot"
          and facing["raise-1/2"].kind == "raise",
          f"面对下注仍应是 Raise：{facing['raise-1/2']}")


def test_value_directive_covers_strong_hands() -> None:
    """强牌默认拿价值；弱牌/翻前不该有指令。"""
    # 口袋 K 在 K 高翻牌 = 暗三（强成牌），无人下注
    set_sit = build_situation(make_point(hero_cards=("Kd", "Kc")))
    d = value_directive(set_sit)
    check(d is not None and "BET for value" in d,
          f"暗三无人下注应默认下注：{d}")
    # 同一手牌面对下注 → 默认加注
    facing = build_situation(make_point(hero_cards=("Kd", "Kc"),
                                        to_call=4.0, pot=10.0))
    d = value_directive(facing)
    check(d is not None and "RAISE for value" in d,
          f"暗三面对下注应默认加注：{d}")
    # 顶对强踢（AhKd 在 K 高翻牌）无人下注 → 下注
    tptk = build_situation(make_point())
    check((value_directive(tptk) or "").startswith("VALUE DEFAULT"),
          f"顶对强踢应默认下注：{value_directive(tptk)}")
    # 顶对弱踢面对下注 → 跟注为主，不该默认弃牌
    tp_weak = build_situation(make_point(hero_cards=("Kh", "9d"),
                                         to_call=4.0, pot=10.0))
    check("SHOWDOWN DEFAULT" in (value_directive(tp_weak) or ""),
          f"顶对弱踢应给跟注指令：{value_directive(tp_weak)}")
    # 强听无人下注 → 半诈唬
    draw = build_situation(make_point(hero_cards=("Ah", "Qh"),
                                      board=("9h", "6h", "5s")))
    check("SEMI-BLUFF" in (value_directive(draw) or ""),
          f"强听应默认下注：{value_directive(draw)}")
    # 空气 → 没有价值指令
    air = build_situation(make_point(hero_cards=("3c", "4c")))
    check(value_directive(air) is None, f"空气不该有价值指令：{value_directive(air)}")


def test_one_pair_does_not_stack_off_against_a_raise() -> None:
    """单对子面对加注/全下默认弃牌；面对普通下注才是跟注。"""
    from poker_ocr.hand_track import ActionRecord, StreetRecord

    raise_street = StreetRecord("flop", 6.0, (
        ActionRecord("flop", "a", "老王", "BB", "raise", 20.0, False, False),))
    tptk = build_situation(make_point(
        to_call=14.0, pot=30.0,
        streets=(StreetRecord("preflop", 0.0, ()), raise_street)))
    d = value_directive(tptk)
    check("ONE PAIR vs AGGRESSION" in (d or "") and "FOLD" in (d or ""),
          f"顶对面对加注应默认弃牌：{d}")

    tp_weak = build_situation(make_point(
        hero_cards=("Kh", "9d"), to_call=14.0, pot=30.0,
        streets=(StreetRecord("preflop", 0.0, ()), raise_street)))
    d = value_directive(tp_weak)
    check("FOLD" in (d or ""), f"顶对弱踢面对加注应弃牌：{d}")

    # 同一手牌面对普通下注（没有 raise 动作）→ 允许跟注
    bet_street = StreetRecord("flop", 6.0, (
        ActionRecord("flop", "a", "老王", "BB", "bet", 14.0, False, False),))
    tptk_bet = build_situation(make_point(
        to_call=14.0, pot=30.0,
        streets=(StreetRecord("preflop", 0.0, ()), bet_street)))
    d = value_directive(tptk_bet)
    check("VALUE/DEFEND" in (d or ""), f"顶对面对普通下注应允许跟注：{d}")


def test_straight_is_not_nuts_on_a_flush_board() -> None:
    """三张同花面：顺子不是坚果，不能被价值指令推着加注。

    990026 河牌 Jc8dKdQcAd：牌面三张方片（8d Kd Ad），对手拿两张方片就是同花。
    这里正确动作是跟注/弃牌，不是 all-in。
    """
    straight = build_situation(make_point(
        street="river", board=("Jc", "8d", "Kd", "Qc", "Ad"),
        hero_cards=("As", "Th"), to_call=144.5, pot=358.0))
    d = value_directive(straight)
    check("VALUE CAUTION" in (d or "") and "NOT the nuts" in (d or ""),
          f"三张同花面的顺子应提示不是坚果：{d}")
    check("Do NOT raise" in (d or ""), f"不该鼓励加注：{d}")

    # 同一张牌面，河牌成葫芦 → 不怕同花，仍然是价值下注
    full_house = build_situation(make_point(
        street="river", board=("2c", "4c", "2d", "5s", "6c"),
        hero_cards=("2h", "5h"), to_call=0.0, pot=84.0))
    d = value_directive(full_house)
    check("VALUE DEFAULT" in (d or "") and "CAUTION" not in (d or ""),
          f"葫芦在花面仍是价值牌：{d}")

    # 牌面对子时，顺子也要提示葫芦可能
    paired = build_situation(make_point(
        street="river", board=("Jc", "Jd", "9d", "Qc", "Ad"),
        hero_cards=("As", "Th"), to_call=0.0, pot=50.0))
    check("VALUE CAUTION" in (value_directive(paired) or ""),
          f"对子面的顺子应提示葫芦可能：{value_directive(paired)}")

    # 对子面的两对面对正常大小下注：可以跟，不要因为「不是坚果」就弃
    two_pair = build_situation(make_point(
        street="turn", board=("Ks", "5s", "2h", "2c"),
        hero_cards=("5d", "Jc"), to_call=44.0, pot=132.5))
    d = value_directive(two_pair)
    check("Call a normal-sized bet" in (d or "") and "Do NOT raise" in (d or ""),
          f"对子面两对应允许跟正常下注：{d}")


def test_prompt_renders_value_directive_and_draw_equity() -> None:
    draw = build_situation(make_point(hero_cards=("Ah", "Qh"),
                                      board=("9h", "6h", "5s")))
    user = render_hand_facts(draw)
    check("Value directive:" in user and "SEMI-BLUFF" in user,
          f"提示词应带价值指令：{user}")
    check("Estimated draw equity" in user, f"听牌应给四二法则胜率：{user}")

    set_sit = build_situation(make_point(hero_cards=("Kd", "Kc")))
    user = render_hand_facts(set_sit)
    check("Estimated draw equity" not in user,
          "成牌不该渲染四二法则胜率（outs 数的是改良张数，不是胜率）")


def test_prompt_renders_implied_odds_for_pocket_pair() -> None:
    """翻前口袋对应给出「有效筹码 / 跟注额」，模型才能判断抽 set 划不划算。"""
    from poker_ocr.hand_track import OpponentSnapshot

    def preflop(cards):
        return build_situation(make_point(
            street="preflop", board=(), to_call=1.0, pot=7.0,
            hero_position="UTG+1", hero_cards=cards, hero_bet=0.0,
            hero_stack=101.5,
            opponents=(OpponentSnapshot("a", "老王", "BB", 56.0, 0.0, False),),
            preflop_shapes=("limped",), cbet_spot=False))

    user = render_hand_facts(preflop(("4s", "4h")))
    check("Implied odds" in user and "56:1" in user,
          f"口袋对翻前应渲染隐含赔率：{user}")
    check("limpers are in, no raise" in user, f"limped 形态文案不对：{user}")

    check("Implied odds" not in render_hand_facts(preflop(("Ah", "Kd"))),
          "非口袋对不该渲染抽 set 隐含赔率")


def test_options_preflop_uses_bb_sizing() -> None:
    """翻前不能给「底池的 1/3」这种尺度：底池只有盲注，而且技能明确要求用 bb。"""
    unopened = make_point(street="preflop", board=(), to_call=0.0, pot=1.5,
                          hero_position="BTN", hero_bet=0.0, hero_stack=100.0,
                          preflop_shapes=("unopened",), cbet_spot=False)
    by = {o.id: o for o in build_options(unopened)}
    check("raise-1/3" not in by, f"翻前不该出现底池倍数选项：{list(by)}")
    check(by["call"].kind == "call" and "limp" in by["call"].label,
          f"无人加注应允许平跟：{by['call']}")
    # 后位基础 3.5-4，无前注、无溜入者
    check(by["open-3.5"].amount == 3.5 and by["open-4"].amount == 4.0,
          f"后位开池尺度不对：{ {k: v.amount for k, v in by.items()} }")

    facing = make_point(street="preflop", board=(), to_call=3.0, pot=4.5,
                        hero_position="BTN", hero_bet=0.0, hero_stack=100.0,
                        preflop_shapes=("single-raise",), cbet_spot=False)
    by = {o.id: o for o in build_options(facing)}
    check(by["call"].amount == 3.0 and "fold" in by, f"面对加注选项不对：{by}")
    check(by["raise-3x"].amount == 9.0 and by["raise-4x"].amount == 12.0,
          f"3bet 应是上家加注的 3/4 倍：{ {k: v.amount for k, v in by.items()} }")

    # 小盲面对大盲只是补齐盲注，不是面对加注
    sb = make_point(street="preflop", board=(), to_call=0.5, pot=1.5,
                    hero_position="SB", hero_bet=0.5, hero_stack=100.0,
                    preflop_shapes=("unopened",), cbet_spot=False)
    by = {o.id: o for o in build_options(sb)}
    check("raise-3x" not in by and "open-3" in by,
          f"小盲补齐不该当成面对加注：{list(by)}")
    check(by["call"].amount == 0.5 and "Complete" in by["call"].label,
          f"小盲补齐的标签应是 Complete：{by['call']}")

    # 非盲位面对溜入是 over-limp，不能写成补盲（complete）
    limp = make_point(street="preflop", board=(), to_call=1.0, pot=7.0,
                      hero_position="UTG+1", hero_cards=("4s", "4h"),
                      hero_bet=0.0, hero_stack=101.5,
                      preflop_shapes=("limped",), cbet_spot=False)
    by = {o.id: o for o in build_options(limp)}
    check(by["call"].amount == 1.0 and "over-limp" in by["call"].label,
          f"非盲位平跟应标 over-limp：{by['call']}")


def test_options_facing_bet_uses_pot_after_call() -> None:
    # 对手下注到 4（老王），hero 已投 0 → 需跟 4；再加注 1/2 池
    # = 4 + 0.5 × (10 + 4) = 11（「加注到 11」）
    opts = build_options(make_point(to_call=4.0, pot=10.0, hero_stack=90.0))
    by = {o.id: o for o in opts}
    check(by["call"].amount == 4.0, f"跟注额应是 4.0，实际 {by['call'].amount}")
    check("fold" in by, "面对下注必须有弃牌")
    check(by["raise-1/2"].amount == 11.0,
          f"1/2 池加注应到 11.0，实际 {by['raise-1/2'].amount}")
    check(by["raise-1/3"].amount == 8.67,
          f"1/3 池 = 4 + 14/3 ≈ 8.67，实际 {by['raise-1/3'].amount}")
    check(not by["raise-1/3"].note, "8.67 高于最小加注 8，不需要抬额")


def test_options_bump_to_min_raise_when_fraction_too_small() -> None:
    # 对手把 10 的池下注到 20：1/3 池加注 = 20 + 30/3 = 30，但最小加注
    # = 20 + (20 - 0) = 40，必须抬到 40 才是合法动作
    opts = build_options(make_point(to_call=20.0, pot=10.0, hero_stack=200.0,
                                    second_bet=0.0))
    by = {o.id: o for o in opts}
    check(by["raise-1/3"].amount == 40.0,
          f"应抬到最小加注 40，实际 {by['raise-1/3'].amount}")
    check("minimum raise" in by["raise-1/3"].note,
          f"抬额应注明：{by['raise-1/3']}")


def test_options_capped_by_stack() -> None:
    opts = build_options(make_point(to_call=0.0, pot=100.0, hero_stack=20.0,
                                    hero_bet=0.0))
    by = {o.id: o for o in opts}
    check(by["raise-1"].amount == 20.0,
          f"后手 20BB 时 1 倍池应封顶到 20，实际 {by['raise-1'].amount}")
    check("all-in" in by["raise-1"].note, "封顶的档位应注明等同全下")


def test_covered_stack_has_only_fold_or_all_in() -> None:
    """下注超过后手时，牌桌上只有 Fold / All in：选项不能再出现 Call 40
    或五个一模一样的「加注到 28.5」。跟注额按后手算，赔率也按后手算。"""
    from poker_ocr.hand_track import ActionRecord, OpponentSnapshot, StreetRecord

    sit = build_situation(make_point(
        street="river", board=("5h", "5s", "2h", "Td", "8c"),
        hero_cards=("Jd", "Jh"), hero_stack=28.5, hero_bet=0.0,
        to_call=40.0, pot=190.0,
        opponents=(OpponentSnapshot("v", "villain", "BB", 229.5, 40.0, False),),
        streets=(StreetRecord("preflop", 0.0, ()), StreetRecord("flop", 0.0, ()),
                 StreetRecord("turn", 0.0, ()),
                 StreetRecord("river", 150.0, (
                     ActionRecord("river", "v", "villain", "BB", "bet", 40.0),)))))
    ids = [o.id for o in sit.options]
    check(ids == ["fold", "call"], f"被 cover 时只应有 fold/call：{ids}")
    call = sit.options[1]
    check(call.amount == 28.5 and "all-in" in call.label.lower(),
          f"跟注应是后手全下 28.5BB：{call}")

    user = render_hand_facts(sit)
    check("To call: 28.5BB" in user, f"应显示实际跟注额 28.5：\n{user}")
    check("Break-even equity to call: 13%" in user,
          f"赔率门槛应按 28.5 算（13%）：\n{user}")
    check("raising is not possible" in user, f"应说明不能再加注：\n{user}")
    check("raise-1/2" not in user, f"不该出现加注选项：\n{user}")

    # 翻前面对超过后手的加注，同样只剩 fold / call all-in
    pre = make_point(street="preflop", board=(), hero_stack=10.0, hero_bet=0.0,
                     to_call=25.0, pot=30.0, preflop_shapes=("three-bet",),
                     cbet_spot=False)
    by = {o.id: o for o in build_options(pre)}
    check(set(by) == {"fold", "call"}, f"翻前被 cover 也只应有 fold/call：{by}")
    check(by["call"].amount == 10.0, f"翻前跟注应封顶到后手 10：{by['call']}")


# ─────────────────────────── 提示词 ───────────────────────────

def test_prompt_marks_inferred_actions() -> None:
    """街末补出来的行动要连 inferred 标记一起给模型，并解释这个标记的含义。"""
    from poker_ocr.hand_track import ActionRecord, StreetRecord
    flop = StreetRecord("flop", 6.0, (
        ActionRecord("flop", "a", "老王", "SB", "bet", 22.0),
        ActionRecord("flop", "hero", None, "CO", "call", 22.0,
                     is_hero=True, inferred=True),
        ActionRecord("flop", "c", "阿强", "BTN", "call", 22.0, inferred=True)))
    user = render_hand_facts(build_situation(make_point(
        to_call=0.0, pot=72.0,
        streets=(StreetRecord("preflop", 0.0, ()), flop))))
    check("Hero calls 22.0BB (inferred)" in user,
          f"补记的跟注应带 inferred 标记：\n{user}")
    check("BTN calls 22.0BB (inferred)" in user,
          f"补记的 BTN 跟注应带 inferred 标记：\n{user}")
    check("SB bets 22.0BB" in user and "SB bets 22.0BB (inferred)" not in user,
          f"直接抓到的下注不该被标成 inferred：\n{user}")
    check('Actions tagged "inferred"' in user,
          f"应解释 inferred 的含义：\n{user}")

    # 没有补记时不要多出这段说明，避免提示词膨胀
    plain = render_hand_facts(build_situation(make_point(
        streets=(StreetRecord("preflop", 0.0, ()),
                 StreetRecord("flop", 6.0, (
                     ActionRecord("flop", "a", "老王", "SB", "bet", 22.0),))))))
    check('Actions tagged "inferred"' not in plain,
          f"没有补记时不该出现 inferred 说明：\n{plain}")


def test_prompt_contains_full_action_line_and_options() -> None:
    from poker_ocr.hand_track import ActionRecord, StreetRecord
    acts = (ActionRecord("preflop", "a", "老王", "CO", "raise", 3.0),
            ActionRecord("preflop", "hero", None, "BTN", "call", 3.0,
                         is_hero=True),
            ActionRecord("flop", "a", "老王", "CO", "bet", 4.0))
    point = make_point(
        to_call=4.0, pot=10.0, hero_stack=90.0, cbet_spot=False,
        streets=(StreetRecord("preflop", 0.0, acts[:2]),
                 StreetRecord("flop", 6.0, acts[2:])))
    sit = build_situation(point)
    system, user = build_messages(sit)

    check("## Coach Skill Catalog" in system, "system 应含技能目录")
    check("## Core Stance & Decision Flow" in system, "system 应含常驻技能正文")
    check("按当前" not in system, "不应出现占位文案")

    check("CO raises to 3.0BB" in user, f"对手加注应写进行动线：\n{user}")
    check("Hero calls 3.0BB" in user, "hero 跟注应写进行动线")
    check("CO bets 4.0BB" in user, "对手翻牌下注应写进行动线")
    check("To call: 4.0BB" in user, f"需跟额不对：\n{user}")
    check("My hand strength: Top pair" in user, f"应有牌力标签：\n{user}")
    for oid in ("fold", "call", "raise-1/3", "raise-1/2", "raise-2/3",
                "raise-1", "raise-1.2"):
        check(oid in user, f"选项 {oid} 应出现在提示词里")


def test_prompt_is_english_only() -> None:
    """JEV 等模型对中文的识别较差：喂给模型的提示词不能有中文文案。

    例外只有 OCR 读到的玩家真实名字和用户手写的 `.table_dy` 内容
    （那是数据，不是提示词文案）。
    """
    import re
    cjk = re.compile(r"[一-鿿]")
    data_names = set("老王小李")          # make_point 里的对手名
    cases = {
        "flop facing bet": make_point(to_call=4.0, pot=10.0, hero_stack=90.0),
        "flop checked": make_point(),
        "preflop": make_point(street="preflop", board=(), to_call=3.0, pot=4.5,
                              cbet_spot=False),
    }
    for name, point in cases.items():
        system, user = build_messages(build_situation(point))
        for part, label in ((system, "system"), (user, "user")):
            leftover = set(cjk.findall(part)) - data_names
            check(not leftover,
                  f"{name} 的 {label} 里还有中文：{''.join(sorted(leftover))}")


def test_prompt_preflop_has_no_strength_facts() -> None:
    point = make_point(street="preflop", board=(), to_call=3.0, pot=4.5,
                       cbet_spot=False, hero_is_pfr=False,
                       preflop_shapes=("single-raise",))
    sit = build_situation(point)
    system, user = build_messages(sit)
    check("Preflop shape: one raise already" in user, f"翻前局面没写：\n{user}")
    check("My hand strength" not in user, "翻前不该给成牌分档")
    check("preflop-vs-open" in system, f"翻前技能应被展开：\n{system[:500]}")


# ─────────────────────────── 牌桌动态 ───────────────────────────

def test_table_dynamics_parse_and_load() -> None:
    """`.table_dy`：注释/空行不进入提示词，缺文件不报错，改文件后热更新。"""
    raw = ("# 当前桌子\n\n新手玩家，比较松弱，诈唬很少。\n"
           "# 下面这条是位置备注\nBTN 老王紧凶，翻后加注基本是强牌。\n")
    want = ("新手玩家，比较松弱，诈唬很少。\n"
            "BTN 老王紧凶，翻后加注基本是强牌。")
    check(parse_table_dynamics(raw) == want,
          f"注释/空行应被去掉，实际 {parse_table_dynamics(raw)!r}")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".table_dy"
        path.write_text(raw, encoding="utf-8")
        dyn = load_table_dynamics(path)
        check(dyn.found and dyn.exists and dyn.text == want,
              f"应读到并解析动态：{dyn}")
        check(dyn.error is None, f"正常文件不该有 error：{dyn.error}")

        # 默认路径不存在是合法的空动态（没有动态就不注入）
        import poker_ocr.table_dynamics as td
        old_root = td.PROJECT_ROOT
        empty_root = Path(tmp) / "empty"
        empty_root.mkdir()
        td.PROJECT_ROOT = empty_root
        try:
            default_missing = td.load_table_dynamics(env={})
            check(not default_missing.found and not default_missing.exists
                  and default_missing.error is None,
                  f"默认路径不存在应是合法的空动态：{default_missing}")
        finally:
            td.PROJECT_ROOT = old_root

        # 显式指定的路径不存在：不抛错，但必须留下可诊断的 error
        missing = load_table_dynamics(Path(tmp) / "not-there")
        check(not missing.found and missing.error is not None,
              f"显式路径读不到应报 error：{missing}")

        # 换桌后直接改文件：内容长度也变，mtime+size 缓存会失效
        path.write_text("紧弱，很少诈唬。\n", encoding="utf-8")
        updated = load_table_dynamics(path)
        check(updated.text == "紧弱，很少诈唬。",
              f"改文件后应读到新内容：{updated}")

    long_text = parse_table_dynamics("x" * (MAX_CHARS + 50))
    check(len(long_text) == MAX_CHARS + 1 and long_text.endswith("…"),
          f"超长动态应截断到 {MAX_CHARS} 字：{len(long_text)}")


def test_table_dynamics_in_prompt() -> None:
    """有动态时进 user facts，并告诉模型怎么按桌风调整；没有时不多出这段。"""
    dyn = "新手玩家，比较松弱，诈唬很少。"
    sit = build_situation(make_point(to_call=4.0, pot=10.0, hero_stack=90.0),
                          table_dynamics=dyn)
    check(sit.table_dynamics == dyn, "Situation 应保存动态原文")
    system, user = build_messages(sit)
    check("## Table dynamics (player-provided, authoritative)" in user,
          f"user facts 应含牌桌动态段：\n{user}")
    check(dyn in user, f"动态原文应进入提示词：\n{user}")
    check("## Table dynamics (user-provided, when present)" in system,
          "system 应解释如何使用桌风动态")
    check("It overrides the default «unknown opponent» assumption" in system,
          "system 应说明动态覆盖「未知对手」默认假设")

    blank = build_situation(make_point(to_call=4.0, pot=10.0, hero_stack=90.0),
                            table_dynamics="  \n ")
    check(blank.table_dynamics is None, "空白动态应归一成 None")
    _, plain_user = build_messages(blank)
    check("## Table dynamics" not in plain_user,
          f"没有动态时不该多出这段：\n{plain_user}")


def test_value_heavy_aggression_flags() -> None:
    """松弱/很少诈唬/尊重持续下注 → 价值线；松凶/爱诈唬 → 不触发。"""
    for text in ("新手玩家，比较松弱，诈唬很少。",
                 "基本都是新手、偏松弱，尊重他们的加注和持续下注",
                 "这些玩家都是松弱玩家，当他们加注、连续下注时，往往都是顶对以上的牌力"):
        check(value_heavy_aggression(text), f"应判为价值线：{text}")
    for text in ("松凶玩家，经常诈唬", "爱诈唬的 maniac", None, ""):
        check(not value_heavy_aggression(text), f"不该判为价值线：{text}")


def test_value_directive_table_dynamics_override() -> None:
    """松弱桌风 + 转/河牌被下注 + 两对 → 硬指令从 call 改成 fold。"""
    dyn = "这些玩家都是松弱玩家，当他们加注、连续下注时，往往都是顶对以上的牌力"
    turn = dict(street="turn", board=("5h", "5s", "2h", "Td"),
                hero_cards=("Jd", "Jh"), to_call=37.5, pot=112.5)
    river = dict(street="river", board=("5h", "5s", "2h", "Td", "8c"),
                 hero_cards=("Jd", "Jh"), to_call=40.0, pot=190.0)

    for street, kw in (("turn", turn), ("river", river)):
        plain = value_directive(build_situation(make_point(**kw)))
        check(plain and "Call a normal-sized bet" in plain,
              f"{street} 无桌风时应保持默认跟注线：{plain}")
        dyn_sit = build_situation(make_point(**kw), table_dynamics=dyn)
        directive = value_directive(dyn_sit)
        check(directive and "TABLE DYNAMICS OVERRIDE" in directive,
              f"{street} 松弱桌风应触发覆盖：{directive}")
        check("FOLD" in directive, f"{street} 覆盖指令应明确 fold：{directive}")

    # 翻牌只有一条街，价格还够，不能被同一条桌风提前劝退
    flop = build_situation(make_point(
        street="flop", board=("5h", "5s", "2h"), hero_cards=("Jd", "Jh"),
        to_call=15.0, pot=60.0), table_dynamics=dyn)
    flop_directive = value_directive(flop)
    check(flop_directive and "TABLE DYNAMICS OVERRIDE" not in flop_directive,
          f"翻牌不该触发多街覆盖：{flop_directive}")


# ─────────────────────────── 模型调用 ───────────────────────────

SETTINGS = AISettings(api_key="k", base_url="https://example.invalid", model="m")


def test_advise_uses_model_choice() -> None:
    sit = build_situation(make_point(to_call=4.0, pot=10.0, hero_stack=90.0))
    seen: dict = {}

    def fake(settings, system, user, timeout=20.0, options=None):
        seen["system"] = system
        return {"option": "raise-2/3", "reason": "顶对在干燥面按 §4 下 2/3 池"}

    advice = advise(sit, SETTINGS, complete=fake)
    check(advice.source == "model", f"应来自模型，实际 {advice.source}")
    check(advice.option.id == "raise-2/3", f"选项不对：{advice.option.id}")
    check(advice.option.amount is not None, "金额应由程序算好")
    check("Coach Skill Catalog" in seen["system"], "调模型时应带上技能层")
    check(advice.to_dict()["street"] == "flop", "落库结构不对")


def test_advise_rejects_unknown_option() -> None:
    sit = build_situation(make_point(to_call=4.0, pot=10.0))
    advice = advise(sit, SETTINGS,
                    complete=lambda *a, **k: {"option": "all-in", "reason": "x"})
    check(advice.source == "fallback", "不在选项里的答案应走兜底")
    check(advice.option.id == "fold", f"面对下注的兜底应是弃牌：{advice.option.id}")
    check(advice.error and "选项" in advice.error, f"应记下原因：{advice.error}")


def test_advise_fallback_without_settings() -> None:
    sit = build_situation(make_point(to_call=0.0))
    advice = advise(sit, None)
    check(advice.source == "fallback" and advice.option.kind == "check",
          f"没配置时应给过牌：{advice.option}")


# ─────────────────────────── JEV 协议 ───────────────────────────

JEV = AISettings(api_key="or-key",
                 base_url="https://openrouter.ai/api/alpha/decisions",
                 model="typesafe/jev-1.14",
                 referer="https://example.com", title="poker-ocr")


def test_is_jev_model() -> None:
    cases = {
        "jev-1.14": True,
        "typesafe/jev-1.14": True,       # OpenRouter 的 vendor/model 写法
        "JEV-2": True,
        "deepseek-flash": False,
        "my-jev-model": False,           # 只认路径分段，不认子串
        "": False,
    }
    for model, want in cases.items():
        check(is_jev_model(model) == want,
              f"{model!r} 应 {'是' if want else '不是'} JEV，实际 {is_jev_model(model)}")


def test_jev_payload_and_headers() -> None:
    state = ["系统提示", {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,AAA"}}]
    questions = {"matches_listing": {"type": "noul",
                                     "instructions": "?"}}
    payload = jev_payload(JEV, state, questions)
    check(payload["model"] == "typesafe/jev-1.14", f"model 不对：{payload}")
    check(payload["state"] == state, f"state 应原样传递：{payload}")
    check(payload["questions"] == questions, f"questions 应原样传递：{payload}")

    headers = jev_headers(JEV)
    check(headers["Authorization"] == "Bearer or-key", f"鉴权头不对：{headers}")
    check(headers["Content-Type"] == "application/json", f"内容头不对：{headers}")
    check(headers["HTTP-Referer"] == "https://example.com", f"Referer 不对：{headers}")
    check(headers["X-OpenRouter-Title"] == "poker-ocr", f"Title 不对：{headers}")

    # 没配可选头时不应发空字段
    bare = jev_headers(AISettings("k", "https://x/decisions", "jev-1"))
    check("HTTP-Referer" not in bare and "X-OpenRouter-Title" not in bare,
          f"可选头不该为空值发送：{bare}")


def test_jev_decisions_posts_sample_schema() -> None:
    seen: dict = {}

    def fake_post(url, payload, headers, timeout):
        seen.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return json.dumps({"answers": {"matches_listing": {"noul": 0.91}}}).encode()

    answers = jev_decisions(
        JEV, ["Listing: vintage red bicycle"],
        {"matches_listing": {"type": "noul", "instructions": "?",
                             "criteria": {"true": "shown", "false": "not shown"}}},
        request=fake_post)
    check(answers["matches_listing"]["noul"] == 0.91, f"answers 解析不对：{answers}")
    check(seen["url"] == JEV.base_url, f"JEV 应直接 POST 配置的 URL：{seen}")
    check(seen["payload"]["model"] == JEV.model, f"model 没带上：{seen}")
    check(seen["payload"]["questions"]["matches_listing"]["type"] == "noul",
          f"questions 没带上：{seen}")
    check(seen["headers"]["Authorization"] == "Bearer or-key", f"鉴权头没带上：{seen}")


def test_complete_jev_builds_choice_question_and_reason() -> None:
    options = [
        {"id": "fold", "label": "Fold", "kind": "fold"},
        {"id": "raise-1/2", "label": "Raise to 1/2 pot", "kind": "raise",
         "amount_bb": 10.25},
    ]
    seen: dict = {}

    def fake_post(url, payload, headers, timeout):
        seen.update(payload)
        return json.dumps({"answers": {"option": {
            "choice": "raise-1/2",
            "probabilities": {"raise-1/2": 0.72, "fold": 0.28},
        }}}).encode()

    got = complete_jev(JEV, "系统", "用户", options=options, request=fake_post)
    check(got["option"] == "raise-1/2", f"choice 解析不对：{got}")
    check(got["confidence"] == 0.72, f"概率没转成 confidence：{got}")
    check("72%" in got["reason"], f"reason 应带概率：{got}")
    check(got["reason"].startswith("JEV chose"), f"reason 结构不对：{got}")

    question = seen["questions"]["option"]
    check(question["type"] == "choice", f"应是 choice 题：{question}")
    check(set(question["criteria"]) == {"fold", "raise-1/2"},
          f"criteria 的 key 必须是选项 id：{question}")
    check("10.25BB" in question["criteria"]["raise-1/2"],
          f"criteria 应带金额：{question}")
    check(seen["state"] == ["系统", "用户"], f"system/user 应进 state：{seen}")


def test_complete_jev_rejects_unknown_choice() -> None:
    def fake_post(url, payload, headers, timeout):
        return json.dumps({"answers": {"option": {"choice": "all-in"}}}).encode()

    try:
        complete_jev(JEV, "s", "u", options=[{"id": "fold", "label": "弃牌"}],
                     request=fake_post)
    except AIError as exc:
        check("选项" in str(exc), f"报错应说明不在选项里：{exc}")
    else:
        FAILS.append("JEV 返回不在选项里的 choice 应抛 AIError")


def test_complete_json_dispatches_by_model_name() -> None:
    options = [{"id": "fold", "label": "弃牌", "kind": "fold"}]
    from unittest.mock import MagicMock
    jev = MagicMock(return_value={"option": "fold", "reason": "jev"})
    with patch.object(advice_mod, "complete_jev", jev):
        got = complete_json(JEV, "s", "u", options=options)
    check(got["option"] == "fold", f"JEV 模型应走 complete_jev：{got}")
    check(jev.called, "complete_jev 没被调用")

    with patch.object(advice_mod, "complete_jev") as jev:
        try:
            # 非 JEV 模型走 chat/completions；没网会报错，但绝不该进 complete_jev
            complete_json(AISettings("k", "http://127.0.0.1:1", "deepseek-chat"),
                          "s", "u", options=options, request=lambda *a: b"{}")
        except Exception:                # noqa: BLE001 — 这里只关心分发路径
            pass
    check(not jev.called, "非 JEV 模型不该走 JEV 分支")


def test_post_json_reuses_connection() -> None:
    """连续两次建议复用同一条连接 —— 握手实测能占到一次建议的一半时间。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        connections = 0
        payloads: list[dict] = []

        def setup(self) -> None:
            type(self).connections += 1
            super().setup()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", 0))
            type(self).payloads.append(json.loads(self.rfile.read(n)))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    headers = {"Content-Type": "application/json"}
    try:
        # 测试环境可能配了代理；这里强制走 keep-alive 分支
        with patch.object(advice_mod, "_proxy_configured", lambda: False):
            first = advice_mod._post_json(url, {"i": 1}, headers, 5.0)
            second = advice_mod._post_json(url, {"i": 2}, headers, 5.0)
        check(json.loads(first) == {"ok": True}, f"响应体不对：{first!r}")
        check(json.loads(second) == {"ok": True}, f"响应体不对：{second!r}")
        check(Handler.connections == 1,
              f"两次请求应复用一条连接，实际新建 {Handler.connections} 条")
        check(Handler.payloads == [{"i": 1}, {"i": 2}],
              f"请求体应按顺序送达：{Handler.payloads}")
    finally:
        srv.shutdown()
        srv.server_close()
        with advice_mod._conn_lock:
            for conn in advice_mod._conns.values():
                conn.close()
            advice_mod._conns.clear()


def test_ai_settings_reads_referer_and_title() -> None:
    env = {"AI_SERVER_API_KEY": "k", "AI_SERVER_BASE_URL": "https://x/decisions",
           "AI_SERVER_MODEL": "typesafe/jev-1.14",
           "AI_SERVER_REFERER": "https://site", "AI_SERVER_TITLE": "t"}
    settings = AISettings.from_env(env)
    check(settings.referer == "https://site" and settings.title == "t",
          f"可选头没读进来：{settings}")


def test_ai_settings_reads_sampling_env() -> None:
    """采样参数从 .env 读进来；没配的项保持 None（= 不发给服务端）。"""
    env = {"AI_SERVER_API_KEY": "k", "AI_SERVER_BASE_URL": "https://x",
           "AI_SERVER_MODEL": "deepseek-flash",
           "AI_SERVER_THINKING": "disabled",
           "AI_SERVER_TEMPERATURE": "0.2",
           "AI_SERVER_MAX_TOKENS": "200"}
    settings = AISettings.from_env(env)
    check(settings.thinking == "disabled", f"thinking 没读进来：{settings}")
    check(settings.temperature == 0.2, f"temperature 没读进来：{settings}")
    check(settings.max_tokens == 200, f"max_tokens 没读进来：{settings}")
    check(settings.top_p is None and settings.reasoning_effort is None,
          f"没配的采样参数应为 None：{settings}")

    # 不配 temperature 时保留原先的 0.2 默认，其余仍为 None
    bare = AISettings.from_env({"AI_SERVER_API_KEY": "k",
                                "AI_SERVER_BASE_URL": "https://x",
                                "AI_SERVER_MODEL": "m"})
    check(bare.temperature == 0.2 and bare.thinking is None,
          f"默认采样参数不对：{bare}")


def test_ai_settings_rejects_bad_sampling_env() -> None:
    base = {"AI_SERVER_API_KEY": "k", "AI_SERVER_BASE_URL": "https://x",
            "AI_SERVER_MODEL": "m"}
    for key, value in (("AI_SERVER_THINKING", "on"),
                       ("AI_SERVER_REASONING_EFFORT", "medium"),
                       ("AI_SERVER_TEMPERATURE", "3"),
                       ("AI_SERVER_MAX_TOKENS", "abc")):
        try:
            AISettings.from_env({**base, key: value})
        except ValueError as exc:
            check(key in str(exc), f"报错应点名 {key}：{exc}")
        else:
            FAILS.append(f"{key}={value} 应该抛 ValueError")


def test_complete_json_sends_sampling_params() -> None:
    """配置过的采样参数要真的进请求体；没配的不能发空值。"""
    seen: dict = {}

    def fake(url, payload, headers, timeout):
        seen.update(payload)
        return json.dumps({"choices": [{"message": {
            "content": '{"option": "call", "reason": "x"}'}}]}).encode()

    settings = AISettings("k", "https://example.invalid", "deepseek-flash",
                          temperature=0.2, max_tokens=200,
                          thinking="disabled")
    got = complete_json(settings, "s", "u", request=fake)
    check(got["option"] == "call", f"响应解析不对：{got}")
    check(seen["temperature"] == 0.2, f"temperature 没进请求体：{seen}")
    check(seen["max_tokens"] == 200, f"max_tokens 没进请求体：{seen}")
    check(seen["thinking"] == {"type": "disabled"},
          f"thinking 没进请求体：{seen}")
    check("top_p" not in seen and "reasoning_effort" not in seen,
          f"没配的参数不该发送：{seen}")

    seen.clear()
    thinking = AISettings("k", "https://example.invalid", "deepseek-flash",
                          temperature=None, top_p=0.95, thinking="enabled",
                          reasoning_effort="low")
    complete_json(thinking, "s", "u", request=fake)
    check("temperature" not in seen, f"思考模式不该发 temperature：{seen}")
    check(seen["top_p"] == 0.95 and seen["reasoning_effort"] == "low",
          f"思考参数没进请求体：{seen}")


def test_parse_json_content_tolerates_fence() -> None:
    got = parse_json_content('```json\n{"option": "call", "reason": "x"}\n```')
    check(got["option"] == "call", f"应剥掉 markdown 围栏：{got}")
    try:
        parse_json_content("这不是 JSON")
    except AIError:
        pass
    else:
        FAILS.append("非 JSON 应抛 AIError")


def test_advice_runner_does_not_block() -> None:
    """submit 必须立即返回，结果从 poll 里取。"""
    import time

    sit = build_situation(make_point(to_call=0.0))

    def slow(settings, system, user, timeout=20.0, options=None):
        time.sleep(0.2)
        return {"option": "raise-1/2", "reason": "x"}

    runner = AdviceRunner(SETTINGS, complete=slow)
    t0 = time.monotonic()
    runner.submit(sit)
    elapsed = time.monotonic() - t0
    check(elapsed < 0.1, f"submit 不应阻塞，耗时 {elapsed:.3f}s")

    got = []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not got:
        got = runner.poll()
        time.sleep(0.02)
    runner.close()
    check(len(got) == 1 and got[0].option.id == "raise-1/2",
          f"后台结果应能取回：{got}")


def test_advice_runner_reports_busy_while_waiting() -> None:
    """生成建议期间 busy=True，主循环据此暂停识别帧。"""
    import threading
    import time

    sit = build_situation(make_point(to_call=0.0))
    started = threading.Event()
    release = threading.Event()

    def blocking(settings, system, user, timeout=20.0, options=None):
        started.set()
        release.wait(2.0)
        return {"option": "call", "reason": "x"}

    runner = AdviceRunner(SETTINGS, complete=blocking)
    check(not runner.busy, "初始不应 busy")
    check(runner.busy_seconds == 0.0, "空闲时 busy_seconds 应为 0")
    runner.submit(sit)
    check(started.wait(2.0), "模型调用应开始")
    check(runner.busy, "等待模型返回期间应 busy")
    check(runner.busy_seconds > 0.0, "等待期间应能报出已等待秒数")
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and runner.busy:
        time.sleep(0.01)
    check(not runner.busy, "结果返回后应清除 busy")
    check(runner.busy_seconds == 0.0, "结果返回后 busy_seconds 应归零")
    check(len(runner.poll()) == 1, "结果应可取回")
    runner.close()


# ─────────────────────────── 主流程 ───────────────────────────

def main() -> int:
    tests = [
        test_street_of_board,
        test_tracker_collects_actions_and_dedupes_frames,
        test_tracker_infers_actions_when_badges_are_missed,
        test_tracker_ignores_stale_badge_after_others_acted,
        test_tracker_backfills_missed_calls_at_street_end,
        test_tracker_backfills_missed_raise_at_street_end,
        test_tracker_backfills_raise_after_recorded_call,
        test_tracker_does_not_guess_when_pot_does_not_match,
        test_tracker_new_hand_on_dealer_change,
        test_tracker_board_drop_needs_two_frames,
        test_tracker_decision_point_confirmed_and_deduped,
        test_snapshot_to_call_and_preflop_shape,
        test_snapshot_marks_limped_pot,
        test_table_size_uses_seats_not_players_in_hand,
        test_skill_library_loads_all_files,
        test_skill_text_matches_call_back_copy,
        test_skill_selection_is_street_scoped,
        test_skill_selection_preflop_shapes,
        test_skill_selection_draw_and_multiway_and_cbet,
        test_plays_board_excludes_made_hand_skills,
        test_catalog_marks_expanded,
        test_options_checked_to_hero,
        test_options_no_bet_are_labelled_bets,
        test_value_directive_covers_strong_hands,
        test_one_pair_does_not_stack_off_against_a_raise,
        test_straight_is_not_nuts_on_a_flush_board,
        test_prompt_renders_value_directive_and_draw_equity,
        test_prompt_renders_implied_odds_for_pocket_pair,
        test_options_preflop_uses_bb_sizing,
        test_options_facing_bet_uses_pot_after_call,
        test_options_bump_to_min_raise_when_fraction_too_small,
        test_options_capped_by_stack,
        test_covered_stack_has_only_fold_or_all_in,
        test_prompt_marks_inferred_actions,
        test_prompt_contains_full_action_line_and_options,
        test_prompt_preflop_has_no_strength_facts,
        test_table_dynamics_parse_and_load,
        test_table_dynamics_in_prompt,
        test_value_heavy_aggression_flags,
        test_value_directive_table_dynamics_override,
        test_advise_uses_model_choice,
        test_advise_rejects_unknown_option,
        test_advise_fallback_without_settings,
        test_is_jev_model,
        test_jev_payload_and_headers,
        test_jev_decisions_posts_sample_schema,
        test_complete_jev_builds_choice_question_and_reason,
        test_complete_jev_rejects_unknown_choice,
        test_complete_json_dispatches_by_model_name,
        test_post_json_reuses_connection,
        test_ai_settings_reads_referer_and_title,
        test_ai_settings_reads_sampling_env,
        test_ai_settings_rejects_bad_sampling_env,
        test_complete_json_sends_sampling_params,
        test_parse_json_content_tolerates_fence,
        test_advice_runner_does_not_block,
        test_advice_runner_reports_busy_while_waiting,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 实时建议链路全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
