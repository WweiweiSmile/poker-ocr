#!/usr/bin/env python3
"""翻前范围表（preflop_ranges）与提示词的一致性的回归。

    .venv/bin/python tests/test_preflop_ranges.py
    .venv/bin/python -m pytest tests/test_preflop_ranges.py -q

背景：`_LEAN_SYSTEM` 里的 RFI 表原来只是给模型看的文本，模型是**凭记忆引用**的。
2026-10-23 那局它编出「AJo is in UTG+1's RFI range」这种和表直接矛盾的理由，
14 次翻前加注里 12 次是表外手，合计 −123。现在范围归属由代码算、作为事实注入。

这个文件守两件事：
  1. 代码里的表和提示词里的表**不能漂移**（改一边忘了另一边就会互相矛盾）；
  2. 判定本身要对（AJo 从 UTG+1 是 OUT、JJ 从 UTG 是 IN 等等）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import _LEAN_SYSTEM  # noqa: E402
from poker_ocr.preflop_ranges import (  # noqa: E402
    CHART_ADDITIONS, OPEN_POSITIONS, RFI, chart_text, expand, hand_key,
    open_frequency, range_verdict, sb_completable,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def test_expand_notation() -> None:
    check(expand("77+") == {"77", "88", "99", "TT", "JJ", "QQ", "KK", "AA"},
          f"77+ 展开错：{sorted(expand('77+'))}")
    check(expand("AJs+") == {"AJs", "AQs", "AKs"},
          f"AJs+ 展开错：{sorted(expand('AJs+'))}")
    check(expand("A2s-A7s") == {"A2s", "A3s", "A4s", "A5s", "A6s", "A7s"},
          f"A2s-A7s 展开错：{sorted(expand('A2s-A7s'))}")
    check(expand("K9s+") == {"K9s", "KTs", "KJs", "KQs"},
          f"K9s+ 展开错：{sorted(expand('K9s+'))}")
    check(expand("AQo+") == {"AQo", "AKo"},
          f"AQo+ 展开错：{sorted(expand('AQo+'))}")
    check(expand("98s") == {"98s"} and expand("KQo") == {"KQo"},
          "字面量展开错")
    check(expand("乱写") == set(), "非法写法应返回空集")


def test_hand_key() -> None:
    check(hand_key(("Jh", "Ks")) == "KJo", f"hand_key：{hand_key(('Jh','Ks'))}")
    check(hand_key(("Ah", "Kh")) == "AKs", f"同花：{hand_key(('Ah','Kh'))}")
    check(hand_key(("Ad", "7c")) == "A7o", f"非同花：{hand_key(('Ad','7c'))}")
    check(hand_key(("2d", "2c")) == "22", f"对子：{hand_key(('2d','2c'))}")


def test_positions_are_monotonic() -> None:
    """越靠后位置的范围只能是前面位置的超集（累积表的定义）。"""
    prev: set[str] = set()
    for pos in OPEN_POSITIONS:
        cur = set(RFI[pos])
        check(prev <= cur, f"{pos} 没有包含前一个位置的全部范围")
        check(len(cur) <= 169, f"{pos} 范围超过 169 手")
        prev = cur
    check(len(RFI["BTN"]) > len(RFI["UTG"]),
          "BTN 的范围应该比 UTG 宽")


def test_known_verdicts() -> None:
    """对着复盘里真实出现过的手牌，逐条确认判定（范围已按跟注站加宽）。"""
    cases = [
        # 加宽后进了范围的
        (("Ah", "Js"), "UTG", True, "UTG"),        # AJo：对跟注站从 UTG 就开
        (("Ah", "Js"), "UTG+1", True, "UTG"),
        (("4d", "4c"), "UTG+1", True, "UTG+1"),    # 小对子：抽三条有人付钱
        (("8d", "Ad"), "UTG+1", True, "UTG+1"),    # 同花 A：坚果花潜力
        (("Kh", "Td"), "LJ", True, "LJ"),          # KTo 从 LJ 起
        (("Kh", "Td"), "CO", True, "LJ"),
        (("Jd", "Jc"), "UTG", True, "UTG"),
        (("As", "Kd"), "UTG", True, "UTG"),
        # 加宽后仍然出局的：非同花的弱 A、底部的同花隔张、纯垃圾
        (("4h", "2c"), "HJ", False, None),         # 42o
        (("7c", "6s"), "CO", False, None),         # 76o：非同花连张不加
        (("Ad", "6c"), "BTN", False, None),        # A6o：反向隐含赔率
        (("8c", "3c"), "BTN", False, None),        # 83s：底部同花隔张不收
        (("Ad", "7c"), "CO", False, "BTN"),        # A7o 只到 BTN
    ]
    for cards, pos, want_in, want_first in cases:
        v = range_verdict(pos, cards)
        check(v is not None, f"{cards} @ {pos} 应给出判定")
        if v is None:
            continue
        check(v.in_range is want_in,
              f"{cards} @ {pos} 应 in_range={want_in}，实际 {v.in_range}")
        check(v.first_position == want_first,
              f"{cards} @ {pos} first_position 应为 {want_first}，实际 {v.first_position}")


def test_sb_bb_are_not_lists() -> None:
    """SB/BB 不是清单：不能给出 IN/OUT，否则会把「补盲 0.5BB」误判成该弃。"""
    sb = range_verdict("SB", ("6s", "10h"))
    bb = range_verdict("BB", ("Ad", "7c"))
    check(sb is not None and sb.in_range is None, "SB 不应该有 IN/OUT 判定")
    check(bb is not None and bb.in_range is None, "BB 不应该有 IN/OUT 判定")


def test_sb_complete_requires_playability() -> None:
    """小盲补 0.5BB：看能不能翻出可下注的牌面，**不看赔率**。

    0.5BB 进 8BB 的池是 17:1、只要 6% 权益，而任何两张牌单挑都有 ~30%。
    但那是原始权益：杂色垃圾在多人池里没位置、没听牌，翻中一对还是第二好的
    对子。所以 92o / T6o / Q6o / A5o 都不该补（这四手是用户的牌桌判断）。
    """
    for cards in (("9d", "2h"), ("Tc", "6h"), ("Qh", "6c"), ("Ac", "5d"),
                  ("8c", "3d")):
        check(not sb_completable(cards),
              f"{''.join(cards)} 没有听牌潜力，不该补盲")
    for cards in (("Th", "9h"), ("7s", "7d"), ("Kh", "Qd"), ("Ad", "Jc"),
                  ("5c", "4c")):
        check(sb_completable(cards), f"{''.join(cards)} 能翻出可下注的牌面，可以补")

    # 判定要真的进到提示词里，不能只写在规则里
    v = range_verdict("SB", ("9d", "2h"))
    check("FOLD" in v.render(), f"92o 的 SB 判定应说 FOLD：{v.render()}")
    v = range_verdict("SB", ("Th", "9h"))
    check("FOLD" not in v.render(), f"T9s 的 SB 判定不该说 FOLD：{v.render()}")


def test_utg2_exists() -> None:
    """9 人桌有 7 个非盲位，原表漏了 UTG+2 整行，模型只能瞎插值。"""
    check("UTG+2" in RFI, "UTG+2 缺失")
    check(RFI["UTG+1"] < RFI["UTG+2"] < RFI["LJ"],
          "UTG+2 应严格介于 UTG+1 与 LJ 之间")


def test_prompt_chart_matches_code() -> None:
    """提示词里的表**由代码生成**，所以两边不可能漂移 —— 这里守的是「别改回手写」。

    手写两份的代价在这次踩过：代码里改了表、prompt 里还是旧的，模型就会拿到
    一份和我们判定不一致的表，然后挑对自己有利的那份用。
    """
    check("<<RFI_CHART>>" not in _LEAN_SYSTEM, "占位符没有被替换")
    check(chart_text() in _LEAN_SYSTEM, "提示词里的表和 chart_text() 不一致（被手写覆盖了？）")
    for pos in OPEN_POSITIONS:
        check(f"{pos} " in _LEAN_SYSTEM or f"{pos} add " in _LEAN_SYSTEM,
              f"提示词缺 {pos} 这一行")


def test_open_frequency_ramps_up_by_position() -> None:
    """加宽后的开池频率：每个位置都要比前一个宽，且落在合理区间。

    参照值（2026-10-23 按跟注站加宽后）：UTG 12.1% … BTN 43.0%。
    下限守「别又缩回去」，上限守「别宽到拿 72o 开池」。
    """
    freqs = {p: open_frequency(p) for p in OPEN_POSITIONS}
    for a, b in zip(OPEN_POSITIONS, OPEN_POSITIONS[1:]):
        check(freqs[a] < freqs[b], f"{a} {freqs[a]:.1f}% 应窄于 {b} {freqs[b]:.1f}%")
    check(10.0 <= freqs["UTG"] <= 16.0,
          f"UTG 应在 10-16%，实际 {freqs['UTG']:.1f}%")
    check(38.0 <= freqs["BTN"] <= 50.0,
          f"BTN 应在 38-50%，实际 {freqs['BTN']:.1f}%")
    # 底部垃圾牌无论多宽都不该进
    for junk in ("72o", "83o", "94o", "52o", "A2o"):
        check(junk not in RFI["BTN"], f"{junk} 不该出现在 BTN 开池范围里")


def main() -> int:
    tests = [
        test_expand_notation, test_hand_key, test_positions_are_monotonic,
        test_known_verdicts, test_sb_bb_are_not_lists,
        test_sb_complete_requires_playability, test_utg2_exists,
        test_prompt_chart_matches_code, test_open_frequency_ramps_up_by_position,
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
    print("\n✅ 翻前范围表全部通过")
    return 0


def test_zz_all_checks_passed() -> None:
    """pytest 兜底：本文件的 check() 只累积不抛异常，没有这条时 pytest 会把失败当通过。"""
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    sys.exit(main())
