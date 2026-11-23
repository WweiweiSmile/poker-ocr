#!/usr/bin/env python3
"""#114 炸弹池守则：书卡层整体让位，普通局面逐字不变。

    nv tests/test_bomb_guard.py

覆盖：
  1. 炸弹池（横幅确认、翻前无动作）：不做书卡匹配、不出「书中无此局面」、
     不出书偏移，整段换成「## 炸弹池守则」，动作仍是本桌合法 options；
  2. 普通局面：`build_messages` 与本票改动前（c5839ff）的基线逐字节相同；
  3. 翻前有真实动作时（横幅串到下一手）：守则不生效，书计划照常，
     `Situation.conflicts` 的冲突提示仍在；
  4. 守则字符预算：超长先丢尾行，单行再截断加「…」，绝不撑爆提示词；
  5. 翻前炸弹池（无决策点）渲染不抛异常。
"""

from __future__ import annotations

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
from poker_ocr.options import build_options  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.playbook import prompt as book_prompt  # noqa: E402
from poker_ocr.playbook.offset import offsets_for_situation  # noqa: E402
from poker_ocr.playbook.prompt import render_book_plan_zh  # noqa: E402

BOOK_HEADER = "## 书中计划（Harrington 现金桌）"
OFFSET_HEADER = "## 偏移"
OPTIONS_HEADER = "## 可选动作"
BOMB_HEADER = "## 炸弹池守则"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _snapshot(*, street: str = "flop", bomb_pot: bool = False,
              preflop_actions: tuple[ActionRecord, ...] | None = None,
              table_size: int = 4) -> HandSnapshot:
    """3+ 人池；默认 hero 拿 KK 击中暗三（与 test_prompt_book_plan 同形）。

    `preflop_actions=None` 是普通局面的单加注线；传 `()` 则是炸弹池
    （翻前只有强制前注，没有下注轮）；传 ActionRecord 可测横幅串手的回退。
    """
    if preflop_actions is None:
        preflop_actions = (
            ActionRecord("preflop", "hero", None, "BTN", "raise", 4.0,
                         False, True),
        )
    board = {"preflop": (), "flop": ("Ks", "7d", "2c"),
             "turn": ("Ks", "7d", "2c", "9h"),
             "river": ("Ks", "7d", "2c", "9h", "3s")}[street]
    opponents = tuple(
        OpponentSnapshot(f"p{i}", None, f"p{i}", 100.0, 0.0, False)
        for i in range(3))
    pot = 45.0 if bomb_pot else 10.0
    streets = [StreetRecord("preflop", 45.0 if bomb_pot else 1.5,
                            preflop_actions)]
    if street != "preflop":
        streets.append(StreetRecord(street, pot, (
            ActionRecord(street, "hero", None, "BTN", "check", 0.0,
                         False, True),
        )))
    pot_before = 1.5 if street == "preflop" else (45.0 if bomb_pot else 9.0)
    return HandSnapshot(
        hand_id="bomb-guard", hand_index=0, street=street,
        board=board, hero_cards=("Kh", "Kd"),
        hero_position="BTN", hero_stack=100.0, hero_bet=0.0,
        hero_invested=0.0 if street == "preflop" else (
            5.0 if bomb_pot else 4.0), to_call=0.0,
        second_bet=None, pot=pot,
        pot_before_street=pot_before, table_size=table_size,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=opponents,
        streets=tuple(streets),
        preflop_shapes=() if bomb_pot else ("single-raise",),
        hero_is_pfr=not bomb_pot, cbet_spot=False,
        decision_key=("bomb-guard", street, 0, pot, 0.0), warnings=(),
        bomb_pot=bomb_pot, players_dealt=table_size,
    )


def _sit(*, street: str = "flop", bomb_pot: bool = False,
         preflop_actions: tuple[ActionRecord, ...] | None = None):
    return build_situation(
        _snapshot(street=street, bomb_pot=bomb_pot,
                  preflop_actions=preflop_actions),
        table_dynamics="")


