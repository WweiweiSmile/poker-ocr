#!/usr/bin/env python3
"""`tools/ab_diff.py` 的段落级解析测试。

    nv tests/test_ab_diff.py

事实块是按 `## 段落名` 组织的。归因正确与否全看这两件事：

  · 切段正确（标题里的「（逐座位…）」这类渲染说明要归一到同一个段落名）；
  · 只有**内容**不同才算变化（排版差一个空行不该算）。

否则「哪一类事实导致动作变化」就会算错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ab_diff import (  # noqa: E402
    changed_sections,
    line_attribution,
    line_diff,
    sections,
    witness_prefix,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


FACTS_A = """## 当前局面
Hero：BTN｜底池 10BB
## 对手画像（逐座位；Hero 下注后行动顺序）
- LJ 松凶（n=142）
## 可选动作
- call｜过牌
"""

FACTS_B = """## 当前局面
Hero：BTN｜底池 10BB
## 对手画像（逐座位；Hero 下注后行动顺序）
- LJ 松凶（n=150）
## 可选动作
- call｜过牌
"""


def test_sections_split() -> None:
    got = sections(FACTS_A)
    check(list(got) == ["当前局面", "对手画像", "可选动作"],
          f"段落名/顺序不对：{list(got)}")
    check("底池 10BB" in got["当前局面"], "正文应归到对应段落")
    check("逐座位" not in "".join(got), "标题里的括号说明不该进段落名")


def test_changed_sections_single() -> None:
    got = changed_sections({"prompt_facts": FACTS_A}, {"prompt_facts": FACTS_B})
    check(got == ["对手画像"], f"应只报对手画像变了，实际 {got}")


def test_changed_sections_none() -> None:
    got = changed_sections({"prompt_facts": FACTS_A}, {"prompt_facts": FACTS_A})
    check(got == [], f"完全相同应返回空，实际 {got}")


def test_changed_sections_multi() -> None:
    facts_c = FACTS_A.replace("Hero：BTN｜底池 10BB", "Hero：BTN｜底池 12BB")
    got = changed_sections({"prompt_facts": FACTS_A}, {"prompt_facts": facts_c})
    check(got == ["当前局面"], f"只改了当前局面，实际 {got}")
    facts_d = facts_c.replace("- call｜过牌", "- fold")
    got = changed_sections({"prompt_facts": FACTS_A}, {"prompt_facts": facts_d})
    check(got == ["当前局面", "可选动作"], f"两段都变了，实际 {got}")


def test_missing_section_counts_as_change() -> None:
    """一边有段、一边没有（例如翻前才有「翻前信息」）也算变化。"""
    facts = FACTS_A + "## 翻前信息\n单加注\n"
    got = changed_sections({"prompt_facts": FACTS_A}, {"prompt_facts": facts})
    check(got == ["翻前信息"], f"新增段落应被识别，实际 {got}")


def test_empty_facts() -> None:
    check(sections("") == {}, "空文本应返回空字典")
    check(changed_sections({"prompt_facts": ""}, {"prompt_facts": ""}) == [],
          "两边都空应无变化")


# ─────────────────── 段内行级：witness 行归一类 ───────────────────
#
# witness（摊牌证据）行落在「## 对手画像」段内部，和别的画像行混在一起。
# 段落级会把「画像里 n 从 142 变 150」和「多了一条证据行」算成同一件事；
# §15.3 要的是段内逐行：新增了几行、变化是不是**只**来自 witness 行。

WIT_A = """## 对手画像
- LJ 书麻头了wlr：STATION（松弱）（n=142，VPIP 41%，PFR 8%）
  打法：翻前偏松，跟注多
