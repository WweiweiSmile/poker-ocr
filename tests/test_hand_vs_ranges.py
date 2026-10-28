#!/usr/bin/env python3
"""手牌 vs 范围：精确 behind、联合 MC、权益、去重牌、确定性与渲染。

    .venv/bin/python tests/test_hand_vs_ranges.py
"""

from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import hand_vs_ranges, ranges  # noqa: E402
from poker_ocr.coach import evaluate, parse_cards  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def keys_combos(*keys: str):
    out = []
    for key in keys:
        out.extend(ranges.combos_of_key(key))
    return out


def make(rid: str, combos):
    return ranges.make_range(rid, {c: 1.0 for c in combos})


def brute(hero: str, board: str, r1, r2) -> tuple[float, float, float, float, float]:
    """两个范围、river 无发牌时的暴力枚举。返回 (behind_any, tie_any, eq, b1, b2)。"""
    hero_cards = parse_cards(hero)
    board_cards = parse_cards(board)
    dead = set(hero_cards) | set(board_cards)
    r1 = [c for c in r1 if c[0] not in dead and c[1] not in dead]
    r2 = [c for c in r2 if c[0] not in dead and c[1] not in dead]
    hs = evaluate(hero_cards + board_cards)
    valid = behind_any = tie_any = eq = 0.0
    # 逐对手 behind 的条件是「只去掉 hero+board 的牌」，不按另一家的牌归一化；
    # 联合量才需要整组去重。
    b1 = sum(1 for a in r1 if evaluate(list(a) + board_cards) > hs) / len(r1)
    b2 = sum(1 for b in r2 if evaluate(list(b) + board_cards) > hs) / len(r2)
    for a in r1:
        for b in r2:
            if len({*a, *b}) < 4:
                continue
            valid += 1
            sa = evaluate(list(a) + board_cards)
            sb = evaluate(list(b) + board_cards)
            if sa > hs or sb > hs:
                behind_any += 1
            elif sa == hs or sb == hs:
                tie_any += 1
            best = max(hs, sa, sb)
            winners = 1 + (sa == best) + (sb == best)
            if hs == best:
                eq += 1.0 / winners
    return (behind_any / valid, tie_any / valid, eq / valid, b1, b2)


def test_exact_and_mc_against_bruteforce() -> None:
    hero, board = "AhQh", "AsKs4d2c7h"
    r1 = keys_combos("QQ", "JJ", "TT", "AKs", "AKo", "KQs", "KJs")
    r2 = keys_combos("99", "88", "AQs", "AJs", "KQo")
    R1, R2 = make("R1", r1), make("R2", r2)
    assert R1 and R2

    hand_vs_ranges.reset_cache()
    got = hand_vs_ranges.compute(hero, board, [("a", None, R1), ("b", None, R2)],
                                 samples=20000)
    assert got is not None
    want_any, want_tie, want_eq, want_b1, want_b2 = brute(hero, board, r1, r2)

    check(abs(got.behind_any - want_any) < 0.025,
          f"behind_any MC {got.behind_any:.3f} vs 枚举 {want_any:.3f}")
    check(abs(got.tie_any - want_tie) < 0.02,
          f"tie_any MC {got.tie_any:.3f} vs 枚举 {want_tie:.3f}")
    check(abs(got.equity - want_eq) < 0.025,
          f"equity MC {got.equity:.3f} vs 枚举 {want_eq:.3f}")
    check(abs(got.opponents[0].behind - want_b1) < 1e-9,
          f"逐对手 behind1 {got.opponents[0].behind:.4f} vs {want_b1:.4f}")
    check(abs(got.opponents[1].behind - want_b2) < 1e-9,
          f"逐对手 behind2 {got.opponents[1].behind:.4f} vs {want_b2:.4f}")


def test_flop_equity_against_bruteforce() -> None:
    hero, board = "AhQh", "AsKs4d"
    r1 = keys_combos("QQ", "JJ", "AKs")
    r2 = keys_combos("99", "TT")
    dead = set(parse_cards(hero)) | set(parse_cards(board))
    c1 = [c for c in r1 if c[0] not in dead and c[1] not in dead]
    c2 = [c for c in r2 if c[0] not in dead and c[1] not in dead]
    board_cards = parse_cards(board)
    deck = [(r, s) for r in range(13) for s in "cdhs"]
    hs = evaluate(parse_cards(hero) + board_cards)
    valid = eq = 0.0
    for a in c1:
        for b in c2:
            used = {*a, *b}
            if len(used) < 4:
                continue
            avail = [c for c in deck if c not in used and c not in dead]
            for runout in itertools.combinations(avail, 2):
                full = board_cards + list(runout)
                sa = evaluate(list(a) + full)
                sb = evaluate(list(b) + full)
                h = evaluate(parse_cards(hero) + full)
                best = max(h, sa, sb)
                winners = 1 + (sa == best) + (sb == best)
                valid += 1
                if h == best:
                    eq += 1.0 / winners
    want = eq / valid
    hand_vs_ranges.reset_cache()
    got = hand_vs_ranges.compute(hero, board,
                                 [("a", None, make("R1", c1)),
                                  ("b", None, make("R2", c2))],
                                 samples=20000)
    assert got is not None
    check(abs(got.equity - want) < 0.025,
          f"flop equity MC {got.equity:.3f} vs 枚举 {want:.3f}")


