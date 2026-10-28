#!/usr/bin/env python3
"""翻前范围层：169 键展开、宽度、具名/规则先验、归一化。

    .venv/bin/python tests/test_ranges.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import ranges  # noqa: E402
from poker_ocr.preflop_ranges import expand  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def test_combo_key_roundtrip() -> None:
    key = ranges.key_of_combo(((12, "s"), (11, "h")))
    check(key == "AKo", f"AKo 键不对：{key}")
    check(ranges.key_of_combo(((12, "s"), (12, "h"))) == "AA", "对子键不对")
    check(ranges.key_of_combo(((12, "s"), (11, "s"))) == "AKs", "同花键不对")
    combo = ranges.canon_combo((12, "s"), (11, "h"))
    check(len(ranges.combos_of_key("AKo")) == 12, "AKo 应展开 12 个 combo")
    check(len(ranges.combos_of_key("AKs")) == 4, "AKs 应展开 4 个 combo")
    check(len(ranges.combos_of_key("AA")) == 6, "AA 应展开 6 个 combo")
    check(combo in ranges.combos_of_key("AKo"), "AKo 展开缺 AsKh")


def test_all_rule_tokens_expand() -> None:
    bad = []
    for name, tokens in {**ranges._RULE_TOKENS,
                         "BB_DEF_FALLBACK": ranges._BB_DEF_FALLBACK}.items():
        for token in tokens:
            if not expand(token):
                bad.append(f"{name}:{token}")
    check(not bad, f"规则表有无法展开的 token：{bad}")


def test_named_ranges_and_widths() -> None:
    btn = ranges.get("RFI_BTN")
    utg = ranges.get("RFI_UTG")
    bb = ranges.get("BB_DEF_vs_LP")
    assert btn and utg and bb
    check(abs(btn.width_pct - 43.0) < 0.5, f"BTN RFI 宽度应约 43%，实际 {btn.width_pct}")
    check(abs(utg.width_pct - 12.1) < 0.5, f"UTG RFI 宽度应约 12.1%，实际 {utg.width_pct}")
    check(abs(bb.width_pct - 43.9) < 0.5, f"BB 防守应约 43.9%，实际 {bb.width_pct}")
    check(abs(sum(btn.combo_weights.values()) - 1.0) < 1e-9, "范围权重应归一化")
    check(btn.combo_count == len(btn.combo_weights), "combo_count 不一致")
    check(btn.source.startswith("named:"), f"具名来源不对：{btn.source}")


def test_rule_prior_widths_follow_design_targets() -> None:
    cases = {
        "COLD_CALL_IP_vs_LP": (10.5, 12.5),
        "COLD_CALL_OOP_vs_EP": (9.5, 11.5),
        "LIMP_EP": (29.5, 31.5),
        "LIMP_MP": (37.0, 39.5),
        "LIMP_LP": (39.0, 43.0),
        "3BET_vs_EP": (5.0, 7.0),
        "3BET_CALL_vs_LP": (3.0, 5.0),
    }
    for rid, (lo, hi) in cases.items():
        rng = ranges.get(rid)
        assert rng is not None, rid
        check(lo <= rng.width_pct <= hi,
              f"{rid} 宽度 {rng.width_pct:.1f}% 不在 {lo}–{hi}%")
        check(rng.source.startswith("rules:"), f"{rid} 来源应为 rules，实际 {rng.source}")


def test_uniform_and_fail_open() -> None:
    uniform = ranges.get("UNIFORM")
    assert uniform is not None
    check(uniform.combo_count == 1326, f"均匀范围应 1326 个 combo，实际 {uniform.combo_count}")
    check(ranges.get("NOT_A_RANGE") is None, "未知 ID 应返回 None")
    check(ranges.get("") is None, "空 ID 应返回 None")
    bomb = ranges.get("BOMB")
    assert bomb is not None
    check("board-only" in bomb.note, "炸弹池/任意两张必须标注 board-only")


def test_make_range_filters_zero_and_negative() -> None:
    combo = ranges.combos_of_key("AA")[0]
    rng = ranges.make_range("x", {combo: 1.0, ranges.combos_of_key("KK")[0]: 0.0,
                                  ranges.combos_of_key("QQ")[0]: -1.0})
    assert rng is not None
    check(rng.combo_count == 1, f"零/负权重应过滤，实际 {rng.combo_count}")
    check(abs(sum(rng.combo_weights.values()) - 1.0) < 1e-9, "过滤后应重新归一化")
    check(ranges.make_range("empty", {}) is None, "空范围应返回 None")


def main() -> int:
    tests = [
        test_combo_key_roundtrip,
        test_all_rule_tokens_expand,
        test_named_ranges_and_widths,
        test_rule_prior_widths_follow_design_targets,
        test_uniform_and_fail_open,
        test_make_range_filters_zero_and_negative,
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
    print("\n✅ 翻前范围层全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
