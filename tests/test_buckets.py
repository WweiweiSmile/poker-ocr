#!/usr/bin/env python3
"""七维桶（`poker_ocr.buckets`）的表驱动测试。

    nv tests/test_buckets.py

票 50 的四条验收对应四组用例：

  1. 纯函数：同一输入永远同样的七个值（这里用表驱动逐维验证取值，并断言
     缺数据时退化而不是抛异常）；
  2. 缺省：维度缺失 → None / 「?」/ 「未知」，不崩；
  3. 词表集中：`replay-console/replay/specs.py` 只是 re-export 这里的词表；
  4. 评分卡输出不变：用 `tests/golden/` 里的固定夹具跑脚本，逐字对比。

期望值不是抄实现，而是对着设计 §3.3 的口径和 coach 的牌力分档写死的；
改这些值等于改桶的定义，应该先改设计文档。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import buckets as b  # noqa: E402
from poker_ocr.coach import (  # noqa: E402
    AIR, BOARD_PLAY, DRY, FLUSH_STRAIGHT_BOARD, MID_BOTTOM, OVERPAIR,
    PAIRED_BOARD, STRAIGHT_BOARD, STRONG_DRAW, STRONG_MADE, TPTK, TP_WEAK,
    WEAK_DRAW,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label}: 期望 {want!r}，实际 {got!r}")


# ─────────────────────────── 词表 ───────────────────────────

def test_vocab_is_complete() -> None:
    eq(b.BUCKET_ORDER, ("street", "position_bucket", "hand_tier",
                        "board_texture", "pot_type", "facing",
                        "opponent_profile"), "BUCKET_ORDER")
    eq(tuple(b.BUCKET_VOCAB), b.BUCKET_ORDER, "BUCKET_VOCAB 的键与顺序")
    # 桶里存英文键（设计 §3.3），中文只在词表里 —— 文案改了不会让老 spec 失配。
    eq(set(b.HAND_TIERS), {"STRONG_MADE", "OVERPAIR", "TPTK", "TP_WEAK",
                           "MID_BOTTOM", "STRONG_DRAW", "WEAK_DRAW", "AIR",
                           "BOARD_PLAY"}, "HAND_TIERS 的键 == 设计 §3.3")
    # 中文文案必须对齐 coach 的牌力常量（两个词表不能各说各的）
    eq(set(b.HAND_TIERS.values()), {STRONG_MADE, OVERPAIR, TPTK, TP_WEAK,
                                    MID_BOTTOM, STRONG_DRAW, WEAK_DRAW, AIR,
                                    BOARD_PLAY},
       "HAND_TIERS 的中文 == coach 牌力常量")
    eq(sorted(b.BOARD_TEXTURES), sorted([DRY, "花面", STRAIGHT_BOARD,
                                        FLUSH_STRAIGHT_BOARD, PAIRED_BOARD]),
       "BOARD_TEXTURES 的键 == coach 牌面质地")
    for dimension, (title, vocab) in b.BUCKET_VOCAB.items():
        check(bool(title), f"{dimension} 缺中文名")
        check(vocab, f"{dimension} 词表为空")


def test_vocab_reused_by_specs() -> None:
    """`replay/specs.py` 不许再抄一份词表 —— 必须是同一批对象。"""
    sys.path.insert(0, str(ROOT / "replay-console"))
    try:
        from replay import specs
    finally:
        sys.path.pop(0)
    for name in ("BUCKET_ORDER", "BUCKET_VOCAB", "STREETS", "POSITION_BUCKETS",
                 "HAND_TIERS", "BOARD_TEXTURES", "POT_TYPES", "FACING",
                 "OPPONENT_PROFILES"):
        check(getattr(specs, name) is getattr(b, name),
              f"replay.specs.{name} 不是 poker_ocr.buckets.{name} 的同一个对象")
    eq(specs.bucket_label({"hand_tier": ["TPTK"], "facing": [0]}),
       "顶对强踢 · 未面对下注", "bucket_label")


# ───────────────────── 七个维度各自的取值 ─────────────────────

def test_street() -> None:
    for street in ("preflop", "flop", "turn", "river"):
        eq(b.street_of(street), street, f"street_of({street})")
    for bad in (None, "", "FLOP", "showdown", 3):
        eq(b.street_of(bad), None, f"street_of({bad!r})")


def test_position_bucket() -> None:
    table = {
        "UTG": "EP", "utg+1": "EP", " UTG+2 ": "EP",
        "LJ": "MP", "HJ": "MP", "CO": "LP", "BTN": "LP",
        "SB": "盲注", "BB": "盲注",
    }
    for position, want in table.items():
        eq(b.position_bucket_of(position), want, f"position_bucket_of({position!r})")
    for missing in (None, "", "   ", "MP3", "button"):
        eq(b.position_bucket_of(missing), "?", f"position_bucket_of({missing!r}) 缺省")


def test_facing() -> None:
    eq(b.facing_of(0), 0, "facing_of(0)")
    eq(b.facing_of(None), 0, "facing_of(None)")
    eq(b.facing_of(0.0), 0, "facing_of(0.0)")
    eq(b.facing_of(2.5), 1, "facing_of(2.5)")
    eq(b.facing_of("3"), 1, "facing_of('3')")
    eq(b.facing_of("oops"), 0, "facing_of('oops') 坏值不崩")


def test_pot_type() -> None:
    table = [
        ([], "bomb"),                                   # 无翻前动作 = 炸弹池
        (["fold"], "unknown"),
        (["call", "call"], "limped"),
        (["raise", "call"], "single"),
        (["bet", "raise", "call"], "3bet+"),
        (["raise", "raise"], "3bet+"),
        (["allin"], "unknown"),                         # 历史口径：allin 不计加注
    ]
    for actions, want in table:
        eq(b.pot_type_of(actions), want, f"pot_type_of({actions})")
    # 也吃 dict（快照动作）与对象（ActionRecord）
    eq(b.pot_type_of([{"action": "raise"}, {"action": "raise"}]), "3bet+",
       "pot_type_of(dict 动作)")
    eq(b.pot_type_of([SimpleNamespace(action="call")]), "limped",
       "pot_type_of(对象动作)")


def test_hand_tier() -> None:
    table = [
        ("AsAh", "Ks7d2c", OVERPAIR),        # 超对
        ("KsQh", "Ks7d2c", TPTK),            # 顶对强踢（Q=10）
        ("Ks5h", "Ks7d2c", TP_WEAK),         # 顶对弱踢
        ("5s5h", "Ks7d2c", MID_BOTTOM),      # 中底对
        ("8s8h", "8d5c2s", STRONG_MADE),     # 三条
        ("QhJd", "9s8c2d", WEAK_DRAW),       # 卡顺 4 outs
        ("QsJs", "9s8s2c", STRONG_DRAW),     # 花听 + 卡顺
        ("2c3d", "AhKhQhJhTh", BOARD_PLAY),  # 打牌面
        ("2c3d", "Ks9h5d", AIR),             # 空气
        # 打牌面 + 听牌：coach 给 WEAK_DRAW，评分卡的 5 档给 mid —— 真实分歧
        ("QhJd", "8d5c9s5h", WEAK_DRAW),
    ]
    for hole, board, want in table:
        eq(b.hand_tier_of(hole, board), want, f"hand_tier_of({hole}, {board})")
    # 缺省：底牌不完整 / 牌面不足 3 张 → None（通配），不抛异常
    eq(b.hand_tier_of("As", "Ks7d2c"), None, "hand_tier_of 单张底牌")
    eq(b.hand_tier_of("AsAh", "Ks7d"), None, "hand_tier_of 两张牌面")
    eq(b.hand_tier_of([], []), None, "hand_tier_of 空输入")


def test_board_texture() -> None:
    table = {
        "Ks7d2c": DRY, "Ks6d6c": PAIRED_BOARD, "9s8d4c": STRAIGHT_BOARD,
        "KhQhJc": FLUSH_STRAIGHT_BOARD, "KcQcJc": FLUSH_STRAIGHT_BOARD,
    }
    for board, want in table.items():
        eq(b.texture_of(board), want, f"texture_of({board})")
    eq(b.texture_of("Ks7d"), None, "texture_of 两张牌面")
    eq(b.texture_of([]), None, "texture_of 空牌面")


def test_opponent_profile() -> None:
    reads = {
        "BTN": SimpleNamespace(archetype="station"),
        "CO": "lag",
        "BB": SimpleNamespace(archetype="maniac"),
    }
    eq(b.opponent_profile_of(reads, position="BTN"), "松弱", "画像 station")
    eq(b.opponent_profile_of(reads, position="CO"), "松凶", "画像字符串 lag")
    eq(b.opponent_profile_of(reads, position="BB"), "松凶", "maniac 归松凶")
    eq(b.opponent_profile_of(reads, position="SB"), "未知", "没有读牌")
    eq(b.opponent_profile_of(None, position="BTN"), "未知", "reads 为 None")
    eq(b.opponent_profile_of(reads, seat="CO"), "松凶", "按座位找")
    eq(b.opponent_profile_of({"xx": "tag"}, position="BTN", name="xx"), "紧凶",
       "按名字找")


def test_cards_of() -> None:
    want = [(12, "h"), (11, "d")]
    eq(b.cards_of(["Ah", "Kd"]), want, "cards_of 列表")
    eq(b.cards_of("AhKd"), want, "cards_of 一个字符串")
    eq(b.cards_of([(12, "h"), (11, "d")]), want, "cards_of 元组")
    eq(b.cards_of(["Ah", "zz"]), [], "cards_of 坏牌 fail open")
    eq(b.cards_of(None), [], "cards_of None")


def test_last_aggressor() -> None:
    streets = [
        {"street": "preflop", "actions": [
            {"action": "raise", "position": "UTG", "name": "a", "seatId": "s1"},
            {"action": "call", "position": "BTN", "name": "h", "seatId": "s2",
             "isHero": True},
        ]},
        {"street": "flop", "actions": [
            {"action": "bet", "position": "UTG", "name": "a", "seatId": "s1"},
            {"action": "raise", "position": "BTN", "name": "h", "seatId": "s2",
             "isHero": True},
        ]},
    ]
    aggressor = b.last_aggressor(streets, "flop")
    check(aggressor is not None, "last_aggressor 找不到人")
    if aggressor:
        eq(aggressor["position"], "UTG", "hero 自己的加注不算「面对谁」")
    eq(b.last_aggressor(streets, "turn"), None, "没有动作的街")


# ─────────────────── 决策点 → 七个桶（单一入口） ───────────────────

SNAPSHOT = {
    "street": "flop",
    "board": ["Ks", "7d", "2c"],
    "heroCards": ["As", "Ah"],
    "heroPosition": "BTN",
    "toCall": 2.0,
    "bombPot": False,
    "streets": [
        {"street": "preflop", "actions": [
            {"action": "raise", "position": "UTG", "name": "a", "seatId": "s1"},
            {"action": "call", "position": "BTN", "name": "h", "seatId": "s2",
             "isHero": True},
        ]},
        {"street": "flop", "actions": [
            {"action": "bet", "position": "UTG", "name": "a", "seatId": "s1"},
        ]},
    ],
}

EXPECTED = {
    "street": "flop",
    "position_bucket": "LP",
    "hand_tier": OVERPAIR,
    "board_texture": DRY,
    "pot_type": "single",
    "facing": 1,
    "opponent_profile": "未知",
}


def test_bucket_of_camel_case() -> None:
    eq(b.bucket_of(SNAPSHOT, opponent_reads={"s1": "nit"}),
       dict(EXPECTED, opponent_profile="紧弱"), "bucket_of(snapshot_to_dict)")
    eq(b.bucket_of(SNAPSHOT), EXPECTED, "bucket_of 没有读牌")


def test_bucket_of_snake_case_and_decision_point() -> None:
    """运行态的 DecisionPoint / HandSnapshot 走同一条路径。"""
    snap = SimpleNamespace(
        street="flop", board=["Ks", "7d", "2c"], hero_cards=["As", "Ah"],
        hero_position="BTN", to_call=2.0, bomb_pot=False,
        streets=[SimpleNamespace(street="preflop", actions=[
            SimpleNamespace(action="raise", position="UTG", name="a",
                            seat_id="s1", is_hero=False),
            SimpleNamespace(action="call", position="BTN", name="h",
                            seat_id="s2", is_hero=True),
        ])],
    )
    eq(b.bucket_of(SimpleNamespace(snap=snap)), EXPECTED, "bucket_of(DecisionPoint)")
    eq(b.bucket_of(snap), EXPECTED, "bucket_of(HandSnapshot)")


def test_bucket_of_bomb_pot_and_empty() -> None:
    bomb = dict(SNAPSHOT, bombPot=True, toCall=0.0, board=["Ks", "7d", "2c"])
    bucket = b.bucket_of(bomb)
    eq(bucket["pot_type"], "bomb", "炸弹池覆盖池型")
    eq(bucket["facing"], 0, "炸弹池未面对下注")

    # 什么都没有：不崩，缺的维度退化成 None / 「?」/「未知」（通配）
    empty = b.bucket_of({})
    eq(empty, {"street": None, "position_bucket": "?", "hand_tier": None,
               "board_texture": None, "pot_type": "bomb", "facing": 0,
               "opponent_profile": "未知"}, "bucket_of({}) 全缺省")
    eq(list(empty), list(b.BUCKET_ORDER), "bucket_of 的键顺序固定")


# ───────────── 评分卡的历史 5 档（coarse_strength，勿与 hand_tier 混用） ─────────────

def test_coarse_strength() -> None:
    table = [
        ("AsAh", "Ks7d2c", "mid"),          # 超对在旧口径里是 mid，不是 strong
        ("KsQh", "Ks7d2c", "top_pair"),
        ("8s8h", "8d5c2s", "strong"),
        ("QhJd", "9s8c2d", "draw"),
        ("2c3d", "Ks9h5d", "air"),
        ("2c3d", "AhKhQhJhTh", "air"),      # 打牌面 → 空气
        ("QhJd", "8d5c9s5h", "mid"),        # 与 hand_tier 的 WEAK_DRAW 分歧
    ]
    for hole, board, want in table:
        eq(b.coarse_strength(hole, board), want, f"coarse_strength({hole}, {board})")
    eq(b.coarse_strength("As", "Ks7d2c"), "air", "coarse_strength 单张底牌")


# ───────────────────── 评分卡输出不变（golden） ─────────────────────

GOLDEN = ROOT / "tests" / "golden"
FIXTURE = GOLDEN / "advice_scorecard.live.jsonl"
EXPECTED_MD = GOLDEN / "advice_scorecard.md"


def test_scorecard_golden() -> None:
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "advice_scorecard.py"),
         "--log", str(FIXTURE)],
        cwd=str(ROOT), capture_output=True, text=True, check=False)
    check(out.returncode == 0, f"评分卡脚本退出码 {out.returncode}：{out.stderr[-400:]}")
    if out.returncode != 0:
        return
    want = EXPECTED_MD.read_text()
    check(out.stdout == want,
          "评分卡输出变了（改口径要显式重跑 tools/advice_scorecard.py "
          f"--log {FIXTURE.relative_to(ROOT)} 覆盖 {EXPECTED_MD.relative_to(ROOT)}）")


def main() -> int:
    tests = [
        test_vocab_is_complete,
        test_vocab_reused_by_specs,
        test_street,
        test_position_bucket,
        test_facing,
        test_pot_type,
        test_hand_tier,
        test_board_texture,
        test_opponent_profile,
        test_cards_of,
        test_last_aggressor,
        test_bucket_of_camel_case,
        test_bucket_of_snake_case_and_decision_point,
        test_bucket_of_bomb_pot_and_empty,
        test_coarse_strength,
        test_scorecard_golden,
    ]
    for t in tests:
        before = len(FAILS)
        t()
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 七维桶测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
