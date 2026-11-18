"""规则台账：加载 → 静态校验 → 按前提匹配。

设计见 `docs/规则台账-设计-2026-11-18.md` §2/§3/§4。动手改之前先认这三条：

1. **事实 > 规则台账 > 兜底经验**（Q24）。规则只能引用事实层给的谓词，不许
   自己算牌力、算尺寸；事实层缺数据时匹配器一律 fail open 到「不命中」，
   绝不抛异常、绝不把缺失当成 False。
2. **槽位模型**（Q33）：`slot ∈ {intent, size, why}`，一个命中点每个槽位最多
   一条。同槽位两条规则的前提有交集 = 静态失败（同一个局面会同时出两条
   intent），唯一逃生口是两边都写 `coexist_with`（双向）；不同槽位可以共存。
3. **规则里不存数字**（Q34）。书上的尺寸住 `poker_ocr/tables/rule_params.json`，
   规则只存引用；`--check` 会拿 `strategy_tables.table()` 去解析，解析不了
   就是错。

M0（票 64）只有骨架：台账是空的，提示词一个字节都没接——票 64 的验收要求
golden 三个 hash 与 `lean-zh-2026-11-16.3` 完全一致，就是在证明这一点。
首批 20 条规则是 M1，走单独 commit（设计 §10）。

三个写进代码的例外（避免「看起来多余的豁免」被后人当 bug 删掉）：

  · `po.cbet.hu` 与 `po.size.texture` 会同时命中，靠**双向** `coexist_with`
    声明共存，牌面尺寸优先；不写进代码，只写在台账里。
  · `ex.*` 是 intent 修正层，不参与普通匹配：`match_rules()` 默认跳过，
    同槽位交集检查也豁免它与非 `ex.` 规则之间的冲突（它自己的冲突照查）。
  · 零命中不渲染任何东西——没有「兜底规则」就没有兜底行，这是 fail open
    的上游约定，不是漏项。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..positions import BB, BTN, CO, HJ, LJ, SB, UTG, UTG1, UTG2
from ..strategy_tables import table as _strategy_table
from .facts import facts_snapshot

DEFAULT_LEDGER_DIR = Path(__file__).resolve().parent / "ledger"

STREETS = ("preflop", "flop", "turn", "river")
SLOTS = frozenset({"intent", "size", "why"})
OPERATORS = frozenset({"eq", "in", "not_in", "between", "gte", "lte", "exists"})
# 必须与 `options.Option.kind` 同词表：规则只能选模型也能选的动作。
ACTIONS = frozenset({"check", "call", "fold", "raise", "bet", "allin"})
CONFIDENCES = frozenset({"book", "inferred"})
SOURCE_STATUS = frozenset({"verbatim", "rebuilt"})

_ID_RE = re.compile(r"^(pf|po|ex)\.[a-z0-9_]+(\.[a-z0-9_]+)*$")
_PREFIX_STREET = {"pf": ("preflop",), "po": ("flop", "turn", "river")}

# 谓词目录（设计 §5 的白名单）。**这是唯一的合法来源**：台账里出现不留
# 在这里的键就是错，匹配器也不认识它。新增谓词要同时改 `facts.py`，否则
# `tests/test_rules_ledger.py` 的「快照键 ⊆ 白名单」会红。
_NUMERIC_PREDICATES = (
    "players_in_hand",
    "limpers_before",
    "raises_before",
    "pot_bb",
    "to_call_bb",
    "pot_odds",
    "spr",
    "effective_stack_bb",
    "equity_pct",
    "outs",
    "opp_vpip",
    "opp_pfr",
    "opp_fold_to_cbet",
    "ante_bb",
)

# 取值域：闭合枚举写全（校验器会拒收域外取值），开区间写 None。
# `not_in` 只对闭合枚举有确定语义——开放域上「不属于」无从判断，校验器
# 直接拒收，避免写出一条永远成立的假条件。
PREDICATE_DOMAINS: dict[str, tuple | None] = {
    "street": STREETS,
    # 位置名只认 `poker_ocr/positions.py` 那一份契约（9 人桌最多 9 个名字）。
    # 写 "MP" 看着顺眼，但牌桌永远不会产出这个值——那样的规则永不命中。
    "hero_position": (UTG, UTG1, UTG2, LJ, HJ, CO, BTN, SB, BB),
    "in_position": (True, False),
    "pot_type": ("bomb pot", "3-bet+ pot", "single-raised pot",
                 "limped pot", "unopened pot"),
    "explosive_pot": (True, False),
    "hero_hand_tier": ("强成牌", "超对", "顶对强踢", "顶对弱踢",
                       "中底对", "强听", "弱听", "空气", "打牌面"),
    "hand_label": None,
    "has_draw": (True, False),
    "board_texture": ("干燥", "花面", "顺面", "花顺面", "公对面"),
    "board_paired": (True, False),
    "opponent_archetype": ("unknown", "station", "lag", "maniac", "nit", "tag"),
    "previous_line": ("fold", "check", "call", "bet", "raise", "allin"),
    "hand_in_open_range": (True, False),
    "hand_in_3bet_range": (True, False),
    "hero_is_first_to_act": (True, False),
}

PREDICATES = frozenset(_NUMERIC_PREDICATES) | frozenset(PREDICATE_DOMAINS)


class RuleError(ValueError):
    """台账不合法。`errors` 是逐条可打印的「[规则 id] 说明」。"""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Precondition:
    """一条前提：`{"fact": 谓词, 算子: 取值}`。"""

    fact: str
    op: str
    value: Any

    def to_dict(self) -> dict:
        return {"fact": self.fact, self.op: self.value}


@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    slot: str
    street: str
    node: str
    preconditions: tuple[Precondition, ...]
    why: str
    source: dict = field(default_factory=dict)
    action: str | None = None
    size: dict | None = None
    coexist_with: tuple[str, ...] = ()
    confidence: str = "book"
    verified: bool = False
    source_file: str = ""


# ─────────────────────────── 加载 ───────────────────────────

def load_ledger(directory: str | Path | None = None, *,
                resolve_size=None) -> tuple[Rule, ...]:
    """读整个台账目录并校验；有任何问题就抛 `RuleError`（附全部错误）。

    `directory` 不存在 = 空台账（M0 就长这样，连 `.gitkeep` 都可能没有）。
    文件形状：`{"schema": 1, "rules": [...]}`，也接受裸数组。
    """
    root = Path(directory) if directory is not None else DEFAULT_LEDGER_DIR
    if not root.exists():
        return ()
    errors: list[str] = []
    rules: list[Rule] = []
    for path in sorted(root.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"[{path.name}] 读不出 JSON：{exc}")
            continue
        if isinstance(doc, dict):
            schema = doc.get("schema", 1)
            if schema != 1:
                errors.append(f"[{path.name}] schema 必须是 1，实际 {schema!r}")
                continue
            items = doc.get("rules", [])
        else:
            items = doc
        if not isinstance(items, list):
            errors.append(f"[{path.name}] rules 必须是数组")
            continue
        for index, raw in enumerate(items):
            rule, issues = _parse_rule(raw, f"{path.name}#{index}", resolve_size)
            errors.extend(issues)
            if rule is not None:
                rules.append(rule)
    errors.extend(validate_rules(rules))
    if errors:
        raise RuleError(errors)
    return tuple(sorted(rules, key=lambda item: item.id))


def validate_rules(rules: Sequence[Rule]) -> list[str]:
    """跨规则校验：id 唯一、coexist 双向、同槽位前提不相交。"""
    errors: list[str] = []
    by_id: dict[str, Rule] = {}
    for rule in rules:
        if rule.id in by_id:
            errors.append(f"[{rule.id}] id 重复（{rule.source_file}）")
        else:
            by_id[rule.id] = rule

    for rule in rules:
        for other_id in rule.coexist_with:
            if other_id == rule.id:
                errors.append(f"[{rule.id}] coexist_with 指向自己")
                continue
            other = by_id.get(other_id)
            if other is None:
                errors.append(f"[{rule.id}] coexist_with 指向不存在的规则 {other_id}")
                continue
            if rule.id not in other.coexist_with:
                errors.append(
                    f"[{rule.id}] coexist_with 必须双向：{other_id} 没有声明 "
                    f"{rule.id}")

    for index, left in enumerate(rules):
        for right in rules[index + 1:]:
            if left.slot != right.slot:
                continue
            if _exempt_overlap(left, right) or _mutual_coexist(left, right):
                continue
            if not _overlaps(left, right):
                continue
            errors.append(
                f"[{left.id}] 与 [{right.id}] 同槽位（{left.slot}）且前提有交集："
                "同一个局面会出两条；确实要共存就两边都写 coexist_with")
    return errors


def _mutual_coexist(left: Rule, right: Rule) -> bool:
    """两边都声明了对方才能共存（单向声明是写错了，`validate_rules` 已单独报错）。"""
    return left.id in right.coexist_with and right.id in left.coexist_with


def _exempt_overlap(left: Rule, right: Rule) -> bool:
    """`ex.*` 修正层与普通规则的相交不算冲突（设计 §2.2 例外 2）。

    ex.* 不进普通匹配（见 `match_rules(include_exploit=False)`），它和
    po.*/pf.* 同时在前提上成立是设计内的常态；两条 ex.* 之间照查。
    """
    if left.slot != "intent":
        return False
    is_ex = (left.id.startswith("ex."), right.id.startswith("ex."))
    return is_ex[0] != is_ex[1]


# ─────────────────────────── 单条校验 ───────────────────────────

def _parse_rule(raw: Any, where: str,
                resolve_size=None) -> tuple[Rule | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"[{where}] 规则必须是对象"]
    label = raw.get("id") if isinstance(raw.get("id"), str) else where
    errors: list[str] = []

    def bad(message: str) -> None:
        errors.append(f"[{label}] {message}")

    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not _ID_RE.match(rule_id):
        bad(f"id 必须是 pf./po./ex. 前缀的小写点分名，实际 {rule_id!r}")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        bad(f"version 必须是 ≥1 的整数，实际 {version!r}")
    slot = raw.get("slot")
    if slot not in SLOTS:
        bad(f"slot 必须是 {sorted(SLOTS)} 之一，实际 {slot!r}")
    street = raw.get("street")
    if street not in STREETS:
        bad(f"street 必须是 {list(STREETS)} 之一，实际 {street!r}")
    elif isinstance(rule_id, str):
        prefix = rule_id.split(".", 1)[0]
        allowed = _PREFIX_STREET.get(prefix)
        if allowed and street not in allowed:
            bad(f"id 前缀 {prefix}. 与 street={street} 不符（应为 {allowed}）")
    node = raw.get("node")
    if not isinstance(node, str) or not node.strip():
        bad("node 不能为空")
    elif len(node) > 30:
        bad(f"node 最多 30 字，实际 {len(node)}")

    why = raw.get("why")
    if not isinstance(why, str) or not why.strip():
        bad("why 不能为空（每条规则都要能一句话说清为什么）")
    elif len(why) > 40:
        bad(f"why 最多 40 字，实际 {len(why)}")

    action = raw.get("action")
    if action is not None and action not in ACTIONS:
        bad(f"action 必须是 {sorted(ACTIONS)} 之一，实际 {action!r}")
    if slot == "intent" and action is None:
        bad("intent 槽必须给 action")
    if slot != "intent" and action is not None:
        bad(f"{slot} 槽不该给 action")

    size = raw.get("size")
    if slot == "size" and size is None:
        bad("size 槽必须给 size 引用")
    if slot == "why" and size is not None:
        bad("why 槽不该给 size")
    if size is not None:
        for message in _check_size_ref(size, resolve_size):
            bad(message)

    preconditions, issues = _check_preconditions(raw.get("preconditions"))
    for message in issues:
        bad(message)

    coexist = raw.get("coexist_with", ())
    if not isinstance(coexist, (list, tuple)) or any(
            not isinstance(x, str) for x in coexist):
        bad("coexist_with 必须是规则 id 数组")
        coexist = ()

    source = raw.get("source")
    for message in _check_source(source):
        bad(message)

    confidence = raw.get("confidence", "book")
    if confidence not in CONFIDENCES:
        bad(f"confidence 必须是 {sorted(CONFIDENCES)} 之一，实际 {confidence!r}")
    verified = raw.get("verified", False)
    if not isinstance(verified, bool):
        bad(f"verified 必须是布尔值，实际 {verified!r}")

    if errors:
        return None, errors
    return Rule(
        id=str(rule_id), version=int(version), slot=str(slot),
        street=str(street), node=str(node),
        preconditions=tuple(preconditions), why=str(why),
        source=dict(source), action=action, size=size,
        coexist_with=tuple(coexist), confidence=confidence,
        verified=verified, source_file=where,
    ), []


def _check_size_ref(size: Any, resolve_size) -> list[str]:
    if not isinstance(size, dict):
        return ["size 只能引用策略表（{\"table\": …, \"name\": …}），"
                f"不要写字面数字：{size!r}"]
    kind, name = size.get("table"), size.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        return [f"size 引用必须给 table 与 name，实际 {size!r}"]
    resolver = resolve_size or _resolve_size_ref
    try:
        ok = bool(resolver(kind, name))
    except Exception as exc:  # noqa: BLE001 - 解析失败一律算错，附原因
        return [f"size 引用 {kind}.{name} 解析失败：{exc}"]
    if not ok:
        return [f"size 引用 {kind}.{name} 在策略表里找不到"]
    return []


def _resolve_size_ref(kind: str, name: str) -> bool:
    """规则里的尺寸只能指向策略表条目：数字住在表里，规则只存路径。"""
    entry = _strategy_table(kind, name)
    return entry is not None


def _check_preconditions(raw: Any) -> tuple[list[Precondition], list[str]]:
    errors: list[str] = []
    if not isinstance(raw, list) or len(raw) < 2:
        return [], ["preconditions 至少要两条（一条前提的规则等于无条件）"]
    out: list[Precondition] = []
    for item in raw:
        if not isinstance(item, dict):
            errors.append("preconditions 的每一项必须是对象")
            continue
        fact = item.get("fact")
        if fact not in PREDICATES:
            errors.append(f"未知谓词 {fact!r}（合法谓词见设计 §5 / rules.PREDICATES）")
            continue
        ops = [key for key in item if key != "fact"]
        if len(ops) != 1:
            errors.append(f"{fact} 必须且只能给一个算子，实际 {ops}")
            continue
        op = ops[0]
        if op not in OPERATORS:
            errors.append(f"未知算子 {op!r}（合法算子：{sorted(OPERATORS)}）")
            continue
        value = item[op]
        problem = _check_operand(fact, op, value)
        if problem:
            errors.append(problem)
            continue
        out.append(Precondition(str(fact), str(op), value))
    if out and not any(p.fact == "street" for p in out):
        errors.append("preconditions 必须含 street（跨街复用同一前提是最常见的写错）")
    for fact in {p.fact for p in out}:
        allowed = _fold_allowed([p for p in out if p.fact == fact])
        if allowed is not None and _empty_on(allowed, PREDICATE_DOMAINS.get(fact)):
            errors.append(f"{fact} 的取值区间为空（前提自相矛盾）")
    return out, errors


def _check_operand(fact: str, op: str, value: Any) -> str | None:
    numeric = fact in _NUMERIC_PREDICATES
    domain = PREDICATE_DOMAINS.get(fact)
    if op == "exists":
        if not isinstance(value, bool):
            return f"{fact}.exists 只接受 true/false，实际 {value!r}"
        return None
    if op in ("in", "not_in"):
        if not isinstance(value, (list, tuple)) or not value:
            return f"{fact}.{op} 要非空数组，实际 {value!r}"
        if op == "not_in" and domain is None:
            return f"{fact}.not_in 需要闭合取值域；{fact} 是开放域，做不到「不属于」"
        for item in value:
            problem = _check_scalar(fact, item, numeric, domain)
            if problem:
                return problem
        return None
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return f"{fact}.between 要 [下界, 上界]，实际 {value!r}"
        if not numeric:
            return f"{fact}.between 只能用在数值谓词上"
        for item in value:
            problem = _check_scalar(fact, item, numeric, domain)
            if problem:
                return problem
        if value[0] > value[1]:
            return f"{fact}.between 下界 {value[0]!r} 大于上界 {value[1]!r}"
        return None
    if op in ("gte", "lte"):
        if not numeric:
            return f"{fact}.{op} 只能用在数值谓词上"
        return _check_scalar(fact, value, numeric, domain)
    # eq
    return _check_scalar(fact, value, numeric, domain)


def _check_scalar(fact: str, value: Any, numeric: bool, domain) -> str | None:
    if numeric:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{fact} 是数值谓词，取值必须是数字，实际 {value!r}"
        if not math.isfinite(float(value)):
            return f"{fact} 不接受非有限数字 {value!r}"
        return None
    if domain is not None and not any(_same(value, x) for x in domain):
        return f"{fact} 只接受 {list(domain)}，实际 {value!r}"
    if not isinstance(value, (bool, str)):
        return f"{fact} 取值必须是字符串或布尔值，实际 {value!r}"
    return None


def _check_source(source: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(source, dict):
        return ["source 必须是对象（book 或 measured）"]
    status = source.get("status")
    if status not in SOURCE_STATUS:
        errors.append(f"source.status 必须是 {sorted(SOURCE_STATUS)} 之一，"
                      f"实际 {status!r}")
    book, measured = source.get("book"), source.get("measured")
    if not book and not measured:
        errors.append("source 必须给 book 或 measured")
    if book is not None:
        if not isinstance(book, str) or not book.strip():
            errors.append("source.book 必须是非空书名")
        page = source.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            errors.append(f"source.page 必须是 ≥1 的整数页码，实际 {page!r}")
    if measured is not None and (not isinstance(measured, str)
                                 or not measured.strip()):
        errors.append("source.measured 必须是非空说明（哪次实测/哪份日志）")
    return errors


# ─────────────────────────── 匹配 ───────────────────────────

def match_rules(facts: dict, rules: Sequence[Rule] | None = None, *,
                include_exploit: bool = False) -> tuple[Rule, ...]:
    """返回所有前提都成立（且命中点不是 ex.* 修正层）的规则。

    `facts` 来自 `facts_snapshot()`；缺键 = 这条事实不知道 → 该前提不成立
    （fail open 到「不命中」），**不是** False。
    """
    pool = load_ledger() if rules is None else tuple(rules)
    hits = [rule for rule in pool
            if (include_exploit or not rule.id.startswith("ex."))
            and all(precondition_holds(p, facts) for p in rule.preconditions)]
    return tuple(sorted(hits, key=lambda item: item.id))


def precondition_holds(precondition: Precondition, facts: dict) -> bool:
    """单条前提在事实快照里是否成立；缺事实一律不成立。"""
    present = precondition.fact in facts
    op, want = precondition.op, precondition.value
    if op == "exists":
        return present == bool(want)
    if not present:
        return False
    got = facts[precondition.fact]
    if op == "eq":
        return _same(got, want)
    if op == "in":
        return any(_same(got, item) for item in want)
    if op == "not_in":
        return not any(_same(got, item) for item in want)
    if op in ("gte", "lte", "between"):
        if not _is_number(got):
            return False
        if op == "gte":
            return got >= want
        if op == "lte":
            return got <= want
        return want[0] <= got <= want[1]
    return False


def _same(left: Any, right: Any) -> bool:
    """严格相等：`1 == True` 在 Python 里成立，台账里不能混。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return (isinstance(left, bool) and isinstance(right, bool)
                and left == right)
    return left == right


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


