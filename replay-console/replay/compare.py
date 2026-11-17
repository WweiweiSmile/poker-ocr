"""版本对照（#60）：两次评分运行按共同决策点配对后的翻转表。

第一性原理（与设计 §9 / §11 第 4 期对齐）：

- **对比的单位是决策点，不是案例行。** 案例集升 revision 是整集复制、每行
  生成新 `case_id`，跨 revision 时 case_id 完全不重叠；只有
  `(canonical_id, decision_key)` 是稳定的身份。按 case_id 配对会把「两个
  版本」算成「零个共同案例」——那是最坏的一种静默错误。
- **只比共同案例，但排除数必须显式报出来。** 交集外的案例不进表、不进均分；
  只比交集是口径，藏掉被排除数才是问题。
- **方向先看硬失败，再看有效分。** 硬失败把分数封顶 40：38 → 40 的「涨分」
  其实是多了一条 `HF_OPTION`，纯比分数会判成「变好」。反过来硬失败消失时
  分数可能只涨一点，纯比分数会判成「不变」。两种都会把最该看的翻转藏掉。
- **不可比单列，不冒充不变。** 某侧 `source=error` 或没有分数时记
  `incomparable`：把「没跑出来」算成「没变」会让版本对照看起来比实际好。
- **口径变化必须提示。** 案例集 revision、spec revision/集合、judge 版本、
  reconstruct 版本任何一项不同，都写进 `version_diff` → `warnings`，页面与
  导出的 CSV 都要带着它：`spec` 期望变了以后，「改好/改坏」里混着期望变化。

本模块是纯函数，不碰数据库：服务层把 run_items、冻结快照、人工改判拼成
输入，这里只做配对与判定。阈值也不在这里另立：分数口径来自 `run_items.score`
（已叠加人工改判后的有效分），分差容差见 `COMPARE_EPS`。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import scoring

# 展示顺序：先看「改坏」——版本对照最该回答的问题是「有没有弄坏」。
DIRECTIONS = ("worse", "improved", "unchanged", "incomparable")

DIRECTION_LABELS = {
    "worse": "改坏",
    "improved": "改好",
    "unchanged": "不变",
    "incomparable": "不可比",
}

# 分差容差：分数是 0–100 的两位小数，0.005 以内视为没动（与页面展示精度一致）。
COMPARE_EPS = 0.005


def comparison_key(item: Mapping[str, Any]) -> tuple[int, str]:
    """决策点身份：跨案例集 revision 稳定；没有 canonical 时退回 case_id。"""
    canonical_id = item.get("canonicalId")
    if canonical_id is None:
        return (0, f"case:{item.get('caseId')}")
    return (int(canonical_id), str(item.get("decisionKey") or ""))


def flips(left: Mapping[str, Any], right: Mapping[str, Any],
          eps: float = COMPARE_EPS) -> str:
    """一次翻转的方向：left = 基准运行，right = 对照运行。

    入参只需 `comparable` / `score` / `hardFail` 三个字段（见
    `side_payload`），这样纯逻辑测试与页面展示用的是同一口径。
    """
    if not left.get("comparable") or not right.get("comparable"):
        return "incomparable"
    left_fail = bool(left.get("hardFail"))
    right_fail = bool(right.get("hardFail"))
    if left_fail != right_fail:
        return "worse" if right_fail else "improved"
    left_score = left.get("score")
    right_score = right.get("score")
    if left_score is None or right_score is None:
        return "incomparable"
    delta = float(right_score) - float(left_score)
    if delta > eps:
        return "improved"
    if delta < -eps:
        return "worse"
    return "unchanged"


def pair(left_items: Iterable[Mapping[str, Any]],
         right_items: Iterable[Mapping[str, Any]]
         ) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any]]], dict]:
    """按决策点身份取交集；返回按身份排序的配对与被排除数。"""
    left_by_key = {comparison_key(item): item for item in left_items}
    right_by_key = {comparison_key(item): item for item in right_items}
    pairs = [(left_by_key[key], right_by_key[key])
             for key in sorted(set(left_by_key) & set(right_by_key))]
    return pairs, {
        "left": len(left_by_key),
        "right": len(right_by_key),
        "common": len(pairs),
        "excludedLeft": len(left_by_key) - len(pairs),
        "excludedRight": len(right_by_key) - len(pairs),
    }


def side_payload(item: Mapping[str, Any]) -> dict:
    """一侧的展示载荷：有效分、硬失败、实际建议与 spec 口径。"""
    return {
        "caseId": int(item.get("caseId") or 0),
        "runId": item.get("runId"),
        "specId": item.get("specId"),
        "specName": item.get("specName") or "",
        "specRevision": item.get("specRevision"),
        "score": item.get("score"),
        "scores": item.get("scores") or {},
        "hardFail": item.get("hardFail") or None,
        "overridden": bool(item.get("overridden")),
        "advice": item.get("advice") or {},
        "source": item.get("source"),
        "comparable": bool(item.get("comparable", True)),
    }


def compare_row(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict:
    """把一对共同案例折成翻转表的一行；桶与案例摘要取基准运行。"""
    direction = flips(left, right)
    delta = None
    if (direction != "incomparable"
            and left.get("score") is not None
            and right.get("score") is not None):
        delta = round(float(right["score"]) - float(left["score"]), 2)
    return {
        "canonicalId": left.get("canonicalId"),
        "decisionKey": left.get("decisionKey"),
        "street": left.get("street"),
        "actionIndex": left.get("actionIndex"),
        "priority": int(left.get("priority") or 0),
        "p0": bool(left.get("p0")),
        "bucket": left.get("bucket"),
        "bucketLabel": left.get("bucketLabel") or "",
        "hand": left.get("hand") or {},
        "left": side_payload(left),
        "right": side_payload(right),
        "delta": delta,
        "direction": direction,
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict:
    """按共同案例汇总：方向计数 + 两侧均分（不可比的两侧都不进均分）。"""
    directions = {direction: 0 for direction in DIRECTIONS}
    for row in rows:
        directions[row["direction"]] = directions.get(row["direction"], 0) + 1
    scored = [row for row in rows if row["direction"] != "incomparable"]
    left = scoring.mean_ci([row["left"]["score"] for row in scored])
    right = scoring.mean_ci([row["right"]["score"] for row in scored])
    delta = None
    if left["mean"] is not None and right["mean"] is not None:
        delta = round(float(right["mean"]) - float(left["mean"]), 2)
    return {
        "n": len(rows),
        "scored": len(scored),
        "directions": directions,
        "left": left,
        "right": right,
        "delta": delta,
    }


def _spec_map(run: Mapping[str, Any]) -> dict[int, dict]:
    config = run.get("config") or {}
    specs: dict[int, dict] = {}
    for snapshot in config.get("specs") or []:
        try:
            spec_id = int(snapshot["id"])
        except (KeyError, TypeError, ValueError):
            continue
        specs[spec_id] = {
            "id": spec_id,
            "name": snapshot.get("name") or "",
            "revision": snapshot.get("revision"),
        }
    return specs


def version_diff(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict:
    """两侧口径的逐项对照；`crossVersion` 只表示「口径变了」。

    被测版本（`version`）不同是版本对照的目的，不算口径变化；案例集、
    spec、judge、reconstruct 任何一项不同，才是「分数不可直接横比」。
    """
    left_set = left.get("caseSet") or {}
    right_set = right.get("caseSet") or {}
    left_specs, right_specs = _spec_map(left), _spec_map(right)
    changed: list[dict] = []
    only_left: list[dict] = []
    only_right: list[dict] = []
    for spec_id in sorted(set(left_specs) | set(right_specs)):
        left_spec = left_specs.get(spec_id)
        right_spec = right_specs.get(spec_id)
        if left_spec and right_spec:
            if left_spec.get("revision") != right_spec.get("revision"):
                changed.append({
                    "id": spec_id,
                    "name": right_spec.get("name") or left_spec.get("name")
                    or "",
                    "left": left_spec.get("revision"),
                    "right": right_spec.get("revision"),
                })
        elif left_spec:
            only_left.append(left_spec)
        else:
            only_right.append(right_spec)

    case_same = (left_set.get("id") == right_set.get("id")
                 and left_set.get("revision") == right_set.get("revision"))
    specs_same = not changed and not only_left and not only_right
    judge_same = left.get("judgeVersion") == right.get("judgeVersion")
    reconstruct_same = (left.get("reconstructVersion")
                        == right.get("reconstructVersion"))
    return {
        "caseSet": {
            "same": case_same,
            "left": {"id": left_set.get("id"), "name": left_set.get("name"),
                     "revision": left_set.get("revision")},
            "right": {"id": right_set.get("id"),
                      "name": right_set.get("name"),
                      "revision": right_set.get("revision")},
        },
        "specs": {"same": specs_same, "changed": changed,
                  "onlyLeft": only_left, "onlyRight": only_right},
        "judge": {"same": judge_same, "left": left.get("judgeVersion"),
                  "right": right.get("judgeVersion")},
        "reconstruct": {"same": reconstruct_same,
                        "left": left.get("reconstructVersion"),
                        "right": right.get("reconstructVersion")},
        "version": {"same": (left.get("version") or {}).get("id")
                    == (right.get("version") or {}).get("id"),
                    "left": left.get("version") or {},
                    "right": right.get("version") or {}},
        "crossVersion": not (case_same and specs_same and judge_same
                             and reconstruct_same),
    }


def warnings(diff: Mapping[str, Any],
             left: Mapping[str, Any] | None = None,
             right: Mapping[str, Any] | None = None) -> list[dict]:
    """把口径差异翻成页面/导出必须展示的告警（按严重度排序）。"""
    items: list[dict] = []
    case_set = diff["caseSet"]
    if not case_set["same"]:
        before, after = case_set["left"], case_set["right"]
        items.append({
            "code": "CASE_SET_CHANGED",
            "message": (f"案例集不同（#{before['id']} rev{before['revision']} "
                        f"→ #{after['id']} rev{after['revision']}）："
                        "只比两边都跑过的决策点，其余不计入"),
        })
    specs = diff["specs"]
    if not specs["same"]:
        bits: list[str] = []
        if specs["changed"]:
            bits.append(f"{len(specs['changed'])} 个 spec 升了 revision")
        if specs["onlyLeft"]:
            bits.append(f"{len(specs['onlyLeft'])} 个只在基准运行")
        if specs["onlyRight"]:
            bits.append(f"{len(specs['onlyRight'])} 个只在对照运行")
        items.append({
            "code": "SPEC_CHANGED",
            "message": ("spec 期望变了（" + "、".join(bits) + "）："
                        "分数里混着期望变化，只能比共同案例、不能当能力对比"),
        })
    if not diff["judge"]["same"]:
        items.append({
            "code": "JUDGE_CHANGED",
            "message": (f"判定版本不同（{diff['judge']['left']} → "
                        f"{diff['judge']['right']}）：判据口径变了，分差不能"
                        "全算在模型头上"),
        })
    if not diff["reconstruct"]["same"]:
        items.append({
            "code": "RECONSTRUCT_CHANGED",
            "message": (f"重建版本不同（{diff['reconstruct']['left']} → "
                        f"{diff['reconstruct']['right']}）：决策点与事实可能"
                        "对不上，只能看共同案例"),
        })
    for label, run in (("基准运行", left), ("对照运行", right)):
        if not run:
            continue
        status = run.get("status")
        if status != "succeeded":
            items.append({
                "code": "RUN_NOT_FINISHED",
                "message": (f"{label} #{run.get('id')} 状态 {status}："
                            "对比只覆盖它已完成的部分"),
            })
    return items


def direction_rank(direction: str) -> int:
    try:
        return DIRECTIONS.index(direction)
    except ValueError:
        return len(DIRECTIONS)
