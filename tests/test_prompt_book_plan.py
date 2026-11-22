#!/usr/bin/env python3
"""书中计划段接进真实提示词（票 #80）的验收。

    nv tests/test_prompt_book_plan.py

覆盖四组：
  1. 与 replay 同一条生产路径（`build_situation` → `build_messages`）上，
     3+ 人池翻牌强成牌的 `build_messages()` 真的出现
     「## 书中计划」，局面/计划/理由/来源可读，且旧段与 options 契约不动；
     快照按 replay 导入格式手搓（本地无决策库），不是 mock 渲染器；
  2. 零匹配写「书中无此局面」，整段不出现任何书页数字；
  3. 书计划段硬上限 ≤800 字（含坏卡），不挤掉事实与可选动作；
  4. 来源行格式：每一页都带 p，多页不出现孤立数字；解析异常 fail open。
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import build_messages, build_situation  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.playbook import load_cards  # noqa: E402
from poker_ocr.playbook import prompt as book_prompt  # noqa: E402
from poker_ocr.playbook.prompt import (  # noqa: E402
    BOOK_PLAN_CHAR_LIMIT, render_book_plan_text,
)
from poker_ocr.prompt.user import book as book_section  # noqa: E402

CARDS = ROOT / "poker_ocr/playbook/cards"
BOOK_HEADER = "## 书中计划（Harrington 现金桌）"
OPTIONS_HEADER = "## 可选动作"
SOURCE_RE = re.compile(
    r"^计划: .+（第 \d+ 卷 p\d+( / p\d+)*，(书中明确|编译归纳)）$")

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _multiway_snapshot(*, street: str = "flop", to_call: float = 0.0) -> HandSnapshot:
    """3+ 人池（3 名对手）、hero 拿 KK 击中暗三：replay 里最常见的强成牌节点。"""
    board = {"flop": ("Ks", "7d", "2c"), "turn": ("Ks", "7d", "2c", "9h"),
             "river": ("Ks", "7d", "2c", "9h", "3s")}[street]
    opponents = tuple(
        OpponentSnapshot(f"p{i}", None, f"p{i}", 100.0, 0.0, False)
        for i in range(3))
    return HandSnapshot(
        hand_id="book-wire", hand_index=0, street=street,
        board=board, hero_cards=("Kh", "Kd"),
        hero_position="BTN", hero_stack=100.0, hero_bet=0.0,
        hero_invested=4.0, to_call=to_call, second_bet=None,
        pot=10.0, pot_before_street=9.0, table_size=4,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=opponents,
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "BTN", "raise", 4.0,
                             False, True),
            )),
            StreetRecord(street, 9.0, (
                ActionRecord(street, "hero", None, "BTN", "check", 0.0,
                             False, True),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("book-wire", street, 0, 10.0, 0.0), warnings=(),
        bomb_pot=False, players_dealt=4,
    )


def _sit(*, street: str = "flop", to_call: float = 0.0):
    return build_situation(_multiway_snapshot(street=street, to_call=to_call),
                           table_dynamics="")


def _section(user: str) -> str:
    """取「## 书中计划」到下一个二级标题之间的文本。"""
    start = user.index(BOOK_HEADER)
    end = user.find("\n\n## ", start)
    return user[start:] if end < 0 else user[start:end]


def _cards() -> tuple:
    cards = []
    for path in sorted(CARDS.glob("*.json")):
        cards.extend(load_cards(path))
    return tuple(cards)


