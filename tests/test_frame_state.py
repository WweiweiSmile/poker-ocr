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
    action_signature, build_state, is_bomb_banner, signature,
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
               hero_turn: bool | None = False,
               bomb_cn: str = "", bomb_en: str = "") -> RoiFrame:
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
                "pot_before_street": read(pbs),
                "bomb_cn": read(bomb_cn), "bomb_en": read(bomb_en)}
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


def test_empty_seat_is_not_in_hand() -> None:
    """没坐满时那个虚线「+」空座不能被当成在场玩家。

    空座三块槽位都是空文本：名字 None、筹码 None、bet 空文本被记成 0.0。
    以前把 `bet is not None` 当在场证据，空座于是带着「有下注字段、但没有
    筹码」进质量闸，报 `stack:<空座>`，整场 100% 丢帧（2026-10-22 实盘）。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["topR"] = {"name": "", "chip": "", "bet": ""}
    s = state(rows)
    top = s.player("topR")
    check(not top.in_hand and top.status == "away",
          f"空座应判 away，实际 {top.to_debug()}")
    check(s.players_in_hand == 8, f"在场人数应为 8，实际 {s.players_in_hand}")
    check(not s.errors, f"空座不该触发质量闸：{s.errors}")


def test_dealer_left_mid_hand_is_dead_button_not_discard() -> None:
    """庄家中途退出（按钮留在空座）不能整帧丢弃。

    实盘 2026-10-23：9 人桌有人退出、按钮正好在他身上，之后每帧都报
    `positions:庄位 xxx 不在环上`，识别彻底停住。按死按钮继续排位置：
    按钮下家 SB，其余人的相对位置不变，只把退出的人从位置表里去掉。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["topR"] = {"name": "", "chip": "", "bet": ""}
    s = state(rows, dealer="topR")
    check(s.ok, f"死按钮帧不该被质量闸丢掉：{s.errors}")
    check(s.players_in_hand == 8, f"在场人数应为 8，实际 {s.players_in_hand}")
    top = s.player("topR")
    check(not top.in_hand and top.position is None,
          f"退出的庄家不该有位置：{top.to_debug()}")
    check(s.player("upR").position == "SB",
          f"按钮下家应是 SB，实际 {s.player('upR').position}")
    check(s.player("midR").position == "BB",
          f"再下家应是 BB，实际 {s.player('midR').position}")
    # dealer=topR 时这一手按 9 人桌发牌：upR=SB、midR=BB、lowR=UTG、
    # hero=UTG+1、lowL=UTG+2、midL=LJ、upL=HJ、topL=CO。死按钮只把
    # topR 的位置去掉，不能让其余人整体往前挪一位。
    check(s.player("hero").position == "UTG+1",
          f"hero 应仍是 UTG+1（不能按剩下人数重新编号），实际 "
          f"{s.player('hero').position}")
    check(s.player("lowL").position == "UTG+2",
          f"lowL 应仍是 UTG+2，实际 {s.player('lowL').position}")
    check(s.player("midL").position == "LJ",
          f"midL 应仍是 LJ，实际 {s.player('midL').position}")
    check(s.player("upL").position == "HJ",
          f"upL 应仍是 HJ，实际 {s.player('upL').position}")
    check(s.player("topL").position == "CO",
          f"按钮前最后一位应是 CO（这一手没有 BTN），实际 "
          f"{s.player('topL').position}")


def test_card_icon_decides_fold_without_ocr() -> None:
    """头像底角的手牌图标：有=还在池里，没有=弃牌。

    弃牌后头像被「弃牌」遮罩盖住，rec 经常读成「奔牌/水/风/O」；
    图标是颜色+几何判断，不受 OCR 抖动影响。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["topR"] = {"name": "雷霆战吉", "chip": "176BB"}
    s = build_state(make_frame(rows), PROFILE,
                    card_icons={"topR": False, "hero": False})
    check(s.player("topR").fold is True
          and s.player("topR").status == "folded",
          f"没图标应判弃牌：{s.player('topR').to_debug()}")
    check(s.player("hero").fold is not True,
          "hero 头像从不挂图标，不能据此判 hero 弃牌")
    check(s.player("lowL").fold is None,
          "没给图标的座位应保持未知")


def test_card_icon_overrides_fold_text() -> None:
    """看到图标就不能判弃牌：rec 把「跟注」误读成弃牌时以此兜底。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    s = build_state(make_frame(rows), PROFILE,
                    avatar_reads={"lowL": ("弃牌", 0.9)},
                    card_icons={"lowL": True})
    check(s.player("lowL").fold is False,
          f"有图标应判未弃牌：{s.player('lowL').to_debug()}")