def _section(user: str, header: str) -> str:
    start = user.index(header)
    end = user.find("\n\n## ", start)
    return user[start:] if end < 0 else user[start:end]


# ── 基线：改动前（c5839ff）普通局面 build_messages 的逐字节输出 ──
BASELINE_SYSTEM = """你是一名德州扑克现金局实时教练。Hero 只有几秒钟，需要你从「可选动作」中选一个能直接执行的动作。

## 决策依据
- 书中计划是基线：有「## 书中计划」段时，按书给的行动/尺寸/后续街执行；书要的动作若不在「可选动作」里，选同方向、最接近的档位，并在 reason 里说明。
- 偏移是显式修正：有「## 偏移」段时，它逐条给出「书上 -> 最终」（只改尺寸/频率），按最终值执行；没有偏移段就是书即最终。
- 书中无此局面（或没有「## 书中计划」段）时，只按事实与「可选动作」判断，不套用任何书页。
- 只能从「可选动作」里选一个动作；模型不发明动作、金额或概率。

## 事实优先级（序号小者优先）
1. 硬事实：牌/公共牌/底池/需跟/在局人数/后手；位置/行动线/有效筹码/底池类型来自 OCR，冲突时以当前帧的硬字段为准，并在 reason 里指出冲突的是哪一项。
2. 确定性计算：SPR、赔率门槛、牌型/坚果/outs、听牌价格、对当前范围权益。
3a. 逐座位摊牌证据（「摊牌证据：」行）：只说他过去在这桌亮过什么，不是当前牌力；次数是事例数，不是频率。
3b. 逐座位画像/本桌统计：类型、VPIP/PFR/AF、加注宽度、打法，只适用该玩家与节点，仍带假设；不得压 1/2。
4. 现场观察：.table_dy 的当前桌记录，只覆盖它明确描述的范围；不得压 1/2/3。
5. 通用经验。
同一层内「更具体」优先：摊牌证据 > 逐座位画像 > 本桌 > 全场统计。3a 压 3b；3a 不压 Hero 牌力事实（第 2 层）。

## 输出
只输出一个 JSON 对象（不要 Markdown/解释）：
{"option": "<id>", "reason": "<中文一句话，≤60 字>"}
可选（建议给）：加 "confidence": 0.0–1.0，表示这次动作在当前事实下有多确定；事实缺口越大、越接近混合策略就越低。
- option 必须严格来自「可选动作」，不得编造 id 或金额。
- reason 引用一个最关键的依据（牌力/赔率/位置/SPR/书中计划/偏移），不得与事实矛盾。
- 「数据质量」标为硬缺口时走保守线：能过牌就过牌；面对下注且赔率/筹码不明则弃，并在 reason 说明缺什么。范围/画像缺口只降低尺度与频率，不是禁止行动的理由。"""

