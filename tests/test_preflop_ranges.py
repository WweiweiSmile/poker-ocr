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
    is_set_mine_pair, is_speculative, open_frequency, range_verdict,
    sb_completable,
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


def test_small_pair_overlimp_exception() -> None:
    """小对子不在开池表里，但事实层必须保留 over-limp 的例外。

    h13（2026-10-23）：44 UTG，桌风松弱「很少主动加注」，有效筹码 85BB。
    模型只看到「Chart says FOLD from UTG」就弃了，理由还写成
    「limping invites raises」——正好和桌风相反。例外不能只埋在 system prompt
    里，必须跟结论一起出现，模型才不会把它当成绝对的 FOLD。
    """
    v = range_verdict("UTG", ("4h", "4s"))
    check(v is not None and v.in_range is False, "44 UTG 应判 OUT")
    if v is not None:
        text = v.render()
        check("over-limp" in text or "set-mine" in text,
              f"44 UTG 的判定应提到 over-limp/set-mine 例外：{text}")
        check("22-66" in text, f"例外应写清 22-66：{text}")
        check("Chart says FOLD" in text,
              f"例外不该抹掉「不在开池范围」这个事实：{text}")

    # 22 在 UTG 也适用；77 已经在 UTG 的开池范围里，不该再触发例外
    v22 = range_verdict("UTG", ("2c", "2d"))
    check(v22 is not None and not v22.in_range
          and "22-66" in v22.render(), f"22 UTG 应保留例外：{v22.render() if v22 else None}")
    v77 = range_verdict("UTG", ("7c", "7d"))
    check(v77 is not None and v77.in_range
          and "22-66" not in v77.render(), "77 UTG 在开池表内，不该走例外")


def test_table_dynamics_outranks_chart() -> None:
    """桌风 > 图表默认：同样的 44 UTG，松弱桌的默认是 over-limp，紧桌才是 FOLD。

    h13 的教训：标准表在错误的桌子上只会越错越多。桌风必须能改变默认线，
    而不是只当一句注释。
    """
    loose = range_verdict("UTG", ("4h", "4s"),
                          table_dynamics="松弱玩家，跟注站，很少主动加注")
    check(loose is not None and loose.loose, "松弱桌应标记 loose")
    text = loose.render()
    check("better default" in text,
          f"松弱桌 44 UTG 应把 over-limp 说成更好的默认：{text}")
    check("folding" in text, f"应明确不建议 fold：{text}")

    tight = range_verdict("UTG", ("4h", "4s"), table_dynamics="紧弱，尊重加注")
    check(tight is not None and not tight.loose, "紧桌不应标记 loose")
    check("better default" not in tight.render(),
          f"紧桌不该给松桌的放宽结论：{tight.render()}")

    # 松桌只放宽有听牌/成牌潜力的类别，不是无脑放宽
    suited = range_verdict("UTG", ("Ah", "8h"), table_dynamics="松弱玩家，跟注站")
    check(suited is not None and not suited.in_range, "A8s UTG 应判 OUT")
    check("playable hands" in suited.render(),
          f"松桌的非对子应以「可玩类别」放宽：{suited.render()}")

    junk = range_verdict("CO", ("7c", "6s"), table_dynamics="松弱玩家，跟注站")
    check(junk is not None and not junk.in_range
          and "OUTSIDE every open range" in junk.render(),
          f"76o 仍在所有范围之外：{junk.render() if junk else None}")


def test_isolation_range_tightens_over_limpers() -> None:
    """RFI 表是无人入池的表；有 limper 时要收紧，不能把「IN open range」当隔离许可证。

    h3（2026-10-23）：A8o CO、前面 3 个 limper，模型看到「A8o is IN the CO open
    range」就开到 11BB；4 家跟注后 A8o 成了弱牌。隔离范围用「N 个更早位置」近似。
    """
    loose = "松弱玩家，跟注站，很少主动加注"

    a8o = range_verdict("CO", ("8h", "Ad"), table_dynamics=loose, limpers=3)
    check(a8o is not None and a8o.in_range is True, "A8o CO 在 RFI 表内")
    check(a8o is not None and a8o.isolation_ok is False,
          "A8o CO 面对 3 个 limper 应判不在隔离范围")
    text = a8o.render() if a8o else ""
    check("UNOPENED pot" in text and "OUTSIDE it" in text,
          f"A8o 隔离判定应说清「RFI 不等于隔离」：{text}")
    check("Do not isolate big" in text, f"A8o 应明确不要大额隔离：{text}")

    ato = range_verdict("CO", ("Th", "Ad"), table_dynamics=loose, limpers=3)
    check(ato is not None and ato.isolation_ok is True,
          "ATo CO 面对 3 个 limper 仍在收紧后的范围里")
    check("still IN the tighter" in (ato.render() if ato else ""),
          f"ATo 应判可以价值隔离：{ato.render() if ato else None}")

    jj = range_verdict("LJ", ("Jc", "Jd"), table_dynamics=loose, limpers=2)
    check(jj is not None and jj.isolation_ok is True, "JJ LJ 面对 2 个 limper 可隔离")

    # 没有 limper 时不算隔离范围
    a8o_open = range_verdict("CO", ("8h", "Ad"), table_dynamics=loose, limpers=0)
    check(a8o_open is not None and a8o_open.isolation_ok is None,
          "无人入池时不该有隔离判定")


