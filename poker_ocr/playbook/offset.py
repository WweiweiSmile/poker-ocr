"""桌况偏移层：书计划 -> 本桌修正（只改尺寸/频率，不改动作）。

设计口径见 `docs/harrington-决策层-设计-2026-11-19.md` §2.3：

    OffsetRule[] + 书卡 -> apply_offsets() -> OffsetDelta[]

三条硬约束：

  · target 只允许 `size_bb` / `size_pct` / `freq`，没有 `action`；
  · when 只读桌况事实（前注、街、翻前节点、位置、池型、桌风、有效筹码），
    不读书中计划；没有匹配就不产生偏移，不猜；
  · 尺寸的最终值必须落在本桌合法 options 里 —— 书是基线，牌桌的合法
    动作空间才是可执行边界。`clamp_options` 专门做这件事：把书里的尺寸
    移到最近的合法 options 金额；3BB 在 0.5BB 前注桌会落到最小合法开注 7BB。

规则住在独立的 `offsets.json`，不在代码里写死；加载时做完整静态校验，
坏配置直接 ValueError（fail fast），运行时再由 `offsets_for_situation`
包一层 fail open，绝不阻塞 OCR 主循环。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .bomb import bomb_guard_applies
from .schema import Card, Plan, Spot

OFFSETS_PATH = Path(__file__).resolve().parent / "offsets.json"

# 设计 §2.3：target 只允许这三个，动作不在偏移层里改。
OFFSET_TARGETS = ("size_bb", "size_pct", "freq")

# add/mul/set 是普通数值变换；clamp_options 把结果夹进本桌合法 options。
OFFSET_OPS = ("add", "mul", "set", "clamp_options")

# when 只允许读这些桌况事实。新增字段必须同时补 _fact / _fmt_fact /
# _FACT_KIND，否则加载校验会拒绝。
_FACT_KIND: dict[str, str] = {
    "ante_bb": "number",
    "effective_stack_bb": "number",
    "street": "string",
    "preflop_node": "string",
    "players": "string",
    "hero_position": "string",
    "pot_type": "string",
    "table_loose": "bool",
    "table_passive": "bool",
}
_CONDITION_OPS = ("gte", "gt", "lte", "lt", "eq", "in")

# 书里的 plan.action -> options 里同族的 kind。
_KIND_FOR_ACTION = {"raise": "raise", "bet": "bet", "all_in": "allin"}

_STREET_ZH = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
_NODE_ZH = {
    "unopened": "未开池", "limped": "溜入池", "raised": "面对加注",
    "three_bet": "3-bet+", "four_bet": "4-bet+", "unknown": "未知",
}
_POT_TYPE_ZH = {
    "unopened": "未开池", "limped": "溜入池", "single_raised": "单加注",
    "three_bet_plus": "3-bet+", "bomb": "炸弹池", "unknown": "未知",
}
_FACT_ZH = {
    "ante_bb": "前注", "effective_stack_bb": "有效筹码", "street": "街",
    "preflop_node": "翻前节点", "players": "人数", "hero_position": "位置",
    "pot_type": "池型", "table_loose": "桌风", "table_passive": "桌风",
}
_OP_ZH = {"gte": "≥", "gt": ">", "lte": "≤", "lt": "<", "eq": "=", "in": "∈"}


@dataclass(frozen=True)
class OffsetRule:
    """一条桌况偏移规则：适用条件 -> 改哪个字段、怎么改、依据。"""

    id: str
    when: Mapping[str, Any]
    target: str
    op: str
    value: float | None
    priority: int
    reason: str
    source: str


@dataclass(frozen=True)
class OffsetDelta:
    """一条实际生效的偏移：书上多少 -> 最终多少、适用什么、依据。"""

    target: str
    baseline: str
    final: str
    reason: str
    applicability: str = ""


# ─────────────────────────── 加载与校验 ───────────────────────────

def _scalar_ok(kind: str, value: Any) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)))
    return isinstance(value, str) and bool(value.strip())


def _validate_condition(key: str, cond: Any) -> str | None:
    kind = _FACT_KIND[key]
    if isinstance(cond, Mapping):
        if not cond:
            return f"when.{key} 的条件为空"
        for op, want in cond.items():
            if op not in _CONDITION_OPS:
                return f"when.{key} 的算子非法：{op!r}"
            if op == "in":
                if not isinstance(want, (list, tuple)) or not want:
                    return f"when.{key}.in 必须是非空数组"
                for item in want:
                    if not _scalar_ok(kind, item):
                        return f"when.{key}.in 的值类型不对：{item!r}"
            elif op == "eq":
                if not _scalar_ok(kind, want):
                    return f"when.{key}.eq 的值类型不对：{want!r}"
            else:
                if kind != "number" or not _scalar_ok("number", want):
                    return f"when.{key}.{op} 只允许数值"
        return None
    if not _scalar_ok(kind, cond):
        return f"when.{key} 的值类型不对：{cond!r}"
    return None


def _validate_value(rule: OffsetRule) -> str | None:
    if rule.op == "clamp_options":
        if rule.value is not None:
            return f"{rule.id}: clamp_options 不使用 value"
        if rule.target == "freq":
            return f"{rule.id}: clamp_options 只作用于尺寸"
        return None
    if rule.value is None:
        return f"{rule.id}: {rule.op} 缺 value"
    if not _scalar_ok("number", rule.value):
        return f"{rule.id}: value 必须是有限数字"
    value = float(rule.value)
    if rule.target == "freq" and not (0 < value <= 1):
        return f"{rule.id}: freq 的 value 必须在 (0,1]（set/add/mul 均在此范围）"
    if rule.target == "size_pct" and rule.op == "set" and not (0 < value <= 2):
        return f"{rule.id}: size_pct.set 必须在 (0,2]"
    if rule.op in ("mul", "set") and value <= 0:
        return f"{rule.id}: {rule.target}.{rule.op} 必须 >0"
    return None


def validate_offset_rules(rules: Iterable[OffsetRule]) -> list[str]:
    """静态校验；返回错误列表，空列表 = 通过。"""
    errors: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if not rule.id:
            errors.append("rule.id 为空")
            continue
        if rule.id in seen:
            errors.append(f"id 重复: {rule.id}")
        seen.add(rule.id)
        if rule.target not in OFFSET_TARGETS:
            errors.append(
                f"{rule.id}: target 只允许 {OFFSET_TARGETS}，实际 {rule.target!r}")
        if rule.op not in OFFSET_OPS:
            errors.append(f"{rule.id}: op 只允许 {OFFSET_OPS}，实际 {rule.op!r}")
        if rule.priority < 0:
            errors.append(f"{rule.id}: priority 必须 >=0")
        if not str(rule.reason).strip():
            errors.append(f"{rule.id}: reason 为空")
        if not str(rule.source).strip():
            errors.append(f"{rule.id}: source 为空")
        if not rule.when:
            errors.append(f"{rule.id}: when 为空（没有适用条件就是无条件偏移，不猜）")
        else:
            for key, cond in rule.when.items():
                if key not in _FACT_KIND:
                    errors.append(f"{rule.id}: when 未知事实 {key!r}")
                    continue
                problem = _validate_condition(key, cond)
                if problem:
                    errors.append(f"{rule.id}: {problem}")
        problem = _validate_value(rule)
        if problem:
            errors.append(problem)
    return errors


def _rule_from_dict(row: Mapping[str, Any], *, where: str) -> OffsetRule:
    allowed = {"id", "when", "target", "op", "value", "priority", "reason",
               "source"}
    extra = set(row) - allowed
    if extra:
        raise ValueError(f"{where}: 未知字段 {sorted(extra)}")
    when = row.get("when")
    if not isinstance(when, Mapping):
        raise ValueError(f"{where}: when 必须是对象")
    value = row.get("value")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: value 必须是数字或 null")
        value = float(value)
    priority = row.get("priority", 100)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError(f"{where}: priority 必须是整数")
    return OffsetRule(
        id=str(row.get("id") or ""),
        when=dict(when),
        target=str(row.get("target") or ""),
        op=str(row.get("op") or ""),
        value=value,
        priority=priority,
        reason=str(row.get("reason") or ""),
        source=str(row.get("source") or ""),
    )


def load_offset_rules(path: Path | str = OFFSETS_PATH) -> tuple[OffsetRule, ...]:
    """从独立 JSON 加载并校验；坏配置直接 ValueError。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        extra = set(data) - {"version", "rules"}
        if extra:
            raise ValueError(f"{path}: 未知顶层字段 {sorted(extra)}")
        version = data.get("version", 1)
        if version != 1:
            raise ValueError(f"{path}: 不支持的 version {version!r}")
        rows = data.get("rules")
    else:
        rows = data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: 期望 rules 数组")
    rules = tuple(_rule_from_dict(row, where=f"{path}[{i}]")
                  for i, row in enumerate(rows))
    errors = validate_offset_rules(rules)
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))
    return rules