BASELINE_USER = """## 当前局面
桌况：4 人桌｜盲注 0.5/1
本手在局：4 人（Hero + 3 名对手）
Hero：BTN｜底牌 Kh Kd｜后手 100BB
街道：翻牌｜公共牌 Ks 7d 2c｜底池 10BB｜需跟 0BB
有效筹码：100BB｜SPR 11.1｜底池类型：单加注池
有效位置：后位（BTN）｜位置未知（座位未读出）：不要假设有/无位置，按人数和行动线保守处理
可争夺底池：10BB
本手已投入：4BB

## 行动线
（行动线来自 OCR/视频解码，可能有归因或顺序误差；与「当前局面」硬事实冲突时，以硬事实为准。）
「未记录到行动」只表示没抓到，不等于没人行动；以底池变化和硬事实为准。
翻前（街前底池 1.5BB）：Hero 加注到 4BB
翻牌（街前底池 10BB）：Hero 过牌

## 实时牌力
牌力：三条(set)
当前牌型：三条
最好五张：Kh Kd Ks 7d 2c
坚果：是（没有任何对手持牌能打败你）
听牌：无
对当前范围权益：92%（按河牌、未计后续下注，4 人池；对手本街未行动，按沿用翻前范围估算）
当前成牌落后至少一家：0.0%（当前五张被至少一家压过的概率，不是输牌概率/权益）
多人池：实际 4 人 / 范围建模 3 名对手 / 公平份额 25.0%
范围：p0 未行动范围 ≈15.1%｜p1 未行动范围 ≈15.1%｜p2 未行动范围 ≈15.1%
- 本街还没有动作的座位：p0、p1、p2（范围沿用翻前先验，不是已过牌；不代表整条街没人下注）
- p0：冷跟先验 入池人数未定，沿用未分档先验（16.7%）；对手成牌领先我 0.0%（平局 0.0%；你领先 100.0%）
- p1：冷跟先验 入池人数未定，沿用未分档先验（16.7%）；对手成牌领先我 0.0%（平局 0.0%；你领先 100.0%）
- p2：冷跟先验 入池人数未定，沿用未分档先验（16.7%）；对手成牌领先我 0.0%（平局 0.0%；你领先 100.0%）
牌面结构：彩虹面（无同花可能）｜顺子不可能｜公对面：否

## 书中计划（Harrington 现金桌）
局面: 翻牌 / 3+ 人 / 强成牌 / 无人下注
计划: 下注 0.67 池（第 1 卷 p301，编译归纳）
理由: 强牌要下注，不能只在坚果牌时才下注（p301）；多人池尺度取 2/3 为编译。
后续/转牌: 转牌继续下注约 2/3 池，河牌按牌力收价值

## 对手画像（逐座位；Hero 下注后行动顺序）
（本手在局对手都没有可用画像）
无画像：p0、p1、p2（按未知对手处理，不伪造数据）

## 数据质量（硬缺口 = 可选动作已被降级，或底牌/公共牌缺失；其余只降低可信度）
- 范围/信息缺口（只降低可信度，不是禁止行动的事实）：部分座位本街行动线缺失，范围按未行动处理（不是已过牌）

## 可选动作
- call｜过牌
- raise-1/3｜下注 3.33BB（1/3 池）
- raise-1/2｜下注 5BB（1/2 池）
- raise-2/3｜下注 6.67BB（2/3 池）
- raise-1｜下注 10BB（满池）
- raise-1.2｜下注 12BB（1.2 池）"""


def test_ordinary_prompt_is_byte_identical_to_baseline() -> None:
    """② 普通局面逐字不变：同一份快照，改动前后的 build_messages 一字节不差。"""
    system, user = build_messages(_sit())
    check(system == BASELINE_SYSTEM,
          "普通局面的 system 与改动前基线不一致")
    check(user == BASELINE_USER,
          "普通局面的 user 事实块与改动前基线不一致")
    # 基线里本来就有书计划，普通局面不许被守则拦截。
    check(BOOK_HEADER in user, "普通局面必须保留书计划段")
    check(BOMB_HEADER not in user, "普通局面不许出现炸弹池守则")


def test_bomb_pot_yields_book_layer_to_guard() -> None:
    """① 炸弹池：书卡/偏移整体让位，守则出现，数据层也没有偏移。"""
    sit = _sit(bomb_pot=True, preflop_actions=())
    _, user = build_messages(sit)
    check(BOMB_HEADER in user, f"炸弹池应出现守则段：\n{user}")
    check(BOOK_HEADER not in user, f"炸弹池不该出现书计划段：\n{user}")
    check("书中无此局面" not in user,
          f"炸弹池不该写「书中无此局面」：\n{user}")
    check(OFFSET_HEADER not in user, f"炸弹池不该出现偏移段：\n{user}")
    check(sit.plan_offsets == (),
          f"炸弹池 plan_offsets 必须为空：{sit.plan_offsets}")
    check(offsets_for_situation(sit) == (),
          "炸弹池 offsets_for_situation 必须直接返回 ()")
    check(render_book_plan_zh(sit) == [],
          "炸弹池 render_book_plan_zh 必须返回 []（不做匹配）")
    check(user.index("## 实时牌力") < user.index(BOMB_HEADER)
          < user.index(OPTIONS_HEADER),
          "守则应占书计划原来的位置：牌力之后、可选动作之前")


