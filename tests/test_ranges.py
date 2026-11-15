#!/usr/bin/env python3
"""翻前范围层：169 键展开、宽度、具名/规则先验、归一化。

    nv tests/test_ranges.py
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
    """每个 token 都必须能展开。

    用 `assert` 而不是 `check`：`check` 只把失败记进 FAILS，pytest 会当成通过。
    这个坑踩过一次 —— `"22-JJ"` 这种对子区间 `expand` 会**静默返回空集**，
    整个对子段就从范围里消失了，而 pytest 全绿。
    """
    bad = []
    for name, tokens in {**ranges._RULE_TOKENS,
                         "BB_DEF": ranges._BB_DEF_TOKENS}.items():
        for token in tokens:
            if not expand(token):
                bad.append(f"{name}:{token}")
    assert not bad, f"规则表有无法展开的 token：{bad}"


def test_named_ranges_and_widths() -> None:
    btn = ranges.get("RFI_BTN")
    utg = ranges.get("RFI_UTG")
    bb = ranges.get("BB_DEF_vs_LP")
    assert btn and utg and bb
    check(abs(btn.width_pct - 44.8) < 0.5, f"BTN RFI 宽度应约 44.8%，实际 {btn.width_pct}")
    check(abs(utg.width_pct - 12.1) < 0.5, f"UTG RFI 宽度应约 12.1%，实际 {utg.width_pct}")
    check(abs(bb.width_pct - 43.9) < 0.5, f"BB 防守应约 43.9%，实际 {bb.width_pct}")
    check(abs(sum(btn.combo_weights.values()) - 1.0) < 1e-9, "范围权重应归一化")
    check(btn.combo_count == len(btn.combo_weights), "combo_count 不一致")
    check(btn.source.startswith("named:"), f"具名来源不对：{btn.source}")


def test_rule_prior_widths_follow_design_targets() -> None:
    cases = {
        # 2026-11-05 按摊牌反核加宽（见 docs/摊牌反核-2026-11-05.md）：
        # 冷跟 IP 11→20%、OOP 10→14.5%；limp 31→37 / 38→41 / 41→47。
        "COLD_CALL_IP_vs_LP": (19.0, 21.0),
        "COLD_CALL_OOP_vs_EP": (15.5, 17.5),
        "LIMP_EP": (36.0, 39.0),
        "LIMP_MP": (39.5, 42.0),
        "LIMP_LP": (45.0, 49.0),
        # 2026-11-15 3-bet 两张表按类别加宽（#40）：5.58→7.69 / 3.77→9.20；
        # 同日晚维护者追加 7 个同花连张/一隔连张：7.69→9.80 / 9.20→11.31。
        "3BET_vs_EP": (8.8, 10.8),
        "3BET_CALL_vs_LP": (10.3, 12.3),
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


def test_three_bet_priors_widen_by_category() -> None:
    """3-bet / 3-bet 跟注按类别加宽（#40），并追加同花连张（2026-11-15 追加）。

    依据 docs/3-bet先验-摊牌反核-2026-11-15.md：两张表的 prior_absent 占
    68%（全局 28.6%，全仓最差），落在先验外的是小/中对子与非同花高张。
    同花连张（76s/87s/97s/T7s/86s/65s/75s）最初因 315 个摊牌样本零证据被排除，
    2026-11-15 维护者决定仍然加入：诈唬类别要成体系，不能只留一手 QTs/QTo。
    """
    def keys(rng: ranges.Range) -> set[str]:
        return {ranges.key_of_combo(combo) for combo in rng.combo_weights}

    bet = ranges.get("3BET_vs_MP")
    call = ranges.get("3BET_CALL_vs_LP")
    assert bet is not None and call is not None
    bet_keys, call_keys = keys(bet), keys(call)

    # 宽度来自票面拍板（#40 表格 + 2026-11-15 追加同花连张），不是从当前表反推的。
    check(abs(bet.width_pct - 9.80) < 0.05,
          f"3-bet 宽度应 9.80%，实际 {bet.width_pct:.2f}%")
    check(abs(call.width_pct - 11.31) < 0.05,
          f"跟 3-bet 宽度应 11.31%，实际 {call.width_pct:.2f}%")

    check({"66", "77", "QTs", "QTo"} <= bet_keys,
          f"3-bet 缺加宽后的 key：{sorted({'66', '77', 'QTs', 'QTo'} - bet_keys)}")
    pairs_22_99 = {f"{rank}{rank}" for rank in "23456789"}
    check(pairs_22_99 <= call_keys,
          f"跟 3-bet 缺 22–99 对子：{sorted(pairs_22_99 - call_keys)}")
    check({"AQo", "KQo"} <= call_keys,
          f"跟 3-bet 缺 AQo/KQo：{sorted({'AQo', 'KQo'} - call_keys)}")

    suited = ("76s", "87s", "97s", "T7s", "86s", "65s", "75s")
    for key in suited:
        check(len(ranges.combos_of_key(key)) == 4, f"{key} 应展开成 4 个 combo")
        check(key in bet_keys, f"3-bet 应含同花连张 {key}")
        check(key in call_keys, f"跟 3-bet 应含同花连张 {key}")

    # 位置桶共用同一张类表：同类先验在不同位置桶之间宽度必须一致。
    for rid, expected in (("3BET_vs_EP", bet.width_pct), ("3BET_vs_LP", bet.width_pct),
                          ("3BET_CALL_vs_EP", call.width_pct),
                          ("3BET_CALL_vs_BB", call.width_pct)):
        rng = ranges.get(rid)
        assert rng is not None, rid
        check(abs(rng.width_pct - expected) < 0.01,
              f"{rid} 宽度 {rng.width_pct:.2f}% 应与同类先验 {expected:.2f}% 一致")


def main() -> int:
    tests = [
        test_combo_key_roundtrip,
        test_all_rule_tokens_expand,
        test_named_ranges_and_widths,
        test_rule_prior_widths_follow_design_targets,
        test_uniform_and_fail_open,
        test_make_range_filters_zero_and_negative,
        test_three_bet_priors_widen_by_category,
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