## 可选动作
- call｜过牌
"""

# 只多一条 witness 行：n 没动
WIT_ONLY = WIT_A.replace(
    "  打法：翻前偏松，跟注多\n",
    "  打法：翻前偏松，跟注多\n"
    "  摊牌证据：他在河牌IP过牌时亮过弱A顶对（1 次）\n")

# 同一条 witness 行次数变了（1 次 → 2 次）：仍属同一类变化
WIT_COUNT = WIT_ONLY.replace("（1 次）", "（2 次）")

# witness 行 + 画像统计一起变：归因不唯一
WIT_MIXED = WIT_ONLY.replace("n=142", "n=150")


def test_line_diff_counts_added_and_removed() -> None:
    added, removed = line_diff(WIT_A, WIT_MIXED)
    check(added == ["- LJ 书麻头了wlr：STATION（松弱）（n=150，VPIP 41%，PFR 8%）",
                    "摊牌证据：他在河牌IP过牌时亮过弱A顶对（1 次）"],
          f"新增行不对：{added}")
    check(removed == ["- LJ 书麻头了wlr：STATION（松弱）（n=142，VPIP 41%，PFR 8%）"],
          f"删除行不对：{removed}")


def test_line_diff_ignores_blank_lines_and_indent() -> None:
    """排版差一个空行 / 缩进不同不算内容变化。"""
    padded = WIT_A.replace("## 可选动作", "\n\n## 可选动作")
    added, removed = line_diff(WIT_A, padded)
    check((added, removed) == ([], []), f"排版差异不该算行变化：{added} / {removed}")


def test_attribution_only_witness() -> None:
    got = line_attribution({"prompt_facts": WIT_A}, {"prompt_facts": WIT_ONLY})
    check(got["witness_only"] is True, f"只多了 witness 行应判 witness_only：{got}")
    check(got["added_lines"] == 1 and got["witness_added"] == 1,
          f"应只新增 1 条 witness 行：{got}")
    check(got["other_added"] == 0 and got["other_removed"] == 0,
          f"不该有其它行变化：{got}")
    check(got["changed_sections"] == ["对手画像"],
          f"变化应落在对手画像段：{got['changed_sections']}")


def test_attribution_witness_count_change_is_same_class() -> None:
    """N 从 1 次变 2 次是同一类变化：不能因为文案不同算成「其它行」。"""
    got = line_attribution({"prompt_facts": WIT_ONLY}, {"prompt_facts": WIT_COUNT})
    check(got["witness_only"] is True, f"次数变化应仍判 witness_only：{got}")
    check(got["witness_added"] == 1 and got["witness_removed"] == 1,
          f"应记 1 增 1 删：{got}")


def test_attribution_mixed_is_not_only_witness() -> None:
    got = line_attribution({"prompt_facts": WIT_A}, {"prompt_facts": WIT_MIXED})
    check(got["witness_only"] is False, f"统计也变了就不能算只来自 witness：{got}")
    check(got["other_added"] == 1 and got["other_removed"] == 1,
          f"其它行应记 1 增 1 删：{got}")
    check(got["witness_added"] == 1, f"witness 行仍应计数：{got}")


def test_attribution_witness_removed_only() -> None:
    """反过来（B 里证据行消失）也算只来自 witness 行。"""
    got = line_attribution({"prompt_facts": WIT_ONLY}, {"prompt_facts": WIT_A})
    check(got["witness_only"] is True, f"只删 witness 行应判 witness_only：{got}")
    check(got["witness_removed"] == 1 and got["added_lines"] == 0,
          f"应记 0 增 1 删：{got}")


def test_attribution_unchanged() -> None:
    got = line_attribution({"prompt_facts": WIT_A}, {"prompt_facts": WIT_A})
    check(got["witness_only"] is False, "没变化不该判 witness_only")
    check(got["added_lines"] == 0 and got["removed_lines"] == 0, "没变化不该有增删")
    check(got["changed_sections"] == [], f"没变化不该报段落：{got['changed_sections']}")


def test_attribution_other_section_change() -> None:
    """witness 没变、别的段变了 → 不算只来自 witness。"""
    facts = WIT_ONLY.replace("- call｜过牌", "- fold")
    got = line_attribution({"prompt_facts": WIT_ONLY}, {"prompt_facts": facts})
    check(got["witness_only"] is False, f"别的段变了不能算 witness_only：{got}")
    check(got["other_added"] == 1, f"其它段的新增行应计入 other：{got}")


def test_attribution_custom_prefix() -> None:
    """前缀可换：换了词表也能认出同一类行。"""
    facts = WIT_A.replace("  打法：翻前偏松，跟注多\n",
                          "  打法：翻前偏松，跟注多\n  亮牌记录：他在河牌过牌\n")
    got = line_attribution({"prompt_facts": WIT_A}, {"prompt_facts": facts},
                           prefix="亮牌记录：")
    check(got["witness_only"] is True, f"自定义前缀应生效：{got}")
    got_default = line_attribution({"prompt_facts": WIT_A}, {"prompt_facts": facts})
    check(got_default["witness_only"] is False,
          f"默认前缀下不该认成 witness 行：{got_default}")


def test_witness_prefix_matches_phrasing_json() -> None:
    """工具认的前缀必须与渲染层读的是同一份词表，否则归因会静默错分类。"""
    import json

    path = ROOT / "poker_ocr" / "tables" / "witness_phrasing.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    check(witness_prefix() == data["prefix"],
          f"前缀不一致：工具 {witness_prefix()!r} vs 词表 {data['prefix']!r}")


def test_ab_merge_dedupes_and_screens_witness() -> None:
    """合并分批复跑：同键只留一条；--witness-only 只留带证据行的点。"""
    import json
    import tempfile

    from tools.ab_merge import merge

    row = {"hand": "h1", "street": "flop", "pot": 3.0, "to_call": 1.0,
           "new": "call", "witness": {"near": ["摊牌证据：x"]}}
    dup = dict(row, new="fold")           # 同键、后出现的应被丢掉
    bare = {"hand": "h2", "street": "flop", "pot": 2.0, "to_call": 0.5,
            "new": "fold", "witness": {}}
    stale = {"hand": "h3", "street": "", "new": "call"}  # 没街的旧帧不进合并

    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a.json"
        b = Path(tmp) / "b.json"
        a.write_text(json.dumps([row, bare], ensure_ascii=False), encoding="utf-8")
        b.write_text(json.dumps([dup, stale], ensure_ascii=False), encoding="utf-8")
        got = merge([a, b])
        screened = merge([a, b], witness_only=True)

    check(len(got) == 2, f"同键应去重、空街应丢弃：{len(got)} 条")
    check(got[0]["new"] == "call", "同键应保留先出现的那条")
    check(len(screened) == 1 and screened[0]["hand"] == "h1",
          f"--witness-only 应只剩带证据行的点：{screened}")


def main() -> int:
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)
             and getattr(v, "__module__", "") == __name__]
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
    print("\n✅ 段落级对比全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
