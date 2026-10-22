#!/usr/bin/env python3
"""FrameState 的单元测试（M2.4）：手工拼 RoiFrame，不依赖图片和模型。

重点覆盖三件事：

  1. **状态拼装**：筹码/下注/底池/盲注/位置/全下/人数。
  2. **质量闸**：无法精准识别的帧必须带明确的 error（调用方据此整帧丢弃）。
  3. **变化指纹**：只有牌局事实触发记录 —— 动作徽章、弃牌检测、hero_turn、
     pbs 误读都不能制造假记录。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_frame_state.py
"""

from __future__ import annotations

import sys
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.dealer import DealerResult  # noqa: E402
from poker_ocr.frame_state import (  # noqa: E402
    action_signature, build_state, signature,
)
from poker_ocr.parse import Blinds, NameTracker  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402
from poker_ocr.roi_reader import RoiFrame, RoiRead, SeatRoi  # noqa: E402

WPK = ROOT / "config" / "table_wpk_1080x2400.json"
PROFILE = Profile.load(WPK)
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def read(text: str, score: float = 0.9) -> RoiRead:
    return RoiRead("x", (0, 0, 10, 10),
                   parts=[(text, score)] if text else [],
                   note=None if text else "rec 没识别出文字")


# 一手真实的牌面：9 人在场，dealer=topL；lowR 全下 20，upR SB 0.5，midR BB 1
BASE = {
    "hero": {"name": "gggamel", "chip": "86.5BB"},
    "lowL": {"name": "YKY", "chip": "234.5BB", "bet": "6BB"},
    "midL": {"name": "伯克利丁真", "chip": "88BB"},
    "upL": {"name": "奶酪味的狗", "chip": "60BB"},
    "topL": {"name": "kelly828", "chip": "238BB"},
    "topR": {"name": "一只猫ii", "chip": "202BB", "bet": "6BB"},
    "upR": {"name": "A河牌救我", "chip": "171BB", "bet": "0.5BB"},
    "midR": {"name": "星际小杨66", "chip": "485.5BB", "bet": "1BB"},
    "lowR": {"name": "Dragon333", "chip": "0", "bet": "20BB"},
}


def make_frame(rows: dict | None = None, blinds: str = "1/2(1)52122200:39:20",
               pot: str = "38BB", pbs: str = "4.5BB", dealer: str | None = "topL",
               hero_turn: bool | None = False) -> RoiFrame:
    rows = BASE if rows is None else rows
    seats = []
    for ring_index, seat in enumerate(PROFILE.seats_in_ring_order):
        r = rows.get(seat.seat_id, {})
        seats.append(SeatRoi(seat.seat_id, ring_index, {
            "name": read(r.get("name", "")),
            "chip": read(r.get("chip", "")),
            "bet": read(r.get("bet", "")),
        }))
    globals_ = {"blinds": read(blinds), "pot": read(pot),
                "pot_before_street": read(pbs)}
    dealer_res = (DealerResult(dealer, "found", "blob") if dealer
                  else DealerResult(None, "not_found", "blob"))
    return RoiFrame(frame_id="t", size=(100, 100), hero_seat_id="hero",
                    seats=seats, globals=globals_, hero_turn=hero_turn,
                    hero_turn_evidence={"rect": [0, 0, 1, 1]}, dealer=dealer_res)


def state(rows=None, **kw):
    return build_state(make_frame(rows, **kw), PROFILE)


# ─────────────────────────── 状态拼装 ───────────────────────────