def test_card_removal_and_zero_weights() -> None:
    aa = ranges.make_range("AA", {c: 1.0 for c in ranges.combos_of_key("AA")})
    assert aa is not None
    got = hand_vs_ranges.compute("AsKh", "2c3d4h", [aa], samples=1000)
    assert got is not None
    check(got.opponents[0].combos_after_removal == 3,
          f"hero 持 As 时 AA 只剩 3 个 combo，实际 {got.opponents[0].combos_after_removal}")

    # 手工构造带 0 权重的 Range：统计和采样都必须跳过它。
    c_aa = ranges.combos_of_key("AA")[0]
    c_kk = ranges.combos_of_key("KK")[0]
    manual = ranges.Range(id="zero", version=1,
                          combo_weights={c_aa: 1.0, c_kk: 0.0})
    got2 = hand_vs_ranges.compute("2c3d", "AsKs4h", [manual], samples=500)
    assert got2 is not None
    check(got2.opponents[0].combos_after_removal == 1,
          "0 权重 combo 不能参与统计")


def test_river_tie_splits_pot() -> None:
    board = "AsKsQsJsTs"
    rng = ranges.make_range("small", {c: 1.0 for c in keys_combos("22", "33")})
    assert rng is not None
    hand_vs_ranges.reset_cache()
    got = hand_vs_ranges.compute("2h3h", board, [("x", None, rng)], samples=500)
    assert got is not None
    check(got.tie_any == 1.0, f"公共牌皇家同花顺应全员平局，实际 tie={got.tie_any}")
    check(abs(got.equity - 0.5) < 0.02, f"两人平局权益应 50%，实际 {got.equity}")


def test_deterministic_and_cached() -> None:
    board = "AsKs4d2c7h"
    rng = ranges.make_range("R", {c: 1.0 for c in keys_combos("QQ", "AKs", "KQo")})
    assert rng is not None
    hand_vs_ranges.reset_cache()
    a = hand_vs_ranges.compute("AhQh", board, [rng], samples=3000)
    b = hand_vs_ranges.compute("AhQh", board, [rng], samples=3000)
    assert a and b
    check(a == b, "同 key 第二次应命中缓存且完全一致")
    hand_vs_ranges.reset_cache()
    c = hand_vs_ranges.compute("AhQh", board, [rng], samples=3000)
    assert c
    check(abs(a.equity - c.equity) < 1e-12,
          f"同 key 清缓存后应确定，{a.equity} vs {c.equity}")


def test_fail_open_and_render() -> None:
    check(hand_vs_ranges.compute("AhQh", "AsKs", []) is None, "牌面不足应 None")
    check(hand_vs_ranges.compute("AhQh", "AsAs4d", []) is None, "重复牌应 None")
    check(hand_vs_ranges.compute("AhQh", "AsKs4d", []) is None, "空对手应 None")
    one = ranges.combos_of_key("AA")[0]
    solo = ranges.make_range("solo", {one: 1.0})
    assert solo is not None
    hero_text = ranges.combo_text(one)
    got = hand_vs_ranges.compute(hero_text, "2c3c4c", [solo], samples=100)
    check(got is None, f"hero 持有范围唯一 combo 时应过滤为空：{hero_text}")

    rng2 = ranges.make_range("R", {c: 1.0 for c in keys_combos("QQ", "AKs", "KQo")})
    assert rng2 is not None
    text = hand_vs_ranges.compute("AhQh", "AsKs4d", [("BTN", "BTN", rng2, "call")],
                                  samples=3000, context="3-way: test").render()
    for needle in ("## Hand vs ranges", "Equity vs field", "Made hand now",
                   "Behind", "Read:"):
        check(needle in text, f"渲染缺 {needle!r}")
    check("[call]" in text, "渲染应写清 mode")


def test_enabled_flag_and_sample_env() -> None:
    old_enabled = os.environ.get("HAND_VS_RANGES_ENABLED")
    old_samples = os.environ.get("HAND_VS_RANGES_SAMPLES")
    try:
        os.environ["HAND_VS_RANGES_ENABLED"] = "0"
        check(not hand_vs_ranges.enabled(), "HAND_VS_RANGES_ENABLED=0 应关闭")
        os.environ["HAND_VS_RANGES_ENABLED"] = "1"
        check(hand_vs_ranges.enabled(), "默认应开启")
        os.environ["HAND_VS_RANGES_SAMPLES"] = "1234"
        check(hand_vs_ranges._samples_for(2) == 1234, "环境变量样本数未生效")
        check(hand_vs_ranges._samples_for(3) == 1234, "4-way 不应抬高已设样本数")
        os.environ["HAND_VS_RANGES_SAMPLES"] = "999999"
        check(hand_vs_ranges._samples_for(2) == 20000, "样本数应有上限")
        check(hand_vs_ranges._samples_for(3) == 1500, "4-way 应降到 1500")
        check(hand_vs_ranges._samples_for(4) == 1500, "5-way 也应降到 1500")
    finally:
        if old_enabled is None:
            os.environ.pop("HAND_VS_RANGES_ENABLED", None)
        else:
            os.environ["HAND_VS_RANGES_ENABLED"] = old_enabled
        if old_samples is None:
            os.environ.pop("HAND_VS_RANGES_SAMPLES", None)
        else:
            os.environ["HAND_VS_RANGES_SAMPLES"] = old_samples


def main() -> int:
    tests = [
        test_exact_and_mc_against_bruteforce,
        test_flop_equity_against_bruteforce,
        test_card_removal_and_zero_weights,
        test_river_tie_splits_pot,
        test_deterministic_and_cached,
        test_fail_open_and_render,
        test_enabled_flag_and_sample_env,
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
    print("\n✅ 手牌 vs 范围全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