# ─────────────────── 前提交集（静态冲突检测） ───────────────────
#
# 前提是若干「单谓词一元条件」的合取，谓词之间不互相引用，所以两条规则的
# 前提相交 ⟺ 每个谓词上的取值集合都相交——不需要真的做笛卡尔积，逐谓词
# 判交集就等价（设计 §2.2 的枚举在数学上可化简成这一步）。
#
# 数值谓词：区间 + 有限点 + 排除点（`between/gte/lte/eq` 给区间或点，
# `in` 给有限点，`not_in` 给排除点）。枚举谓词：有限点集（None = 全集）
# 减排除点。

@dataclass(frozen=True)
class _Allowed:
    """一个谓词的取值集合；`impossible` = 这组前提自相矛盾。"""

    present: bool = True
    numeric: bool = False
    impossible: bool = False
    lo: float = float("-inf")
    hi: float = float("inf")
    points: frozenset | None = None
    excluded: frozenset = frozenset()


_IMPOSSIBLE = _Allowed(impossible=True)


def _fold_allowed(items: Sequence[Precondition]) -> _Allowed | None:
    """同一谓词上的多条前提取交集；None = 没有约束。"""
    allowed: _Allowed | None = None
    for item in items:
        one = _allowed_of(item)
        allowed = one if allowed is None else _meet(allowed, one)
        if allowed is not None and _empty(allowed):
            return allowed
    return allowed


