#!/usr/bin/env python3
"""实时牌力解析：牌力档位 + 权益档次 + 价值/听牌判定 + 一行结论渲染。

    .venv/bin/python tests/test_hand_strength.py

纯判定层单测：构造 HandVsRanges 数字（不跑 Monte Carlo），验证 size / equity_tier /
kind / summary_en 与设计文档 §4 / §7 一致。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import hand_strength as hs  # noqa: E402
from poker_ocr.coach import (  # noqa: E402
    AIR, BOARD_PLAY, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK, WEAK_DRAW,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def sit_stub(tier, label_en="", *, nuts=False, behind_any=0.0, equity=0.0,
             n=1, made_name=""):
    """构造一个最小 sit + hand_ranges，只喂判定层需要的字段。"""
    hr = SimpleNamespace(opponents=tuple(range(n)), behind_any=behind_any,
                         equity=equity)
    gt = SimpleNamespace(hero_is_nuts=nuts,
                         made=SimpleNamespace(name=made_name or label_en))
    return SimpleNamespace(hand_ranges=hr, hand_tier=tier,
                           hand_label_en=label_en, ground_truth=gt)


def test_two_pair_big_strong_value() -> None:
    """两对 + 低 behind + 高 equity → big / strong / value。"""
    r = hs.compute_hand_strength(sit_stub(
        STRONG_MADE, "Two pair", behind_any=0.04, equity=0.80, n=3))
    check(r is not None, "应产出结论")
    check(r.size == hs.SIZE_BIG, f"两对非坚果应为 big，实际 {r.size}")
    check(r.equity_tier == hs.TIER_STRONG, f"3.2x 平均线应为 strong，实际 {r.equity_tier}")
    check(r.kind == hs.KIND_VALUE, f"应为 value，实际 {r.kind}")
    check(r.n == 3 and abs(r.avg - 0.25) < 1e-9, "n=3 平均线应为 0.25")
    check("BIG Two pair" in r.summary_en(), f"summary 应以 BIG Two pair 开头：{r.summary_en()}")
    check(r.summary_en().endswith("value hand."), "summary 结尾应是 value hand.")


def test_two_pair_nuts_very_big() -> None:
    r = hs.compute_hand_strength(sit_stub(
        STRONG_MADE, "Two pair", nuts=True, behind_any=0.0, equity=0.9, n=2))
    check(r is not None and r.size == hs.SIZE_VERY_BIG and r.nuts,
          f"坚果两对应为 very_big，实际 {r and r.size}")


def test_top_pair_multiway_behind_medium_weak() -> None:
    """顶对多路落后 → medium / weak，kind 仍 value（是一对，不是听牌）。"""
    r = hs.compute_hand_strength(sit_stub(
        TPTK, "Top pair K kicker", behind_any=0.72, equity=0.18, n=2))
    check(r is not None, "应产出结论")
    check(r.size == hs.SIZE_MEDIUM, f"多路落后顶对应降为 medium，实际 {r.size}")
    check(r.equity_tier == hs.TIER_WEAK, f"0.54x 平均线应为 weak，实际 {r.equity_tier}")
    check(r.kind == hs.KIND_VALUE, f"顶对仍是 value，实际 {r.kind}")
    check("ahead pair, multiway behind; do not overplay" in r.summary_en(),
          f"summary 应提示 multiway behind：{r.summary_en()}")


def test_draw_is_always_draw() -> None:
    """强听 equity 再高也标 DRAW，绝不标 value。"""
    r = hs.compute_hand_strength(sit_stub(
        STRONG_DRAW, "Strong draw (12 outs)", behind_any=0.99, equity=0.38, n=1))
    check(r is not None and r.kind == hs.KIND_DRAW,
          f"强听应标 draw，实际 {r and r.kind}")
    check(r.size == hs.SIZE_VERY_SMALL, "听牌不计成牌大小")
    check(r.summary_en().startswith("DRAW "), f"summary 应以 DRAW 开头：{r.summary_en()}")
    check("judge by outs and price, not by behind-now" in r.summary_en(),
          "summary 应提示按 outs/赔率判断")
    # 弱听同样
    w = hs.compute_hand_strength(sit_stub(
        WEAK_DRAW, "Weak draw (4 outs)", behind_any=0.95, equity=0.16, n=1))
    check(w is not None and w.kind == hs.KIND_DRAW, "弱听也应标 draw")


def test_air_very_small_weak() -> None:
    r = hs.compute_hand_strength(sit_stub(
        AIR, "Air", behind_any=1.0, equity=0.0, n=1))
    check(r is not None, "应产出结论")
    check(r.size == hs.SIZE_VERY_SMALL and r.kind == hs.KIND_WEAK,
          f"空气应为 very_small/weak，实际 {r.size}/{r.kind}")
    check("no made hand, no draw" in r.summary_en(),
          f"summary 应写 no made hand, no draw：{r.summary_en()}")


def test_overpair_and_tptk_big() -> None:
    over = hs.compute_hand_strength(sit_stub(
        OVERPAIR, "Overpair", behind_any=0.05, equity=0.6, n=1))
    check(over is not None and over.size == hs.SIZE_BIG,
          f"超对低 behind 应为 big，实际 {over and over.size}")
    tptk = hs.compute_hand_strength(sit_stub(
        TPTK, "Top pair A kicker", behind_any=0.10, equity=0.45, n=1))
    check(tptk is not None and tptk.size == hs.SIZE_BIG,
          f"顶对强踢低 behind 应为 big，实际 {tptk and tptk.size}")


def test_board_play_kind() -> None:
    r = hs.compute_hand_strength(sit_stub(
        BOARD_PLAY, "Two pair (board play)", behind_any=1.0, equity=0.0, n=2))
    check(r is not None and r.kind == hs.KIND_BOARD_PLAY,
          f"打牌面应标 board_play，实际 {r and r.kind}")
    check("playing the board" in r.summary_en(),
          "打牌面 summary 应提示 everyone shares the board")


def test_boundaries() -> None:
    # behind_any=0、equity=1（nuts 成牌）
    hi = hs.compute_hand_strength(sit_stub(
        STRONG_MADE, "Full house", nuts=True, behind_any=0.0, equity=1.0, n=1))
    check(hi is not None and hi.equity_tier == hs.TIER_STRONG and hi.size == hs.SIZE_VERY_BIG,
          "边界高值应 strong/very_big")
    # behind_any=1、equity=0（成牌被全面压制）
    lo = hs.compute_hand_strength(sit_stub(
        TPTK, "Top pair", behind_any=1.0, equity=0.0, n=4))
    check(lo is not None and lo.size == hs.SIZE_MEDIUM and lo.equity_tier == hs.TIER_WEAK,
          f"边界低值应 medium/weak，实际 {lo and (lo.size, lo.equity_tier)}")
    # n=1 vs n=4 平均线不同
    a = hs.compute_hand_strength(sit_stub(TPTK, "Top pair", equity=0.5, n=1))
    b = hs.compute_hand_strength(sit_stub(TPTK, "Top pair", equity=0.5, n=4))
    check(a is not None and b is not None and abs(a.avg - 0.5) < 1e-9
          and abs(b.avg - 0.2) < 1e-9,
          f"平均线应按人数分池：{a and a.avg} vs {b and b.avg}")


def test_hand_ranges_none_is_none() -> None:
    sit = SimpleNamespace(hand_ranges=None, hand_tier=TPTK, hand_label_en="X",
                          ground_truth=None)
    check(hs.compute_hand_strength(sit) is None, "无 hand_ranges 应返回 None")
    empty = SimpleNamespace(hand_ranges=SimpleNamespace(opponents=()), hand_tier=TPTK,
                            hand_label_en="X", ground_truth=None)
    check(hs.compute_hand_strength(empty) is None, "空对手应返回 None")


def test_summary_exact_format() -> None:
    """锁定 §7 的行格式（两个完全确定例）。"""
    r = hs.compute_hand_strength(sit_stub(
        STRONG_MADE, "Two pair", behind_any=0.04, equity=0.80, n=3))
    want = ("BIG Two pair; behind now 4%, equity 80% vs 4-way avg 25% "
            "(strong) — value hand.")
    check(r is not None and r.summary_en() == want,
          f"两对 summary 应精确等于 {want!r}，实际 {r and r.summary_en()!r}")
    air = hs.compute_hand_strength(sit_stub(AIR, "Air", behind_any=1.0, equity=0.0, n=1))
    want_air = ("VERY SMALL Air; behind now 100%, equity 0% vs 2-way avg 50% "
                "(weak) — no made hand, no draw.")
    check(air is not None and air.summary_en() == want_air,
          f"空气 summary 应精确等于 {want_air!r}，实际 {air and air.summary_en()!r}")


def test_enabled_and_thresholds() -> None:
    import os
    try:
        os.environ["HAND_STRENGTH_ENABLED"] = "0"
        check(not hs.enabled(), "HAND_STRENGTH_ENABLED=0 应关闭")
        os.environ["HAND_STRENGTH_ENABLED"] = "false"
        check(not hs.enabled(), "false 应关闭")
        os.environ["HAND_STRENGTH_ENABLED"] = "1"
        check(hs.enabled(), "默认/1 应开启")

        os.environ["HAND_STRENGTH_THRESHOLDS"] = \
            '{"behind_demote":0.5,"ratio_strong":2.5,"ratio_above":1.2,"ratio_neutral":0.9}'
        got = hs._thresholds()
        check(got == (0.5, 2.5, 1.2, 0.9), f"阈值覆盖应生效：{got}")
        # 阈值生效：behind 0.4 在新门槛 0.5 下不再降档
        r = hs.compute_hand_strength(sit_stub(TPTK, "Top pair", behind_any=0.4,
                                              equity=0.5, n=1))
        check(r is not None and r.size == hs.SIZE_BIG,
              f"behind 0.4 在 demote=0.5 下应保持 big，实际 {r and r.size}")
    finally:
        os.environ.pop("HAND_STRENGTH_ENABLED", None)
        os.environ.pop("HAND_STRENGTH_THRESHOLDS", None)


def test_thresholds_bad_json_falls_back() -> None:
    import os
    try:
        os.environ["HAND_STRENGTH_THRESHOLDS"] = "not json"
        check(hs._thresholds() == (0.35, 2.0, 1.3, 0.85),
              "非法 JSON 应回退默认阈值")
        os.environ["HAND_STRENGTH_THRESHOLDS"] = "[1,2]"
        check(hs._thresholds() == (0.35, 2.0, 1.3, 0.85),
              "非对象 JSON 应回退默认阈值")
    finally:
        os.environ.pop("HAND_STRENGTH_THRESHOLDS", None)


def main() -> int:
    tests = [
        test_two_pair_big_strong_value,
        test_two_pair_nuts_very_big,
        test_top_pair_multiway_behind_medium_weak,
        test_draw_is_always_draw,
        test_air_very_small_weak,
        test_overpair_and_tptk_big,
        test_board_play_kind,
        test_boundaries,
        test_hand_ranges_none_is_none,
        test_summary_exact_format,
        test_enabled_and_thresholds,
        test_thresholds_bad_json_falls_back,
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
    print("\n✅ 实时牌力解析全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
