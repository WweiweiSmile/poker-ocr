#!/usr/bin/env python3
"""对手类型识别：观察解析、统计聚合、规则分类、pin/先验与建议集成。

    .venv/bin/python tests/test_opponent_profile.py

全部是纯逻辑：手工拼 hand dict / FrameState，不连 RTSP，不调模型。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import advice as advice_mod  # noqa: E402
from poker_ocr.advice import (  # noqa: E402
    _aggressive_context, advise, build_situation, decision_read,
    render_hand_facts, value_directive,
)
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, LiveHandTracker, OpponentSnapshot, StreetRecord,
)
from poker_ocr.opponent_profile import (  # noqa: E402
    CLASSIFIER_VERSION, HandObservation, OpponentRead, OpponentStats, ProfileStore,
    classify, normalize_name, observations_from_hand, parse_table_dynamics_priors,
    pool_key_from_blinds,
)
from poker_ocr.parse import Blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2（1）")


# ─────────────────────────── 工具 ───────────────────────────

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
          hero_seat: str = "hero", table_seats: int | None = None
          ) -> FrameState:
    return FrameState(
        frame_id="f", table_seats=len(players) if table_seats is None else table_seats,
        players_in_hand=len(players), players=list(players), blinds=BLINDS,
        dealer=dealer, hero_seat_id=hero_seat, hero_turn=hero_turn, pot=pot,
        pot_before_street=pbs, errors=[])


def make_hand(*, uid: str = "s01-h1", session: int = 1,
              hero: str = "hero", board: tuple[str, ...] = (),
              preflop: list[dict] | None = None,
              flop: list[dict] | None = None,
              turn: list[dict] | None = None,
              river: list[dict] | None = None,
              seats: list[dict] | None = None,
              street_active: dict[str, list[str]] | None = None,
              shows: list[dict] | None = None,
              bomb_pot: bool = False,
              blinds: dict | None = None,
              ts: str = "2026-10-23T21:00:00+08:00") -> dict:
    streets = []
    for name, actions, pbs in (("preflop", preflop, 4.0),
                               ("flop", flop, 20.0),
                               ("turn", turn, 50.0),
                               ("river", river, 100.0)):
        if actions is not None:
            streets.append({"street": name, "pot_start": pbs,
                            "actions": actions})
    dealt = [hero, "a", "b", "c"]
    folded_pre = {x["seat_id"] for x in (preflop or [])
                  if x.get("action") == "fold"}
    remaining = [seat for seat in dealt if seat not in folded_pre]
    if street_active is None:
        street_active = {"preflop": dealt}
        if flop is not None:
            street_active["flop"] = remaining
        if turn is not None:
            street_active["turn"] = remaining
        if river is not None:
            street_active["river"] = remaining
    return {
        "type": "hand", "ts": ts, "hand_id": uid.split("-")[-1],
        "uid": uid, "session": session, "hero_seat_id": hero,
        "hero_cards": ["Jh", "Qd"], "hero_position": "BB", "table_size": 9,
        "blinds": blinds or {"sb": 0.5, "bb": 1.0, "ante": 0.5},
        "board": list(board), "streets": streets,
        "street_active": street_active,
        **({"bomb_pot": True} if bomb_pot else {}),
        "players_dealt": 4,
        "seat_meta": seats or [
            {"seat_id": hero, "name": "我", "position": "BB", "stack": 100.0},
            {"seat_id": "a", "name": "芒果与西米", "position": "BTN", "stack": 100.0},
            {"seat_id": "b", "name": "好运常在ok", "position": "SB", "stack": 100.0},
            {"seat_id": "c", "name": "短码", "position": "CO", "stack": 100.0},
        ],
        **({"shows": shows} if shows else {}),
    }


def act(seat: str, action: str, amount: float | None = None, *,
        name: str | None = None, position: str | None = None,
        is_hero: bool = False, inferred: bool = False, all_in: bool = False
        ) -> dict:
    return {"seat_id": seat, "name": name, "position": position,
            "action": action, "amount": amount, "all_in": all_in,
            "is_hero": is_hero, **({"inferred": True} if inferred else {})}


def feed(tracker: LiveHandTracker, st: FrameState, hero_cards, board):
    return tracker.observe(st, hero_cards, board)


# ─────────────────────────── P0：弃牌/过牌补全 ───────────────────────────

def test_fold_and_check_are_inferred_at_street_end() -> None:
    """弃牌徽章漏读：街初在局、街末不在局 → 补 inferred fold。
    无人下注、街末仍在局且没有动作 → 补 inferred check。"""
    tr = LiveHandTracker(confirm_frames=1)
    pre = state(
        player("hero", name="我", position="BTN", stack=100.0),
        player("a", name="A", position="SB", bet=0.5, stack=99.5),
        player("b", name="B", position="BB", bet=1.0, stack=99.0),
        player("c", name="C", position="CO", stack=100.0),
        pot=1.5, pbs=0.0)
    feed(tr, pre, ["Ah", "Kd"], [])

    # 翻牌：B 已经弃牌（徽章一帧都没抓到），A/C/hero 继续
    flop = state(
        player("hero", name="我", position="BTN", stack=100.0),
        player("a", name="A", position="SB", stack=99.5),
        player("b", name="B", position="BB", stack=99.0,
               fold=True, status="folded"),
        player("c", name="C", position="CO", stack=100.0),
        pot=1.5, pbs=1.5, dealer="a")
    feed(tr, flop, ["Ah", "Kd"], ["Ks", "7d", "2c"])

    pre_acts = [(x.seat_id, x.action, x.inferred)
                for x in tr.hand.street_actions("preflop")]
    check(("b", "fold", True) in pre_acts,
          f"翻前漏读的弃牌应补成 inferred fold：{pre_acts}")
    check(tr.hand.street_start_active["flop"] == {"hero", "a", "c"},
          f"翻牌在局集合应排除已弃牌的 B："
          f"{tr.hand.street_start_active.get('flop')}")

    # 转牌：C 也弃牌；翻牌没人下注，hero/A 都该补过牌
    turn = state(
        player("hero", name="我", position="BTN", stack=100.0),
        player("a", name="A", position="SB", stack=99.5),
        player("b", name="B", position="BB", stack=99.0,
               fold=True, status="folded"),
        player("c", name="C", position="CO", stack=100.0,
               fold=True, status="folded"),
        pot=1.5, pbs=1.5, dealer="a")
    feed(tr, turn, ["Ah", "Kd"], ["Ks", "7d", "2c", "Th"])

    flop_acts = [(x.seat_id, x.action, x.inferred)
                 for x in tr.hand.street_actions("flop")]
    check(("c", "fold", True) in flop_acts,
          f"翻牌漏读的弃牌应补出来：{flop_acts}")
    check(("hero", "check", True) in flop_acts
          and ("a", "check", True) in flop_acts,
          f"无人下注时仍在局的人应补 check：{flop_acts}")

    # 手末：转牌也没动作，hero/A 补过牌；不会给已弃牌的人补
    tr.finish_current()
    hand = tr.take_finished()
    check(hand is not None and hand.get("street_active"),
          f"落库应带每街在局集合：{hand}")
    check(hand.get("players_dealt") == 4,
          f"players_dealt 应为 4，实际 {hand.get('players_dealt')}")
    turn_acts = next(s for s in hand["streets"] if s["street"] == "turn")["actions"]
    check([(x["seat_id"], x["action"]) for x in turn_acts]
          == [("a", "check"), ("hero", "check")],
          f"转牌手末应补 A/hero 过牌：{turn_acts}")
    check(all(x.get("inferred") for x in turn_acts),
          f"补出来的动作应标 inferred：{turn_acts}")


def test_no_fold_inferred_when_action_badge_recorded() -> None:
    """已经抓到 fold 徽章时，街末不能重复补一条。"""
    tr = LiveHandTracker(confirm_frames=1)
    pre = state(player("hero", position="BTN"), player("a", position="BB"),
                pot=1.5, pbs=0.0)
    feed(tr, pre, ["Ah", "Kd"], [])
    flop_st = state(
        player("hero", position="BTN"),
        player("a", position="BB", fold=True, status="folded"),
        pot=1.5, pbs=1.5)
    # 先让 fold 徽章被记录
    flop_st.players[1].action = "fold"
    feed(tr, flop_st, ["Ah", "Kd"], ["Ks", "7d", "2c"])
    folds = [x for x in tr.hand.street_actions("flop") if x.action == "fold"]
    check(len(folds) == 1 and folds[0].inferred is False,
          f"抓到的弃牌只应有一条、且不是 inferred：{folds}")
    tr.finish_current()
    hand = tr.take_finished()
    flop = next(s for s in hand["streets"] if s["street"] == "flop")
    folds = [x for x in flop["actions"] if x["action"] == "fold"]
    check(len(folds) == 1, f"手末不应重复补弃牌：{folds}")


# ─────────────────────────── 观察解析 ───────────────────────────

def test_bomb_pot_preflop_has_no_inferred_checks() -> None:
    """爆炸底池的「翻前」只是强制前注，不能补出 check/fold。"""
    tr = LiveHandTracker(confirm_frames=1)
    pre = state(
        player("hero", name="我", position="BTN", stack=95.0),
        player("a", name="A", position="SB", stack=95.0),
        player("b", name="B", position="BB", stack=95.0),
        pot=45.0, pbs=45.0, dealer="a", table_seats=9)
    pre.bomb_pot = True
    feed(tr, pre, ["Ah", "Kd"], [])
    flop = state(
        player("hero", name="我", position="BTN", stack=95.0),
        player("a", name="A", position="SB", stack=95.0),
        player("b", name="B", position="BB", stack=95.0),
        pot=45.0, pbs=45.0, dealer="a", table_seats=9)
    flop.bomb_pot = True
    feed(tr, flop, ["Ah", "Kd"], ["5d", "4h", "9s"])
    tr.finish_current()
    hand = tr.take_finished()
    check(hand.get("bomb_pot") is True, f"手牌应带 bomb_pot：{hand}")
    pre_actions = next(s for s in hand["streets"]
                       if s["street"] == "preflop")["actions"]
    check(pre_actions == [],
          f"爆炸底池翻前不应有补出来的 check/fold：{pre_actions}")

    # 横幅没抓到时，ante=5 也要能识别：不能凭空补翻前 check
    tr2 = LiveHandTracker(confirm_frames=1)
    big_ante = Blinds(sb=0.5, bb=1.0, ante=5.0, raw="1/2(5)")
    pre2 = state(player("hero", name="我", position="BTN"),
                 player("a", name="A", position="SB"),
                 player("b", name="B", position="BB"),
                 pot=45.0, pbs=45.0, dealer="a")
    pre2.blinds = big_ante
    feed(tr2, pre2, ["Ah", "Kd"], [])
    flop2 = state(player("hero", name="我", position="BTN"),
                  player("a", name="A", position="SB"),
                  player("b", name="B", position="BB"),
                  pot=45.0, pbs=45.0, dealer="a")
    flop2.blinds = big_ante
    feed(tr2, flop2, ["Ah", "Kd"], ["5d", "4h", "9s"])
    tr2.finish_current()
    hand2 = tr2.take_finished()
    pre2_actions = next(s for s in hand2["streets"]
                        if s["street"] == "preflop")["actions"]
    check(pre2_actions == [],
          f"ante≥5 且无翻前行动也不该补 check：{pre2_actions}")


def test_bomb_pot_passive_entry_excluded_from_vpip() -> None:
    """强制 5BB 前注是「被动入池」：不计 VPIP 分母，单独计数。"""
    seats = [
        {"seat_id": "hero", "name": "我", "position": "BB"},
        {"seat_id": "a", "name": "A", "position": "BTN"},
        {"seat_id": "b", "name": "B", "position": "SB"},
        {"seat_id": "c", "name": "C", "position": "CO"},
    ]
    bomb = make_hand(
        uid="s01-h10", bomb_pot=True,
        blinds={"sb": 0.5, "bb": 1.0, "ante": 5.0},
        board=("5d", "4h", "9s", "Jc", "10d"),
        seats=seats,
        preflop=[
            act("a", "check", None, name="A", position="BTN", inferred=True),
            act("b", "check", None, name="B", position="SB", inferred=True),
            act("hero", "check", None, is_hero=True, inferred=True),
        ],
        flop=[
            act("a", "bet", 32.0, name="A", position="BTN"),
            act("b", "call", 32.0, name="B", position="SB"),
            act("hero", "call", 32.0, is_hero=True),
        ],
        street_active={"preflop": ["hero", "a", "b"],
                       "flop": ["hero", "a", "b"]},
    )
    obs = {o.display_name: o for o in observations_from_hand(bomb)}
    a = obs["A"]
    check(a.bomb_pot and a.passive_entry,
          f"爆炸池应标 bomb_pot/passive_entry：{a}")
    check(not a.preflop_opp and not a.vpip and not a.pfr,
          f"被动前注不应算 VPIP 机会/入池：{a}")
    check(a.saw_flop and a.postflop_opp and a.bet_raise == 1,
          f"翻后动作仍应统计：{a}")
    check(a.inferred_ratio == 0.0,
          f"旧记录里的 inferred 翻前 check 不应拉低数据质量：{a}")

    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(Path(tmp) / "p.json", "0.5/1-9max")
        store.update_hand(bomb)
        stats = store.players["A"]
        check(stats.preflop_opps == 0 and stats.n == 0,
              f"被动入池不应进 VPIP 分母：{stats.preflop_opps}/{stats.n}")
        check(stats.bomb_pot_hands == 1 and stats.passive_entry_hands == 1,
              f"被动入池应单独计数：{stats.bomb_pot_hands}/"
              f"{stats.passive_entry_hands}")
        check(stats.postflop_opps > 0 and stats.bets_raises > 0,
              "翻后统计不应被丢掉")

        # 普通手牌的主动跟注仍要计入 VPIP
        normal_a = make_hand(
            uid="s01-h12",
            preflop=[act("a", "call", 1.0, name="A", position="BTN"),
                     act("hero", "raise", 3.0, is_hero=True)],
            flop=[act("a", "check", None, name="A")],
            seats=seats,
        )
        store.update_hand(normal_a)
        stats = store.players["A"]
        check(stats.n == 1 and stats.vpip_pct == 100.0,
              f"普通手牌的主动跟注应计入 VPIP：n={stats.n}/"
              f"vpip={stats.vpip_pct}")


def test_bomb_pot_with_voluntary_preflop_counts_vpip() -> None:
    """爆炸底池若有真实的翻前 call/raise，仍应算主动入池。"""
    hand = make_hand(
        uid="s01-h16", bomb_pot=True,
        blinds={"sb": 0.5, "bb": 1.0, "ante": 5.0},
        preflop=[act("a", "call", 6.0, name="芒果与西米", position="BTN"),
                 act("hero", "check", None, is_hero=True)],
        flop=[act("a", "bet", 10.0, name="芒果与西米")],
    )
    obs = {o.display_name: o for o in observations_from_hand(hand)}
    a = obs["芒果与西米"]
    check(a.bomb_pot and not a.passive_entry,
          f"有真实翻前行动时不应判被动入池：{a}")
    check(a.preflop_opp and a.vpip,
          f"真实 call 应计入 VPIP：{a}")


def test_bomb_ante_without_banner_is_detected() -> None:
    """没有横幅但 ante>=5 且翻前无行动 → 按爆炸底池被动入池处理。"""
    hand = make_hand(
        uid="s01-h17",
        blinds={"sb": 0.5, "bb": 1.0, "ante": 5.0},
        preflop=[act("a", "check", None, name="芒果与西米", inferred=True)],
        flop=[act("a", "check", None, name="芒果与西米")],
    )
    obs = {o.display_name: o for o in observations_from_hand(hand)}
    a = obs["芒果与西米"]
    check(a.bomb_pot and a.passive_entry,
          f"ante=5 且无主动行动应判被动入池：{a}")
    check(not a.preflop_opp and not a.vpip, f"不应计入 VPIP：{a}")

    # 普通大前注局里真实发生的 call 不能被误判成爆炸底池
    normal = make_hand(
        uid="s01-h18",
        blinds={"sb": 0.5, "bb": 1.0, "ante": 5.0},
        preflop=[act("a", "call", 5.0, name="芒果与西米")],
        flop=[act("a", "check", None, name="芒果与西米")],
    )
    b = {o.display_name: o for o in observations_from_hand(normal)}["芒果与西米"]
    check(not b.bomb_pot and not b.passive_entry,
          f"有大 ante 但有主动 call 时不应判爆炸池：{b}")
    check(b.preflop_opp and b.vpip, f"主动 call 应计入 VPIP：{b}")


def test_normalize_and_pool_key() -> None:
    check(normalize_name("  芒果  与西米 \n") == "芒果 与西米",
          f"名字应 NFKC + 合并空白：{normalize_name('  芒果  与西米 \n')!r}")
    check(normalize_name("　ＡＢＣ　") == "ABC", "全角应 NFKC 成半角")
    check(normalize_name("   ") is None, "空白名字应归一成 None")
    check(pool_key_from_blinds(BLINDS, 9) == "0.5/1-9max",
          f"池键格式不对：{pool_key_from_blinds(BLINDS, 9)}")
    check(pool_key_from_blinds(None, 6) == "unknown-6max",
          "盲注未知时应给 unknown 池键")


def test_observations_from_hand() -> None:
    hand = make_hand(
        board=("Ad", "As", "Qc", "Kc", "8d"),
        preflop=[
            act("a", "raise", 8.0, name="芒果与西米", position="BTN"),
            act("b", "call", 8.0, name="好运常在ok", position="SB"),
            act("c", "fold", None, name="短码", position="CO"),
            act("hero", "call", 8.0, is_hero=True),
        ],
        flop=[
            act("a", "bet", 10.0, name="芒果与西米"),
            act("b", "call", 10.0, name="好运常在ok"),
            act("hero", "call", 10.0, is_hero=True),
        ],
        turn=[
            act("a", "bet", 30.0, inferred=True),
            act("hero", "fold", None, is_hero=True, inferred=True),
        ],
        shows=[{"seat_id": "a", "name": "芒果与西米", "position": "BTN",
                "cards": ["Kd", "Qs"]}],
    )
    obs = {o.display_name: o for o in observations_from_hand(hand)}
    check(set(obs) == {"芒果与西米", "好运常在ok", "短码"},
          f"应解析三个对手（hero 除外）：{set(obs)}")
    a = obs["芒果与西米"]
    check(a.preflop_opp and a.vpip and a.pfr,
          f"BTN 翻前加注应 VPIP+PFR：{a}")
    check(a.postflop_opp and a.bet_raise == 2 and a.open_size == 8.0,
          f"翻后两次下注 / open 8BB：{a}")
    check(a.showdown_category == 2 and a.aggressive_showdown,
          f"KQ 在 AAQK8 上是两对且翻后激进：{a}")
    check(a.inferred_ratio > 0.3 and a.weight == 0.5,
          f"inferred 占比超过 0.3 应降权：{a.inferred_ratio}/{a.weight}")
    b = obs["好运常在ok"]
    check(b.vpip and not b.pfr and b.threebet_opp,
          f"SB 平跟加注：VPIP、不是 PFR、有 3bet 机会：{b}")
    check(b.facing_bet == 1 and b.call == 1,
          f"翻牌面对下注跟注：facing=1/call=1，实际 {b.facing_bet}/{b.call}")
    c = obs["短码"]
    check(c.preflop_opp and not c.vpip and not c.saw_flop,
          f"翻前弃牌的人应有机会但无 VPIP、没看翻牌：{c}")


def test_table_dynamics_priors() -> None:
    text = (
        "当前是混合桌：\n"
        "A. 松浪/松凶（芒果与西米、葡萄牙h）：open 8–11BB\n"
        "B. 松弱/跟注站（FatherTM、好运常在ok）：很少加注\n"
    )
    priors = parse_table_dynamics_priors(text)
    check(priors.get("芒果与西米") == "maniac",
          f"松浪/松凶组应解析成 maniac：{priors}")
    check(priors.get("葡萄牙h") == "maniac", f"同组第二个名字：{priors}")
    check(priors.get("FatherTM") == "station", f"跟注站组：{priors}")
    check(priors.get("好运常在ok") == "station", f"跟注站组第二个名字：{priors}")
    check("当前是混合桌" not in priors, "标题不应被当成玩家名")


# ─────────────────────────── 分类与门控 ───────────────────────────

def _stats(name: str, **kw) -> OpponentStats:
    base = dict(key=name, preflop_opps=20, sample_preflop_opps=20,
                hands=20, postflop_opps=15, bets_raises=8, calls=5,
                open_sizes=[7.0, 7.0, 8.0, 6.5, 7.5],
                bet_sizes=[0.5, 0.66, 0.5, 0.6],
                showdowns=5, aggressive_showdowns=3,
                weak_aggressive_showdowns=1)
    base.update(kw)
    return OpponentStats(**base)


def test_classifier_archetypes_and_gates() -> None:
    cases = {
        "station": _stats("station", vpip=10, pfr=1,
                          bets_raises=2, calls=12,
                          open_sizes=[2.0, 2.0, 2.5, 2.0, 2.0],
                          bet_sizes=[0.4] * 5,
                          showdowns=5, aggressive_showdowns=1,
                          weak_aggressive_showdowns=1),
        "lag": _stats("lag", vpip=9, pfr=6, bets_raises=12,
                      open_sizes=[6.5, 7.0, 8.0, 7.0, 6.0]),
        "maniac": _stats("maniac", vpip=12, pfr=11, bets_raises=15,
                         calls=1, open_sizes=[10, 11, 12, 9, 10],
                         bet_sizes=[0.7, 1.3, 1.4, 0.8, 1.5, 0.9, 1.3,
                                    1.2, 0.7, 1.1, 1.5, 1.6],
                         showdowns=5, aggressive_showdowns=4,
                         weak_aggressive_showdowns=3),
        "nit": _stats("nit", vpip=2, pfr=1, bets_raises=1, calls=6,
                      open_sizes=[2.0, 2.0, 2.5, 2.0, 2.0],
                      bet_sizes=[0.4] * 5,
                      showdowns=5, aggressive_showdowns=0,
                      weak_aggressive_showdowns=0),
        "tag": _stats("tag", vpip=5, pfr=4, bets_raises=12, calls=5,
                      open_sizes=[3.0, 3.0, 3.5, 3.0, 3.0],
                      bet_sizes=[0.6] * 8,
                      showdowns=5, aggressive_showdowns=3,
                      weak_aggressive_showdowns=1),
    }
    for want, stats in cases.items():
        read = classify(stats)
        check(read.archetype == want,
              f"{want} 应分类为 {want}，实际 {read.archetype}：{read.note}")
        check(read.gate in ("read", "stable"),
              f"{want} 在 n=20 时应达到 read/stable，实际 {read.gate}")

    tiny = classify(_stats("tiny", preflop_opps=7, sample_preflop_opps=7))
    check(tiny.archetype == "unknown" and tiny.gate == "unknown",
          f"n<8 必须 unknown：{tiny}")
    tentative = classify(_stats("tentative", preflop_opps=10,
                                sample_preflop_opps=10, hands=10))
    check(tentative.gate == "tentative",
          f"n=10 应只到 tentative：{tentative.gate}")
    stable = classify(_stats("stable", preflop_opps=40,
                             sample_preflop_opps=40, hands=40))
    check(stable.gate == "stable",
          f"n=40 的高置信样本应试 stable，实际 {stable.gate}/{stable.confidence}")


# ─────────────────────────── 存储 ───────────────────────────

def _lag_hand(uid: str, session: int = 1) -> dict:
    return make_hand(
        uid=uid, session=session,
        preflop=[
            act("a", "raise", 8.0, name="芒果与西米", position="BTN"),
            act("b", "call", 8.0, name="好运常在ok", position="SB"),
            act("c", "fold", None, name="短码", position="CO"),
            act("hero", "call", 8.0, is_hero=True),
        ],
        flop=[
            act("a", "bet", 10.0, name="芒果与西米"),
            act("b", "call", 10.0, name="好运常在ok"),
            act("hero", "call", 10.0, is_hero=True),
        ],
        board=("Ad", "As", "Qc", "Kc", "8d"),
    )


def test_store_update_dedupe_and_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "profiles.json"
        store = ProfileStore(path, "0.5/1-9max")
        for i in range(20):
            hand = _lag_hand(f"s01-h{i}")
            store.update_hand(hand)
        # 同一手重复 update 不能重复计数
        before = store.players["芒果与西米"].preflop_opps
        store.update_hand(_lag_hand("s01-h1"))
        after = store.players["芒果与西米"].preflop_opps
        check(before == after, f"重复 update 同一手应去重：{before} -> {after}")
        stats = store.players["芒果与西米"]
        check(stats.n == 20 and stats.vpip_pct == 100.0,
              f"20 手全 VPIP：n={stats.n}, vpip={stats.vpip_pct}")
        check(stats.pfr_pct == 100.0, f"每手都加注：pfr={stats.pfr_pct}")
        read = store.read_one("芒果与西米")
        check(read.archetype in ("lag", "maniac") and read.gate in ("read", "stable"),
              f"20 手松凶样本应可读：{read}")
        check(store.save(force=True), "force save 应成功")
        check(path.exists(), "画像文件应落盘")

        loaded = ProfileStore(path, "0.5/1-9max")
        loaded.load()
        check(loaded.load_error is None, f"重新加载不应报错：{loaded.load_error}")
        check("芒果与西米" in loaded.players, "重新加载后玩家不应丢")
        check(loaded.players["芒果与西米"].n == 20,
              f"样本数应持久化：{loaded.players['芒果与西米'].n}")
        check(loaded.seen_hands, "seen_hands 应持久化（重建时去重）")
        data = json.loads(path.read_text(encoding="utf-8"))
        check(data["classifier_version"] == CLASSIFIER_VERSION,
              f"画像文件应带分类器版本：{data.get('classifier_version')}")
        check(data["pool_key"] == "0.5/1-9max", f"池键：{data.get('pool_key')}")

        # clear 必须同时丢掉 seen_hands，否则重置后重建同一批手牌会被去重跳过
        removed = loaded.clear()
        check(removed >= 1 and not loaded.players and not loaded.seen_hands,
              f"clear 应清空玩家和去重状态：{removed}/{len(loaded.players)}/"
              f"{len(loaded.seen_hands)}")
        check(len(loaded.update_hand(_lag_hand("s01-h1"))) > 0,
              "clear 后同一 hand_uid 应能重新计入")


def test_pin_beats_auto_and_prior() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(Path(tmp) / "p.json", "0.5/1-9max")
        store.set_table_dynamics("A. 松弱/跟注站（芒果与西米）：很少加注")
        for i in range(20):
            store.update_hand(_lag_hand(f"s01-h{i}"))
        auto = store.read_one("芒果与西米")
        check(auto.source == "auto",
              f"高置信自动画像应优先于 .table_dy 先验：{auto.source}")
        check(auto.archetype in ("lag", "maniac"),
              f"自动画像应是松凶/松浪：{auto.archetype}")
        store.pin("芒果与西米", "station")
        pinned = store.read_one("芒果与西米")
        check(pinned.archetype == "station" and pinned.source == "pin"
              and pinned.confidence == 1.0,
              f"pin 应覆盖自动结果：{pinned}")
        check(pinned.n == 20, f"pin 仍应显示自动样本数：{pinned.n}")
        store.pin("芒果与西米", None)
        check(store.read_one("芒果与西米").source == "auto",
              "清除 pin 后应回到自动结果")


def test_prior_used_when_sample_small() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(Path(tmp) / "p.json", "0.5/1-9max")
        store.update_hand(_lag_hand("s01-h1"))
        store.set_table_dynamics("A. 松浪/松凶（芒果与西米）：open 8-11BB")
        read = store.read_one("芒果与西米")
        check(read.source == "prior" and read.archetype == "maniac",
              f"n<8 时应用 .table_dy 先验：{read}")
        check(read.gate == "tentative",
              f"先验只能是 tentative，不能改硬指令：{read.gate}")


def test_temp_key_merged_when_name_recovered() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(Path(tmp) / "p.json", "0.5/1-9max")
        # 第一手：座位 a 没读到名字 → temp key
        no_name = make_hand(
            uid="s01-h1",
            preflop=[act("a", "raise", 8.0)],
            flop=[act("a", "bet", 10.0)],
            seats=[{"seat_id": "hero", "name": "我", "position": "BB"},
                   {"seat_id": "a", "position": "BTN"},
                   {"seat_id": "b", "name": "路人甲", "position": "SB"},
                   {"seat_id": "c", "name": "路人乙", "position": "CO"}],
        )
        store.update_hand(no_name)
        temp_keys = [k for k in store.players if k.startswith("temp:")]
        check(len(temp_keys) == 1, f"没名字应使用 temp key：{list(store.players)}")

        # 第二手：同一 session/座位读到名字 → 合并到名字键，并写 alias
        named = make_hand(
            uid="s01-h2",
            preflop=[act("a", "call", 1.0, name="芒果与西米", position="BTN"),
                     act("hero", "raise", 3.0, is_hero=True)],
            flop=[act("a", "check", None, name="芒果与西米")],
        )
        store.update_hand(named)
        check("芒果与西米" in store.players,
              f"名字恢复后应合并到名字键：{list(store.players)}")
        check(temp_keys[0] not in store.players,
              "临时键不应继续留在画像里")
        check(store.aliases.get(temp_keys[0]) == "芒果与西米",
              f"应记录 alias：{store.aliases}")
        stats = store.players["芒果与西米"]
        check(stats.n == 2, f"合并后样本数应包含临时键那一手：{stats.n}")


def test_pool_keys_are_separated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(Path(tmp) / "p.json", "")
        hand_1_2 = _lag_hand("s01-h1")
        hand_5_10 = _lag_hand("s02-h1")
        hand_5_10["blinds"] = {"sb": 5.0, "bb": 10.0}
        hand_5_10["session"] = 2
        hand_5_10["uid"] = "s02-h1"
        store.update_hand(hand_1_2)
        store.update_hand(hand_5_10)
        keys = set(store.players)
        check("芒果与西米" in keys, f"1/2 池应保留基础键：{keys}")
        check(any("@5/10-9max" in k for k in keys),
              f"5/10 池应分开存储：{keys}")


# ─────────────────────────── 建议集成 ───────────────────────────

def _read(seat: str, name: str, archetype: str, *, confidence: float = 0.85,
          n: int = 50, source: str = "auto",
          gate: str = "stable") -> OpponentRead:
    return OpponentRead(seat_id=seat, key=name, display_name=name,
                        archetype=archetype, confidence=confidence, n=n,
                        vpip=55.0, pfr=25.0, af=3.0, source=source,
                        note="test", gate=gate)


def _h8_point(*, to_call: float = 0.0, bettor: str | None = None):
    streets = (StreetRecord("preflop", 4.0, ()),
               StreetRecord("flop", 19.0, ()),
               StreetRecord("turn", 47.5, ()))
    if bettor:
        streets = streets[:2] + (
            StreetRecord("turn", 47.5, (
                ActionRecord("turn", bettor, "芒果与西米", "BTN", "bet", 10.0),
            )),
        )
    return HandSnapshot(
        hand_id="h8", hand_index=8, street="turn",
        board=("Ad", "As", "Qc", "Kc"), hero_cards=("Jh", "Qd"),
        hero_position="BB", hero_stack=95.0, hero_bet=0.0,
        hero_invested=24.0, to_call=to_call, second_bet=None,
        pot=47.5, pot_before_street=47.5, table_size=9, blinds=BLINDS,
        opponents=(
            OpponentSnapshot("midR", "芒果与西米", "BTN", 95.0, to_call, False),
            OpponentSnapshot("lowR", "好运常在ok", "SB", 95.0, 0.0, False),
        ),
        streets=streets, preflop_shapes=("single-raise",),
        hero_is_pfr=False, cbet_spot=False,
        decision_key=("h8", "turn", 0, 47.5, to_call, ("Jh", "Qd")),
        players_dealt=9,
    )


MIXED = ("A. 松浪/松凶（芒果与西米、葡萄牙h）：open 8-11BB，翻后爱加注。"
         "B. 松弱/跟注站（FatherTM、好运常在ok）：很少加注。")


def test_advice_uses_per_opponent_read() -> None:
    reads = (_read("midR", "芒果与西米", "maniac"),
             _read("lowR", "好运常在ok", "station"))
    point = _h8_point()
    sit = build_situation(point, table_dynamics=MIXED,
                          opponent_reads=reads)
    check(sit.opponent_reads == reads, "Situation 应保存画像")
    check(_aggressive_context(sit), "有松浪画像时应走 aggressive 分支")
    directive = value_directive(sit) or ""
    check("AGGRESSIVE TABLE" in directive and "DO NOT THIN-VALUE" in directive,
          f"h8 类两对应压成 check：{directive}")
    check("芒果与西米" in directive,
          f"硬指令应点名是哪个对手：{directive}")

    # 全部是被动型画像：即使 .table_dy 写的是混合桌，也不该按松浪处理
    passive = (_read("midR", "芒果与西米", "station"),
               _read("lowR", "好运常在ok", "nit"))
    passive_sit = build_situation(point, table_dynamics=MIXED,
                                  opponent_reads=passive)
    check(not _aggressive_context(passive_sit),
          "本手在局对手全是 station/nit 时不该走 aggressive 分支")
    check("AGGRESSIVE TABLE" not in (value_directive(passive_sit) or ""),
          f"被动型画像不应出现松浪硬指令：{value_directive(passive_sit)}")

    # 面对下注：看下注的人，而不是桌上另一个人的标签
    facing = _h8_point(to_call=10.0, bettor="lowR")
    facing_sit = build_situation(facing, opponent_reads=(
        _read("midR", "芒果与西米", "maniac"),
        _read("lowR", "好运常在ok", "station")))
    selected = decision_read(facing_sit)
    check(selected is not None and selected.seat_id == "lowR",
          f"面对下注应选中下注者：{selected}")
    check(not _aggressive_context(facing_sit),
          "下注者是 station 时不应按 LAG 抓诈")


def test_advice_records_read_snapshot() -> None:
    reads = (_read("midR", "芒果与西米", "maniac"),
             _read("lowR", "好运常在ok", "station"))
    sit = build_situation(_h8_point(), opponent_reads=reads)
    result = advise(sit, None).to_dict()
    snapshot = result.get("opponent_reads")
    check(snapshot and len(snapshot) == 2,
          f"建议记录应带画像快照：{result}")
    check(snapshot[0]["archetype"] == "maniac"
          and snapshot[0]["seat_id"] == "midR"
          and snapshot[0]["n"] == 50 and snapshot[0]["source"] == "auto",
          f"快照字段应含 seat/type/n/source：{snapshot[0]}")


def test_prompt_renders_opponent_reads() -> None:
    point = _h8_point()
    tentative = _read("lowR", "好运常在ok", "station", confidence=0.55,
                      n=12, gate="tentative")
    sit = build_situation(point, opponent_reads=(
        _read("midR", "芒果与西米", "maniac"), tentative,
        _read("x", "路人", "unknown", confidence=0.0, n=0,
              source="unknown", gate="unknown")))
    facts = render_hand_facts(sit)
    check("Opponent reads" in facts, f"事实块应含对手画像：\n{facts}")
    check("BTN 芒果与西米: MANIAC" in facts,
          f"画像应带位置和类型：\n{facts}")
    check("好运常在ok: STATION" in facts and "[tentative]" in facts,
          f"tentative 应标注：\n{facts}")
    check("路人" not in facts, "unknown 不应输出，避免噪声")


def run() -> int:
    tests = [
        test_fold_and_check_are_inferred_at_street_end,
        test_no_fold_inferred_when_action_badge_recorded,
        test_bomb_pot_preflop_has_no_inferred_checks,
        test_bomb_pot_passive_entry_excluded_from_vpip,
        test_bomb_pot_with_voluntary_preflop_counts_vpip,
        test_bomb_ante_without_banner_is_detected,
        test_normalize_and_pool_key,
        test_observations_from_hand,
        test_table_dynamics_priors,
        test_classifier_archetypes_and_gates,
        test_store_update_dedupe_and_persistence,
        test_pin_beats_auto_and_prior,
        test_prior_used_when_sample_small,
        test_temp_key_merged_when_name_recovered,
        test_pool_keys_are_separated,
        test_advice_uses_per_opponent_read,
        test_advice_records_read_snapshot,
        test_prompt_renders_opponent_reads,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 对手类型识别全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