@lru_cache(maxsize=1)
def default_offset_rules() -> tuple[OffsetRule, ...]:
    """仓库自带的偏移配置；lru_cache 保证每次决策不重复读盘。"""
    return load_offset_rules()


# ─────────────────────────── 条件匹配 ───────────────────────────

def _fact(sit, key: str):
    snap = getattr(sit, "snap", None)
    if key == "ante_bb":
        blinds = getattr(snap, "blinds", None)
        return None if blinds is None else getattr(blinds, "ante", None)
    if key == "effective_stack_bb":
        try:
            from ..pot_math import _effective_stack
            return _effective_stack(snap)
        except Exception:                     # noqa: BLE001 - 缺数据 = 不匹配
            return None
    if key == "street":
        return getattr(sit, "street", None)
    if key in ("preflop_node", "players"):
        try:
            spot = Spot.from_situation(sit)
            return getattr(spot, key, None)
        except Exception:                     # noqa: BLE001
            return None
    if key == "hero_position":
        return getattr(snap, "hero_position", None)
    if key == "pot_type":
        return getattr(sit, "pot_type", None)
    if key in ("table_loose", "table_passive"):
        attr = key[len("table_"):]
        stats = getattr(sit, "table_stats", None)
        value = getattr(stats, attr, None) if stats is not None else None
        if value is not None:
            return value
        # 画像还没有数字时，和 Situation 的 preflop 结论一样退回 .table_dy；
        # 没有桌风文本 = 未知，不能用 False 假装「不松/不被动」。
        text = getattr(sit, "table_dynamics", None)
        if not text:
            return None
        try:
            from ..table_dynamics import is_loose_table, is_passive_table
            return is_loose_table(text) if attr == "loose" else is_passive_table(text)
        except Exception:                     # noqa: BLE001 - 缺模块 = 不匹配
            return None
    return None


