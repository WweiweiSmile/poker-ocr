"""规则段：把命中台账的规则压成 1–2 行中文（spec #63 / 票 67）。

    load_ledger() -> facts_snapshot(sit) -> match_rules(include_exploit=False)

**渲染契约**（写死在 issue #67；#69/#70/#71 只往 `ledger/` 加 JSON，不改这里）：

  · intent 行：`规则: <node> → <动作中文>（<why>）[书 p<page>]`
    动作中文：check→过牌 call→跟注 fold→弃牌 raise→加注 bet→下注 allin→全下。
  · size 行：`尺寸: <node> → <尺寸文字> [书 p<page>]`
    尺寸文字由 `rule.size = {"table": …, "name": …}` 经 `strategy_tables.table()`
    解析（规则里不存数字）。`unit` 决定量纲（`pot` 默认 / `bb` / `raise`）：
    value 标量按 §6 的池比例表（0.25→1/4 池、0.33/0.34→1/3 池、0.4→2/5 池、
    0.5→半池、0.66/0.67→2/3 池、0.75→3/4 池、1.0→1 倍池，其他 `<x> 倍池`）；
    bb 用 `<x>BB`；raise 用 `对手加注的 <x> 倍`。
  · `rows` + `key`：`position_group` 按 hero_position 分早/中/晚/小盲，
    `in_position` 按 in_position 取 `in_position`/`out_of_position` 行，
    `hand_class` 按 facts 的 strong/medium/weak 取 size_ladder 的三行；
    facts 缺该谓词或表里没有这一行 → 不出尺寸行（**绝不**拿第一行当答案）。
  · `items`：长度为 2 的数字区间渲染 `<低>–<高>`；非数字（如 `premium`）不出行。
    `per: limpers_before` 渲染成「每多一个溜入者 +<x>BB / +<x> 倍池」。
  · 只命中 intent 不命中 size：只出 intent 行，不渲染半截尺寸（尺寸交回现役
    `aggression.py` 的默认值）；intent 自带的默认尺寸（如 po.cbet.hu 的
    cbet_size_hu）此时顶上，仍然只出一行尺寸。
  · intent 的动作是过牌/跟注/弃牌时不出尺寸行：规则 15（转牌吓人牌）命中
    「过牌控池」的同时牌面尺寸规则也会命中，P 一个 1/3 池会自相矛盾。
  · 零命中返回 `[]`：不产生空行、不产生标题（组装函数见空列表就不加分隔符）。
  · 总行数 ≤2，整段 ≤800 字（按各行字符数累计，不含段内分隔换行）；
    超了从尾部丢行（intent 比 size 先保）。

**fail open**（语料坏了也不能把 OCR 主循环搞挂，见 AGENTS.md 铁律）：

  · `load_ledger()` 抛 `RuleError`、`facts_snapshot()`/`match_rules()` 任何
    异常 → 返回 `[]`，绝不上抛。
  · 尺寸表解析失败、`value` 缺失/非数字/非有限/≤0、rows 选不中行、items 不是
    两段正数 → 该行不渲染（intent 行照出），绝不猜一个默认行。
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
    if size is None and intent is not None:
        # intent 规则可以自带默认尺寸（如 pf.rfi.first_in 的 open_size_bb）：
        # 没有更具体的 size 槽命中时退回它，仍然只出一行尺寸。
        size = intent
    if size is not None and size.size and _sizing_ok(intent):
        text = _size_text(size.size, facts)
        if text is not None:
            lines.append(_size_line(size, text))

    return _within_budget(lines)


# ─────────────────────────── 选行 ───────────────────────────

def _sizing_ok(intent) -> bool:
    """动作不是下注/加注时不给尺寸行（否则「过牌」配「1/3 池」自相矛盾）。

    没有 intent 命中（只命中尺寸槽）时不冲突，照出；这条只挡「同一点上
    动作行与尺寸行打架」的情况。
    """
    if intent is None:
        return True
    return intent.action in ("bet", "raise", "allin")


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

# 位置 → 开池尺寸分组（与 `rule_params.open_size_bb` 的 rows 键同一份契约）。
POSITION_GROUP = {
    "UTG": "early", "UTG+1": "early", "UTG+2": "early",
    "LJ": "middle", "HJ": "middle",
    "CO": "late", "BTN": "late",
    "SB": "sb",
}
POSITION_GROUP_ZH = {
    "early": "早位", "middle": "中位", "late": "晚位", "sb": "小盲",
}
IN_POSITION_ROW = {True: "in_position", False: "out_of_position"}
IN_POSITION_ZH = {True: "有位置", False: "无位置"}
# 牌力档 → size_ladder 的行键（行键本身来自 facts.hand_class，不在这里再映射一遍）
HAND_CLASS_ZH = {"strong": "强牌", "medium": "中等牌", "weak": "弱牌"}


def _size_text(size: Any, facts: Any = None) -> str | None:
    """把一个 size 引用 + 事实渲染成尺寸文字；读不出就 None（fail open）。

    纯函数：只读 `size` 与 `facts`，不碰 `Situation`、不写任何规则 id 特判。
    三种载体：`value` 标量、`rows` + `key` 选行、`items` 两段区间；
    外加 `per: limpers_before` 的「每个溜入者递增」说明。
    """
    if not isinstance(size, dict):
        return None
    kind, name = size.get("table"), size.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        return None
    try:
        entry = table(kind, name)
    except Exception:  # noqa: BLE001 - 表缺失/坏 JSON 都算「没这行」
        return None
    if entry is None:
        return None

    facts = facts if isinstance(facts, dict) else {}
    unit = size.get("unit", "pot")
    if unit not in ("pot", "bb", "raise"):
        return None

    # per：数值是「每多一个 X」的增量，不是一次下注的总尺寸。
    per = size.get("per")
    if per is not None:
        if per != "limpers_before":
            return None
        value = _positive(entry.value)
        if value is None:
            return None
        if unit == "bb":
            return f"每多一个溜入者 +{value:g}BB"
        if unit == "pot":
            return f"每多一个溜入者 +{value:g} 倍池"
        return None

    key = size.get("key")
    if key is not None:
        row, suffix = _size_row(key, facts)
        if row is None:
            return None
        value = _positive((entry.rows or {}).get(row))
        if value is None:
            return None
        text = _unit_text(value, unit)
        return f"{text}（{suffix}）" if text and suffix else text

    value = _positive(entry.value)
    if value is not None:
        return _unit_text(value, unit)
    return _size_band(entry.items, unit)


def _size_row(key: str, facts: dict) -> tuple[str | None, str]:
    """按 facts 选出 rows 的行键与中文后缀；选不出就 (None, "")。"""
    if key == "position_group":
        group = POSITION_GROUP.get(facts.get("hero_position"))
        if group is None:
            return None, ""
        return group, POSITION_GROUP_ZH[group]
    if key == "in_position":
        flag = facts.get("in_position")
        if not isinstance(flag, bool):
            return None, ""
        return IN_POSITION_ROW[flag], IN_POSITION_ZH[flag]
    if key == "hand_class":
        cls = facts.get("hand_class")
        if not isinstance(cls, str):
            return None, ""
        return cls, HAND_CLASS_ZH.get(cls, "")
    return None, ""


def _unit_text(value: float, unit: str) -> str:
    """标量 → 中文尺寸；池比例走 §6 的档位表。"""
    if unit == "bb":
        return f"{value:g}BB"
    if unit == "raise":
        return f"对手加注的 {value:g} 倍"
    return SIZE_LABELS.get(value, f"{value:g} 倍池")


def _size_band(items: Any, unit: str) -> str | None:
    """`items` 两段数字 → 区间文字；不是两段正数就不出行（如 premium）。"""
    if not isinstance(items, (list, tuple)) or len(items) != 2:
        return None
    values = [_positive(item) for item in items]
    if any(item is None for item in values):
        return None
    low, high = values
    lo, hi = _band_num(low), _band_num(high)
    if unit == "raise":
        return f"对手加注的 {lo}–{hi} 倍"
    if unit == "bb":
        return f"{lo}–{hi}BB"
    return f"{lo}–{hi} 倍池"


def _band_num(value: float) -> str:
    """区间端点：0.66→0.66、1.0→1.0（区间里保留一位小数，读起来像倍数）。"""
    text = f"{value:g}"
    return text if "." in text else f"{value:.1f}"


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


# ─────────────────────────── 预算 ───────────────────────────

def _within_budget(lines: list[str]) -> list[str]:
    """≤2 行、整段 ≤800 字；超了从尾部丢（intent 比 size 优先）。"""
    kept = lines[:MAX_LINES]
    while kept and sum(len(line) for line in kept) + len(kept) - 1 > MAX_CHARS:
        kept.pop()
    return kept