def test_set_mine_pairs_below_tt() -> None:
    """TT 以下（22-99）都按买 set 玩；TT+ 才是超对价值。

    2026-10-23 用户口径：平均 3-4 人底池，99 翻牌约 79% 见高张、77 约 92%，
    没中 set 的超对太容易被高张/更大对子统治。
    """
    check(is_set_mine_pair("99") and is_set_mine_pair("77")
          and is_set_mine_pair("22"), "22-99 应判 set-mine 对")
    check(not is_set_mine_pair("TT") and not is_set_mine_pair("JJ")
          and not is_set_mine_pair("A4s"), "TT+/非对子不该判 set-mine 对")

    v = range_verdict("CO", ("9h", "9d"))
    text = v.render() if v else ""
    check("set-mine" in text and "value/isolation" not in text,
          f"99 CO 应按 set-mine 渲染：{text}")

    loose = "松弱玩家，跟注站，很少主动加注"
    tt = range_verdict("CO", ("Th", "Td"), table_dynamics=loose)
    check("value/isolation" in (tt.render() if tt else ""),
          f"TT CO 仍是超对价值：{tt.render() if tt else None}")

    # 有 limper 时也一样：99 仍是 set-mine，不是价值隔离
    v1 = range_verdict("CO", ("9h", "9d"), table_dynamics=loose, limpers=1)
    text1 = v1.render() if v1 else ""
    check("set-mine" in text1 and "value/isolation" not in text1,
          f"99 面对 1 个 limper 仍是 set-mine：{text1}")


def test_speculative_hands_not_value_isolation() -> None:
    """#990052：A4s CO、1 个 limper，模型把它说成「value/isolation」。

    松弱跟注站不弃牌，隔离不了；A4s 对 1 个 limper 只有 ~55%，2-3 家更差
    （36% / 28%）。它在收紧后的范围里，但加注理由只能是「死钱 + 主动权，
    预期多人池」—— 这手牌的真正价值是坚果同花听牌的隐含赔率。
    """
    loose = "松弱玩家，跟注站，很少主动加注"

    check(is_speculative("A4s") and is_speculative("54s")
          and is_speculative("J9s"), "小同花 A / 同花连张应判投机牌")
    check(not is_speculative("ATs") and not is_speculative("ATo")
          and not is_speculative("KQs") and not is_speculative("77"),
          "ATs/KQs/对子不该判投机牌")

    for limpers in (1, 2):
        v = range_verdict("CO", ("Ah", "4h"), table_dynamics=loose, limpers=limpers)
        check(v is not None and v.isolation_ok is True,
              f"A4s CO 面对 {limpers} 个 limper 仍在收紧范围里")
        text = v.render() if v else ""
        check("SPECULATIVE" in text and "not a value hand" in text,
              f"A4s 应标明是投机牌而不是价值牌：{text}")
        check("value/isolation" not in text,
              f"A4s 不该被说成 value/isolation：{text}")
        check("over-limp" in text, f"A4s 应给出 over-limp 选项：{text}")

    # 3 个 limper 出局时，不能套「dominated offsuit」的理由（A4s 是同花）
    out = range_verdict("CO", ("Ah", "4h"), table_dynamics=loose, limpers=3)
    check(out is not None and out.isolation_ok is False, "A4s 3 limper 应出局")
    text = out.render() if out else ""
    check("speculative" in text.lower() and "over-limp" in text,
          f"出局的 A4s 应给「便宜看翻牌」的理由：{text}")
    check("Dominated offsuit" not in text,
          f"A4s 是同花，不能说成 dominated offsuit：{text}")

    # 真正的价值牌仍走 value/isolation
    ato = range_verdict("CO", ("Th", "Ad"), table_dynamics=loose, limpers=2)
    check(ato is not None and ato.isolation_ok is True, "ATo 2 limper 可隔离")
    check("value/isolation" in (ato.render() if ato else ""),
          "ATo 是价值牌，应保留 value/isolation")

    jj = range_verdict("LJ", ("Jc", "Jd"), table_dynamics=loose, limpers=2)
    check("value/isolation" in (jj.render() if jj else ""),
          "JJ 是价值牌，不该走投机牌文案")


def test_prompt_hierarchy_table_dynamics_first() -> None:
    """提示词必须把优先级写清楚：桌风 > 图表默认 > 通用启发。"""
    check("Priority (highest first)" in _LEAN_SYSTEM, "提示词缺优先级段")
    check("outranks the chart" in _LEAN_SYSTEM, "桌风应明确高于图表")
    check("never override table dynamics" in _LEAN_SYSTEM,
          "图表应明确不能覆盖桌风")


def test_sb_low_straight_note() -> None:
    """43o 有顺子结构，弃牌理由不能说「没有顺子潜力」。

    h60（2026-10-23）：SB 43o，补 0.5BB 进 9BB。模型说「no flush or straight
    potential」——但 43 能成 A-5/2-6/3-7，只是底端顺。理由要说对，否则用户
    会以为规则把 43o 当成了 92o 那种纯垃圾。
    """
    v = range_verdict("SB", ("3h", "4s"))
    text = v.render()
    check("low-end straight" in text, f"43o 应说低端顺：{text}")
    check("no straight potential" not in text, f"43o 不能说没有顺子潜力：{text}")
    check("FOLD" in text, f"43o 仍应默认弃牌：{text}")

    v92 = range_verdict("SB", ("9d", "2h"))
    check("limited straight" in v92.render() and "FOLD" in v92.render(),
          f"92o 应说顺子潜力有限：{v92.render()}")


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
        test_sb_complete_requires_playability,
        test_small_pair_overlimp_exception, test_table_dynamics_outranks_chart,
        test_isolation_range_tightens_over_limpers,
        test_set_mine_pairs_below_tt,
        test_speculative_hands_not_value_isolation,
        test_sb_low_straight_note,
        test_prompt_hierarchy_table_dynamics_first, test_utg2_exists,
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
