#!/usr/bin/env python3
"""实时建议链路的测试：行动线累积、决策点触发、按街加载技能、选项与模型调用。

    .venv/bin/python tests/test_advice.py

全部是纯逻辑：手工拼 FrameState，不连 RTSP、不加载 OCR、不真的调模型
（模型调用用注入的 `complete` 桩）。期望值对着 call-back/services/skills/ 的规则。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import skills  # noqa: E402
from poker_ocr.advice import (  # noqa: E402
    AISettings, AIError, AdviceRunner, OPTION_ORDER, advise,
    build_messages, build_options, build_situation, parse_json_content,
)
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker, street_of_board  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402

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
          hero_seat: str = "hero", errors: list[str] | None = None) -> FrameState:
    return FrameState(
        frame_id="f", table_seats=len(players), players_in_hand=len(players),
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
    check(len(all_) == 14, f"应有 14 篇技能，实际 {len(all_)}")
    check(all(s.body.strip() for s in all_), "每篇都应有正文")
    core = next(s for s in all_ if s.code == "core-stance")
    check("底层立场" in core.body, "core-stance 正文不对")


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
    check("教练技能目录" in catalog, "目录应有标题")


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
    check(by["call"].amount == 0.5, f"补齐额应是 0.5BB：{by['call']}")


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
    check("最小加注额" in by["raise-1/3"].note,
          f"抬额应注明：{by['raise-1/3']}")


def test_options_capped_by_stack() -> None:
    opts = build_options(make_point(to_call=0.0, pot=100.0, hero_stack=20.0,
                                    hero_bet=0.0))
    by = {o.id: o for o in opts}
    check(by["raise-1"].amount == 20.0,
          f"后手 20BB 时 1 倍池应封顶到 20，实际 {by['raise-1'].amount}")
    check("全下" in by["raise-1"].note, "封顶的档位应注明等同全下")


# ─────────────────────────── 提示词 ───────────────────────────

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

    check("## 教练技能目录" in system, "system 应含技能目录")
    check("## 底层立场与决策流程" in system, "system 应含常驻技能正文")
    check("按当前" not in system, "不应出现占位文案")

    check("CO 加注到 3.0BB" in user, f"对手加注应写进行动线：\n{user}")
    check("Hero 跟注 3.0BB" in user, "hero 跟注应写进行动线")
    check("CO 下注 4.0BB" in user, "对手翻牌下注应写进行动线")
    check("本街需再跟: 4.0BB" in user, f"需跟额不对：\n{user}")
    check("我的牌力: 顶对" in user or "顶对" in user, f"应有牌力标签：\n{user}")
    for oid in ("fold", "call", "raise-1/3", "raise-1/2", "raise-2/3",
                "raise-1", "raise-1.2"):
        check(oid in user, f"选项 {oid} 应出现在提示词里")


def test_prompt_preflop_has_no_strength_facts() -> None:
    point = make_point(street="preflop", board=(), to_call=3.0, pot=4.5,
                       cbet_spot=False, hero_is_pfr=False,
                       preflop_shapes=("single-raise",))
    sit = build_situation(point)
    system, user = build_messages(sit)
    check("翻前局面: 前面已有一次加注" in user, f"翻前局面没写：\n{user}")
    check("我的牌力" not in user, "翻前不该给成牌分档")
    check("preflop-vs-open" in system, f"翻前技能应被展开：\n{system[:500]}")


# ─────────────────────────── 模型调用 ───────────────────────────

SETTINGS = AISettings(api_key="k", base_url="https://example.invalid", model="m")


def test_advise_uses_model_choice() -> None:
    sit = build_situation(make_point(to_call=4.0, pot=10.0, hero_stack=90.0))
    seen: dict = {}

    def fake(settings, system, user, timeout=20.0):
        seen["system"] = system
        return {"option": "raise-2/3", "reason": "顶对在干燥面按 §4 下 2/3 池"}

    advice = advise(sit, SETTINGS, complete=fake)
    check(advice.source == "model", f"应来自模型，实际 {advice.source}")
    check(advice.option.id == "raise-2/3", f"选项不对：{advice.option.id}")
    check(advice.option.amount is not None, "金额应由程序算好")
    check("教练技能目录" in seen["system"], "调模型时应带上技能层")
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

    def slow(settings, system, user, timeout=20.0):
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


# ─────────────────────────── 主流程 ───────────────────────────

def main() -> int:
    tests = [
        test_street_of_board,
        test_tracker_collects_actions_and_dedupes_frames,
        test_tracker_infers_actions_when_badges_are_missed,
        test_tracker_ignores_stale_badge_after_others_acted,
        test_tracker_new_hand_on_dealer_change,
        test_tracker_board_drop_needs_two_frames,
        test_tracker_decision_point_confirmed_and_deduped,
        test_snapshot_to_call_and_preflop_shape,
        test_skill_library_loads_all_files,
        test_skill_text_matches_call_back_copy,
        test_skill_selection_is_street_scoped,
        test_skill_selection_preflop_shapes,
        test_skill_selection_draw_and_multiway_and_cbet,
        test_plays_board_excludes_made_hand_skills,
        test_catalog_marks_expanded,
        test_options_checked_to_hero,
        test_options_preflop_uses_bb_sizing,
        test_options_facing_bet_uses_pot_after_call,
        test_options_bump_to_min_raise_when_fraction_too_small,
        test_options_capped_by_stack,
        test_prompt_contains_full_action_line_and_options,
        test_prompt_preflop_has_no_strength_facts,
        test_advise_uses_model_choice,
        test_advise_rejects_unknown_option,
        test_advise_fallback_without_settings,
        test_parse_json_content_tolerates_fence,
        test_advice_runner_does_not_block,
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