def test_full_state() -> None:
    s = state()
    check(s.ok, f"完整帧不该有 error，实际 {s.errors}")
    check(s.pot == 38.0 and s.pot_before_street == 4.5, f"pot/pbs 不对：{s.pot}/{s.pot_before_street}")
    check(s.street_bet_total == 33.5, f"本街总额应为 33.5，实际 {s.street_bet_total}")
    check(s.blinds is not None and (s.blinds.sb, s.blinds.bb, s.blinds.ante) == (0.5, 1.0, 0.5),
          f"盲注不对：{s.blinds}")
    check(s.dealer == "topL", f"庄位不对：{s.dealer}")
    check(s.players_in_hand == 9, f"在场人数应为 9，实际 {s.players_in_hand}")
    check(s.table_seats == 9, "桌容量 9")

    lowr = s.player("lowR")
    check(lowr.stack == 0.0 and lowr.bet == 20.0 and lowr.all_in,
          f"lowR 应是全下 20：{lowr.to_debug()}")

    # 位置：dealer=topL → BTN；他左边 topR=SB、upR=BB
    check(s.player("topL").position == "BTN", f"topL 应是 BTN，实际 {s.player('topL').position}")
    check(s.player("topR").position == "SB", f"topR 应是 SB，实际 {s.player('topR').position}")
    check(s.player("upR").position == "BB", f"upR 应是 BB，实际 {s.player('upR').position}")
    check(s.player("hero").position == "UTG+2",
          f"hero 应是 UTG+2，实际 {s.player('hero').position}")


def test_action_badge_keeps_sticky_name() -> None:
    """动作徽章盖住名字时，名字沿用上一份状态（而不是变成「跟注」）。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowL"]["name"] = "跟注"
    s = build_state(make_frame(rows), PROFILE, prev_names={"lowL": "YKY"})
    check(s.player("lowL").name == "YKY", f"名字应沿用 YKY，实际 {s.player('lowL').name}")
    check(s.player("lowL").action == "call", "动作应识别为 call")


def test_idle_zero_player_is_not_in_hand() -> None:
    """筹码 0、本街没下注、没动作 → 不在这一手（正在带入/离桌）。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowR"] = {"name": "Dragon333", "chip": "0", "bet": ""}
    s = state(rows)
    lowr = s.player("lowR")
    check(not lowr.in_hand and lowr.status == "sitting_out",
          f"zero-idle 应判 sitting_out，实际 {lowr.to_debug()}")
    check(s.players_in_hand == 8, f"在场人数应为 8，实际 {s.players_in_hand}")
    check(s.player("lowR").name == "Dragon333", "人还坐在桌上，名字要留着")


def test_lenient_chip_parse() -> None:
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowL"]["chip"] = "460.538"       # BB 被读成 38
    strict = build_state(make_frame(rows), PROFILE)
    check(strict.player("lowL").stack is None, "严格模式应拒绝无单位筹码")
    check(not strict.ok and any(e.startswith("stack:lowL") for e in strict.errors),
          f"严格模式应报 stack:lowL：{strict.errors}")

    loose = build_state(make_frame(rows), PROFILE, lenient_chips=True)
    check(loose.player("lowL").stack == 460.538,
          f"宽松模式应取前导数字，实际 {loose.player('lowL').stack}")
    check(loose.ok, f"宽松模式这一帧应通过：{loose.errors}")


def test_all_in_carried_after_bet_pill_disappears() -> None:
    """全下玩家在下注结算后变成「筹码 0、pill 收走」，不能当成离桌。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowR"] = {"name": "Dragon333", "chip": "0", "bet": ""}
    s = build_state(make_frame(rows), PROFILE, prev_all_in={"lowR"})
    lowr = s.player("lowR")
    check(lowr.in_hand and lowr.all_in,
          f"上一帧全下、这帧 0/0 应仍是全下，实际 {lowr.to_debug()}")
    check(s.players_in_hand == 9, f"人数应保持 9，实际 {s.players_in_hand}")


def test_all_in_badge_and_equity() -> None:
    """头像黄色 All-in 徽章 + 筹码位的 `30%`：全下且带实时胜率，不算坏帧。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["hero"] = {"name": "gggamel", "chip": "", "bet": "30%"}
    s = build_state(make_frame(rows), PROFILE,
                    avatar_reads={"hero": ("Alin", 0.74)})
    p = s.player("hero")
    check(p.all_in, f"头像 Alin 应判全下：{p.to_debug()}")
    check(p.equity == 30.0, f"应解析出 30% 胜率：{p.to_debug()}")
    check(p.bet is None and p.stack is None,
          f"胜率不是下注/筹码：{p.to_debug()}")
    check("bet:hero" not in s.errors and "stack:hero" not in s.errors,
          f"全下玩家允许缺 bet/stack：{s.errors}")