def _match_condition(actual, cond) -> bool:
    if isinstance(cond, Mapping):
        for op, want in cond.items():
            if op == "in":
                if actual not in want:
                    return False
            elif op == "eq":
                if actual != want:
                    return False
            else:
                if actual is None:
                    return False
                try:
                    a, w = float(actual), float(want)
                except (TypeError, ValueError):
                    return False
                if op == "gte" and not a >= w:
                    return False
                if op == "gt" and not a > w:
                    return False
                if op == "lte" and not a <= w:
                    return False
                if op == "lt" and not a < w:
                    return False
        return True
    if isinstance(cond, bool):
        return actual is cond
    return actual == cond


def _matches(rule: OffsetRule, sit) -> bool:
    return all(_match_condition(_fact(sit, key), cond)
               for key, cond in rule.when.items())


# ─────────────────────────── 展示 ───────────────────────────

def _fmt_fact(key: str, value) -> str:
    if key in ("ante_bb", "effective_stack_bb"):
        return f"{float(value):g}BB"
    if key == "street":
        return _STREET_ZH.get(str(value), str(value))
    if key == "preflop_node":
        return _NODE_ZH.get(str(value), str(value))
    if key == "players":
        return f"{value} 人"
    if key == "pot_type":
        return _POT_TYPE_ZH.get(str(value), str(value))
    if key in ("table_loose", "table_passive"):
        yes, no = (("松桌", "非松桌") if key == "table_loose"
                   else ("被动桌", "非被动桌"))
        return yes if value else no
    return str(value)


def _applicability(rule: OffsetRule) -> str:
    parts: list[str] = []
    for key, cond in rule.when.items():
        label = _FACT_ZH.get(key, key)
        if isinstance(cond, Mapping):
            sub: list[str] = []
            for op, want in cond.items():
                if op == "in":
                    values = "、".join(_fmt_fact(key, item) for item in want)
                    sub.append(f"{label}∈{{{values}}}")
                else:
                    sub.append(f"{label}{_OP_ZH[op]}{_fmt_fact(key, want)}")
            parts.append("且".join(sub))
        else:
            parts.append(f"{label}={_fmt_fact(key, cond)}")
    return " · ".join(parts)


def _fmt_value(target: str, value: float) -> str:
    if target == "size_bb":
        return f"{round(float(value), 2):g}BB"
    if target == "size_pct":
        return f"{round(float(value), 3):g} 池"
    return f"{float(value):.0%}"


# ─────────────────────────── 应用 ───────────────────────────

def _baselines(plan: Plan) -> dict[str, float]:
    out: dict[str, float] = {"freq": float(plan.freq)}
    if plan.size is not None:
        if plan.size.kind == "bb":
            out["size_bb"] = float(plan.size.value)
        elif plan.size.kind == "pct_pot":
            out["size_pct"] = float(plan.size.value)
    return out


