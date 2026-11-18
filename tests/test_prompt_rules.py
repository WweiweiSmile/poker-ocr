#!/usr/bin/env python3
"""规则段接进用户事实块（票 67 / spec #63）的测试。

    ./.venv/bin/python -m pytest tests/test_prompt_rules.py -q

这一票只做「管道 + 空渲染」：台账还是空的，所以提示词必须**一个字节都不
变**；等 #69 往 ledger/ 里放 JSON 时，同一个 render_rules_zh 才开始出行。
五组用例对应验收：

  1. 空台账：render_rules_zh 返回 []、user 段不含「规则:/尺寸:」、整段
     prompt 的 sha256 与接入前逐字节一致（常量在上面提前抄好）。
  2. 永不命中的规则：照样零命中，不产生空行/标题。
  3. intent + size 都命中：两行、格式符合契约、位置在牌力段之后、可选动作
     之前、整段 ≤800 字。
  4. 只命中 intent：只渲染一行，不渲染半截尺寸。
  5. rows 型尺寸表：按 facts 选行留给 #70，现在 fail open（只有 intent 行）；
     标量尺寸的文字映射逐档钉死。

期望值不是抄实现：行格式对着 issue #67 的渲染契约写死；尺寸文字对着
docs/规则台账-设计-2026-11-18.md §6 的映射写死。改这些等于改契约。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poker_ocr.rules as rules_pkg  # noqa: E402
import poker_ocr.strategy_tables as strategy_tables  # noqa: E402
from poker_ocr.advice import build_messages, build_situation  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.prompt.user.rules import render_rules_zh  # noqa: E402

# 接入规则段之前（lean-zh-2026-11-16.3）整段 prompt 的 sha256：
#   sha256(system + "\n---\n" + user)，fixture 见下面 _flop_sit()。
# 空台账时这个值必须一直成立——它就是「一个字节都不变」的证据。
EMPTY_LEDGER_PROMPT_HASH = (
    "86b1de13fc2d40317be4c6a93678b7d7386b42063749760e3652ded8b8780adc"
)

# 「牌力段之后、render_options_zh 之前」的锚点：这两行必须把规则段夹在中间。
STRENGTH_HEADER = "## 实时牌力"
OPTIONS_HEADER = "## 可选动作"


# ─────────────────────── fixture 局面 ───────────────────────

def _flop_sit():
    """单加注池、BTN 拿 KQ 顶对：有牌力段、也有可选动作段。"""
    snap = HandSnapshot(
        hand_id="rules-wire", hand_index=0, street="flop",
        board=("Ks", "7d", "2c"), hero_cards=("Kh", "Qh"),
        hero_position="BTN", hero_stack=92.0, hero_bet=0.0,
        hero_invested=8.0, to_call=4.0, second_bet=None,
        pot=12.5, pot_before_street=8.0, table_size=6,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=(
            OpponentSnapshot("CO", "Villain", "CO", 100.0, 4.0, False),
            OpponentSnapshot("BB", None, "BB", 100.0, 1.0, False),
        ),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "BTN", "raise", 8.0,
                             False, True),
                ActionRecord("preflop", "BB", None, "BB", "call", 8.0,
                             False, False),
            )),
            StreetRecord("flop", 8.0, (
                ActionRecord("flop", "CO", "Villain", "CO", "bet", 4.0,
                             False, False),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("rules-wire", "flop", 0, 12.5, 4.0), warnings=(),
        bomb_pot=False, players_dealt=6,
    )
    return build_situation(snap, table_dynamics="")


# ─────────────────────── 台账 / 策略表 ───────────────────────

def _rule(**kw) -> dict:
    """一条合法规则；用例只覆盖要破坏的那个字段。"""
    base = {
        "id": "po.test.intent",
        "version": 1,
        "slot": "intent",
        "street": "flop",
        "node": "单挑翻牌先行动",
        "preconditions": [
            {"fact": "street", "eq": "flop"},
            {"fact": "hero_position", "eq": "BTN"},
        ],
        "action": "bet",
        "why": "对手只有约 35% 中翻牌",
        "source": {"book": "Little Green Book", "page": 30,
                   "status": "verbatim"},
        "confidence": "book",
        "verified": False,
    }
    base.update(kw)
    return base


def _write_ledger(directory: Path, *items: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "postflop.json").write_text(
        json.dumps({"schema": 1, "rules": list(items)}, ensure_ascii=False),
        encoding="utf-8")


def _use_ledger(monkeypatch, directory: Path) -> None:
    """把默认台账目录指到临时目录（load_ledger() 无参时读这个模块常量）。"""
    monkeypatch.setattr(rules_pkg, "DEFAULT_LEDGER_DIR", directory)


def _write_rule_params(directory: Path, name: str, entry: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rule_params.json").write_text(
        json.dumps({"schema": 1, "default_variant": "v0",
                    "tables": {name: {"v0": entry}}}, ensure_ascii=False),
        encoding="utf-8")


def _use_rule_params(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv(strategy_tables.DIR_ENV, str(directory))
    # _load_kind 按 (kind, 目录) 做 lru_cache；换目录前先清，避免读到上一档。
    strategy_tables.reset_cache()


# ─────────────────────── 1. 空台账 ───────────────────────

def test_empty_ledger_keeps_prompt_bytes(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path / "no_ledger_here")
    sit = _flop_sit()

    assert render_rules_zh(sit) == [], "空台账必须返回 []，不是 [\'\'] 或标题"

    system, user = build_messages(sit)
    digest = hashlib.sha256(
        (system + "\n---\n" + user).encode()).hexdigest()
    assert digest == EMPTY_LEDGER_PROMPT_HASH, (
        "空台账时提示词变了。接入规则段的 diff 必须一个字节都不动；"
        "若这是有意的，先确认不是规则段漏了空列表分支。")
    assert "规则:" not in user, "空台账不该出现规则行"
    assert "尺寸:" not in user, "空台账不该出现尺寸行"


def test_rules_segment_wired_between_strength_and_options(monkeypatch):
    """段真的接进 render_facts_zh：桩输出落在牌力段之后、可选动作之前。"""
    import poker_ocr.prompt.user as user_pkg

    stub = "规则: 桩节点 → 下注（桩）[书 p1]"
    monkeypatch.setattr(user_pkg, "render_rules_zh", lambda sit: [stub])
    _, user = build_messages(_flop_sit())
    lines = user.split("\n")
    assert stub in lines, "render_facts_zh 没有把规则段拼进去"
    at = lines.index(stub)
    strength_at = next(i for i, line in enumerate(lines)
                       if line.startswith(STRENGTH_HEADER))
    options_at = next(i for i, line in enumerate(lines)
                      if line.startswith(OPTIONS_HEADER))
    assert strength_at < at < options_at, "规则段必须在牌力段之后、可选动作之前"
    assert lines[at - 1] == "", "规则段前面要空一行（与其它段一致）"


# ─────────────────────── 2. 永不命中 ───────────────────────

def test_non_matching_rule_renders_nothing(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path / "ledger")
    _write_ledger(tmp_path / "ledger", _rule(
        id="po.test.never",
        preconditions=[
            {"fact": "street", "eq": "river"},
            {"fact": "hero_position", "eq": "BTN"},
        ],
    ))
    sit = _flop_sit()
    assert render_rules_zh(sit) == [], "前提不满足时必须零命中"
    _, user = build_messages(sit)
    assert "规则:" not in user and "尺寸:" not in user


# ─────────────────────── 3. intent + size ───────────────────────

def _intent_and_size_rules(ratio: float = 0.5) -> tuple[dict, dict]:
    intent = _rule()
    size = _rule(
        id="po.test.size", slot="size", action=None, why="牌面干燥，半池即可",
        source={"book": "Little Green Book", "page": 35,
                "status": "verbatim"},
        size={"table": "rule_params", "name": "cbet_size"},
    )
    return intent, size


def _hit_fixture(monkeypatch, tmp_path, ratio: float = 0.5):
    _use_rule_params(monkeypatch, tmp_path / "tables")
    _write_rule_params(tmp_path / "tables", "cbet_size",
                       {"value": ratio, "source": "book", "verified": False})
    _use_ledger(monkeypatch, tmp_path / "ledger")
    intent, size = _intent_and_size_rules()
    _write_ledger(tmp_path / "ledger", intent, size)
    return _flop_sit()


def test_intent_and_size_render_two_contract_lines(monkeypatch, tmp_path):
    sit = _hit_fixture(monkeypatch, tmp_path)
    lines = render_rules_zh(sit)

    assert lines == [
        "规则: 单挑翻牌先行动 → 下注（对手只有约 35% 中翻牌）[书 p30]",
        "尺寸: 单挑翻牌先行动 → 半池 [书 p35]",
    ], f"渲染契约不符：{lines!r}"
    assert len(lines) <= 2, "规则段最多两行"
    assert len("\n".join(lines)) <= 800, "规则段整段 ≤800 字"

    _, user = build_messages(sit)
    text_lines = user.split("\n")
    rule_at = text_lines.index(lines[0])
    assert text_lines[rule_at + 1] == lines[1], "尺寸行紧跟动作行"
    strength_at = next(i for i, line in enumerate(text_lines)
                       if line.startswith(STRENGTH_HEADER))
    options_at = next(i for i, line in enumerate(text_lines)
                      if line.startswith(OPTIONS_HEADER))
    assert strength_at < rule_at < options_at


# ─────────────────────── 4. 只命中 intent ───────────────────────

def test_intent_only_renders_one_line(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path / "ledger")
    _write_ledger(tmp_path / "ledger", _rule())
    lines = render_rules_zh(_flop_sit())
    assert len(lines) == 1, f"只命中 intent 时只该一行：{lines!r}"
    assert lines[0].startswith("规则: ") and lines[0].endswith("[书 p30]")
    assert "尺寸:" not in "\n".join(lines), "不渲染半截尺寸"


def test_rows_size_table_fails_open(monkeypatch, tmp_path):
    """rows 型尺寸要按 facts 选行（#70 才做）；现在必须整行不渲染。"""
    _use_rule_params(monkeypatch, tmp_path / "tables")
    _write_rule_params(tmp_path / "tables", "size_ladder", {
        "rows": {"strong": 0.33, "medium": 0.66, "weak": 1.0},
        "source": "book", "verified": False,
    })
    _use_ledger(monkeypatch, tmp_path / "ledger")
    intent, size = _intent_and_size_rules()
    size["size"] = {"table": "rule_params", "name": "size_ladder"}
    _write_ledger(tmp_path / "ledger", intent, size)

    lines = render_rules_zh(_flop_sit())
    assert len(lines) == 1, f"rows 表应 fail open，只留 intent 行：{lines!r}"
    assert lines[0].startswith("规则: ")
    assert "尺寸:" not in lines[0]


# ─────────────────────── 5. 尺寸文字映射 ───────────────────────

@pytest.mark.parametrize(("ratio", "label"), [
    (0.25, "1/4 池"),
    (0.33, "1/3 池"),
    (0.34, "1/3 池"),
    (0.4, "2/5 池"),
    (0.5, "半池"),
    (0.6, "0.6 倍池"),
    (0.66, "2/3 池"),
    (0.67, "2/3 池"),
    (0.75, "3/4 池"),
    (1.0, "1 倍池"),
])
def test_size_label_mapping(monkeypatch, tmp_path, ratio, label):
    sit = _hit_fixture(monkeypatch, tmp_path, ratio)
    lines = render_rules_zh(sit)
    assert len(lines) == 2
    assert lines[1] == f"尺寸: 单挑翻牌先行动 → {label} [书 p35]"