def test_equity_slot_marks_all_in() -> None:
    """筹码胶囊下方读到 `69%`：该座应判全下并带胜率，不要求头像徽章也读到。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["midL"] = {"name": "许文文啊", "chip": "15.5BB", "bet": ""}
    s = build_state(make_frame(rows), PROFILE,
                    avatar_reads={"midL": ("2", 0.07)},  # 徽章被亮牌盖住
                    equity_reads={"midL": ("69%", 0.79)})
    p = s.player("midL")
    check(p.all_in, f"有胜率显示应判全下：{p.to_debug()}")
    check(p.equity == 69.0, f"应解析出 69% 胜率：{p.to_debug()}")


def test_bare_equity_slot_number_is_not_all_in() -> None:
    """胜率槽只认带 % 的读数；裸 1 很可能是牌背/筹码，不能判全下。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowR"] = {"name": "西装暴徒李", "chip": "159.5BB", "bet": ""}
    s = build_state(make_frame(rows), PROFILE,
                    equity_reads={"lowR": ("1", 0.8)})
    p = s.player("lowR")
    check(not p.all_in, f"裸 1 不该判全下：{p.to_debug()}")
    check(p.equity is None, f"裸 1 不该当胜率：{p.to_debug()}")


def test_buying_in_by_avatar() -> None:
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowR"] = {"name": "Dragon333", "chip": "0", "bet": ""}
    s = build_state(make_frame(rows), PROFILE,
                    avatar_reads={"lowR": ("正在带入", 0.7)})
    check(s.player("lowR").status == "buying_in",
          f"头像写着带入应判 buying_in，实际 {s.player('lowR').status}")
    check(not s.player("lowR").in_hand, "带入中不算在场")


def test_name_tracker_used_when_given() -> None:
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowL"]["name"] = "Yk丫"          # 抖动读法
    tracker = NameTracker(confirm=3)
    tracker.resolve("lowL", "YKY")
    s = build_state(make_frame(rows), PROFILE, name_tracker=tracker)
    check(s.player("lowL").name == "YKY", f"应保持 YKY，实际 {s.player('lowL').name}")


# ─────────────────────────── 质量闸 ───────────────────────────

def test_missing_dealer() -> None:
    s = state(dealer=None)
    check("dealer" in s.errors, f"没有庄位应报 dealer，实际 {s.errors}")


def test_pot_label_without_number() -> None:
    """底池转移时横幅只剩「总底池」——这帧必须丢。"""
    s = state(pot="总底池")
    check("pot" in s.errors, f"pot 读不出应报错，实际 {s.errors}")


def test_unreadable_bet_is_an_error() -> None:
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowL"]["bet"] = "入池36%"
    s = state(rows)
    check("bet:lowL" in s.errors, f"下注乱码应报 bet:lowL，实际 {s.errors}")


def test_unreadable_chip_is_an_error() -> None:
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["upL"]["chip"] = "对子"           # 摊牌时手牌强度标签盖住筹码胶囊
    s = state(rows)
    check("stack:upL" in s.errors, f"筹码乱码应报 stack:upL，实际 {s.errors}")


def test_unparseable_blinds_without_previous() -> None:
    s = state(blinds="Allin金场梦高中")
    check("blinds" in s.errors, f"没有可继承的盲注应报 blinds，实际 {s.errors}")


def test_blinds_carried_from_previous() -> None:
    """盲注横幅被盖住时继承上一帧（盲注是慢变量）。"""
    prev = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")
    s = build_state(make_frame(blinds="Allin金场梦高中"), PROFILE, prev_blinds=prev)
    check(s.blinds_source == "carried", f"应标记 carried，实际 {s.blinds_source}")
    check(s.blinds.sb == 0.5 and s.blinds.ante == 0.5,
          f"应继承 0.5/1/0.5，实际 {s.blinds}")


def test_ante_inherited_when_parenthesis_lost() -> None:
    prev = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")
    s = build_state(make_frame(blinds="1/2）52122200:39:38"), PROFILE, prev_blinds=prev)
    check(s.blinds_source == "frame", "sb/bb 解析成功仍算 frame")
    check(s.blinds.ante == 0.5, f"前注应继承 0.5，实际 {s.blinds.ante}")


