"""规则段：把命中台账的规则压成 1–2 行中文（spec #63 / 票 67）。

    load_ledger() -> facts_snapshot(sit) -> match_rules(include_exploit=False)

**渲染契约**（写死在 issue #67；#69/#70/#71 只往 `ledger/` 加 JSON，不改这里）：

  · intent 行：`规则: <node> → <动作中文>（<why>）[书 p<page>]`
    动作中文：check→过牌 call→跟注 fold→弃牌 raise→加注 bet→下注 allin→全下。
  · size 行：`尺寸: <node> → <尺寸文字> [书 p<page>]`
    尺寸文字由 `rule.size = {"table": …, "name": …}` 经 `strategy_tables.table()`
    解析出 `value` 得到（规则里不存数字）：
    0.25→1/4 池、0.33/0.34→1/3 池、0.4→2/5 池、0.5→半池、0.66/0.67→2/3 池、
    0.75→3/4 池、1.0→1 倍池；其他 `<x> 倍池`（`%g`，如 0.6→「0.6 倍池」）。
  · 只命中 intent 不命中 size：只出 intent 行，不渲染半截尺寸（尺寸交回现役
    `aggression.py` 的默认值）。
  · 零命中返回 `[]`：不产生空行、不产生标题（组装函数见空列表就不加分隔符）。
  · 总行数 ≤2，整段 ≤800 字（按各行字符数累计，不含段内分隔换行）；
    超了从尾部丢行（intent 比 size 先保）。

**fail open**（语料坏了也不能把 OCR 主循环搞挂，见 AGENTS.md 铁律）：

  · `load_ledger()` 抛 `RuleError`、`facts_snapshot()`/`match_rules()` 任何
    异常 → 返回 `[]`，绝不上抛。
  · 尺寸表解析失败、`value` 缺失/非数字/非有限/≤0 → 该行不渲染（intent 行照出）。
  · **rows 型尺寸表（如 `size_ladder`）现在一律 fail open 不渲染**：按 facts
    选行是 #70 的分支逻辑，本票只做标量 `value`；不要把 rows 的第一行当成 answer。
  · 未知 `action`（校验器已拦，防御性）→ intent 行不渲染。

**刻意的取舍**（改之前先看理由）：

  · 不登记 refs：`prompt/refs.py` 的 collector 只认 `facts`/`trace` 两个 ns
    （tests/test_prompt_refs.py:121 断言），设计 §6 的 `ns: "rules"` 要等
    collector 支持新 ns 再补；本票只接文本，不把未支持的 ns 引进来。
  · `source.page` 缺失（`measured` 规则）时省略 `[书 p…]`，不伪造页码；
    也不留尾空格。
  · 同槽位多条命中（`coexist_with` 白名单）：取 `len(preconditions)` 最多的一条
    （更具体优先）；平手取 id 升序的第一条（`match_rules` 已按 id 排序）。
  · `slot == "why"` 不单独出行：why 已经拼进 intent 行的括号里，#71 再谈
    独立的「为什么」行。
  · size 行独立渲染：只命中 size 时照样出行（契约按槽位各自成立）。
"""

from __future__ import annotations

import math
from typing import Any

from ...rules import facts_snapshot, load_ledger, match_rules
from ...strategy_tables import table

MAX_LINES = 2
MAX_CHARS = 800

ACTION_ZH = {
    "check": "过牌",
    "call": "跟注",
    "fold": "弃牌",
    "raise": "加注",
    "bet": "下注",
    "allin": "全下",
}

# 书上的尺寸档位（设计 §6 / issue #67 的映射，不在这里发明新档）。
SIZE_LABELS = {
    0.25: "1/4 池",
    0.33: "1/3 池",
    0.34: "1/3 池",
    0.4: "2/5 池",
    0.5: "半池",
    0.66: "2/3 池",
    0.67: "2/3 池",
    0.75: "3/4 池",
    1.0: "1 倍池",
}


def render_rules_zh(sit) -> list[str]:
    """返回规则段的 0–2 行；任何读不出的东西都退化成「没这行」。"""
    try:
        rules = load_ledger()
        facts = facts_snapshot(sit)
        hits = match_rules(facts, rules, include_exploit=False)
    except Exception:  # noqa: BLE001 - 规则层是旁路，不能阻塞主链路
        return []

    lines: list[str] = []
    intent = _pick(hits, "intent")
    if intent is not None:
        line = _intent_line(intent)
        if line is not None:
            lines.append(line)

    size = _pick(hits, "size")
    if size is not None:
        text = _size_text(size.size)
        if text is not None:
            lines.append(_size_line(size, text))

    return _within_budget(lines)


# ─────────────────────────── 选行 ───────────────────────────

def _pick(hits: tuple, slot: str):
    """同槽位取前提最多的一条；平手留 id 更小的（输入已按 id 升序）。"""
    best = None
    for rule in hits:
        if rule.slot != slot:
            continue
        if best is None or len(rule.preconditions) > len(best.preconditions):
            best = rule
    return best


def _intent_line(rule) -> str | None:
    action = ACTION_ZH.get(rule.action)
    if action is None:
        return None
    return f"规则: {rule.node} → {action}（{rule.why}）{_page_suffix(rule.source)}"


def _size_line(rule, text: str) -> str:
    suffix = _page_suffix(rule.source)
    tail = f" {suffix}" if suffix else ""
    return f"尺寸: {rule.node} → {text}{tail}"


def _page_suffix(source: Any) -> str:
    """`[书 p30]`；页码缺失（measured 规则）就整段省略，不编页码。

    intent 行让 `）` 直接贴 `[`（契约示例如此），size 行前面留一个空格；
    空格由调用方补，这里只管方括号本身。
    """
    if not isinstance(source, dict):
        return ""
    page = source.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return ""
    return f"[书 p{page}]"


# ─────────────────────────── 尺寸文字 ───────────────────────────

def _size_text(size: Any) -> str | None:
    """解析 `{"table": …, "name": …}` 的标量 value；读不出就 None。"""
    if not isinstance(size, dict):
        return None
    kind, name = size.get("table"), size.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        return None
    try:
        entry = table(kind, name)
    except Exception:  # noqa: BLE001 - 表缺失/坏 JSON 都算「没这行」
        return None

    value = entry.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ratio = float(value)
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    return SIZE_LABELS.get(ratio, f"{ratio:g} 倍池")


# ─────────────────────────── 预算 ───────────────────────────

def _within_budget(lines: list[str]) -> list[str]:
    """≤2 行、整段 ≤800 字；超了从尾部丢（intent 比 size 优先）。"""
    kept = lines[:MAX_LINES]
    while kept and sum(len(line) for line in kept) + len(kept) - 1 > MAX_CHARS:
        kept.pop()
    return kept
