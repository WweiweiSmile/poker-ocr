#!/usr/bin/env python3
"""名字词表的单元测试。

用例全部来自真实读数（2026-11-01 那次 `20261101-7cf9412b92` 的会话）和
「必须拒绝」的近邻名字。这一层最怕的不是漏纠正，而是**把两个真名并成一个**，
所以「不该改」的用例和「该改」的一样多。

不依赖 pytest：
    .venv/bin/python tests/test_name_lexicon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.name_lexicon import (  # noqa: E402
    NameLexicon, duplicate_pairs, normalize_name, shared_prefix,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# 那一桌的真实名单（画像库里都认识）
TABLES = ["台风来咯咯", "爽歪的麻雀", "兄弟你吃完", "多读书无数",
          "俊伯老公", "深海潜水艇", "两发入魂", "svsvbmbm"]


def test_exact_match_is_identity() -> None:
    """本身就是已知拼法：原样返回，不动。"""
    lex = NameLexicon(TABLES)
    for name in TABLES:
        m = lex.match(name)
        check(m is not None and m.kind == "exact" and m.auto,
              f"{name!r} 应该是 exact 命中，实际 {m}")
        check(lex.canonical(name) == name, f"{name!r} 不该被改写")


def test_alias_maps_to_canonical() -> None:
    """人工别名优先于模糊匹配。"""
    lex = NameLexicon(TABLES, {"台风来吧多": "台风来咯咯"})
    m = lex.match("台风来吧多")
    check(m is not None and m.kind == "alias" and m.text == "台风来咯咯",
          f"别名应直接命中，实际 {m}")


def test_prefix_rule_fixes_broken_tail() -> None:
    """`台风来咯咯` → `台风来吧多`：前缀完全一致、尾巴被读坏 → 自动改。

    这就是 2026-11-01 的那次错读（相似度 0.60，低于任何「安全阈值」）。
    """
    lex = NameLexicon(TABLES)
    m = lex.match("台风来吧多")
    check(m is not None and m.text == "台风来咯咯" and m.auto,
          f"尾巴读坏应按已知拼法纠正，实际 {m}")
    check(m is not None and m.kind == "similar-prefix",
          f"应记成前缀规则命中，实际 {m}")
    check(lex.canonical("台风来吧多") == "台风来咯咯", "canonical 应直接给出规范拼写")


def test_latin_typo_is_only_suspect() -> None:
    """`ssvhmbm` vs `svsvbmbm`（0.667，公共前缀只有 1）**不自动改**，只告警。

    英文 ID 里 0.6x 的相似太常见了（`Alex01`/`Alex02` 是两个人），自动并名
    的代价比错一个名字大得多。这一档交给「票数」和人工别名处理。
    """
    lex = NameLexicon(TABLES)
    m = lex.match("ssvhmbm")
    check(m is not None and not m.auto and m.kind == "similar-suspect",
          f"拉丁 ID 的近似读法只应告警，实际 {m}")
    check(lex.canonical("ssvhmbm") is None, "没有把握时不能改")


def test_ambiguous_match_is_skipped() -> None:
    """同时像两个已知名字 → 不动（领先第二名不够）。"""
    lex = NameLexicon(["两发入魂", "两发入神"])
    check(lex.canonical("两发入瑰") is None,
          f"两个候选一样像时不该挑一个：{lex.match('两发入瑰')}")


def test_short_names_do_not_use_prefix_rule() -> None:
    """短名字的「公共前缀 2 个字」没有信息量，不能据此并名。"""
    lex = NameLexicon(["陈凤呀"])
    m = lex.match("陈凤吗")
    check(m is None or not m.auto,
          f"3 字名字只对上前 2 个字时不该自动改，实际 {m}")


def test_two_live_players_are_never_merged() -> None:
    """`俊伯老公` / `俊伯老婆`（0.75）是两个人：这两道闸都不能放过它。

    第一道是前缀规则只对 5 字名生效（4 字名差 1 个字就是 0.75，太常见）；
    第二道是 exclude —— 那个名字此刻正被另一个座位用着。
    """
    lex = NameLexicon(["俊伯老公"])
    m = lex.match("俊伯老婆")
    check(m is None or not m.auto,
          f"4 字名的 0.75 相似度只能告警，不能自动并：{m}")
    check(lex.canonical("俊伯老婆") is None, "没有上下文时也不能自动改")
    check(lex.canonical("俊伯老婆", exclude=["俊伯老公"]) is None,
          "另一个座位已经叫「俊伯老公」时更不能算成他")


def test_prefix_rule_needs_five_chars() -> None:
    """5 字名才用「前缀一致」这条规则；4 字名一律只告警。"""
    lex = NameLexicon(["深海潜水艇"])
    m = lex.match("深海潜水延")
    check(m is not None and m.auto and m.kind == "similar",
          f"5 字名 0.80 应走相似规则自动改：{m}")
    short = NameLexicon(["俊伯老公"]).match("俊伯老八")
    check(short is None or not short.auto,
          f"4 字名不该自动改：{short}")


def test_exclude_ignores_irrelevant_names() -> None:
    """exclude 里是无关名字时不影响纠正。"""
    lex = NameLexicon(TABLES)
    check(lex.canonical("台风来吧多", exclude=["爽歪的麻雀"]) == "台风来咯咯",
          "别的座位叫别的名字时不该挡住纠正")


def test_remember_does_not_overwrite() -> None:
    """先入为主：已经认识的拼法不会被后来的读数覆盖。"""
    lex = NameLexicon(["台风来咯咯"])
    lex.remember("台风来吧多")
    check(lex.canonical("台风来吧多") == "台风来咯咯",
          "remember 不能把规范拼写改掉")
    check(len(lex) == 1, f"词表大小不该变，实际 {len(lex)}")


def test_normalization_and_prefix() -> None:
    """全角/空格/大小写都归一化后再比；公共前缀按归一化形式算。"""
    check(normalize_name("  多读书   无数 ") == "多读书 无数",
          f"空白应压缩：{normalize_name('  多读书   无数 ')!r}")
    lex = NameLexicon(["svsvbmbm"])
    check(lex.canonical("sVsybmbm") == "svsvbmbm", "大小写不同应算同一个拼法")
    check(lex.canonical("多读书 无数") is None, "词表里没有的名字不要硬凑")
    check(shared_prefix("台风来吧多", "台风来咯咯") == 3, "公共前缀应是 3")


def test_corrector_protocol() -> None:
    """NameLexicon 直接当 NameTracker 的 corrector 用（签名带 seat_id）。"""
    lex = NameLexicon(TABLES)
    check(lex("台风来吧多", "midL") == "台风来咯咯",
          "带 seat_id 调用应照常纠正")
    check(lex("台风来咯咯", "midL") == "台风来咯咯", "已知拼法原样返回")
    check(lex("", "midL") is None, "空读数不纠正")


def test_duplicate_pairs_reports_existing_split() -> None:
    """已经裂成两条的拼法：运行时改不了（两边都是已知名字），要能报出来。"""
    pairs = duplicate_pairs(["台风来咯咯", "台风来吧多", "爽歪的麻雀"])
    check(len(pairs) == 1, f"应只有一对相似名字：{pairs}")
    ratio, a, b = pairs[0]
    check({a, b} == {"台风来咯咯", "台风来吧多"}, f"这一对报错了：{pairs}")
    check(abs(ratio - 0.6) < 1e-9, f"相似度应是 0.6，实际 {ratio}")
    # 归一化后完全相同的（大小写/空格）不是「两个人」，别重复报
    check(duplicate_pairs(["盘盘AAAA", "盘盘 AAAA"]) == [],
          "只差空格的名字应在归一化后去重")


def main() -> int:
    tests = [
        test_exact_match_is_identity,
        test_alias_maps_to_canonical,
        test_prefix_rule_fixes_broken_tail,
        test_latin_typo_is_only_suspect,
        test_ambiguous_match_is_skipped,
        test_short_names_do_not_use_prefix_rule,
        test_two_live_players_are_never_merged,
        test_prefix_rule_needs_five_chars,
        test_exclude_ignores_irrelevant_names,
        test_remember_does_not_overwrite,
        test_normalization_and_prefix,
        test_corrector_protocol,
        test_duplicate_pairs_reports_existing_split,
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
    print("\n✅ 名字词表全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
