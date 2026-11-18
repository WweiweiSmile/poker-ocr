"""把 tools/ab_set.json 映射成 seed 案例集（#55 §10 步骤 3–4）。

纯逻辑，不碰数据库：ab_set 现有 22 条用例 → 22 条 spec + 13 条 case。
口径以设计文档正文为准：

- 每条 ab_set 用例都建一条 spec（name = category，bucket 只锁 street）；
- 只有 status == "ready" 且 expect != "n-a" 才建 case；
- 其余条目写进 skipped 并写明原因，绝不静默丢。

case 的 spec 在这里就写死 specName，落库时由服务层解析成 spec_id；
之后新增 spec 不会改变已有案例的引用（AC1）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import specs

REPO_ROOT = Path(__file__).resolve().parents[2]
AB_SET_PATH = REPO_ROOT / "tools" / "ab_set.json"

SEED_SET_NAME = "seed-2026-11"
PRIORITY_BY_LABEL = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# ab_set 的 new_advice 是给人看的写法（all-in / raise-1/3），spec 只认词表。
_ADVICE_ALIASES = {
    "all-in": "allin",
    "allin": "allin",
    "raise-1/2": "raise",
    "raise-1/3": "raise",
    "raise": "raise",
    "fold": "fold",
    "check": "check",
    "call": "call",
    "bet": "bet",
}

_EXPECT_LABELS = {
    "cross_down": "跨越向下",
    "cross_up": "跨越向上",
    "stay_above": "保持在上方",
    "stay_below": "保持在下方",
}


def load_ab_set(path: str | Path | None = None) -> dict:
    source = Path(path) if path else AB_SET_PATH
    with source.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError(f"{source} 不是 ab_set.v1：缺少 cases 数组")
    return data


def normalize_advice(value: Any) -> str | None:
    """ab_set 的建议词 → spec 的 action 词；空 / n/a → None。"""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    if not word or word == "n/a":
        return None
    return _ADVICE_ALIASES.get(word, word if word in specs.ACTIONS else None)


def priority_of(label: Any) -> int:
    return PRIORITY_BY_LABEL.get(str(label or "").strip().upper(), 1)


def _rationale(case: dict) -> str:
    parts: list[str] = []
    note = (case.get("note") or "").strip()
    if note:
        parts.append(note)
    old = (case.get("old_advice") or "").strip()
    new = (case.get("new_advice") or "").strip()
    if old and new:
        parts.append(f"ab_set 对照：{old} → {new}")
    expect = case.get("expect")
    if expect in _EXPECT_LABELS:
        parts.append(f"期望：{_EXPECT_LABELS[expect]}")
    return "；".join(parts)


def _spec_content(case: dict) -> dict:
    street = (case.get("street") or "").strip()
    advice = normalize_advice(case.get("new_advice"))
    return specs.validate_spec({
        "schema": specs.SPEC_SCHEMA,
        "bucket": {"street": [street]} if street else {},
        "allowed_actions": [advice] if advice else [],
        "forbidden_actions": [],
        "sizing": None,
        "freq": None,
        "required_points": [],
        "forbidden_points": [],
        "rationale": _rationale(case),
    })


def _skip_reason(case: dict) -> str | None:
    status = (case.get("status") or "").strip()
    expect = (case.get("expect") or "").strip()
    reasons: list[str] = []
    if status != "ready":
        reasons.append(f"status={status or 'missing'}")
    if expect in ("n-a", "n/a"):
        reasons.append("expect=n-a")
    if reasons:
        return "，".join(reasons) + "，只建 spec 不建 case"
    if case.get("action_index") is None:
        return "action_index 缺失，无法定位决策点"
    return None


def plan_seed(data: dict) -> dict:
    """ab_set → {setName, specs, cases, skipped}；同输入同输出（可重复跑）。"""
    seen: set[str] = set()
    plan_specs: list[dict] = []
    plan_cases: list[dict] = []
    skipped: list[dict] = []

    for case in data.get("cases") or []:
        seed_id = str(case.get("id") or "").strip()
        category = str(case.get("category") or "").strip()
        name = category or seed_id
        if name in seen:
            # category 撞名时带上 seed id，绝不把两条 spec 静默合并。
            name = f"{name}-{seed_id}"
        seen.add(name)

        plan_specs.append({
            "name": name,
            "status": "active",
            "note": f"ab_set:{seed_id}",
            "content": _spec_content(case),
        })

        reason = _skip_reason(case)
        if reason:
            skipped.append({"seedId": seed_id, "reason": reason})
            continue

        plan_cases.append({
            "seedId": seed_id,
            "handUid": str(case.get("hand") or "").strip(),
            "street": (case.get("street") or "").strip(),
            "actionIndex": int(case["action_index"]),
            "priority": priority_of(case.get("priority")),
            "expect": case.get("expect"),
            "specName": name,
            "note": f"ab_set:{seed_id} {category}".strip(),
        })

    return {
        "setName": SEED_SET_NAME,
        "specs": plan_specs,
        "cases": plan_cases,
        "skipped": skipped,
    }
