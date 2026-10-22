#!/usr/bin/env python3
"""字段解析的单元测试（M2.3）：全部用例都来自真机视频里的**实际 OCR 原文**。

这一层的错误都是「安静错值」：把 `入池27%` 解析成 27、把房间号拼进盲注算出一个
sb≈5e-11、把 `11B` 当成没有单位。所以这里既测「该解析的」，也测「必须拒绝的」。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_parse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.parse import (  # noqa: E402
    NameTracker, is_all_in_badge, is_fold_badge, names_similar, parse_action,
    parse_amount, parse_blinds, parse_chip_loose, parse_equity, parse_percent,
    sticky_name,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# ─────────────────────────── 金额 ───────────────────────────

# 真机视频里实际出现过的形状（来自 images/video/video1.mp4 的 213 帧）
AMOUNT_OK = {
    "242.5BB": 242.5, "86.5BB": 86.5, "01BB": 1.0, "00.5BB": 0.5,
    "11B": 11.0, "9BBO": 9.0, "0.5BBO": 0.5, "C6BB": 6.0,
    "O0.5BB": 0.5, "C1BB": 1.0, "1BBO": 1.0, "50.5BB": 50.5,
}
AMOUNT_REJECT = [
    "对子", "葫芦", "两对", "我", "利东的",          # 摊牌标签 / 名字
    "入池27%", "胜率15%", "%带入400", "100%", "82%",  # 排行榜面板
    "200", "88.", "60.", "BB", "-80",                 # 没单位 = 不可信
    "Dragon33", "奶酪味的", "V", "24",                # 其它噪声
]
AMOUNT_ZERO = ["0", "０", "。", "o", "O", "〇"]


def test_amount_accepts_real_shapes() -> None:
    for text, want in AMOUNT_OK.items():
        a = parse_amount(text)
        check(a.ok and abs(a.value - want) < 1e-9,
              f"{text!r} 应解析成 {want}，实际 {a.value!r}（{a.error}）")


def test_amount_rejects_noise_without_unit() -> None:
    """没有 BB 单位的一律不采信 —— 这是防 `BB→88` 静默错值的唯一闸。"""
    for text in AMOUNT_REJECT:
        a = parse_amount(text)
        check(not a.ok, f"{text!r} 不该被解析成金额，实际 {a.value!r}")


def test_amount_zero_glyphs() -> None:
    """All-in / 正在带入的裸 0 会被读成各种圈圈，这些是真实的 0。"""
    for text in AMOUNT_ZERO:
        a = parse_amount(text)
        check(a.ok and a.value == 0.0 and a.zero_no_unit,
              f"{text!r} 应解析成「无单位 0」，实际 {a.to_debug()}")


def test_parse_chip_loose() -> None:
    cases = {
        "1,234": 1234.0, "$1,234": 1234.0, "243DB": 243.0,
        "478.5B8": 478.5, "460.538": 460.538, "93.500": 93.5, "18B": 18.0,
    }
    for text, want in cases.items():
        got = parse_chip_loose(text)
        check(got == want, f"宽松解析 {text!r} 应为 {want}，实际 {got}")
    for text in ("星际小杨66", "1/2", "21:36", "200代入拉新300手100",
                 "%", "弃牌", ""):
        got = parse_chip_loose(text)
        check(got is None, f"{text!r} 不该被宽松解析成筹码，实际 {got}")


def test_amount_empty_is_not_zero() -> None:
    """空文本 ≠ 0：空表示这块没渲染（比如没有下注 pill），由调用方决定。"""
    a = parse_amount("")
    check(not a.ok and a.error == "空文本", f"空文本不该是 0，实际 {a.to_debug()}")


# ─────────────────────────── 盲注 ───────────────────────────

def test_blinds_variants() -> None:
    b = parse_blinds("1/2(1）52122200:39:20")
    check(b is not None and (b.sb, b.bb, b.ante) == (0.5, 1.0, 0.5),
          f"1/2(1) 应得 0.5/1/0.5，实际 {b}")

    b = parse_blinds("1/2）52122200:39:38")
    check(b is not None and (b.sb, b.bb, b.ante) == (0.5, 1.0, None),
          f"前注括号读丢时 ante 应为 None（由上层继承），实际 {b}")

    b = parse_blinds("1/2(）522200:39:32")
    check(b is not None and b.ante is None, f"空括号 -> ante None，实际 {b}")

    b = parse_blinds("2/5(0.5)")
    check(b is not None and abs(b.sb - 0.4) < 1e-9 and abs(b.ante - 0.1) < 1e-9,
          f"2/5 应按 1BB=5 筹码折算，实际 {b}")


def test_blinds_rejects_fused_room_number() -> None:
    """房间号会被粘进盲注行。这些必须拒绝（→ 继承上一帧），不能算出荒谬值。"""
    for text in ("1/20052220039:39", "1/21）522200:39:28",
                 "1/20）52122200:38:04", "12（1）521222", "Allin金场梦高中", ""):
        b = parse_blinds(text)
        check(b is None, f"{text!r} 不该被解析成盲注，实际 {b}")


def test_blinds_explicit_override_allows_high_ratio() -> None:
    """人工指定的盲注允许 1/20 这类高比例（2026-10-19 实测桌面）；
    OCR 自动读数仍用默认上限，不能把融合产物放进来。"""
    b = parse_blinds("1/20", max_ratio=50.0)
    check(b is not None and (b.sb, b.bb) == (0.05, 1.0),
          f"显式指定 1/20 应得 sb=0.05 / bb=1，实际 {b}")
    check(parse_blinds("1/20") is None, "OCR 默认路径仍应拒绝 1/20")
    check(parse_blinds("1/21", max_ratio=50.0) is not None,
          "显式允许到 50 时 1/21 可解析（最终以用户显式输入为准）")


def test_blinds_rejects_bad_ratio() -> None:
    """bb/sb 必须落在 2–10：真实的 1/2、2/5、5/10 都在里面，融合产物不在。"""
    check(parse_blinds("1/2") is not None, "1/2 应通过")
    check(parse_blinds("2/5") is not None, "2/5 应通过")
    check(parse_blinds("5/10") is not None, "5/10 应通过")
    check(parse_blinds("1/20") is None, "1/20 比率 20，应拒绝")
    check(parse_blinds("1/1") is None, "1/1 比率 1，应拒绝")
    check(parse_blinds("0/2") is None, "0/2 应拒绝")


# ─────────────────────────── 动作 / 弃牌 / 名字 ───────────────────────────

def test_actions() -> None:
    want = {"让牌": "check", "跟注": "call", "下注": "bet",
            "加注": "raise", "弃牌": "fold"}
    for text, action in want.items():
        check(parse_action(text) == action, f"{text!r} 应映射到 {action}")
    for text in ("gggamel", "YKY", "一只猫ii", "", None):
        check(parse_action(text) is None, f"{text!r} 不该被当成动作")


def test_fold_badge_is_best_effort() -> None:
    """弃牌徽章被 rec 系统性读成形近字（弃→奔），按首字判；没读到要报 None。"""
    check(is_fold_badge("奔牌", 0.92) is True, "奔牌(0.92) 应判弃牌")
    check(is_fold_badge("弃牌", 0.9) is True, "弃牌 应判弃牌")
    check(is_fold_badge("奔#", 0.5) is True, "奔#(0.5) 应判弃牌")
    check(is_fold_badge("", 0.9) is None, "空读数应报 None（≠ 没弃牌）")
    check(is_fold_badge("奔牌", 0.2) is None, "分数太低应报 None")
    check(is_fold_badge("国", 0.9) is False, "头像照片的噪声不该判弃牌")


def test_all_in_badge_and_equity() -> None:
    """头像 All-in 徽章会被读成 Alin；筹码位的 `30%` 是胜率，不是下注。"""
    check(is_all_in_badge("Alin", 0.74) is True, "Alin(0.74) 应判 All in")
    check(is_all_in_badge("ALL IN", 0.8) is True, "ALL IN 应判 All in")
    check(is_all_in_badge("aiin", 0.6) is True, "aiin(0.6) 应判 All in")
    check(is_all_in_badge("奔牌", 0.92) is False, "弃牌徽章不是 All in")
    check(is_all_in_badge("", 0.9) is None, "空读数应报 None")
    check(is_all_in_badge("Alin", 0.2) is None, "分数太低应报 None")

    check(parse_percent("30%") == 30.0, "30% 应解析成 30")
    check(parse_percent("胜率 69%") == 69.0, "胜率 69% 应解析成 69")
    check(parse_percent("入池36%") is None, "HUD 的入池36% 不是胜率")
    check(parse_percent("15.5BB") is None, "金额不是胜率")
    check(parse_equity("69", allow_bare=True) == 69.0,
          "胜率槽的裸数字 69 应能解析")
    check(parse_equity("69") is None, "非胜率槽不接受裸数字")


def test_names_similar() -> None:
    """实测抖动要合并，真实换人要分开。"""
    for a, b in (("YKY", "YkY"), ("YKY", "YKkY"), ("一只猫i", "一只猫ii"),
                 ("Dragon333", "Oragon333"), ("星际小杨66", "星际小杨 66")):
        check(names_similar(a, b), f"{a!r} 与 {b!r} 应视为同一人")
    for a, b in (("Dragon333", "咸蛋鸡柳饭"), ("YKY", "一只猫ii"),
                 ("伯克利丁真", "kelly828")):
        check(not names_similar(a, b), f"{a!r} 与 {b!r} 不该视为同一人")


def test_sticky_name() -> None:
    check(sticky_name("YKY", "YkY") == "YKY", "相似读法应保留首读")
    check(sticky_name("YKY", None) == "YKY", "没读到时保留旧名字")
    check(sticky_name("YKY", "咸蛋鸡柳饭") == "咸蛋鸡柳饭", "明显不同应更新")


def test_name_tracker_needs_confirmation() -> None:
    """名字要连续确认 N 帧才换 —— 单帧/两帧的乱码不能改身份。"""
    t = NameTracker(confirm=3)
    check(t.resolve("lowL", "YKY") == "YKY", "首次直接认")
    check(t.resolve("lowL", "YkY") == "YKY", "相似读法保持")
    check(t.resolve("lowL", "東Yk丫") == "YKY", "单帧乱码保持")
    check(t.resolve("lowL", "YKkY") == "YKY", "候选被打断，重新计数")
    check(t.resolve("lowL", "跟注", action="call") == "YKY", "动作徽章盖住名字时沿用")
    # 真正的换人：连续 3 帧同一个新名字才换
    check(t.resolve("lowL", "咸蛋鸡柳饭") == "YKY", "第 1 帧不换")
    check(t.resolve("lowL", "咸蛋鸡柳饭") == "YKY", "第 2 帧不换")
    check(t.resolve("lowL", "咸蛋鸡柳饭") == "咸蛋鸡柳饭", "第 3 帧确认换人")


def main() -> int:
    tests = [
        test_amount_accepts_real_shapes,
        test_amount_rejects_noise_without_unit,
        test_amount_zero_glyphs,
        test_parse_chip_loose,
        test_amount_empty_is_not_zero,
        test_blinds_variants,
        test_blinds_rejects_fused_room_number,
        test_blinds_explicit_override_allows_high_ratio,
        test_blinds_rejects_bad_ratio,
        test_actions,
        test_fold_badge_is_best_effort,
        test_all_in_badge_and_equity,
        test_names_similar,
        test_sticky_name,
        test_name_tracker_needs_confirmation,
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
    print("\n✅ 字段解析全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