def test_show_cards_do_not_clear_fold() -> None:
    """河底亮牌不代表没弃牌：弃牌判定沿用前面动作得出的结论。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    frame = make_frame(rows)
    # 头像区被两张明牌顶掉，rec 读到的是牌面（不是「弃牌」）
    reads = {"lowL": ("A♣ Q♥", 0.9)}
    folded = build_state(frame, PROFILE, avatar_reads=reads,
                         shown_seats={"lowL"}, prev_fold={"lowL": True})
    check(folded.player("lowL").fold is True,
          f"已弃牌的人亮牌后仍应判弃牌：{folded.player('lowL').to_debug()}")
    playing = build_state(frame, PROFILE, avatar_reads=reads,
                          shown_seats={"lowL"}, prev_fold={"lowL": False})
    check(playing.player("lowL").fold is False,
          f"未弃牌的人亮牌应保持未弃牌：{playing.player('lowL').to_debug()}")
    unknown = build_state(frame, PROFILE, avatar_reads=reads,
                          shown_seats={"lowL"})
    check(unknown.player("lowL").fold is None,
          f"没有历史时亮牌只算未知：{unknown.player('lowL').to_debug()}")
    check(unknown.player("lowL").in_hand,
          "亮牌座位仍然算在座/在池")


def test_empty_plus_seat_is_away() -> None:
    """虚线「+」空位：三块槽位全空 + 没有手牌图标 → 这个位置没人。

    即使 rec 在空位上读出了「弃牌」之类噪声，也不能把它当成玩家。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["topR"] = {"name": "", "chip": "", "bet": ""}
    s = build_state(make_frame(rows), PROFILE,
                    avatar_reads={"topR": ("弃牌", 0.9)},
                    card_icons={"topR": False})
    top = s.player("topR")
    check(not top.in_hand and top.status == "away",
          f"空位应判 away：{top.to_debug()}")
    check(top.fold is None, f"空位不该带弃牌标记：{top.to_debug()}")
    check(not s.errors, f"空位不该触发质量闸：{s.errors}")


def test_zero_stack_without_cards_is_not_in_hand() -> None:
    """筹码 0 + 没有手牌/亮牌/All-in → 人不在这一手。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowR"] = {"name": "Dragon333", "chip": "0", "bet": ""}
    s = build_state(make_frame(rows), PROFILE, card_icons={"lowR": False})
    lowr = s.player("lowR")
    check(not lowr.in_hand and lowr.status == "sitting_out",
          f"0 筹码无手牌应判坐出：{lowr.to_debug()}")

    # 亮牌 / All-in 是「人还在」的反证：不能被 0 筹码判成空位
    shown = build_state(make_frame(rows), PROFILE,
                        card_icons={"lowR": False}, shown_seats={"lowR"})
    check(shown.player("lowR").in_hand,
          f"亮牌的 0 筹码玩家仍应在池：{shown.player('lowR').to_debug()}")
    allin = build_state(make_frame(rows), PROFILE,
                        avatar_reads={"lowR": ("All in", 0.9)},
                        card_icons={"lowR": False})
    check(allin.player("lowR").in_hand and allin.player("lowR").all_in,
          f"All-in 的 0 筹码玩家仍应在池：{allin.player('lowR').to_debug()}")


def test_leaving_seat_does_not_keep_old_name() -> None:
    """人走后空座的筹码槽常带乱码：旧名字不能把座位永远钉在「在场」。

    实盘 2026-10-29：中途有人退出，空座的 chip 槽读成 `--`，NameTracker
    沿用上一帧的名字 → in_hand=True、stack=None，每帧报 `stack:<seat>`，
    整场丢帧；因为每帧都判「还在场」，vanished 也永远不会触发。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    tracker = NameTracker(confirm=3)
    first = build_state(make_frame(rows), PROFILE, name_tracker=tracker)
    check(first.player("lowL").name == "YKY", "基线应记住 lowL 的名字")

    gone_rows = {k: dict(v) for k, v in BASE.items()}
    gone_rows["lowL"] = {"name": "", "chip": "--", "bet": ""}
    for _ in range(4):
        s = build_state(make_frame(gone_rows), PROFILE, name_tracker=tracker)
        low = s.player("lowL")
        check(low.name is None,
              f"整座没人时不能沿用旧名字：{low.to_debug()}")
        check(not low.in_hand and low.status == "away",
              f"空座应判 away：{low.to_debug()}")
        check(not s.errors, f"空座乱码不该触发质量闸：{s.errors}")
        check(s.players_in_hand == 8, f"在场人数应为 8，实际 {s.players_in_hand}")
    check("lowL" not in tracker.current(), "空座应同时清掉名字缓存")

    # 换人坐下：新名字直接生效，不会先显示上一位的名字
    new_rows = {k: dict(v) for k, v in BASE.items()}
    new_rows["lowL"] = {"name": "新玩家", "chip": "100BB", "bet": ""}
    s = build_state(make_frame(new_rows), PROFILE, name_tracker=tracker)
    check(s.player("lowL").name == "新玩家",
          f"新玩家应直接用自己的名字：{s.player('lowL').to_debug()}")