def test_guard_suppresses_ante_offset_at_data_layer() -> None:
    """① 0.5BB 前注桌本来有偏移；炸弹池让位后数据层也不该有。"""
    from poker_ocr.playbook.schema import Card, Plan, Size, Spot

    ante = _snapshot(street="preflop", preflop_actions=())
    ante = replace(ante, blinds=Blinds(0.5, 1.0, 0.5, "1/2(1)"), pot=6.0,
                   pot_before_street=0.0, hero_invested=0.0,
                   preflop_shapes=("unopened",), table_size=9,
                   players_dealt=9, opponents=tuple(
                       OpponentSnapshot(f"p{i}", None, None, 100.0, 0.0,
                                        False) for i in range(8)))
    card = Card(
        id="preflop.demo.open", book="Harrington", volume=1,
        chapter="demo", pages=(1,), street="preflop",
        spot=Spot(street="preflop", preflop_node="unopened"),
        plan=Plan(action="raise", size=Size("bb", 3.0)),
        why="演示卡：翻前未开池按书开注", confidence="interpreted",
    )
    with patch.object(book_prompt, "_all_cards", return_value=(card,)):
        plain = build_situation(replace(ante, bomb_pot=False),
                               table_dynamics="")
        bomb = build_situation(replace(ante, bomb_pot=True), table_dynamics="")
    check(bool(plain.plan_offsets), "对照组（非炸弹池）应有偏移")
    check(bomb.plan_offsets == (),
          f"炸弹池数据层偏移必须为空：{bomb.plan_offsets}")
    check("## 偏移" not in build_messages(bomb)[1], "炸弹池不该出现偏移段")


def test_bomb_banner_conflict_falls_back_to_normal_path() -> None:
    """③ 横幅串到下一手：翻前有真实动作 ⇒ 不套守则，书层照常，冲突提示仍在。"""
    from poker_ocr.playbook.bomb import bomb_guard_applies

    sit = _sit(bomb_pot=True, preflop_actions=(
        ActionRecord("preflop", "p0", None, "UTG", "raise", 3.0, False, True),
    ))
    check(not bomb_guard_applies(sit), "翻前有动作时守则不该生效")
    _, user = build_messages(sit)
    check(BOMB_HEADER not in user, f"冲突时不该出现守则段：\n{user}")
    check(render_book_plan_zh(sit) != [], "冲突时书层不该被拦截")
    check(BOOK_HEADER in user, "冲突时书计划应照常渲染")
    check(any("炸弹池标记与翻前行动冲突" in c for c in sit.conflicts),
          f"冲突口径应留在 conflicts：{sit.conflicts}")
    check("炸弹池标记与翻前行动冲突" in user,
          "冲突提示应渲染进数据质量段")


def test_guard_never_resolves_book_card() -> None:
    """② 「不做匹配」是验收原文：守则生效时连书卡解析都不该发生。"""
    sit = _sit(bomb_pot=True, preflop_actions=())

    def _boom(*_args, **_kwargs):
        raise AssertionError("炸弹池守则生效时不该解析书卡")

    with patch.object(book_prompt, "resolve_for_situation", _boom):
        check(render_book_plan_zh(sit) == [],
              "守则生效时书计划必须直接返回 []")
        check(offsets_for_situation(sit) == (),
              "守则生效时偏移必须直接返回 ()")
        _, user = build_messages(sit)
    check(BOMB_HEADER in user, "不解析书卡时守则仍应出现")


def test_guard_content_covers_ticket_points() -> None:
    """① 守则内容覆盖票面 6 点，一行一条，不把换行塞进元素。"""
    from poker_ocr.prompt.user.bomb import render_bomb_guard_zh

    lines = render_bomb_guard_zh(_sit(bomb_pot=True, preflop_actions=()))
    text = "\n".join(lines)
    check(lines[0] == BOMB_HEADER, f"守则标题不对：{lines[0]!r}")
    for point in ("没有翻前下注轮",
                  "set（暗三）、两对、顺子、同花、坚果同花听牌",
                  "超对只在干燥面小注", "过牌或弃牌", "不诈唬",
                  "5BB 是沉没成本"):
        check(point in text, f"守则缺少票面要点：{point}")
    check(all("\n" not in line for line in lines), "单个元素里不许有换行")
    check(len(lines) >= 6, f"守则应至少 6 行：{len(lines)}")


