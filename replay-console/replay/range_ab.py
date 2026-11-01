"""范围模型 A/B 只读视图。

读父项目的 `tools/ab_set.json`（清单）和可选的 `tools/ab_results.json`
（`tools/ab_range_set.py --json` 的输出），合并成控制台「范围 A/B」页的数据。

只读：不碰 DB、不调模型、不写文件。两个路径都可以用环境变量覆盖：

    REPLAY_RANGE_AB_SET=/path/to/ab_set.json
    REPLAY_RANGE_AB_RESULTS=/path/to/ab_results.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

# replay-console/replay/range_ab.py -> 仓库根
ROOT = Path(__file__).resolve().parents[2]
SET_PATH = ROOT / "tools" / "ab_set.json"
RESULT_PATH = ROOT / "tools" / "ab_results.json"
OUTBOX = ROOT / "data" / "hand_outbox" / "pending"

_STATUS_ORDER = ("ready", "blocked_cap", "blocked_data", "data_broken",
                 "manual", "needs_reconstruction")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(
            timespec="seconds")
    except OSError:
        return None


def _rows(raw) -> list[dict]:
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        rows = raw.get("rows") or raw.get("cases") or []
        return [r for r in rows if isinstance(r, dict)]
    return []


def _hand_detail(hand_id: str) -> dict | None:
    """手牌的行动线 + 线上原建议；只读 outbox，缺失时返回 None。"""
    raw = _read_json(OUTBOX / f"{hand_id}.json")
    if not isinstance(raw, dict):
        return None
    streets = []
    for rec in raw.get("streets") or []:
        actions = []
        for action in rec.get("actions") or []:
            actions.append({
                "position": action.get("position"),
                "action": action.get("action"),
                "amount": action.get("amount"),
                "allIn": bool(action.get("all_in")),
                "inferred": bool(action.get("inferred")),
                "isHero": bool(action.get("is_hero")),
            })
        streets.append({
            "street": rec.get("street"),
            "potStart": rec.get("pot_start"),
            "actions": actions,
        })
    advices = []
    for advice in raw.get("advices") or []:
        option = advice.get("option") or {}
        amount = option.get("amount_bb")
        if amount is None:
            amount = option.get("amount")
        advices.append({
            "street": advice.get("street"),
            "actionIndex": advice.get("action_index"),
            "optionId": option.get("id"),
            "optionLabel": option.get("label") or option.get("id"),
            "amount": amount,
            "reason": advice.get("reason") or "",
            "pot": advice.get("pot"),
            "toCall": advice.get("to_call"),
            "model": advice.get("model"),
            "promptVersion": advice.get("prompt_version"),
        })
    return {
        "heroCards": " ".join(raw.get("hero_cards") or []),
        "board": " ".join(raw.get("board") or []),
        "heroPosition": raw.get("hero_position"),
        "pot": raw.get("pot"),
        "bombPot": bool(raw.get("bomb_pot")),
        "streets": streets,
        "advices": advices,
    }


def _summary(cases: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_priority: dict[str, dict[str, int]] = {}
    passed = failed = skipped = missing = 0
    for case in cases:
        status = str(case.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        priority = str(case.get("priority") or "—")
        bucket = by_priority.setdefault(priority, {"total": 0, "pass": 0,
                                                   "fail": 0, "skip": 0})
        bucket["total"] += 1
        result = (case.get("result") or {}).get("result")
        if result == "PASS":
            passed += 1
            bucket["pass"] += 1
        elif result == "FAIL":
            failed += 1
            bucket["fail"] += 1
        elif result == "SKIP":
            skipped += 1
            bucket["skip"] += 1
        elif result in ("MISSING", "MISSING_ADVICE", "NO_RANGE"):
            missing += 1
            bucket["skip"] += 1
    return {
        "total": len(cases),
        "ready": by_status.get("ready", 0),
        "blocked": len(cases) - by_status.get("ready", 0),
        "pass": passed,
        "fail": failed,
        "skip": skipped,
        "missing": missing,
        "byStatus": by_status,
        "byPriority": by_priority,
    }


def load() -> dict:
    set_path = Path(os.environ.get("REPLAY_RANGE_AB_SET") or SET_PATH)
    manifest = _read_json(set_path)
    if not isinstance(manifest, dict):
        return {
            "available": False,
            "error": f"找不到或无法解析 {set_path}",
            "hint": "确认父项目 tools/ab_set.json 存在，或用 "
                    "REPLAY_RANGE_AB_SET 指定路径。",
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
        }

    result_path = Path(os.environ.get("REPLAY_RANGE_AB_RESULTS")
                       or RESULT_PATH)
    raw = _read_json(result_path)
    rows = _rows(raw)
    by_id = {str(r.get("id")): r for r in rows if r.get("id") is not None}

    cases: list[dict] = []
    for case in manifest.get("cases") or []:
        if not isinstance(case, dict):
            continue
        merged = dict(case)
        result = by_id.get(str(case.get("id")))
        merged["handDetail"] = _hand_detail(str(case.get("hand")))
        if result:
            merged["result"] = {
                "result": result.get("result"),
                "old": result.get("old"),
                "new": result.get("new"),
                "threshold": result.get("threshold"),
                "problems": result.get("problems") or [],
                "adviceDiff": result.get("advice_diff"),
                "model": result.get("model"),
            }
        cases.append(merged)

    return {
        "available": True,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "setPath": str(set_path),
        "resultsPath": str(result_path),
        "resultsAvailable": bool(rows),
        "resultsUpdatedAt": _mtime(result_path) if rows else None,
        "summary": _summary(cases),
        "cases": cases,
        "skipped": manifest.get("skipped") or [],
        "run": manifest.get("run") or "",
        "baseline": manifest.get("baseline") or "",
        "candidate": manifest.get("candidate") or "",
        "statusOrder": list(_STATUS_ORDER),
    }
