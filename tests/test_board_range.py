#!/usr/bin/env python3
"""实时牌面范围：combo 级分类、连接度打分、动作 mode 与宽度。

    .venv/bin/python tests/test_board_range.py

验收基准来自 docs/实时牌面范围与手牌强度-设计文档.md：
  · AsKs4d 上 25/34/5s6s/7s8s/7h8h/QJ/22/44 的分类；
  · 纯牌面 44.6%、∩ BTN 24.5%、∩ UTG 9.4%；
  · 7s8s 必须包含（不能被“78 无关”误杀），7h8h 必须排除。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import board_range, ranges  # noqa: E402
from poker_ocr.coach import parse_cards  # noqa: E402

FAILS: list[str] = []
BOARD = "AsKs4d"


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def combo(text: str):
    return ranges.canon_combo(*parse_cards(text))


def has(rng, text: str) -> bool:
    return rng is not None and combo(text) in rng.combo_weights


def test_as_ks_4d_classification() -> None:
    expected = {
        # 手牌: (score, flush_draw, pair_kind)
        "5s6s": (0.90, True, "none"),
        "7s8s": (0.90, True, "none"),
        "7h8h": (0.10, False, "none"),
        "2h5h": (0.35, False, "none"),
        "3h4h": (0.70, False, "bottom"),
        "2c2h": (0.55, False, "underpair"),
        "4c4h": (1.40, False, "set"),
        "AcAd": (1.40, False, "set"),
        "QhJh": (0.35, False, "none"),
        "AhQh": (0.90, False, "top"),
        "AcKc": (1.25, False, "two_pair"),
        "7h2c": (0.00, False, "none"),
    }
    for text, (want_score, want_fd, want_pair) in expected.items():
        feat = board_range.classify(combo(text), BOARD)
        got = board_range.score(feat)
        check(abs(got - want_score) < 1e-9,
              f"{text} 分数应 {want_score}，实际 {got}（{feat}）")
        check(feat.flush_draw == want_fd,
              f"{text} flush_draw 应 {want_fd}，实际 {feat.flush_draw}")
        check(feat.pair_kind == want_pair,
              f"{text} pair_kind 应 {want_pair}，实际 {feat.pair_kind}")


def test_straight_and_backdoor_details() -> None:
    f25 = board_range.classify(combo("2h5h"), BOARD)
    check(f25.straight_outs == 1, f"25 是卡顺（1 个点数），实际 {f25.straight_outs}")
    f7h8h = board_range.classify(combo("7h8h"), BOARD)
    check(f7h8h.backdoor_straight, "7h8h 应有后门顺")
    check(not f7h8h.flush_draw, "7h8h 不是花听")
    # turn/river 不再给后门分
    turn = "AsKs4d2c"
    f_turn = board_range.classify(combo("7h8h"), turn)
    check(not f_turn.backdoor_straight, "turn 不应再有后门顺")
    check(board_range.score(f_turn) == 0.0, f"turn 的 7h8h 应是空气，实际 {board_range.score(f_turn)}")
    # 轮子顺：A2345 的顶张是 5
    check(board_range.straight_high((1 << 12) | (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)) == 3,
          "A2345 的顺子顶张应是 5（bit 3）")


def test_action_modes() -> None:
    call = board_range.build(BOARD, "call", None)
    bet = board_range.build(BOARD, "bet", None)
    assert call and bet
    checks = [
        ("2h5h", True, False),
        ("3h4h", True, False),
        ("5s6s", True, True),
        ("7s8s", True, True),
        ("7h8h", False, False),
        ("QhJh", True, False),
        ("2c2h", True, False),
        ("4c4h", True, True),
        ("AcAd", True, True),
        ("7h2c", False, False),
    ]
    for text, in_call, in_bet in checks:
        check(has(call, text) == in_call,
              f"{text} 在 call 范围应 {in_call}，实际 {has(call, text)}")
        check(has(bet, text) == in_bet,
              f"{text} 在 bet 范围应 {in_bet}，实际 {has(bet, text)}")


def test_widths_match_design() -> None:
    pure = board_range.build(BOARD, "call", None)
    btn = board_range.build(BOARD, "call", ranges.get("RFI_BTN"))
    utg = board_range.build(BOARD, "call", ranges.get("RFI_UTG"))
    assert pure and btn and utg
    check(abs(pure.width_pct - 44.6) < 0.8,
          f"纯牌面宽度应约 44.6%，实际 {pure.width_pct:.1f}%")
    check(abs(btn.width_pct - 24.5) < 0.8,
          f"∩ BTN 宽度应约 24.5%，实际 {btn.width_pct:.1f}%")
    check(abs(utg.width_pct - 9.4) < 0.8,
          f"∩ UTG 宽度应约 9.4%，实际 {utg.width_pct:.1f}%")
    check(pure.width_pct > btn.width_pct > utg.width_pct, "先验越紧，范围应越窄")


def test_check_mode_keeps_preflop_range() -> None:
    prior = ranges.get("RFI_BTN")
    assert prior is not None
    got = board_range.build(BOARD, "check", prior)
    check(got is prior, "check mode 应直接返回翻前先验（不按牌面筛）")
    uniform = board_range.build(BOARD, "check", None)
    assert uniform is not None
    check(uniform.combo_count == 1326, "没有先验时 check 也应是任意两张")
    unknown = board_range.build(BOARD, "unknown", prior)
    assert unknown is not None
    check(unknown.id == "board:unknown:uniform",
          "动作读不到时应降级为纯牌面范围（board-only）")


def test_invalid_board_and_cache() -> None:
    check(board_range.build("AsKs", "call", None) is None, "不足 3 张应返回 None")
    check(board_range.build("AsAs4d", "call", None) is None, "重复牌应返回 None")
    check(board_range.build("AsKs4d", "call", None, min_weight=2.0) is None,
          "阈值高到没有 combo 时应返回 None")
    t0 = time.perf_counter()
    first = board_range.build("Ks7d2c", "call", None)
    cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    second = board_range.build("Ks7d2c", "call", None)
    warm = (time.perf_counter() - t0) * 1000
    check(first is second, "同 board/mode/prior 应命中缓存")
    check(cold < 200, f"冷启动过慢：{cold:.1f}ms")
    check(warm < 10, f"缓存后应 <10ms，实际 {warm:.1f}ms")


def main() -> int:
    tests = [
        test_as_ks_4d_classification,
        test_straight_and_backdoor_details,
        test_action_modes,
        test_widths_match_design,
        test_check_mode_keeps_preflop_range,
        test_invalid_board_and_cache,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print("\n❌ " + "\n❌ ".join(FAILS))
        return 1
    print("\n✅ 实时牌面范围全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