def test_pbs_gt_pot_is_an_error() -> None:
    s = state(pot="10BB", pbs="20BB")
    check("pbs_gt_pot" in s.errors, f"pbs>pot 应报错，实际 {s.errors}")


def test_pbs_equal_pot_with_bets_is_an_error() -> None:
    """有人还有下注 → 本街投进底池的钱 > 0 → pbs 必须 < pot。"""
    s = state(pot="38BB", pbs="38BB")
    check("pbs_inconsistent" in s.errors, f"应报 pbs_inconsistent，实际 {s.errors}")


# ─────────────────────────── 变化指纹 ───────────────────────────

def test_signature_ignores_advisory_fields() -> None:
    """动作/弃牌/hero_turn/pbs/ante 的单独变化不该触发记录。"""
    s1 = state()
    p = s1.player("lowL")
    players = [dc_replace(x, action="raise", fold=True) if x.seat_id == "lowL" else x
               for x in s1.players]
    s2 = dc_replace(
        s1, players=players, hero_turn=True, pot_before_street=99.0,
        street_bet_total=0.0,
        blinds=Blinds(sb=0.5, bb=1.0, ante=None, raw="x"),
    )
    check(signature(s1) == signature(s2),
          "动作/弃牌/hero_turn/pbs/ante 不应进入变化指纹")


def test_signature_detects_real_facts() -> None:
    base = state()
    players = [dc_replace(p, stack=(p.stack or 0) + 1.0) if p.seat_id == "hero" else p
               for p in base.players]
    check(signature(base) != signature(dc_replace(base, players=players)),
          "筹码变化必须进指纹")
    check(signature(base) != signature(dc_replace(base, pot=base.pot + 1)),
          "底池变化必须进指纹")
    check(signature(base) != signature(dc_replace(base, dealer="topR")),
          "庄位变化必须进指纹")
    check(signature(base) != signature(dc_replace(base, players_in_hand=8)),
          "人数变化必须进指纹")


def test_action_signature_detects_turn_and_action_changes() -> None:
    """自动点击重试用：hero_turn / 动作徽章的变化必须能看出来。"""
    base = state()
    check(action_signature(base) != action_signature(
        dc_replace(base, hero_turn=True)), "hero_turn 变化必须进动作指纹")

    players = [dc_replace(p, action="check") if p.seat_id == "hero" else p
               for p in base.players]
    check(action_signature(base) != action_signature(
        dc_replace(base, players=players)), "动作徽章变化必须进动作指纹")

    fold_players = [dc_replace(p, fold=True) if p.seat_id == "hero" else p
                    for p in base.players]
    check(action_signature(base) != action_signature(
        dc_replace(base, players=fold_players)), "弃牌检测变化必须进动作指纹")

    # 动作指纹仍应包含真实的牌局事实
    check(action_signature(base) != action_signature(
        dc_replace(base, pot=(base.pot or 0) + 1)), "底池变化必须进动作指纹")


def main() -> int:
    tests = [
        test_full_state,
        test_action_badge_keeps_sticky_name,
        test_idle_zero_player_is_not_in_hand,
        test_all_in_carried_after_bet_pill_disappears,
        test_lenient_chip_parse,
        test_all_in_badge_and_equity,
        test_equity_slot_marks_all_in,
        test_bare_equity_slot_number_is_not_all_in,
        test_buying_in_by_avatar,
        test_name_tracker_used_when_given,
        test_missing_dealer,
        test_pot_label_without_number,
        test_unreadable_bet_is_an_error,
        test_unreadable_chip_is_an_error,
        test_unparseable_blinds_without_previous,
        test_blinds_carried_from_previous,
        test_ante_inherited_when_parenthesis_lost,
        test_pbs_gt_pot_is_an_error,
        test_pbs_equal_pot_with_bets_is_an_error,
        test_signature_ignores_advisory_fields,
        test_signature_detects_real_facts,
        test_action_signature_detects_turn_and_action_changes,
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
    print("\n✅ 桌面状态全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
