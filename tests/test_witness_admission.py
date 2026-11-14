#!/usr/bin/env python3
"""摊牌证据准入（#12 族 / #19 闸门 / 规范 hand 键）。

    nv tests/test_witness_admission.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.witness import admission as A  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def actions(*rows):
    return [{"seat_id": seat, "action": action} for seat, action in rows]


def make_hand(**over):
    """一手默认合法的 9-max 手：河牌三家过牌，upL 亮 Ad7s。"""
    hand = {
        "type": "hand",
        "ts": "2026-11-01T20:00:00+08:00",
        "hand_id": "20261101-abcdef1234",
        "uid": "20261101-abcdef1234",
        "table_signature": "20261101-deadbeefcafe",
        "hero_seat_id": "hero",
        "hero_cards": ["Jc", "8s"],
        "hero_position": "LJ",
        "board": ["6c", "2h", "As", "5c", "9d"],
        "shows": [{"seat_id": "upL", "name": "书麻头了wlr", "position": "BTN",
                   "cards": ["7s", "Ad"]}],
        "seat_meta": [
            {"seat_id": "hero", "name": "h", "position": "LJ", "stack": 100},
            {"seat_id": "topL", "name": "t", "position": "SB", "stack": 100},
            {"seat_id": "upL", "name": "u", "position": "BTN", "stack": 100},
        ],
        "streets": [
            {"street": "flop", "actions": actions(("hero", "check"))},
            {"street": "turn", "actions": actions(("hero", "check"), ("upL", "check"))},
            {"street": "river", "actions": actions(
                ("hero", "check"), ("topL", "check"), ("upL", "check"))},
        ],
        "street_active": {"flop": ["hero", "topL", "upL"],
                          "turn": ["hero", "topL", "upL"],
                          "river": ["hero", "topL", "upL"]},
    }
    hand.update(over)
    return hand


# ------------------------------------------------------------------ hand 键


def test_canonical_hand_key() -> None:
    check(A.canonical_hand_key({"uid": "state:live.states.jsonl:s15-20261101-228c7832f9"})
          == "20261101-228c7832f9", "uid 尾部 date-digest 优先")
    check(A.canonical_hand_key({"uid": "x", "hand_id": "20261030-1e5ec30022"})
          == "20261030-1e5ec30022", "hand_id 本身是 date-digest")
    check(A.canonical_hand_key({"uid": "state:live:x:s03-h41", "hand_id": "h41"})
          == "state:live:x:s03-h41", "旧式 uid 原样保留")
    check(A.canonical_hand_key({"hand_id": "h41", "session": 3}) == "s03-h41",
          "session + hand_id 兜底")
    check(A.canonical_hand_key({"hand_id": "h41", "session": "7"}) == "s07-h41",
          "字符串 session 也能拼")
    check(A.canonical_hand_key({"uid": "", "hand_id": ""}) == "", "空手牌 → 空键")


# ------------------------------------------------------------------ 12 族


def test_family_table() -> None:
    cases = [
        ("weak_ace_top_pair", ["Ad", "7s"], ["As", "Kh", "5c", "2d", "9h"]),
        ("strong_ace_top_pair", ["Ad", "Jh"], ["As", "7c", "3d", "2h", "9s"]),
        ("non_ace_top_pair", ["Kd", "Qh"], ["Ks", "7c", "3d", "2h", "9s"]),
        ("overpair", ["Qd", "Qh"], ["9s", "7c", "3d", "2h", "Js"]),
        ("middle_pair", ["9d", "9h"], ["As", "7c", "3d", "2h", "Js"]),
        ("small_pair", ["5d", "5h"], ["As", "7c", "3d", "2h", "Js"]),
        ("playing_board", ["Qd", "Jh"], ["Ks", "Kd", "3c", "2h", "9s"]),
        ("air_high_card", ["Qd", "3h"], ["2s", "5c", "8d", "9h", "Ks"]),
        ("suited_connector", ["8d", "7d"], ["2s", "5c", "Kh", "Ad", "3s"]),
        ("busted_draw", ["Qd", "Jd"], ["2d", "5d", "8h", "9s", "Ks"]),
        ("two_pair_plus", ["9h", "8s"], ["9c", "8d", "As", "5c", "2h"]),
        ("set", ["6h", "6s"], ["6c", "2h", "As", "5c", "9d"]),
    ]
    for want, hole, board in cases:
        got = A.classify_family(hole, board)
        check(got == want, f"{want}: hole={hole} board={board} → {got}")
        check(want in A.FAMILIES, f"{want} 在 12 族表里")


def test_family_edge_cases() -> None:
    check(A.classify_family(["Ad"], ["As", "Kh", "5c"]) is None, "单张不给族")
    check(A.classify_family(["Ad", "7s"], ["As", "Kh"]) is None, "牌面不足 3 张不给族")
    check(A.classify_family(["Ad", "10s"], ["As", "Kh", "5c", "2d", "9h"])
          == "strong_ace_top_pair", "10 以上的踢脚算强踢")
    check(A.classify_family(["6h", "10s"], ["6c", "2h", "As", "5c", "9d"]) == "small_pair",
          "底对（6 对 6，非第二对）算小对子")
    check(A.classify_family(["9h", "8s"], ["9c", "8d", "As", "5c", "2h"]) == "two_pair_plus",
          "两对归两对+")
    check(A.classify_family(["9h", "9d"], ["9c", "8d", "As", "5c", "2h"]) == "set",
          "口袋对中三条归暗三")


# ------------------------------------------------------------------ 准入


def test_fixture_hand_row() -> None:
    path = ROOT / "data/hand_outbox/sent/20261030-1e5ec30022.json"
    if not path.exists():
        return
    hand = json.loads(path.read_text(encoding="utf-8"))
    adm = A.admit(hand, "upL")
    check(adm is not None and adm.status == A.STATUS_ADMITTED, "upL 入池")
    check(adm is not None and adm.show_kind == A.SHOW_SHOWDOWN, "upL 摊牌亮牌")
    check(adm is not None and adm.family == "weak_ace_top_pair", "upL 弱A顶对")
    check(adm is not None and adm.winner.status == A.WINNER_DERIVED
          and adm.winner.seat == "upL", "全桌底牌可复原 → derived winner")
    row = A.to_row(hand, "upL")
    check(row is not None, "upL 成行")
    if row:
        check(row["street"] == "river" and row["action"] == "check", "河牌过牌")
        check(row["position"] == "ip", "BTN 相对 LJ 是 IP")
        check(row["player"] == "书麻头了wlr", "玩家名走 normalize_name")
        check(row["hand"] == "20261030-1e5ec30022", "规范 hand 键")
        check(row["source"] == "outbox", "来源 outbox")
        check(list(row) == list(A._ROW_FIELDS), "行字段顺序与契约一致")
        check(isinstance(row["cards"], list) and len(row["cards"]) == 2, "两张底牌")
    top = A.to_row(hand, "topL")
    check(top is not None and top["family"] in A.FAMILIES, "topL 也有行且族合法")


def test_hero_and_unknown_position() -> None:
    hand = make_hand(shows=[{"seat_id": "hero", "name": "我", "position": "LJ",
                             "cards": ["Jc", "8s"]}])
    check(A.admit(hand, "hero") is not None, "hero 亮牌也能准入")
    check(A.to_row(hand, "hero") is None, "但 hero 座位不投影（§4.4）")

    no_pos = make_hand(
        shows=[{"seat_id": "upL", "name": "书麻头了wlr", "cards": ["7s", "Ad"]}],
        seat_meta=[{"seat_id": "hero", "name": "h", "position": "LJ", "stack": 100}],
    )
    check(A.position_of(no_pos, "upL") is None, "缺位置 → None")
    check(A.to_row(no_pos, "upL") is None, "缺位置不成行")

    no_name = make_hand(shows=[{"seat_id": "upL", "name": "  ", "position": "BTN",
                                "cards": ["7s", "Ad"]}])
    check(A.to_row(no_name, "upL") is None, "玩家名空不成行")


def test_action_mapping() -> None:
    table = {
        "bet": A.ACTION_BET,
        "raise": A.ACTION_FACING_BET,
        "call": A.ACTION_FACING_BET,
        "check": A.ACTION_CHECK,
        "fold": None,
    }
    for raw, want in table.items():
        hand = make_hand(streets=[
            {"street": "turn", "actions": actions(("upL", "check"))},
            {"street": "river", "actions": actions(("upL", raw))},
        ])
        got = A.action_of(hand, "upL", "river")
        check(got == want, f"河牌最后动作 {raw} → {got}")
    hand = make_hand(streets=[{"street": "river", "actions": []}])
    check(A.action_of(hand, "upL", "river") == A.ACTION_UNKNOWN, "没有动作 → unknown")


def test_reject_reasons() -> None:
    folded = make_hand(streets=[
        {"street": "turn", "actions": actions(("upL", "check"))},
        {"street": "river", "actions": actions(("upL", "fold"))},
    ])
    adm = A.admit(folded, "upL")
    check(adm is not None and adm.status == A.STATUS_REJECTED
          and adm.reason == A.REASON_FOLD_INFERRED, "最后动作 fold → fold_inferred")
    check(A.to_row(folded, "upL") is None, "rejected 不成行")

    inactive = make_hand(street_active={"flop": ["hero"], "turn": ["hero"], "river": ["hero"]})
    adm = A.admit(inactive, "upL")
    check(adm is not None and adm.reason == A.REASON_NOT_IN_STREET_ACTIVE,
          "不在最后街活跃集 → not_in_street_active")

    short = make_hand(board=["6c", "2h"])
    adm = A.admit(short, "upL")
    check(adm is not None and adm.reason == A.REASON_BOARD_SHORT, "牌面不足 → board_short")

    weird = make_hand(shows=[{"seat_id": "upL", "name": "x", "position": "BTN",
                              "cards": ["7s", "Ad", "9c"]}])
    adm = A.admit(weird, "upL")
    check(adm is not None and adm.reason == A.REASON_HAND_MISMATCH, "三张底牌 → hand_mismatch")


def test_flop_only_hand_skipped() -> None:
    hand = make_hand(
        board=["6c", "2h", "As"],
        streets=[{"street": "flop", "actions": actions(("upL", "check"))}],
        street_active={"flop": ["hero", "upL"]},
    )
    check(A.admit(hand, "upL") is None, "翻牌结束的手不投影（只 turn/river）")
    check(A.to_row(hand, "upL") is None, "翻牌手不成行")


def test_single_and_voluntary() -> None:
    single = make_hand(shows=[{"seat_id": "upL", "name": "书麻头了wlr", "position": "BTN",
                               "cards": ["7s"]}])
    adm = A.admit(single, "upL")
    check(adm is not None and adm.status == A.STATUS_TAGGED, "单张是 tagged")
    check(adm is not None and adm.show_kind == A.SHOW_SINGLE, "单张 show_kind=single")
    check(adm is not None and adm.family is None, "单张没有族")
    check(adm is not None and adm.winner.status == A.WINNER_SINGLE_SHOW,
          "单张 winner=single_show")
    row = A.to_row(single, "upL")
    check(row is not None and len(row["cards"]) == 1, "单张仍入 rows（v1 不渲染）")

    voluntary = make_hand(streets=[
        {"street": "turn", "actions": actions(("upL", "check"))},
        {"street": "river", "actions": actions(("upL", "bet"), ("hero", "fold"))},
    ], street_active={"flop": ["hero", "upL"], "turn": ["hero", "upL"], "river": ["upL"]})
    adm = A.admit(voluntary, "upL")
    check(adm is not None and adm.status == A.STATUS_TAGGED, "河牌下注无人跟 → tagged")
    check(adm is not None and adm.show_kind == A.SHOW_VOLUNTARY, "show_kind=voluntary")
    check(adm is not None and adm.family == "weak_ace_top_pair", "主动秀仍有族")
    check(adm is not None and adm.winner.status == A.WINNER_UNKNOWN,
          "主动秀比不出 winner（别的底牌不可见）")


def test_winner_needs_full_information() -> None:
    hand = make_hand(street_active={"flop": ["hero"], "turn": ["hero", "upL"],
                                    "river": ["hero", "upL", "topL"]})
    adm = A.admit(hand, "upL")
    check(adm is not None and adm.winner.status == A.WINNER_UNKNOWN,
          "topL 没亮牌 → 比不出 winner，只能 unknown")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)
             and getattr(value, "__module__", "") == __name__]
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
    print("\n✅ 摊牌证据准入全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