def test_card_icons_on_real_samples() -> None:
    """真实样张回归：no_fold 全在池；fold 样张只有 4 家没弃牌。"""
    import cv2

    from poker_ocr.card_icon import read_card_icons

    cases = [
        (ROOT / "images" / "status" / "no_fold",
         {"lowL", "midL", "upL", "topL", "topR", "upR", "midR", "lowR"}),
        (ROOT / "images" / "position" / "fold",
         {"lowL", "midL", "upR", "midR"}),
    ]
    for path, expected in cases:
        if not path.exists():
            continue
        img = cv2.imread(str(path))
        if img is None:
            continue
        img = cv2.resize(img, PROFILE.reference_size)
        icons = read_card_icons(img, PROFILE)
        got = {sid for sid, ok in icons.items() if ok is True}
        check(got == expected, f"{path.name} 图标识别不对：{got} != {expected}")

    shown = ROOT / "images" / "card" / "show_card.jpg"
    if shown.exists():
        img = cv2.imread(str(shown))
        if img is not None:
            img = cv2.resize(img, PROFILE.reference_size)
            icons = read_card_icons(img, PROFILE)
            check(icons.get("midL") is None,
                  f"摊牌亮牌的头像应返回未知，实际 {icons.get('midL')}")


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
    """读成了乱码、但明显不是 UI 标签（没有汉字）→ 真的没读出来，整帧丢。"""
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["lowL"]["bet"] = "1OBB"          # 数字 0 被 rec 读成字母 O
    s = state(rows)
    check("bet:lowL" in s.errors, f"下注乱码应报 bet:lowL，实际 {s.errors}")