def test_replay_decision_prompt_contains_readable_book_plan() -> None:
    """① 真实决策链路：段在、四行可读、旧段与 options 都还在。"""
    sit = _sit()
    _, user = build_messages(sit)
    check(BOOK_HEADER in user, "build_messages 没有书中计划段")
    section = _section(user)
    lines = section.split("\n")
    check(lines[0] == BOOK_HEADER, f"段首必须是标题：{lines[0]!r}")
    check(any(line.startswith("局面: 翻牌 / 3+ 人 / 强成牌")
              for line in lines), f"局面行不可读：{section}")
    plan_lines = [line for line in lines if line.startswith("计划: ")]
    check(len(plan_lines) == 1, f"计划行应恰好一行：{plan_lines}")
    check("下注 0.67 池" in plan_lines[0],
          f"计划动作/尺寸不对：{plan_lines}")
    check(any(line.startswith("理由: ") for line in lines),
          f"理由行缺失：{section}")
    check(any(line.startswith("后续/转牌: ") for line in lines),
          f"后续行缺失：{section}")
    check(SOURCE_RE.match(plan_lines[0]) is not None,
          f"来源行格式不符：{plan_lines[0]!r}")
    # 只加不删：旧策略段与 options 契约仍在，且顺序是 规则/牌力 -> 书计划 -> options。
    check(OPTIONS_HEADER in user, "options 段被挤掉了")
    check(user.index(BOOK_HEADER) < user.index(OPTIONS_HEADER),
          "书计划段必须在 options 之前")
    system, user2 = build_messages(sit)
    check(user2 == user, "同一局面两次渲染不一致（书层做了随机选择）")


def test_zero_match_says_no_book_spot_without_page_number() -> None:
    """② 零匹配：明说书中无此局面，不伪造页码。

    河牌强成牌面对下注故意留空（两对 vs 三条以上在 Spot 里拆不开，
    见设计稿 M8 残余），用真实生产路径验证零匹配，而不是塞空卡集。
    """
    sit = _sit(street="river", to_call=5.0)
    _, user = build_messages(sit)
    section = _section(user)
    check("书中无此局面" in section, f"零匹配没明说：{section}")
    check(re.search(r"p\d", section) is None,
          f"零匹配不该出现书页数字：{section}")


def _big_card():
    cards = [card for card in _cards() if card.id == "flop.mw.strong_made.bet"]
    check(len(cards) == 1, "找不到强成牌多人池卡")
    card = cards[0]
    future = {f"street{i}": "后续计划" * 200 for i in range(6)}
    return replace(
        card,
        why="理由" * 500,
        plan=replace(card.plan, future=future),
    )


def test_book_plan_section_fits_hard_budget() -> None:
    """③ 坏卡也不能超 800 字：先丢后续/混合，再截断，主干保留。"""
    with patch.object(book_prompt, "_all_cards", return_value=(_big_card(),)):
        text = render_book_plan_text(_sit())
        _, user = build_messages(_sit())
    check(len(text) <= BOOK_PLAN_CHAR_LIMIT,
          f"书计划段超预算：{len(text)} > {BOOK_PLAN_CHAR_LIMIT}")
    check(text.startswith(BOOK_HEADER), "超预算截断把标题丢了")
    section = _section(user)
    check(len(section) <= BOOK_PLAN_CHAR_LIMIT,
          f"真实提示词里的书计划段超预算：{len(section)}")
    check(all("\n" not in line for line in section.split("\n")),
          "书计划段每一行必须是独立元素")
    check(OPTIONS_HEADER in user, "截断挤掉了 options")


def test_source_line_marks_every_page() -> None:
    """④ 多页来源：每页都带 p，不能出现「p186、190」这种孤立数字。"""
    card = [c for c in _cards() if c.id == "flop.mw.strong_made.bet"][0]
    card = replace(card, pages=(186, 190), confidence="explicit")
    with patch.object(book_prompt, "_all_cards", return_value=(card,)):
        text = render_book_plan_text(_sit())
    check("第 1 卷 p186 / p190，书中明确" in text,
          f"多页来源格式不对：{text}")


def test_render_failure_fails_open() -> None:
    """⑤ 书层异常不能阻塞主循环：整段消失，事实与 options 原样。"""
    with patch.object(book_section, "_render_book_plan_zh",
                      side_effect=RuntimeError("bad card")):
        _, user = build_messages(_sit())
    check(BOOK_HEADER not in user, "异常时不该渲染半截书计划段")
    check(OPTIONS_HEADER in user, "异常时 options 段必须还在")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {test.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n✅ 书中计划段全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