def test_guard_char_budget_truncates_instead_of_overflow() -> None:
    """④ 字符预算：先丢尾行；单行仍超长时截断加「…」，不超过上限。"""
    from poker_ocr.prompt.user import bomb as bomb_section

    sit = _sit(bomb_pot=True, preflop_actions=())
    long_lines = tuple(f"- 第{i}条：" + "很长的守则" * 20 for i in range(6))
    with patch.object(bomb_section, "BOMB_GUARD_LINES", long_lines), \
            patch.object(bomb_section, "BOMB_GUARD_CHAR_LIMIT", 120):
        lines = bomb_section.render_bomb_guard_zh(sit)
    text = "\n".join(lines)
    check(len(text) <= 120, f"守则超预算：{len(text)} 字\n{text}")
    check(lines[0] == BOMB_HEADER, "丢行时标题必须保留")
    check(len(lines) < len(long_lines) + 1, "超长时应丢掉尾行")

    with patch.object(bomb_section, "BOMB_GUARD_LINES",
                      ("- " + "超长单行" * 100,)), \
            patch.object(bomb_section, "BOMB_GUARD_CHAR_LIMIT", 60):
        single = bomb_section.render_bomb_guard_zh(sit)
    single_text = "\n".join(single)
    check(len(single_text) <= 60, f"单行截断后仍超预算：{len(single_text)}")
    check(single[-1].endswith("…"), f"单行超长应以「…」截断：{single[-1]!r}")
    check(all("\n" not in line for line in single), "截断不许引入换行")


def test_guard_keeps_options_and_legal_actions() -> None:
    """⑤ 动作合法性：守则不碰 options，可选动作仍是同一批。"""
    sit = _sit(bomb_pot=True, preflop_actions=())
    expected = build_options(sit.snap)
    check([o.id for o in sit.options] == [o.id for o in expected],
          f"守则改变了 options：{[o.id for o in sit.options]}")
    _, user = build_messages(sit)
    options_text = _section(user, OPTIONS_HEADER)
    for option in sit.options:
        check(f"- {option.id}｜" in options_text,
              f"守则生效时缺少合法动作 {option.id}：\n{options_text}")


def test_preflop_bomb_pot_renders_without_crash() -> None:
    """⑤ 翻前炸弹池（没有决策点）：守则出现，整段渲染不抛异常。"""
    sit = _sit(street="preflop", bomb_pot=True, preflop_actions=())
    system, user = build_messages(sit)
    check(bool(system), "system 不该为空")
    check(BOMB_HEADER in user, f"翻前炸弹池也应给守则：\n{user}")
    check("书中无此局面" not in user, "翻前炸弹池不该写书中无此局面")
    check(BOOK_HEADER not in user, "翻前炸弹池不该出现书计划")


def test_explosive_wording_only_points_to_guard_when_it_applies() -> None:
    """守则不生效时（横幅串手/纯算术爆炸池）保留原文，不指向不存在的段。"""
    from poker_ocr.advice import render_explosive_zh

    plain = render_explosive_zh(_sit(bomb_pot=False))
    check(any("按正常翻后规则" in line for line in plain),
          f"非炸弹池的爆炸池文案应保持原文：{plain}")
    bomb = render_explosive_zh(_sit(bomb_pot=True, preflop_actions=()))
    check(any("炸弹池守则" in line for line in bomb),
          f"守则生效时应指向守则段：{bomb}")
    check(not any("按正常翻后规则" in line for line in bomb),
          f"守则生效时不该再说按正常规则打：{bomb}")


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
    print("\n✅ 炸弹池守则全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