def test_bet_slot_ui_label_is_not_an_error() -> None:
    """bet 槽被中文 UI 文本占住时按 0 处理，不能整帧丢掉。

    实盘：WePoker 新加的「发发看 / 免费」促销按钮正好压在 hero 的 bet 槽上，
    OCR 读成「发发表」。以前这算「读到乱码」→ bet=None → `bet:hero` →
    整帧进质量闸丢弃；hero 一弃牌，下一手的牌面/建议就全被闸在外面，
    上一手也要等好几帧才能落库。下注 pill 只有数字 + BB，汉字只可能是别的
    UI 元素，本街就是没下注。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["hero"] = {"name": "gggamel", "chip": "86.5BB", "bet": "发发表"}
    s = state(rows)
    check("bet:hero" not in s.errors, f"UI 标签不该报 bet:hero，实际 {s.errors}")
    check(not s.errors, f"这一帧应当可用，实际 {s.errors}")
    hero = next(p for p in s.players if p.seat_id == "hero")
    check(hero.bet == 0.0, f"标签文本应落成本街 0，实际 {hero.bet}")
    check(hero.in_hand, "hero 仍在手里，不能因为 bet 槽被占就不算在场")
    check(any("标签文本" in n for n in hero.notes),
          f"应留一条 note 方便查 ROI 被占，实际 {hero.notes}")


def test_mojibake_bet_without_cjk_is_still_an_error() -> None:
    """没有汉字的乱码仍算「没读出来的下注」，不能悄悄当成 0。

    `lOBB`（数字被读成字母）很可能是真有下注，当成 0 会让 hero 的 to_call
    和底池都算错 —— 宁可丢一帧等下一帧，也不能给错建议。
    """
    rows = {k: dict(v) for k, v in BASE.items()}
    rows["hero"] = {"name": "gggamel", "chip": "86.5BB", "bet": "lOBB"}
    s = state(rows)
    check("bet:hero" in s.errors, f"无汉字的乱码仍应报错，实际 {s.errors}")


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


def test_ante_fallback_when_banner_loses_it() -> None:
    """横幅读成 `1/2(）` 时用 profile 的 default_ante 兜底，不能当没前注。"""
    frame = make_frame(blinds="1/2(）49939300:22:24")
    s = build_state(frame, PROFILE, ante_fallback=0.5)
    check(s.blinds is not None and s.blinds.ante == 0.5,
          f"应兜底前注 0.5：{s.blinds}")
    plain = build_state(make_frame(blinds="1/2(）49939300:22:24"), PROFILE)
    check(plain.blinds is not None and plain.blinds.ante is None,
          f"不传兜底时应保持 None：{plain.blinds}")


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


# ─────────────────────────── 爆炸底池横幅 ───────────────────────────

def test_bomb_banner_detection() -> None:
    """英文为主、中文兜底；动画里的半个词/低分不能误触发。"""
    # 实测 bomm.mp4：英文 0.86–0.95，OCR 偶尔多一个字母（BOMBPOTV）
    check(is_bomb_banner("", 0.0, "BOMB POT", 0.92), "BOMB POT 应命中")
    check(is_bomb_banner("", 0.0, "BOMBPOTV", 0.87), "多一个字母仍应命中")
    check(is_bomb_banner("", 0.0, "BOMBPOT", 0.6), "刚好到分数门槛应命中")
    check(not is_bomb_banner("", 0.0, "BOMBPOT", 0.59), "分数不足不该命中")
    # frame 005 的动画中间态：BOMBTON / 老记暴击都只是形近，必须拒绝
    check(not is_bomb_banner("老记暴击", 0.61, "BOMBTON", 0.82),
          "动画中间态不能误报")
    # 中文四个字读到三个（实测「幸起暴击」）
    check(is_bomb_banner("幸运暴击", 0.91, "", 0.0), "完整中文应命中")
    check(is_bomb_banner("幸起暴击", 0.7, "", 0.0), "四字命中三个应兜底")
    check(not is_bomb_banner("暴击", 0.95, "", 0.0), "两字不该命中")
    check(not is_bomb_banner("", 0.0, "", 0.0), "空读数不该命中")


def test_bomb_banner_in_state_and_signature() -> None:
    """横幅要写进 FrameState，并作为牌局事实进入变化指纹。"""
    normal = state()
    check(not normal.bomb_pot, "普通帧不该带爆炸标记")

    bomb = state(bomb_en="BOMB POT")
    check(bomb.bomb_pot, "读到 BOMB POT 应带爆炸标记")
    check("bomb_pot" in bomb.to_debug(), "to_debug 应输出 bomb_pot")

    bomb_cn = state(bomb_cn="幸运暴击")
    check(bomb_cn.bomb_pot, "中文横幅也应能确认")

    check(signature(bomb) != signature(normal),
          "爆炸底池横幅出现应触发一条状态记录")


def main() -> int:
    tests = [
        test_full_state,
        test_action_badge_keeps_sticky_name,
        test_idle_zero_player_is_not_in_hand,
        test_empty_seat_is_not_in_hand,
        test_card_icon_decides_fold_without_ocr,
        test_card_icon_overrides_fold_text,
        test_show_cards_do_not_clear_fold,
        test_empty_plus_seat_is_away,
        test_zero_stack_without_cards_is_not_in_hand,
        test_card_icons_on_real_samples,
        test_all_in_carried_after_bet_pill_disappears,
        test_lenient_chip_parse,
        test_all_in_badge_and_equity,
        test_equity_slot_marks_all_in,
        test_bare_equity_slot_number_is_not_all_in,
        test_buying_in_by_avatar,
        test_name_tracker_used_when_given,
        test_leaving_seat_does_not_keep_old_name,
        test_missing_dealer,
        test_pot_label_without_number,
        test_unreadable_bet_is_an_error,
        test_bet_slot_ui_label_is_not_an_error,
        test_mojibake_bet_without_cjk_is_still_an_error,
        test_unreadable_chip_is_an_error,
        test_unparseable_blinds_without_previous,
        test_blinds_carried_from_previous,
        test_ante_fallback_when_banner_loses_it,
        test_ante_inherited_when_parenthesis_lost,
        test_pbs_gt_pot_is_an_error,
        test_pbs_equal_pot_with_bets_is_an_error,
        test_bomb_banner_detection,
        test_bomb_banner_in_state_and_signature,
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
