#!/usr/bin/env python3
"""`tools/ab_diff.py` 的段落级解析测试。

    .venv/bin/python tests/test_ab_diff.py

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

from tools.ab_diff import changed_sections, sections  # noqa: E402

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
