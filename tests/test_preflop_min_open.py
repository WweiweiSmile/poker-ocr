#!/usr/bin/env python3
"""#112 翻前开池下限进桌配置 + ante 假设可见 的验收。

    nv tests/test_preflop_min_open.py

覆盖：
  1. profile 字段 `preflop_min_open_bb`：默认 5.5、范围 2-20、越界在加载时失败；
  2. 0.5BB 前注 9-max 桌（下限 6.0）：UTG 开池 6/7/8BB，CO/BTN 仍 7-8BB；
  3. 无前注 9-max 桌（下限 5.5）：UTG 起档 5.5BB，且不产生 ante 假设告警；
  4. ante 假设事实：横幅读丢前注时标在帧状态上，经手牌快照进 facts/prompt；
  5. 提示词「本桌翻前最低加注」取自快照，不是模块常量；
  6. 其它既有告警/备注的措辞原样保留；
  7. 每一个 config/ 里的 profile 仍然可加载。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    _facts_snapshot, build_messages, build_situation, render_facts_zh,
)
from poker_ocr.dealer import DealerResult  # noqa: E402
from poker_ocr.frame_state import (  # noqa: E402
    FrameState, PlayerState, build_state,
)
from poker_ocr.hand_track import (  # noqa: E402
    HandSnapshot, LiveHandTracker, OpponentSnapshot,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.profile import (  # noqa: E402
    DEFAULT_PREFLOP_MIN_OPEN_BB, PREFLOP_MIN_OPEN_RANGE, Profile,
)
from poker_ocr.roi_reader import RoiFrame, RoiRead, SeatRoi  # noqa: E402
from poker_ocr.prompt.user.quality import _summarize_gaps  # noqa: E402

ANTE_PROFILE = ROOT / "config" / "table_wpk_live.json"
NOANTE_PROFILE = ROOT / "config" / "table_wpk_live_noante.json"
FAILS: list[str] = []

ANTE_WARNING = "本手 ante 按桌配置假设"


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def snap9(*, position: str = "UTG", pot: float = 6.0, to_call: float = 1.0,
          hero_bet: float = 0.0, ante: float | None = 0.5,
          floor: float = DEFAULT_PREFLOP_MIN_OPEN_BB,
          hero_stack: float | None = 100.0, table_size: int = 9,
          warnings: tuple[str, ...] = (),
          preflop_shapes: tuple[str, ...] = ("unopened",),
          second_bet: float | None = None) -> HandSnapshot:
    opponents = tuple(
        OpponentSnapshot(f"p{i}", None, "BB", 100.0, 0.0, False)
        for i in range(table_size - 1)
    )
    return HandSnapshot(
        hand_id="min-open", hand_index=0, street="preflop", board=(),
        hero_cards=("Ah", "Kd"), hero_position=position,
        hero_stack=hero_stack, hero_bet=hero_bet, hero_invested=hero_bet,
        to_call=to_call, second_bet=second_bet, pot=pot,
        pot_before_street=0.0, table_size=table_size,
        blinds=Blinds(0.5, 1.0, ante, "1/2"),
        opponents=opponents, streets=(), preflop_shapes=preflop_shapes,
        hero_is_pfr=False, cbet_spot=False,
        decision_key=("min-open", "preflop", 0, pot, to_call),
        warnings=warnings, bomb_pot=False, players_dealt=table_size,
        preflop_min_open_bb=floor,
    )


def opens(sit) -> list[float]:
    return [o.amount for o in sit.options if o.id.startswith("open-")]


def all_raises(sit) -> set[float]:
    return {o.amount for o in sit.options
            if o.kind in ("raise", "bet") and o.amount is not None}


def situation(**kw):
    return build_situation(snap9(**kw), table_dynamics="")


# ─────────────────────────── 1. profile 字段 ───────────────────────────

def test_profile_field_defaults_to_five_half() -> None:
    """缺省配置没有这个键时按 5.5：老 profile 不许被强制改动。"""
    check(abs(DEFAULT_PREFLOP_MIN_OPEN_BB - 5.5) < 1e-9,
          f"缺省下限应为 5.5，实际 {DEFAULT_PREFLOP_MIN_OPEN_BB}")
    check(PREFLOP_MIN_OPEN_RANGE == (2.0, 20.0),
          f"允许区间应为 2-20，实际 {PREFLOP_MIN_OPEN_RANGE}")
    profile = Profile.load(ROOT / "config" / "table_default.json")
    check(abs(profile.preflop_min_open_bb - 5.5) < 1e-9,
          f"老配置缺省值应回落到 5.5，实际 {profile.preflop_min_open_bb}")


def test_profile_floor_outside_range_fails_at_load() -> None:
    """越界（<2 / >20）必须在加载时失败，并写明允许区间 2-20。

    spec #110 与 #112 都写「允许范围 2–20」，所以 3.0 是**合法值**（ticket 的
    验收句「下限设成 3 或 21」里 3 落在区间内，与范围自相矛盾；按 spec 为准，
    这里用 1.9 / 21.0 测越界，并额外确认 2.0 / 20.0 是闭区间端点）。
    """
    raw = json.loads(ANTE_PROFILE.read_text(encoding="utf-8"))
    for ok in (2.0, 20.0):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edge_profile.json"
            path.write_text(json.dumps(dict(raw, preflop_min_open_bb=ok)),
                            encoding="utf-8")
            profile, problems = Profile.inspect(path)
            check(profile is not None and problems == [],
                  f"{ok} 是闭区间端点，应能加载：{problems}")
    for bad in (1.9, 21.0):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad_profile.json"
            path.write_text(json.dumps(dict(raw, preflop_min_open_bb=bad)),
                            encoding="utf-8")
            profile, problems = Profile.inspect(path)
            check(profile is not None, f"结构完整应能构造 profile：{problems}")
            hit = [p for p in problems if "preflop_min_open_bb" in p]
            check(bool(hit), f"{bad} 应报 preflop_min_open_bb 越界：{problems}")
            check(any("2-20" in p for p in hit),
                  f"{bad} 的错误信息要写清允许区间 2-20：{hit}")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    Profile.load(path)
                except SystemExit:
                    pass
                else:
                    FAILS.append(f"{bad} 应让 Profile.load 失败")
            check("2-20" in err.getvalue(),
                  f"{bad} 加载失败信息要含 2-20：{err.getvalue()!r}")


def test_shipped_profiles_declare_their_floor() -> None:
    """两个 9-max 桌：0.5BB 前注桌 6.0；无前注桌 5.5 且不兜底前注。"""
    ante = Profile.load(ANTE_PROFILE)
    check(ante.default_ante == 0.5,
          f"前注桌 default_ante 应 0.5，实际 {ante.default_ante}")
    check(abs(ante.preflop_min_open_bb - 6.0) < 1e-9,
          f"前注桌开池下限应 6.0，实际 {ante.preflop_min_open_bb}")

    noante = Profile.load(NOANTE_PROFILE)
    check(noante.default_ante is None,
          f"无前注桌不许再兜底前注，实际 {noante.default_ante}")
    check(abs(noante.preflop_min_open_bb - 5.5) < 1e-9,
          f"无前注桌开池下限应 5.5，实际 {noante.preflop_min_open_bb}")
    check(len(noante.seats) == 9, f"无前注桌应 9 人，实际 {len(noante.seats)}")


def test_every_shipped_profile_still_loads() -> None:
    """新增字段不能把任何一个 config/ profile 弄坏。"""
    for path in sorted((ROOT / "config").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "seats" not in raw:
            continue
        profile, problems = Profile.inspect(path)
        check(profile is not None, f"{path.name} 应能加载：{problems}")
        check(problems == [], f"{path.name} 校验失败：{problems}")


# ─────────────────────────── 2. 开池档位 ───────────────────────────

def test_ante_profile_floor_sets_utg_open_tiers() -> None:
    """0.5BB 前注 + 下限 6.0：UTG 开池 6/7/8BB，三档互不相同，没有 5.5。"""
    sit = situation(ante=0.5, floor=6.0, position="UTG", pot=6.0)
    amounts = opens(sit)
    check(amounts == [6.0, 7.0, 8.0],
          f"UTG 开池应 6/7/8BB，实际 {amounts}")
    check(len(set(amounts)) == 3, f"三档不能撞车：{amounts}")
    check(5.5 not in all_raises(sit), f"下限 6.0 的桌不该出现 5.5BB：{all_raises(sit)}")


def test_ante_profile_co_and_btn_stay_in_seven_to_eight() -> None:
    """CO/BTN 的底池放大档本来就在 7-8BB，下限 6.0 不许把它们压平。"""
    for position in ("CO", "BTN"):
        sit = situation(ante=0.5, floor=6.0, position=position, pot=6.0)
        amounts = opens(sit)
        check(all(7.0 <= amount <= 8.0 for amount in amounts),
              f"{position} 开池应仍在 7-8BB，实际 {amounts}")


def test_raise_geometry_uses_the_same_floor() -> None:
    """面对加注的再加注档也取同一份快照下限（不许留第二份 5.5）。"""
    sit = situation(ante=0.5, floor=6.0, position="BTN", pot=6.0,
                    to_call=1.5, preflop_shapes=("single-raise",))
    amounts = all_raises(sit)
    check(bool(amounts), "面对加注应有加注选项")
    check(all(amount >= 6.0 for amount in amounts),
          f"下限 6.0 时加注档不能低于 6BB：{sorted(amounts)}")


def test_no_ante_profile_tiers_start_at_five_half() -> None:
    """无前注桌：UTG 起档 5.5BB，且任何地方都不出现 ante 假设告警。"""
    noante = Profile.load(NOANTE_PROFILE)
    sit = situation(ante=None, floor=noante.preflop_min_open_bb,
                    position="UTG", pot=1.5)
    amounts = opens(sit)
    check(amounts[0] == 5.5, f"无前注 UTG 起档应 5.5BB，实际 {amounts}")

    check(not [w for w in sit.warnings if ANTE_WARNING in w],
          f"无前注桌不该有 ante 假设告警：{sit.warnings}")
    facts = _facts_snapshot(sit)
    check(not [w for w in facts["warnings"] if ANTE_WARNING in w],
          f"facts 里不该有 ante 假设告警：{facts['warnings']}")
    user = render_facts_zh(sit)
    check(ANTE_WARNING not in user, "无前注桌的 prompt 里不该出现 ante 假设")


# ─────────────────────────── 3. ante 假设事实 ───────────────────────────

def read(text: str) -> RoiRead:
    return RoiRead("x", (0, 0, 10, 10),
                   parts=[(text, 0.9)] if text else [],
                   note=None if text else "rec 没识别出文字")


def frame(profile: Profile, blinds_text: str) -> RoiFrame:
    seats = []
    for ring_index, seat in enumerate(profile.seats_in_ring_order):
        seats.append(SeatRoi(seat.seat_id, ring_index, {
            "name": read(f"p{ring_index}"),
            "chip": read("100BB"),
            "bet": read(""),
        }))
    return RoiFrame(
        frame_id="t", size=(100, 100), hero_seat_id=profile.hero_seat_id,
        seats=seats,
        globals={
            "blinds": read(blinds_text), "pot": read("1.5BB"),
            "pot_before_street": read("0BB"),
            "bomb_cn": read(""), "bomb_en": read(""),
        },
        hero_turn=False, hero_turn_evidence={"rect": [0, 0, 1, 1]},
        dealer=DealerResult(profile.hero_seat_id, "found", "blob"))


def test_banner_missing_ante_is_marked_as_assumed() -> None:
    """横幅没有前注、profile 兜底填上 → 帧状态记下「假设的 0.5BB」。"""
    ante = Profile.load(ANTE_PROFILE)
    state = build_state(frame(ante, "1/2"), ante,
                        ante_fallback=ante.default_ante)
    check(state.blinds is not None and state.blinds.ante == 0.5,
          f"兜底后前注应为 0.5：{state.blinds}")
    check(state.ante_assumed_bb == 0.5,
          f"兜底出来的前注要标成假设：{state.ante_assumed_bb}")
    check(state.preflop_min_open_bb == 6.0,
          f"帧状态要带本桌开池下限：{state.preflop_min_open_bb}")


def test_banner_read_ante_is_not_assumed() -> None:
    """画面真读到 0.5BB 前注 → 不许再报「假设」。"""
    ante = Profile.load(ANTE_PROFILE)
    # 横幅里的括号是**筹码**口径：`1/2(1)` = 前注 1 筹码 = 0.5BB。
    state = build_state(frame(ante, "1/2(1)"), ante,
                        ante_fallback=ante.default_ante)
    check(state.blinds is not None and state.blinds.ante == 0.5,
          f"读到的前注应为 0.5：{state.blinds}")
    check(state.ante_assumed_bb is None,
          f"读到前注时不该标假设：{state.ante_assumed_bb}")


def test_noante_profile_never_assumes_ante() -> None:
    """default_ante=null：横幅没前注就是一桌 0 前注，不猜、不告警。"""
    noante = Profile.load(NOANTE_PROFILE)
    state = build_state(frame(noante, "1/2"), noante,
                        ante_fallback=noante.default_ante)
    check(state.blinds is not None and state.blinds.ante is None,
          f"无前注桌不该兜底前注：{state.blinds}")
    check(state.ante_assumed_bb is None,
          f"无前注桌不该标假设：{state.ante_assumed_bb}")


def player(seat_id: str, *, position: str | None = None,
           stack: float | None = 100.0, bet: float | None = 0.0,
           name: str | None = None, action: str | None = None) -> PlayerState:
    return PlayerState(seat_id=seat_id, name=name, position=position,
                       stack=stack, bet=bet, action=action, fold=None,
                       in_hand=True, all_in=False, status="playing",
                       left=False, bet_unknown=False)


def observed_point(*, ante_assumed: float | None, floor: float = 6.0,
                   warnings: tuple[str, ...] = ()):
    """走真实的手牌跟踪：两帧确认后在我这里出一个决策点。"""
    players = (player("hero", position="BTN", stack=100.0),
               player("a", position="BB", bet=1.0, name="老王"),
               player("b", position="SB", bet=0.5, name="小李"))
    state = FrameState(
        frame_id="f", table_seats=9, players_in_hand=3,
        players=list(players), blinds=Blinds(0.5, 1.0, 0.5, "1/2"),
        dealer="a", hero_seat_id="hero", hero_turn=False, pot=2.0,
        pot_before_street=0.0, preflop_min_open_bb=floor,
        ante_assumed_bb=ante_assumed)
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state, ["Ah", "Kd"], [])
    point = tracker.observe(replace(state, hero_turn=True), ["Ah", "Kd"], [])
    check(point is not None, "轮到我时应产出决策点")
    return point


def test_ante_assumed_warning_reaches_facts_and_prompt() -> None:
    """假设的 ante 从帧状态进快照、facts["warnings"] 和提示词数据质量段。"""
    point = observed_point(ante_assumed=0.5)
    if point is None:
        return
    check(any(ANTE_WARNING in w and "0.5" in w for w in point.warnings),
          f"快照 warnings 要带 ante 假设（含数值）：{point.warnings}")
    check(point.preflop_min_open_bb == 6.0,
          f"快照要带本桌开池下限：{point.preflop_min_open_bb}")

    sit = build_situation(point, table_dynamics="")
    hit = [w for w in sit.warnings if ANTE_WARNING in w]
    check(bool(hit), f"Situation 要保留 ante 假设告警：{sit.warnings}")
    facts = _facts_snapshot(sit)
    check(bool([w for w in facts["warnings"] if ANTE_WARNING in w]),
          f'facts["warnings"] 里要有 ante 假设：{facts["warnings"]}')

    user = render_facts_zh(sit)
    check("## 数据质量" in user, "prompt 应有数据质量段")
    quality = user.split("## 数据质量", 1)[1]
    check(ANTE_WARNING in quality,
          f"ante 假设必须出现在数据质量段：{quality[:300]}")
    check("6BB" in user, "同一份 prompt 的开池下限应来自快照（6BB）")

    _, messages = build_messages(sit)
    check(ANTE_WARNING in messages, "发给模型的消息里也要有 ante 假设")


def test_read_ante_does_not_produce_warning() -> None:
    """真读到前注：快照/facts/prompt 都不许多一条假设告警。"""
    point = observed_point(ante_assumed=None)
    if point is None:
        return
    check(not [w for w in point.warnings if ANTE_WARNING in w],
          f"读到前注时不该有假设告警：{point.warnings}")
    sit = build_situation(point, table_dynamics="")
    check(not [w for w in _facts_snapshot(sit)["warnings"] if ANTE_WARNING in w],
          "读到前注时 facts 里不该有假设告警")


def test_ante_assumed_survives_the_four_line_cap() -> None:
    """数据质量段最多 4 条，但 ante 假设不能被别的软缺口挤掉。

    spec #110 §1 要求这条假设一路到提示词数据质量段；`_summarize_gaps` 的
    cap 是 4，若排在软缺口末尾，多来几条范围告警它就不见了。所以它排在
    硬缺口之后、软缺口之前。
    """
    soft = ["只建模前 2 名对手；另有 1 名未计入范围权益",
            "部分座位翻前动作线缺失，范围按未开池先验估计",
            "部分座位本街行动线缺失，范围按未行动处理（不是已过牌）",
            "某座位范围未知",
            "街前底池未读全，范围 SPR 为近似值"]
    assumed = "本手 ante 按桌配置假设为 0.5 BB，未从画面读到"
    summary = _summarize_gaps([*soft, assumed])
    check(len(summary) <= 4, f"最多 4 条，实际 {len(summary)}：{summary}")
    check(any("ante 按桌配置假设" in gap for gap in summary),
          f"ante 假设必须活过 cap：{summary}")
    # 硬缺口仍然排在它前面（动作空间被砍才是第一位）。
    hard_first = _summarize_gaps(["底牌未读出，无法评估牌力", assumed])
    check(hard_first and hard_first[0].startswith("底牌未读出"),
          f"硬缺口要排在 ante 假设之前：{hard_first}")


# ─────────────────────────── 4. 提示词取快照 ───────────────────────────

def test_prompt_min_open_line_follows_snapshot() -> None:
    """「本桌翻前最低加注」渲染的是快照值，改快照就跟着改。"""
    for floor in (6.0, 7.5, 5.5):
        sit = situation(ante=0.5 if floor != 5.5 else None, floor=floor)
        user = render_facts_zh(sit)
        check(f"本桌翻前最低加注：{floor:g}BB" in user,
              f"快照下限 {floor} 应渲染到提示词：{user[-400:]}")


def test_prompt_min_open_line_absent_without_raise_options() -> None:
    """没有加注选项时不写这条（保持原有门控）。"""
    snap = snap9(ante=0.5, floor=6.0, hero_stack=1.0, to_call=1.0)
    sit = build_situation(snap, table_dynamics="")
    user = render_facts_zh(sit)
    check("本桌翻前最低加注" not in user,
          "只剩跟注/弃牌时不该写最低加注行")


# ─────────────────────────── 5. 既有措辞不变 ───────────────────────────

def test_existing_warnings_keep_exact_wording() -> None:
    """新增 ante 告警不许改掉 4-bet+ / 后手不足 这些既有措辞。"""
    original = "4-bet+ pot: no range table for this spot (range unknown)"
    sit = situation(ante=0.5, floor=6.0, warnings=(original,))
    check(original in sit.warnings,
          f"快照告警要原样保留：{sit.warnings}")
    facts = _facts_snapshot(sit)
    check(original in facts["warnings"],
          f"4-bet+ 告警进事实时必须逐字不变：{facts['warnings']}")
    # 渲染口径也不能变：`quality._gap_zh` 的既有映射是「range unknown →
    # 某座位范围未知」（4-bet+ 这条恰好含 range unknown，映射顺序是既有
    # 行为，不在本 ticket 改动范围内）。用一条无歧义的既有告警确认翻译没被
    # 新逻辑挤掉。
    user = render_facts_zh(situation(
        ante=0.5, floor=6.0, warnings=("hero hole cards unread",)))
    check("底牌未读出，无法评估牌力" in user,
          "既有告警的中文措辞不许改")

    short = situation(ante=0.5, floor=6.0, hero_stack=2.0)
    check(any(o.note == "后手不足，等同全下" for o in short.options),
          f"后手不足的备注措辞不许改：{[o.note for o in short.options]}")


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
    print("\n✅ 开池下限 / ante 假设全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