def _allowed_of(precondition: Precondition) -> _Allowed:
    numeric = precondition.fact in _NUMERIC_PREDICATES
    op, value = precondition.op, precondition.value
    if op == "exists":
        return _Allowed(present=bool(value), numeric=numeric)
    if op == "eq":
        return (_Allowed(numeric=True, lo=value, hi=value) if numeric
                else _Allowed(points=frozenset({value})))
    if op == "in":
        return _Allowed(points=frozenset(value))
    if op == "not_in":
        return _Allowed(numeric=numeric, excluded=frozenset(value))
    if op == "between":
        return _Allowed(numeric=True, lo=value[0], hi=value[1])
    if op == "gte":
        return _Allowed(numeric=True, lo=value)
    if op == "lte":
        return _Allowed(numeric=True, hi=value)
    raise AssertionError(f"未处理的算子 {op!r}")


def _meet(left: _Allowed, right: _Allowed) -> _Allowed:
    """两个取值集合求交（只用来判空，形状不要求精确）。"""
    if left.impossible or right.impossible:
        return _IMPOSSIBLE
    if left.present is False and right.present is False:
        return _Allowed(present=False, numeric=left.numeric or right.numeric)
    if left.present is False or right.present is False:
        # 「事实缺席」与「事实取某值」互斥。
        return _IMPOSSIBLE
    numeric = left.numeric or right.numeric
    excluded = left.excluded | right.excluded

    points = None
    if left.points is not None or right.points is not None:
        out = set(left.points if left.points is not None else right.points)
        if left.points is not None:
            out &= set(left.points)
        if right.points is not None:
            out &= set(right.points)
        if numeric:
            out = {v for v in out if left.lo <= v <= left.hi}
            out = {v for v in out if right.lo <= v <= right.hi}
        points = frozenset(out)

    lo = max(left.lo, right.lo)
    hi = min(left.hi, right.hi)
    if points is not None:
        return _Allowed(numeric=numeric, points=points, excluded=excluded)
    return _Allowed(numeric=numeric, lo=lo, hi=hi, excluded=excluded)


