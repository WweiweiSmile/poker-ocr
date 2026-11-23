#!/usr/bin/env python3
"""tools/blind_review.py 的单测：决策键、分层抽样、盲卡片、幂等合并、汇总。

只测纯函数：不启动交互、不调模型、不连数据库。CLI 只做参数解析与文件读写。

    nv tests/test_blind_review.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import blind_review as br  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# ── 造数据 ──────────────────────────────────────────────────────────────

def option(oid: str, label: str, kind: str, amount: float | None = None) -> dict:
    out = {"id": oid, "label": label, "kind": kind}
    if amount is not None:
        out["amount_bb"] = amount
    return out


def advice(**over) -> dict:
    """一条接近实盘形状的 advice 记录（不含 decision_key，历史日志如此）。"""
    record = {
        "type": "advice",
        "ts": "2026-01-01T20:01:00+08:00",
        "hand_id": "h1",
        "street": "flop",
        "hero_cards": ["Ah", "Kd"],
        "board": ["Ks", "7d", "2c"],
        "pot": 12.0,
        "to_call": 2.0,
        "option": option("call", "跟注", "call", 2.0),
        "reason": "顶对，赔率合适",
        "source": "model",
        "prompt_version": "2026-01-01a",
        "facts": {
            "pot": 12.0,
            "to_call": 2.0,
            "hero_stack": 100.0,
            "opponents_in_hand": 2,
            "hero_position": "BTN",
            "warnings": ["某座位范围未知"],
            "options": [
                option("call", "跟注", "call", 2.0),
                option("fold", "弃牌", "fold"),
                option("raise-1/2", "加注到 6BB", "raise", 6.0),
            ],
        },
    }
    record.update(over)
    return record


def point(record: dict, key: str = "k1") -> br.Point:
    return br.Point(key=key, record=record)


def main() -> int:
    tests = [
        test_derive_key_prefers_existing_and_action_index,
        test_derive_key_falls_back_to_facts_and_collision_suffix,
        test_load_reads_only_advice_and_tolerates_bad_lines,
        test_assign_points_suffixes_duplicate_keys,
        test_sample_is_deterministic_per_seed,
        test_sample_stratifies_every_street_and_explains_short_input,
        test_blind_card_keeps_only_facts_and_options,
        test_blind_card_hides_model_and_book_hints_until_reveal,
        test_position_falls_back_to_situation_section,
        test_render_card_labels_and_action_line,
        test_blind_card_degrades_on_minimal_advice_record,
        test_review_row_and_merge_result_are_idempotent,
        test_unreviewed_skips_already_reviewed,
        test_summarize_reports_per_street_agreement_and_disagreements,
        test_parse_answer_splits_choice_confidence_and_note,
        test_build_parser_defaults_and_multiple_inputs,
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
    print("\n✅ 盲评 CLI 全部通过")
    return 0


def test_derive_key_prefers_existing_and_action_index() -> None:
    # 有现成决策键就用它（未来日志会带）；列表形式按同一口径拼。
    check(br.derive_key({"decision_key": ["h1", "flop", 2]}) == "dk:h1|flop|2",
          "显式 decision_key 应归一成 dk: 前缀")
    # 历史日志没有决策键：hand_id + street + action_index。
    check(br.derive_key({"hand_id": "h1", "street": "flop",
                         "action_index": 3}) == "h1|flop|a3",
          "有 action_index 时应用它派生")
    # 同一决策点无论 ts/reason 怎么变，键都不变（幂等续评的前提）。
    a = advice()
    b = dict(a, ts="2026-02-02T00:00:00+08:00", reason="换个理由")
    check(br.derive_key(a) == br.derive_key(b),
          "ts / reason 不应影响决策键")


def test_derive_key_falls_back_to_facts_and_collision_suffix() -> None:
    # 没有 action_index 时退化到「手牌+公共牌+需跟」。
    check(br.derive_key(advice()) == "h1|flop|AhKd|Ks7d2c|2",
          "退化键应是 hand|street|hero|board|to_call")
    # 数值按 BB 归一：2.0 与 2 同键，2.50 与 2.5 同键。
    check(br.derive_key(advice(to_call=2.0)) == br.derive_key(advice(to_call=2)),
          "to_call 2.0 与 2 应同键")
    check(br.derive_key({**advice(), "board": [], "to_call": 0.0})
          == "h1|flop|AhKd|-|0",
          "缺公共牌用 - 占位，0 需跟写成 0")
    # 完全相同的历史记录（同街同牌同需跟）靠出现序号消歧。
    check(br.derive_key(advice(), occurrence=2) == "h1|flop|AhKd|Ks7d2c|2#2",
          "重号记录应带 #n 后缀")
    check(br.derive_key(advice(), occurrence=1) == br.derive_key(advice()),
          "第一条重号记录不带后缀")


def test_load_reads_only_advice_and_tolerates_bad_lines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        first = tmp / "a.jsonl"
        first.write_text(
            json.dumps(advice(hand_id="h1")) + "\n"
            + "\n"                                     # 空行忽略
            + json.dumps({"type": "hand", "hand_id": "h1"}) + "\n"
            + "{不是 JSON\n"
            + "[1, 2, 3]\n"                            # 不是 JSON 对象
            + json.dumps(advice(hand_id="h2", street="turn")) + "\n",
            encoding="utf-8")
        second = tmp / "b.jsonl"
        second.write_text(
            json.dumps(advice(hand_id="h3", street="river")) + "\n",
            encoding="utf-8")
        records, bad = br.load([first, second])
        check([r["hand_id"] for r in records] == ["h1", "h2", "h3"],
              f"应只保留 advice 行并按输入顺序合并：{records}")
        check([r["type"] for r in records] == ["advice"] * 3,
              "load 只返回 advice 记录")
        check(len(bad) == 2, f"应记 2 条坏行：{bad}")
        check(bad[0][0] == str(first) and bad[0][1] == 4,
              f"坏行应带文件与行号：{bad}")
        check(bool(bad[0][2]), "坏行应带原因")
        records2, bad2 = br.load([tmp / "missing.jsonl"])
        check(records2 == [] and len(bad2) == 1,
              f"输入文件不存在应记为坏行而不是崩：{bad2}")
        check("不存在" in bad2[0][2], f"应说明文件不存在：{bad2}")


def test_assign_points_suffixes_duplicate_keys() -> None:
    same = advice()
    points = br.assign_points([advice(hand_id="h1"), copy.deepcopy(same),
                               copy.deepcopy(same), advice(hand_id="h2")])
    keys = [p.key for p in points]
    check(keys[0] == "h1|flop|AhKd|Ks7d2c|2",
          f"第一条用基础键：{keys}")
    check(keys[1] == "h1|flop|AhKd|Ks7d2c|2#2" and keys[2] == keys[1][:-1] + "3",
          f"重号记录按出现顺序加后缀：{keys}")
    check(keys[3] == "h2|flop|AhKd|Ks7d2c|2",
          f"不同手牌各自独立编号：{keys}")
    check(points[1].record["hand_id"] == "h1", "Point 应保留原始记录引用")


def points_for(distribution: dict[str, int]) -> list[br.Point]:
    """按「街 → 条数」造点数不同的决策点，键互不重复。"""
    records = []
    for street, count in distribution.items():
        for i in range(count):
            records.append(advice(hand_id=f"{street}-{i}", street=street,
                                  board=["Ks", "7d", "2c"], to_call=float(i)))
    return br.assign_points(records)


def test_sample_is_deterministic_per_seed() -> None:
    points = points_for({"preflop": 200, "flop": 60, "turn": 20, "river": 5})
    first = br.sample(points, n=50, seed=7)
    again = br.sample(points, n=50, seed=7)
    other = br.sample(points, n=50, seed=8)
    check(len(first.points) == 50, f"应抽满 50 点，实际 {len(first.points)}")
    check([p.key for p in first.points] == [p.key for p in again.points],
          "同一 seed + 同一输入应抽到同一批点")
    check([p.key for p in first.points] != [p.key for p in other.points],
          "换 seed 应换一批点")
    check(sum(first.by_street.values()) == 50,
          f"各街点数之和应等于样本数：{first.by_street}")
    order = [br.STREET_ORDER.index(p.record["street"]) for p in first.points]
    check(order == sorted(order), f"输出应按街排序：{[p.record['street'] for p in first.points]}")


def test_sample_stratifies_every_street_and_explains_short_input() -> None:
    points = points_for({"preflop": 100, "flop": 20, "turn": 6, "river": 4})
    picked = br.sample(points, n=50, seed=0)
    check(len(picked.points) == 50, f"应抽满 50 点：{len(picked.points)}")
    check(picked.by_street == {"preflop": 36, "flop": 8, "turn": 3, "river": 3},
          f"分层名额应「每街保底 1 + 按点数比例」：{picked.by_street}")
    check(all(count >= 1 for count in picked.by_street.values()),
          "每条出现的街都应抽到点")
    check(picked.note == "", "够 50 点时不应有「不足」说明")

    # 输入不足 50：按实际数量抽，并说明差多少。
    short = br.sample(points_for({"preflop": 3, "flop": 3, "turn": 3, "river": 3}),
                      n=50, seed=0)
    check(len(short.points) == 12, f"不足 50 时应全取：{len(short.points)}")
    check("12" in short.note and "50" in short.note,
          f"应说明实际不足 50：{short.note!r}")
    check(short.by_street == {"preflop": 3, "flop": 3, "turn": 3, "river": 3},
          f"不足 50 时应按街全取：{short.by_street}")

    # 街名缺失/未知的决策点也要有位置，不能被静默丢掉。
    unknown = br.assign_points([advice(hand_id="x", street=""),
                                advice(hand_id="y", street="preflop")])
    got = br.sample(unknown, n=50, seed=0)
    check(len(got.points) == 2, f"未知街也要保留：{[p.key for p in got.points]}")
    check(got.by_street.get(br.UNKNOWN_STREET) == 1,
          f"未知街应单列：{got.by_street}")


def test_blind_card_keeps_only_facts_and_options() -> None:
    card = br.blind_card(point(advice()))
    check(card["hero_cards"] == ["Ah", "Kd"], f"手牌：{card}")
    check(card["board"] == ["Ks", "7d", "2c"], f"公共牌：{card}")
    check(card["pot"] == 12.0, f"底池：{card}")
    check(card["to_call"] == 2.0, f"需跟：{card}")
    check(card["hero_stack"] == 100.0, f"后手：{card}")
    check(card["position"] == "BTN", f"位置：{card}")
    check(card["opponents"] == 2, f"在局对手：{card}")
    check([o["id"] for o in card["options"]] == ["call", "fold", "raise-1/2"],
          f"给模型的 options 应原样保留：{card['options']}")
    check(card["decision_key"] == "k1", f"卡片应带决策键：{card}")
    check("option" not in card and "reason" not in card and "plan" not in card,
          f"卡片不应带模型结论字段：{sorted(card)}")


def _poisoned_record() -> dict:
    """一条「把能泄密的字段都塞进去」的记录：卡片与渲染都不得带出这些串。"""
    record = advice()
    record["option"] = option("raise-1", "加注到 10BB", "raise", 10.0)
    record["reason"] = "模型理由：这里应该加注"
    record["plan"] = "下一街继续下注"
    record["previous_advice"] = {"option": {"id": "fold"}}
    record["prompt_system"] = "系统提示词：你是教练"
    record["prompt_user"] = (
        "## 当前局面\n底池 12BB\n\n"
        "## 书中计划（Harrington 现金桌）\n顶对下注 2/3 池\n\n"
        "## 偏移（书上是基线；只改尺寸/频率，不改动作）\n翻牌加大 20%\n\n"
        "## 行动线\nUTG 加注到 3BB → Hero 跟注\n")
    record["facts"]["book_plan"] = "书页计划文字"
    record["facts"]["offsets"] = ["偏移文字"]
    return record


def test_blind_card_hides_model_and_book_hints_until_reveal() -> None:
    record = _poisoned_record()
    p = point(record)
    card = br.blind_card(p)
    text = br.render_card(card)
    # 「raise-1」是选项菜单里「raise-1/2」的前缀，菜单本身就该出现在卡上；
    # 泄密的标记是「被选中的那个」的完整形状与它独有的尺寸。
    secrets = ["raise-1（", "10BB", "模型理由", "下一街继续下注", "顶对下注 2/3 池",
               "书中计划", "偏移", "系统提示词", "书页计划文字", "偏移文字"]
    for secret in secrets:
        check(secret not in text, f"揭示前卡片不该出现「{secret}」：\n{text}")
    check("揭示" not in text, f"揭示前不该有揭示段：\n{text}")

    revealed = br.reveal(p)
    reveal_text = br.render_card(card, revealed)
    for secret in ["raise-1", "模型理由", "下一街继续下注",
                   "顶对下注 2/3 池", "翻牌加大 20%"]:
        check(secret in reveal_text, f"揭示后应出现「{secret}」：\n{reveal_text}")


def test_position_falls_back_to_situation_section() -> None:
    record = advice()
    record["facts"].pop("hero_position", None)
    record["prompt_user"] = (
        "## 当前局面\n桌况：9 人桌\n"
        "位置：Hero 有位置（还能行动的对手里最后）｜过牌后待行动：0 人\n\n"
        "## 上一街我的建议（同一手）\n这里应该弃牌\n")
    card = br.blind_card(point(record))
    check("有位置" in card["position"],
          f"没有结构化位置时应从「当前局面」退化取：{card['position']}")
    text = br.render_card(card)
    check("这里应该弃牌" not in text,
          f"上一街建议仍必须隐藏：\n{text}")
    # 结构化字段优先：不会因为提示词里另有一行而改变。
    record["facts"]["hero_position"] = "BTN"
    check(br.blind_card(point(record))["position"] == "BTN",
          "结构化位置应优先于提示词退化")


def test_render_card_labels_and_action_line() -> None:
    record = _poisoned_record()
    text = br.render_card(br.blind_card(point(record)))
    for label in ["手牌", "公共牌", "底池", "需跟", "后手", "位置",
                  "在局", "行动线", "数据质量", "选项"]:
        check(label in text, f"卡片应包含「{label}」：\n{text}")
    check("UTG 加注到 3BB → Hero 跟注" in text,
          f"行动线应从 prompt_user 的「## 行动线」段取出：\n{text}")
    check("某座位范围未知" in text, f"数据质量应带上 facts.warnings：\n{text}")


def test_blind_card_degrades_on_minimal_advice_record() -> None:
    records, bad = br.load([ROOT / "tests" / "golden"
                            / "advice_scorecard.live.jsonl"])
    check(not bad and len(records) == 45,
          f"最小 fixture 应读出 45 条 advice：{len(records)} 条、坏行 {bad}")
    points = br.assign_points(records)
    card = br.blind_card(points[0])
    check(card["hero_cards"] == ["Ah", "Kd"], f"手牌仍应有：{card}")
    check(card["board"] == [], f"没有 board 时应为空列表：{card}")
    check(card["pot"] is None and card["hero_stack"] is None,
          f"缺事实应为 None：{card}")
    check(card["options"] == [], f"没有 facts.options 时应为空：{card}")
    check(br.UNKNOWN_STREET not in card["quality"], "最小记录不应伪造事实")
    check(any("没有事实快照" in note for note in card["quality"]),
          f"应说明这是旧记录、没有事实快照：{card['quality']}")
    text = br.render_card(card)
    check("Ah Kd" in text and "（日志没记）" in text,
          f"缺字段应显示「日志没记」而不是崩：\n{text}")
    for p in points:
        check(bool(br.render_card(br.blind_card(p))),
              f"每条最小记录都应渲染出非空卡片：{p.key}")


def test_review_row_and_merge_result_are_idempotent() -> None:
    record = advice()
    p = point(record)
    row = br.review_row(p, "fold", confidence=3, note="我觉得该弃", ts="t0")
    check(row["decision_key"] == "k1", f"结果应带决策键：{row}")
    check(row["blind_option"] == "fold", f"应记录盲评选择：{row}")
    check(row["blind_confidence"] == 3, f"应记录盲评置信度：{row}")
    check(row["model_option"] == "call", f"应记录程序选择：{row}")
    check(row["agree"] is False, f"call vs fold 应判不一致：{row}")
    check(row["prompt_version"] == "2026-01-01a", f"应带提示词版本：{row}")
    check(row["street"] == "flop" and row["hand_id"] == "h1", f"应带定位信息：{row}")
    check(row["note"] == "我觉得该弃", f"应带备注：{row}")

    rows = br.merge_result([], row)
    check(len(rows) == 1 and rows[0] == row, f"空结果集应直接收下：{rows}")
    again = br.merge_result(rows, row)
    check(again == rows, f"同一批重交不应产生重复记录：{again}")

    # 同一个决策点重评：替换旧行，不是追加。
    revised = br.review_row(p, "call", confidence=2, ts="t1")
    merged = br.merge_result(rows, revised)
    check(len(merged) == 1, f"重评应替换而不是追加：{merged}")
    check(merged[0]["blind_option"] == "call" and merged[0]["agree"] is True,
          f"重评后应为一致：{merged}")

    # 不同决策点：追加。
    p2 = point(advice(hand_id="h2"), key="k2")
    merged = br.merge_result(merged, br.review_row(p2, "call"))
    check(len(merged) == 2, f"不同决策点应各占一行：{merged}")
    check({row["decision_key"] for row in merged} == {"k1", "k2"},
          f"决策键应唯一：{merged}")
    check([row["decision_key"] for row in merged] == ["k1", "k2"],
          f"合并应保序、便于人读：{merged}")


def test_unreviewed_skips_already_reviewed() -> None:
    points = [br.Point(key=f"k{i}", record=advice(hand_id=f"h{i}"))
              for i in (1, 2, 3)]
    rows = [br.review_row(points[0], "call", ts="t0")]
    todo = br.unreviewed(points, rows)
    check([p.key for p in todo] == ["k2", "k3"],
          f"已评过的点应跳过、只留未评的：{[p.key for p in todo]}")
    rows.append(br.review_row(points[1], "fold", ts="t1"))
    check([p.key for p in br.unreviewed(points, rows)] == ["k3"],
          "断点续评：再跑一次只该剩最后一个点")
    index = br.index_results(rows)
    check(set(index) == {"k1", "k2"}, f"结果索引应按决策键建：{index}")


def test_summarize_reports_per_street_agreement_and_disagreements() -> None:
    records = [
        advice(hand_id="f1", street="flop"),
        advice(hand_id="f2", street="flop"),
        advice(hand_id="t1", street="turn"),
        advice(hand_id="r1", street="river"),
    ]
    sampled = br.sample(br.assign_points(records), n=4, seed=3)
    check(len(sampled.points) == 4, f"4 条记录应全抽到：{len(sampled.points)}")
    flops = [p for p in sampled.points if p.record["street"] == "flop"]
    turns = [p for p in sampled.points if p.record["street"] == "turn"]
    check(len(flops) == 2 and len(turns) == 1, "分层抽样应按街分组")

    rows = [
        br.review_row(flops[0], "call", ts="t0"),    # 与程序一致
        br.review_row(flops[1], "fold", ts="t1"),    # 与程序分歧
        br.review_row(turns[0], "call", ts="t2"),    # 与程序一致
    ]
    summary = br.summarize(sampled, rows)
    check(summary["sampled"] == 4 and summary["reviewed"] == 3,
          f"总数应分开记抽样数与已评数：{summary}")
    streets = {row["street"]: row for row in summary["streets"]}
    check(set(streets) >= {"flop", "turn", "river"},
          f"汇总应按街分行：{sorted(streets)}")
    flop = streets["flop"]
    check(flop["sampled"] == 2 and flop["reviewed"] == 2,
          f"flop 应记 2 抽样 2 已评：{flop}")
    check(flop["agree"] == 1 and flop["agreement"] == 0.5,
          f"flop 一致率应为 1/2：{flop}")
    check(len(flop["disagreements"]) == 1,
          f"分歧应可逐条列出：{flop}")
    miss = flop["disagreements"][0]
    check(miss["blind_option"] == "fold" and miss["model_option"] == "call",
          f"分歧条目应带两侧选择：{miss}")
    check(miss["decision_key"], f"分歧条目应带决策键：{miss}")
    check(streets["turn"]["agreement"] == 1.0, f"turn 应全一致：{streets['turn']}")
    check(streets["river"]["reviewed"] == 0
          and streets["river"]["agreement"] is None,
          f"未评的街不应伪造一致率：{streets['river']}")
    check(summary["agreement"] == round(2 / 3, 4),
          f"总体一致率应为 2/3：{summary['agreement']}")
    check([p.key for p in br.unreviewed(sampled.points, rows)] ==
          [p.key for p in sampled.points if p.record["street"] == "river"],
          "汇总与续评应互相印证：只剩 river 未评")

    text = br.render_summary(summary)
    for label in ["flop", "turn", "river", "一致"]:
        check(label in text, f"汇总文本应包含「{label}」：\n{text}")
    check("盈利" in text, f"汇总必须写明一致率不是盈利证明：\n{text}")


def test_parse_answer_splits_choice_confidence_and_note() -> None:
    check(br.parse_answer("call") == ("call", 2, ""),
          f"只给选项时应默认置信度 2：{br.parse_answer('call')}")
    check(br.parse_answer(" fold 3 太被动 ") == ("fold", 3, "太被动"),
          f"应解析出选择/置信度/备注：{br.parse_answer(' fold 3 太被动 ')}")
    check(br.parse_answer("raise-1/2 1") == ("raise-1/2", 1, ""),
          f"选项 id 可带斜杠：{br.parse_answer('raise-1/2 1')}")
    check(br.parse_answer("") == ("", 2, ""), "空输入应表示跳过")
    check(br.parse_answer("q") == ("q", 2, ""), "q 应原样返回给调用方处理")


def test_build_parser_defaults_and_multiple_inputs() -> None:
    parser = br.build_parser()
    args = parser.parse_args([])
    check(args.inputs == [br.DEFAULT_INPUT],
          f"缺省输入应是默认日志路径：{args.inputs}")
    check(args.count == 50 and args.seed == 0 and args.out == br.DEFAULT_RESULTS,
          f"缺省样本数/种子/结果文件不对：{args}")
    args = parser.parse_args(["a.jsonl", "b.jsonl", "-n", "7", "--seed", "9",
                              "-o", "x.jsonl", "--summary-only"])
    check(args.inputs == ["a.jsonl", "b.jsonl"], f"应支持多输入：{args.inputs}")
    check(args.count == 7 and args.seed == 9 and args.out == "x.jsonl",
          f"应支持 -n/--seed/-o：{args}")
    check(args.summary_only is True, "应支持只看汇总")


if __name__ == "__main__":
    sys.exit(main())