def _option_amounts(sit, action: str) -> tuple[float, ...]:
    kind = _KIND_FOR_ACTION.get(action)
    if kind is None:
        return ()
    amounts = {
        float(option.amount)
        for option in (getattr(sit, "options", None) or ())
        if getattr(option, "kind", None) == kind
        and getattr(option, "amount", None) is not None
    }
    return tuple(sorted(amounts))


def _clamp_to_options(target: str, value: float, action: str, sit) -> float | None:
    """把尺寸移到本桌最近的合法 options 金额；没有 options 返回 None（不猜）。

    只夹区间不够：选项是离散金额（7 / 7.5 / 8BB），区间内的 7.2BB 仍然
    选不到。这里按绝对距离取最近的一档，保证最终值一定在 options 里。
    """
    amounts = _option_amounts(sit, action)
    if not amounts:
        return None
    if target == "size_bb":
        return min(amounts, key=lambda amount: (abs(amount - float(value)), amount))
    if target == "size_pct":
        # pct 只对「下注」有明确口径：options.amount / 当前底池。
        if action != "bet":
            return None
        snap = getattr(sit, "snap", None)
        pot = float(getattr(snap, "pot", 0) or 0)
        if pot <= 0:
            return None
        amount = min(amounts, key=lambda item: (abs(item - float(value) * pot), item))
        return amount / pot
    return None


def _apply_rule(rule: OffsetRule, before: float, action: str, sit) -> float | None:
    if rule.op == "clamp_options":
        return _clamp_to_options(rule.target, before, action, sit)
    if rule.op == "add":
        raw = before + float(rule.value or 0.0)
    elif rule.op == "mul":
        raw = before * float(rule.value or 0.0)
    else:
        raw = float(rule.value or 0.0)
    if rule.target == "freq":
        return min(max(raw, 0.001), 1.0)
    if raw <= 0:
        return None
    if rule.target == "size_pct" and raw > 2:
        raw = 2.0
    clamped = _clamp_to_options(rule.target, raw, action, sit)
    return raw if clamped is None else clamped


def apply_offsets(card: Card | None, sit,
                  rules: Sequence[OffsetRule] | None = None
                  ) -> tuple[OffsetDelta, ...]:
    """按桌况应用偏移；返回实际发生变化的条目（没有变化 = 空）。

    只读 card.plan 的尺寸/频率，绝不改 action；`offset_policy="none"`
    的卡直接跳过。规则按 (priority, id) 升序叠加，每条看到的是上一条之后
    的值，因此多条规则的「书上 -> 最终」是一条可审计的链。
    """
    if card is None or getattr(card, "offset_policy", "size_only") == "none":
        return ()
    plan = getattr(card, "plan", None)
    if plan is None:
        return ()
    try:
        seq = default_offset_rules() if rules is None else tuple(rules)
    except Exception:                         # noqa: BLE001 - 坏配置不阻塞主循环
        return ()
    current = _baselines(plan)
    deltas: list[OffsetDelta] = []
    for rule in sorted(seq, key=lambda r: (r.priority, r.id)):
        if rule.target not in current:
            continue
        if not _matches(rule, sit):
            continue
        before = current[rule.target]
        final = _apply_rule(rule, before, plan.action, sit)
        if final is None or abs(final - before) < 1e-9:
            continue
        deltas.append(OffsetDelta(
            target=rule.target,
            baseline=_fmt_value(rule.target, before),
            final=_fmt_value(rule.target, final),
            reason=rule.reason,
            applicability=_applicability(rule),
        ))
        current[rule.target] = final
    return tuple(deltas)


def offsets_for_situation(sit) -> tuple[OffsetDelta, ...]:
    """决策链路入口：解析书卡 -> 应用偏移；任何异常 fail open。

    炸弹池守则生效时整层让位：不解析书卡、不产生偏移。书卡本身都不该
    出现在提示词里，偏移（书 -> 本桌）自然也没有立足点。
    """
    if bomb_guard_applies(sit):
        return ()
    try:
        from .prompt import resolve_for_situation
        _, card = resolve_for_situation(sit)
        return apply_offsets(card, sit)
    except Exception:                         # noqa: BLE001
        return ()


__all__ = [
    "OFFSETS_PATH",
    "OFFSET_OPS",
    "OFFSET_TARGETS",
    "OffsetDelta",
    "OffsetRule",
    "apply_offsets",
    "default_offset_rules",
    "load_offset_rules",
    "offsets_for_situation",
    "validate_offset_rules",
]