def _empty(allowed: _Allowed) -> bool:
    if allowed.impossible:
        return True
    if allowed.present is False:
        return False          # 「缺席」本身是合法的取值状态
    if allowed.points is not None:
        return not (set(allowed.points) - set(allowed.excluded))
    if not allowed.numeric:
        return False          # 开放枚举域上假定还有别的取值
    if allowed.lo > allowed.hi:
        return True
    if allowed.lo == allowed.hi:
        return any(_same(allowed.lo, v) for v in allowed.excluded)
    return False


def _empty_on(allowed: _Allowed, domain) -> bool:
    """在**闭合**取值域上判空。

    `_empty` 只看区间与点，对「开放枚举域」只能假定还有别的取值；一旦谓词
    有闭合域（`PREDICATE_DOMAINS`），`not_in` 把所有取值都排除掉时集合是真
    空的——两条规则各自 `not_in` 不同子集也可能一个值都不剩。
    """
    if _empty(allowed):
        return True
    if allowed.numeric or not domain:
        return False
    keep = set(domain) - set(allowed.excluded)
    if allowed.points is not None:
        keep &= set(allowed.points)
    return not keep


def _overlaps(left: Rule, right: Rule) -> bool:
    facts_a = _group(left.preconditions)
    facts_b = _group(right.preconditions)
    for fact in set(facts_a) | set(facts_b):
        one = _fold_allowed(facts_a.get(fact, ()))
        two = _fold_allowed(facts_b.get(fact, ()))
        if one is None or two is None:
            continue
        if _empty_on(_meet(one, two), PREDICATE_DOMAINS.get(fact)):
            return False
    return True


def _group(preconditions: Sequence[Precondition]) -> dict[str, list[Precondition]]:
    out: dict[str, list[Precondition]] = {}
    for item in preconditions:
        out.setdefault(item.fact, []).append(item)
    return out


__all__ = [
    "DEFAULT_LEDGER_DIR", "PREDICATES", "PREDICATE_DOMAINS", "SLOTS",
    "OPERATORS", "STREETS", "ACTIONS", "Precondition", "Rule", "RuleError",
    "facts_snapshot", "load_ledger", "match_rules", "precondition_holds",
    "validate_rules",
]
