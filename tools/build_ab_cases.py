#!/usr/bin/env python3
"""从真实历史建议记录派生「回归锚点」用例，追加进 tools/ab_set.json（#66）。

锚点不是人工构造的期望，而是线上真实发生过的建议快照：

  · 过滤掉不能落成决策点的行（动作词不在 specs.ACTIONS、缺 uid / street /
    action_index），并跳过 ab_set 里已经存在的 (hand, street, action_index)；
  · 每条街的候选先按 prompt_version 日期从新到旧、同版本按 advice 行 id
    升序；再按 option_kind（bet/raise/call/check/fold/allin）轮转取，避免
    某一类动作（尤其 fold）占满整条街；
  · 每个 hand 最多取 2 条，每条街补到 per_street + surplus；
  · 凑不够就 raise，绝不静默少给、也不编造期望。

产出条目只复用 ab_set 现有 case 的字段：expect 留空串（真实历史记录没有
人工期望），new_advice 用 specs.ACTIONS 词表（allin 不写 all-in），note 固定为
可追溯的一行：`锚点：advice_row=… hand=… street=… action_index=… version=…
建议=…｜真实动作=…`；decision_key 只有非空且不是 "[]"/"null" 才追加
` decision=…`；真实动作与建议不一致时末尾再补 `｜分歧`。

用法：

    ./.venv/bin/python tools/build_ab_cases.py --source /tmp/ab_cases_dump.json --dry-run
    ./.venv/bin/python tools/build_ab_cases.py --source /tmp/ab_cases_dump.json
    ./.venv/bin/python tools/build_ab_cases.py              # 不给 --source → 连 MySQL

不给 --source 时复用 replay-console 的 call-back env 约定（Settings.from_env：
REPLAY_DB_* / DB_*，再回落到 ../call-back/.env）。连不上会打印明确错误并以
非零退出，不会退化成空结果。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "replay-console"
for path in (str(ROOT), str(CONSOLE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from replay import case_seed, specs  # noqa: E402

AB_SET_PATH = ROOT / "tools" / "ab_set.json"
STREETS = ("preflop", "flop", "turn", "river")
DEFAULT_PER_STREET = 15
DEFAULT_SURPLUS = 2
DEFAULT_MIN_TOTAL = 60
MAX_PER_HAND = 2
ANCHOR_PRIORITY = "P1"
ANCHOR_SOURCE = "regression"
# 每街轮转取样的动作顺序：先主动下注，再跟/过/弃，最后 allin。
ACTION_ROTATION = ("bet", "raise", "call", "check", "fold", "allin")

CASE_FIELDS = (
    "id", "priority", "hand", "street", "action_index", "category",
    "source", "status", "expect", "new_advice", "note",
)

_VERSION_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:\.(\d+))?")
_ACTION_ALIASES = {"all-in": "allin", "all_in": "allin", "allin": "allin"}

_DB_SQL = """
    SELECT a.id, a.street, a.action_index, a.decision_key,
           a.option_id, a.option_kind, a.actual_action,
           a.prompt_version, a.model, a.bucket, a.source,
           a.hand_id, h.external_uid
      FROM replay_live_advices a
      JOIN replay_imported_hands h ON h.id = a.hand_id
     WHERE h.deleted_at IS NULL
     ORDER BY a.id
"""


# ───────────────────────────── 纯函数 ─────────────────────────────

def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_action(value: Any) -> str:
    """把来源里的动作词归一到 specs.ACTIONS；认不出就返回空串。"""
    word = _text(value).lower()
    word = _ACTION_ALIASES.get(word, word)
    return word if word in specs.ACTIONS else ""


def version_key(version: Any) -> tuple[date, int]:
    """`lean-zh-2026-11-16.3` → (date(2026,11,16), 3)；认不出排最旧。"""
    match = _VERSION_RE.search(_text(version))
    if not match:
        return (date.min, 0)
    year, month, day, revision = match.groups()
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return (date.min, 0)
    return (parsed, int(revision or 0))


def decision_of(row: dict) -> tuple[str, str, int] | None:
    """(hand, street, action_index)；缺任一或 action_index 非法 → None。"""
    hand = _text(row.get("external_uid") or row.get("hand"))
    street = _text(row.get("street"))
    action_index = row.get("action_index")
    if not hand or not street or action_index is None:
        return None
    try:
        index = int(action_index)
    except (TypeError, ValueError):
        return None
    return (hand, street, index)


def is_eligible(row: Any) -> bool:
    """能不能做锚点：动作词合法、uid/street/action_index 齐全且街在四街内。"""
    if not isinstance(row, dict):
        return False
    if not normalize_action(row.get("option_kind")):
        return False
    key = decision_of(row)
    return bool(key and key[1] in STREETS)


def _existing_keys(existing_cases: Iterable[dict]) -> set[tuple[str, str, int]]:
    keys: set[tuple[str, str, int]] = set()
    for case in existing_cases:
        if not isinstance(case, dict):
            continue
        key = decision_of(case)
        if key:
            keys.add(key)
    return keys


def _street_counts(existing_cases: Sequence[dict]) -> Counter:
    return Counter(
        street for street in (
            _text(case.get("street")) for case in existing_cases
            if isinstance(case, dict)
        )
        if street in STREETS
    )


def _plan_case_counts(existing_cases: Sequence[dict]) -> Counter:
    """按 case_seed.plan_seed 的真实口径数每条街能落成 case 的条数。"""
    plan = case_seed.plan_seed({"cases": list(existing_cases)})
    return Counter(
        case["street"] for case in plan["cases"] if case["street"] in STREETS)


def _sort_key(row: dict) -> tuple[int, int, int]:
    day, revision = version_key(row.get("prompt_version"))
    return (-day.toordinal(), -revision,
            _int_or(row.get("id"), sys.maxsize))


def select_anchors(rows: Sequence[dict], existing_cases: Sequence[dict],
                   per_street: int = DEFAULT_PER_STREET,
                   surplus: int = DEFAULT_SURPLUS,
                   max_per_hand: int = MAX_PER_HAND,
                   min_total: int | None = None,
                   streets: Sequence[str] | None = None) -> list[dict]:
    """确定性挑选锚点源行；同输入同输出。

    每条街补到 `per_street + surplus`：同时看 ab_set 里该街条目数、以及
    plan_seed 后真正能落成 case 的条数，取更缺的那个为准（这样老条目里
    status≠ready / expect=n-a 的街不会虚高）。

    候选先按版本新→旧、id 升序排，再按 option_kind 轮转取样：每轮从
    bet/raise/call/check/fold/allin 各取至多一条，某类耗尽就跳过，直到该街
    补满目标数。同一 hand 仍最多 max_per_hand 条。凑不够 raise，绝不静默少给。

    `streets` 不给时只看数据里出现过的街（方便单街单测）；CLI 显式传四街，
    保证线上每条街都被补齐。
    """
    if per_street < 0 or surplus < 0 or max_per_hand < 1:
        raise ValueError("per_street / surplus 不能为负，max_per_hand 至少为 1")

    existing = list(existing_cases)
    rows = list(rows)
    taken = _existing_keys(existing)
    total_counts = _street_counts(existing)
    plan_counts = _plan_case_counts(existing)
    target = per_street + surplus

    if streets is None:
        seen = {street for street in STREETS
                if total_counts[street] or plan_counts[street]}
        seen.update(_text(row.get("street")) for row in rows
                    if is_eligible(row))
        scope = tuple(street for street in STREETS if street in seen)
    else:
        unknown = sorted(set(streets) - set(STREETS))
        if unknown:
            raise ValueError(f"未知街：{unknown}")
        scope = tuple(street for street in STREETS if street in set(streets))

    needs = {
        street: max(0, target - total_counts[street],
                    target - plan_counts[street])
        for street in scope
    }

    ordered = sorted((row for row in rows if is_eligible(row)), key=_sort_key)
    buckets: dict[str, dict[str, list[dict]]] = {
        street: {kind: [] for kind in ACTION_ROTATION} for street in needs
    }
    for row in ordered:
        key = decision_of(row)
        assert key is not None  # is_eligible 已保证
        street = key[1]
        if street not in needs:
            continue
        buckets[street][normalize_action(row.get("option_kind"))].append(row)

    selected: list[dict] = []
    got = Counter()
    per_hand = Counter()
    for street in needs:
        positions = {kind: 0 for kind in ACTION_ROTATION}
        while got[street] < needs[street]:
            picked = False
            for kind in ACTION_ROTATION:
                if got[street] >= needs[street]:
                    break
                bucket = buckets[street][kind]
                while positions[kind] < len(bucket):
                    row = bucket[positions[kind]]
                    positions[kind] += 1
                    key = decision_of(row)
                    assert key is not None
                    hand = key[0]
                    if key in taken or per_hand[hand] >= max_per_hand:
                        continue
                    selected.append(dict(row))
                    got[street] += 1
                    per_hand[hand] += 1
                    taken.add(key)
                    picked = True
                    break
            if not picked:
                break

    missing = [
        (street, needs[street], got[street])
        for street in scope if got[street] < needs[street]
    ]
    if missing:
        detail = "；".join(
            f"{street} 需新增 {need} 条、只凑到 {have} 条（缺 {need - have}）"
            for street, need, have in missing)
        raise ValueError(f"锚点不足：{detail}")

    if min_total is not None:
        total = len(existing) + len(selected)
        if total < min_total:
            raise ValueError(
                f"锚点总数不足：现有 {len(existing)} + 新增 {len(selected)} "
                f"= {total} < {min_total}")
    return selected


def _next_seq(existing_cases: Sequence[dict], street: str) -> int:
    """续接已有 C-<street>-NN，避免二次运行撞 id / category。"""
    biggest = 0
    for case in existing_cases:
        if not isinstance(case, dict):
            continue
        for field, pattern in (
                ("id", rf"C-{re.escape(street)}-(\d+)"),
                ("category", rf"anchor_{re.escape(street)}_(\d+)")):
            match = re.fullmatch(pattern, _text(case.get(field)))
            if match:
                biggest = max(biggest, int(match.group(1)))
    return biggest + 1


def _decision_text(value: Any) -> str:
    """decision_key 的空壳（空串 / "[]" / "null"）不写进 note。"""
    text = _text(value)
    return "" if text.lower() in ("", "[]", "null") else text


def anchor_note(row: dict) -> str:
    """固定格式的一行溯源；真实动作缺失写「未记录」，不误标分歧。"""
    advice = normalize_action(row.get("option_kind"))
    actual_raw = _text(row.get("actual_action"))
    actual = actual_raw or "未记录"
    note = (
        f"锚点：advice_row={_text(row.get('id'))} "
        f"hand={_text(row.get('external_uid'))} "
        f"street={_text(row.get('street'))} "
        f"action_index={_int_or(row.get('action_index'), -1)} "
        f"version={_text(row.get('prompt_version')) or '（未知）'} "
        f"建议={advice}｜真实动作={actual}"
    )
    decision = _decision_text(row.get("decision_key"))
    if decision:
        note += f" decision={decision}"
    if actual_raw and normalize_action(actual_raw) != advice:
        note += "｜分歧"
    return note


def build_anchor_cases(selected_rows: Sequence[dict],
                       existing_cases: Sequence[dict] = ()) -> list[dict]:
    """源行 → ab_set case 条目；字段集合与现有 case 一致，只追加不改。"""
    existing = list(existing_cases)
    counters = {street: _next_seq(existing, street) - 1 for street in STREETS}
    cases: list[dict] = []
    for row in selected_rows:
        street = _text(row.get("street"))
        if street not in STREETS:
            raise ValueError(f"未知街：{street!r}")
        counters[street] += 1
        number = f"{counters[street]:02d}"
        case = {
            "id": f"C-{street}-{number}",
            "priority": ANCHOR_PRIORITY,
            "hand": _text(row.get("external_uid")),
            "street": street,
            "action_index": int(row["action_index"]),
            "category": f"anchor_{street}_{number}",
            "source": ANCHOR_SOURCE,
            "status": "ready",
            "expect": "",
            "new_advice": normalize_action(row.get("option_kind")),
            "note": anchor_note(row),
        }
        if set(case) != set(CASE_FIELDS):
            raise AssertionError(f"case 字段集合漂移：{sorted(case)}")
        cases.append(case)
    return cases


def plan_anchor_cases(rows: Sequence[dict], existing_cases: Sequence[dict],
                      **select_kwargs: Any) -> list[dict]:
    """选择 + 构造，一次完成；CLI 和测试都走这里。"""
    existing = list(existing_cases)
    selected = select_anchors(rows, existing, **select_kwargs)
    return build_anchor_cases(selected, existing)


def merge_ab_set(data: dict, new_cases: Sequence[dict],
                 *, updated: str | None = None) -> dict:
    """只追加 cases 尾部并改 updated；skipped 与其它顶层键原样保留。"""
    if not isinstance(data, dict) or data.get("schema") != "ab_set.v1":
        raise ValueError("不是 ab_set.v1：缺少 schema=ab_set.v1")
    existing = data.get("cases")
    if not isinstance(existing, list):
        raise ValueError("ab_set 缺少 cases 数组")
    merged = dict(data)
    merged["cases"] = list(existing) + list(new_cases)
    merged["updated"] = updated or date.today().isoformat()
    return merged


def load_rows(path: str | Path) -> list[dict]:
    """读 dump：支持 {"rows": [...]} 与裸数组两种形状。"""
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        rows = payload.get("rows")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError(f"{source} 不是建议 dump：缺少 rows 数组")
    return [row for row in rows if isinstance(row, dict)]


def load_rows_from_db() -> list[dict]:
    """走 replay-console 的 env 约定连 MySQL；连不上抛出可读错误。"""
    try:
        from replay.config import Settings
        from replay.db import Database
        settings = Settings.from_env()
    except Exception as exc:  # noqa: BLE001 - CLI 要把原因完整带出去
        raise RuntimeError(f"无法初始化 MySQL 配置：{exc}") from exc

    try:
        db = Database(settings)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法初始化 MySQL 连接：{exc}") from exc
    try:
        return db.query(_DB_SQL)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"连不上 MySQL {settings.db_host}:{settings.db_port}/"
            f"{settings.db_name}：{exc}；可改用 --source <dump.json> 离线运行"
        ) from exc
    finally:
        db.close()


def write_ab_set(data: dict, path: str | Path) -> None:
    """按现有 ab_set 的 1 空格缩进写回，避免无意义 diff。"""
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    Path(path).write_text(text, encoding="utf-8")


def _report(existing: Sequence[dict], cases: Sequence[dict],
            merged: dict) -> list[str]:
    old = _street_counts(existing)
    new = Counter(case["street"] for case in cases)
    plan = case_seed.plan_seed({"cases": merged["cases"]})
    plan_counts = Counter(
        case["street"] for case in plan["cases"] if case["street"] in STREETS)
    lines = [f"新增 {len(cases)} 条；ab_set 总数 {len(merged['cases'])}"]
    for street in STREETS:
        lines.append(
            f"  {street}: 现有 {old[street]} + 新增 {new[street]} "
            f"= {old[street] + new[street]}（plan cases {plan_counts[street]}）")
    lines.append(
        f"plan_seed：specs={len(plan['specs'])} cases={len(plan['cases'])} "
        f"skipped={len(plan['skipped'])}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从真实建议记录派生回归锚点并追加进 ab_set.json")
    parser.add_argument("--source", default=None,
                        help="建议 dump JSON（{\"rows\": [...]} 或数组）；"
                             "不给则连 MySQL")
    parser.add_argument("--out", default=str(AB_SET_PATH),
                        help="ab_set.json 路径（默认仓库内）")
    parser.add_argument("--per-street", type=int, default=DEFAULT_PER_STREET)
    parser.add_argument("--surplus", type=int, default=DEFAULT_SURPLUS,
                        help="每条街在 per-street 之上的富余")
    parser.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL,
                        help="现有 + 新增的 ab_set 总数下限")
    parser.add_argument("--updated", default=None,
                        help="写回 updated 的日期（默认今天）")
    parser.add_argument("--dry-run", action="store_true", help="只算统计，不写回")
    args = parser.parse_args(argv)

    try:
        data = case_seed.load_ab_set(args.out)
        rows = (load_rows(args.source) if args.source
                else load_rows_from_db())
        existing = list(data["cases"])
        new_cases = plan_anchor_cases(
            rows, existing, per_street=args.per_street,
            surplus=args.surplus, min_total=args.min_total, streets=STREETS)
        merged = merge_ab_set(data, new_cases, updated=args.updated)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    for line in _report(existing, new_cases, merged):
        print(line)
    if args.dry_run:
        print("dry-run：未写回（去掉 --dry-run 才落盘）")
        return 0
    write_ab_set(merged, args.out)
    print(f"已写回 {args.out}（updated={merged['updated']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
