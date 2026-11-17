"""场景期望（spot spec）：§4.1 的词表、校验、revision diff 与展示。

这个模块只做纯逻辑，不碰数据库：

  · `validate_spec()` 把 API 传来的 §4.1 内容校验并规范化；非法维度键、
    非法单位、`min_n` 之类的门槛字段一律 **保存时报错**，不静默丢弃。
  · `diff_specs()` 比较两个 revision，产出人能直接看的变更清单。
  · `bucket_label()` / `meta()` 给列表分组和前端表单提供词表。

存储形状见设计文档 §3.2：`replay_spot_specs.bucket_json` 只放桶维度，
`spec_json` 放 §4.1 的其余字段（含 `schema`）。旧 revision 只读；编辑 =
插入新 revision（见 `replay/service.py` 的 spec_* 方法）。
"""

from __future__ import annotations

import math
from typing import Any

# 桶维度与词表的唯一定义在 `poker_ocr/buckets.py`（票 50）。推导（决策点 →
# 桶）和展示（桶 → 中文）必须同源：否则页面分组用的桶和回填进
# replay_decisions 的桶会变成两套口径，而 A/B 结论正是按桶聚合出来的。
# 这里只做 re-export，供 §4.1 的校验、diff 和前端表单使用。
from poker_ocr.buckets import (  # noqa: E402
    BUCKET_ORDER, BUCKET_VOCAB, BOARD_TEXTURES, FACING, HAND_TIERS,
    OPPONENT_PROFILES, POSITION_BUCKETS, POT_TYPES, STREETS, bucket_label,
)

from .errors import ApiError
from .util import json_load

SPEC_SCHEMA = "spot_spec.v1"
SPEC_STATUSES = ("draft", "active")
SPEC_STATUS_LABELS = {"draft": "草稿", "active": "生效"}

# 与 `poker_ocr/options.py` 的 Option.kind 同一套动作词表。不是桶维度，
# 所以留在 spec 这边。
ACTIONS = {
    "fold": "弃牌", "check": "过牌", "call": "跟注",
    "bet": "下注", "raise": "加注", "allin": "全下",
}
SIZING_UNITS = {"pot_fraction": "底池倍数", "bb": "BB"}

_SPEC_KEYS = {
    "schema", "bucket", "allowed_actions", "forbidden_actions", "sizing",
    "freq", "required_points", "forbidden_points", "rationale",
}
_BAND_KEYS = {"unit", "min", "max"}
_FREQ_KEYS = {"action", "min", "max"}
# 这些字段曾经出现在旧讨论里；它们属于代码常量，写进 spec 必须报错。
_FORBIDDEN_THRESHOLD_KEYS = {"min_n", "minN", "freq_min_n", "FREQ_MIN_N"}
_MAX_NAME = 120
_MAX_NOTE = 2000
_MAX_RATIONALE = 4000
_MAX_POINT_TEXT = 500
_MAX_POINT_KEY = 64


def _err(code: str, message: str) -> ApiError:
    return ApiError(code, message, 422)


def _label(vocab: dict, value: Any) -> str:
    return str(vocab.get(value, value))


def _as_list(value: Any, field: str, code: str) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _err(code, f"{field} 必须是数组")
    return list(value)


def _validate_bucket(raw: Any) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _err("INVALID_SPEC_BUCKET", "bucket 必须是对象（维度 → 取值数组）")
    unknown = [key for key in raw if key not in BUCKET_VOCAB]
    if unknown:
        raise _err(
            "INVALID_SPEC_DIMENSION",
            f"未知桶维度 {', '.join(map(str, unknown))}；允许的维度："
            f"{', '.join(BUCKET_ORDER)}")
    out: dict[str, list] = {}
    for dimension in BUCKET_ORDER:
        if dimension not in raw:
            continue
        values = _as_list(raw[dimension], f"bucket.{dimension}",
                          "INVALID_SPEC_BUCKET")
        allowed = BUCKET_VOCAB[dimension][1]
        cleaned: list = []
        for value in values:
            if dimension == "facing":
                if isinstance(value, bool) or value not in (0, 1):
                    raise _err(
                        "INVALID_SPEC_VALUE",
                        f"bucket.facing 只接受 0（未面对下注）或 1（面对下注），"
                        f"收到 {value!r}")
                value = int(value)
            else:
                if not isinstance(value, str) or value not in allowed:
                    raise _err(
                        "INVALID_SPEC_VALUE",
                        f"bucket.{dimension} 的取值 {value!r} 不在词表内；"
                        f"允许：{', '.join(map(str, allowed))}")
            if value not in cleaned:
                cleaned.append(value)
        if cleaned:
            out[dimension] = cleaned
    return out


def _validate_actions(raw: Any, field: str) -> list[str]:
    values = _as_list(raw, field, "INVALID_SPEC_ACTION")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise _err("INVALID_SPEC_ACTION", f"{field} 只接受动作字符串")
        value = value.strip()
        if value not in ACTIONS:
            raise _err(
                "INVALID_SPEC_ACTION",
                f"{field} 含未知动作 {value!r}；允许："
                f"{', '.join(ACTIONS)}")
        if value not in out:
            out.append(value)
    return [action for action in ACTIONS if action in out]


def _number(value: Any, field: str, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(code, f"{field} 必须是数字")
    number = float(value)
    if not math.isfinite(number):
        raise _err(code, f"{field} 必须是有限数字")
    return round(number, 4)


def _band(raw: Any, field: str) -> dict:
    if not isinstance(raw, dict):
        raise _err("INVALID_SPEC_SIZING", f"{field} 必须是对象")
    unknown = [key for key in raw if key not in _BAND_KEYS]
    if unknown:
        raise _err(
            "INVALID_SPEC_SIZING",
            f"{field} 含未知字段 {', '.join(map(str, unknown))}；"
            f"只允许 unit / min / max")
    unit = raw.get("unit")
    if unit not in SIZING_UNITS:
        raise _err(
            "INVALID_SPEC_SIZING",
            f"{field}.unit 非法：{unit!r}；只允许 "
            f"{' / '.join(SIZING_UNITS)}")
    minimum = _number(raw.get("min"), f"{field}.min", "INVALID_SPEC_SIZING")
    maximum = _number(raw.get("max"), f"{field}.max", "INVALID_SPEC_SIZING")
    if minimum < 0:
        raise _err("INVALID_SPEC_SIZING", f"{field}.min 不能为负数")
    if maximum < minimum:
        raise _err("INVALID_SPEC_SIZING",
                   f"{field}.max 不能小于 min（{minimum} > {maximum}）")
    return {"unit": unit, "min": minimum, "max": maximum}


def _validate_sizing(raw: Any) -> dict | list[dict] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return _band(raw, "sizing")
    if isinstance(raw, list):
        if not raw:
            return None
        return [_band(band, f"sizing[{index}]")
                for index, band in enumerate(raw)]
    raise _err("INVALID_SPEC_SIZING",
               "sizing 必须是对象或对象数组（多条带同时约束）")


def _validate_freq(raw: Any) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _err("INVALID_SPEC_FREQ", "freq 必须是对象")
    unknown = [key for key in raw if key not in _FREQ_KEYS]
    if unknown:
        if any(key in _FORBIDDEN_THRESHOLD_KEYS for key in unknown):
            raise _err(
                "INVALID_SPEC_FREQ",
                "min_n / FREQ_MIN_N 是代码常量（FREQ_MIN_N=15），"
                "不写进 spec、也不随 run 覆盖")
        raise _err(
            "INVALID_SPEC_FREQ",
            f"freq 含未知字段 {', '.join(map(str, unknown))}；"
            f"只允许 action / min / max")
    action = raw.get("action")
    if action not in ACTIONS:
        raise _err("INVALID_SPEC_FREQ",
                   f"freq.action 非法：{action!r}；允许：{', '.join(ACTIONS)}")
    minimum = _number(raw.get("min"), "freq.min", "INVALID_SPEC_FREQ")
    maximum = _number(raw.get("max"), "freq.max", "INVALID_SPEC_FREQ")
    if not 0 <= minimum <= 1 or not 0 <= maximum <= 1:
        raise _err("INVALID_SPEC_FREQ", "freq.min / freq.max 必须在 0 到 1 之间")
    if maximum < minimum:
        raise _err("INVALID_SPEC_FREQ",
                   f"freq.max 不能小于 min（{minimum} > {maximum}）")
    return {"action": action, "min": minimum, "max": maximum}


def _validate_points(raw: Any, field: str, *, with_weight: bool) -> list[dict]:
    values = _as_list(raw, field, "INVALID_SPEC_POINT")
    allowed_keys = {"key", "text", "weight"} if with_weight else {"key", "text"}
    out: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise _err("INVALID_SPEC_POINT", f"{field}[{index}] 必须是对象")
        unknown = [key for key in item if key not in allowed_keys]
        if unknown:
            raise _err(
                "INVALID_SPEC_POINT",
                f"{field}[{index}] 含未知字段 {', '.join(map(str, unknown))}；"
                f"只允许 {' / '.join(sorted(allowed_keys))}")
        key = item.get("key")
        text = item.get("text")
        if not isinstance(key, str) or not key.strip():
            raise _err("INVALID_SPEC_POINT", f"{field}[{index}].key 不能为空")
        if not isinstance(text, str) or not text.strip():
            raise _err("INVALID_SPEC_POINT", f"{field}[{index}].text 不能为空")
        key, text = key.strip(), text.strip()
        if len(key) > _MAX_POINT_KEY:
            raise _err("INVALID_SPEC_POINT",
                       f"{field}[{index}].key 最长 {_MAX_POINT_KEY} 字符")
        if len(text) > _MAX_POINT_TEXT:
            raise _err("INVALID_SPEC_POINT",
                       f"{field}[{index}].text 最长 {_MAX_POINT_TEXT} 字符")
        if key in seen:
            raise _err("INVALID_SPEC_POINT",
                       f"{field} 里要点 key 重复：{key}")
        seen.add(key)
        point = {"key": key, "text": text}
        if with_weight:
            weight = item.get("weight", 1)
            if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 9:
                raise _err("INVALID_SPEC_POINT",
                           f"{field}[{index}].weight 必须是 1–9 的整数")
            point["weight"] = weight
        out.append(point)
    return out


def validate_spec(payload: Any) -> dict:
    """校验并规范化 §4.1 的 spec 内容；非法输入抛 ApiError(422)。"""
    if not isinstance(payload, dict):
        raise _err("INVALID_SPEC", "spec 必须是对象")
    unknown = [key for key in payload if key not in _SPEC_KEYS]
    if unknown:
        raise _err("INVALID_SPEC",
                   f"spec 含未知字段 {', '.join(map(str, unknown))}；"
                   f"只允许 {', '.join(sorted(_SPEC_KEYS))}")
    schema = payload.get("schema")
    if schema not in (None, SPEC_SCHEMA):
        raise _err("INVALID_SPEC_SCHEMA",
                   f"不支持的 spec schema：{schema!r}；只支持 {SPEC_SCHEMA}")

    allowed = _validate_actions(payload.get("allowed_actions"),
                                "allowed_actions")
    forbidden = _validate_actions(payload.get("forbidden_actions"),
                                  "forbidden_actions")
    overlap = [action for action in allowed if action in forbidden]
    if overlap:
        raise _err("INVALID_SPEC_ACTION",
                   f"动作不能同时出现在 allowed 与 forbidden："
                   f"{', '.join(overlap)}")

    rationale = payload.get("rationale")
    if rationale is None:
        rationale = ""
    if not isinstance(rationale, str):
        raise _err("INVALID_SPEC", "rationale 必须是字符串")
    rationale = rationale.strip()
    if len(rationale) > _MAX_RATIONALE:
        raise _err("INVALID_SPEC",
                   f"rationale 最长 {_MAX_RATIONALE} 字符")

    return {
        "schema": SPEC_SCHEMA,
        "bucket": _validate_bucket(payload.get("bucket")),
        "allowed_actions": allowed,
        "forbidden_actions": forbidden,
        "sizing": _validate_sizing(payload.get("sizing")),
        "freq": _validate_freq(payload.get("freq")),
        "required_points": _validate_points(
            payload.get("required_points"), "required_points", with_weight=True),
        "forbidden_points": _validate_points(
            payload.get("forbidden_points"), "forbidden_points",
            with_weight=False),
        "rationale": rationale,
    }


def validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _err("INVALID_SPEC", "name 不能为空")
    name = value.strip()
    if len(name) > _MAX_NAME:
        raise _err("INVALID_SPEC", f"name 最长 {_MAX_NAME} 字符")
    return name


def validate_note(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _err("INVALID_SPEC", "note 必须是字符串")
    note = value.strip()
    if len(note) > _MAX_NOTE:
        raise _err("INVALID_SPEC", f"note 最长 {_MAX_NOTE} 字符")
    return note


def validate_status(value: Any) -> str:
    if value not in SPEC_STATUSES:
        raise _err("INVALID_SPEC_STATUS",
                   f"status 只能是 {' / '.join(SPEC_STATUSES)}")
    return value


def _bands(sizing: Any) -> list[dict]:
    if sizing is None:
        return []
    if isinstance(sizing, dict):
        return [sizing]
    return list(sizing)


def band_label(band: dict) -> str:
    unit = _label(SIZING_UNITS, band.get("unit"))
    return f"{unit} {band.get('min'):g}–{band.get('max'):g}"


def _freq_label(freq: dict | None) -> str:
    if not freq:
        return "无"
    return (f"{_label(ACTIONS, freq.get('action'))} "
            f"{float(freq.get('min', 0)) * 100:g}%–"
            f"{float(freq.get('max', 0)) * 100:g}%")


def diff_specs(old: dict, new: dict) -> list[dict]:
    """比较两个规范化 spec，返回按固定顺序排列的变更项。

    每项都带 `section`（bucket/actions/sizing/freq/points/rationale）、
    `kind`（added/removed/changed）和可直接展示的 `label`。
    """
    changes: list[dict] = []
    old_bucket, new_bucket = old.get("bucket") or {}, new.get("bucket") or {}
    for dimension in BUCKET_ORDER:
        before = list(old_bucket.get(dimension) or [])
        after = list(new_bucket.get(dimension) or [])
        if before == after:
            continue
        kind = "changed" if before and after else ("added" if after else "removed")
        changes.append({
            "section": "bucket",
            "field": dimension,
            "label": BUCKET_VOCAB[dimension][0],
            "kind": kind,
            "before": before,
            "after": after,
        })

    for field, label in (("allowed_actions", "允许动作"),
                         ("forbidden_actions", "禁止动作")):
        before = list(old.get(field) or [])
        after = list(new.get(field) or [])
        for value in before:
            if value not in after:
                changes.append({
                    "section": "actions", "field": field, "key": value,
                    "label": label, "kind": "removed", "value": value,
                })
        for value in after:
            if value not in before:
                changes.append({
                    "section": "actions", "field": field, "key": value,
                    "label": label, "kind": "added", "value": value,
                })

    old_bands, new_bands = _bands(old.get("sizing")), _bands(new.get("sizing"))
    for band in old_bands:
        if band not in new_bands:
            changes.append({
                "section": "sizing", "kind": "removed",
                "label": band_label(band), "before": band, "after": None,
            })
    for band in new_bands:
        if band not in old_bands:
            changes.append({
                "section": "sizing", "kind": "added",
                "label": band_label(band), "before": None, "after": band,
            })

    old_freq, new_freq = old.get("freq"), new.get("freq")
    if old_freq != new_freq:
        kind = "changed"
        if not old_freq:
            kind = "added"
        elif not new_freq:
            kind = "removed"
        changes.append({
            "section": "freq", "kind": kind,
            "label": f"频率带：{_freq_label(old_freq)} → {_freq_label(new_freq)}",
            "before": old_freq, "after": new_freq,
        })

    for field, label in (("required_points", "必需要点"),
                         ("forbidden_points", "禁止要点")):
        before = {point["key"]: point for point in old.get(field) or []}
        after = {point["key"]: point for point in new.get(field) or []}
        for key, point in before.items():
            if key not in after:
                changes.append({
                    "section": "points", "field": field, "key": key,
                    "label": label, "kind": "removed",
                    "before": point, "after": None,
                })
        for key, point in after.items():
            if key not in before:
                changes.append({
                    "section": "points", "field": field, "key": key,
                    "label": label, "kind": "added",
                    "before": None, "after": point,
                })
            elif before[key] != point:
                changes.append({
                    "section": "points", "field": field, "key": key,
                    "label": label, "kind": "changed",
                    "before": before[key], "after": point,
                })

    old_rationale = old.get("rationale") or ""
    new_rationale = new.get("rationale") or ""
    if old_rationale != new_rationale:
        changes.append({
            "section": "rationale", "kind": "changed", "label": "理由",
            "before": old_rationale, "after": new_rationale,
        })
    return changes


def spec_from_row(row: dict) -> dict:
    """把 `replay_spot_specs` 一行还原成规范化 spec（供 diff / 详情）。"""
    spec = json_load(row.get("spec_json"), {}) or {}
    return {
        "schema": spec.get("schema") or SPEC_SCHEMA,
        "bucket": json_load(row.get("bucket_json"), {}) or {},
        "allowed_actions": list(spec.get("allowed_actions") or []),
        "forbidden_actions": list(spec.get("forbidden_actions") or []),
        "sizing": spec.get("sizing"),
        "freq": spec.get("freq"),
        "required_points": list(spec.get("required_points") or []),
        "forbidden_points": list(spec.get("forbidden_points") or []),
        "rationale": spec.get("rationale") or "",
    }


def public_spec(row: dict, *, revision_count: int | None = None,
                active_revision_id: int | None = None) -> dict:
    """仓储行 → API/前端用的 camelCase 对象。"""
    from .util import iso

    spec = spec_from_row(row)
    return {
        "id": int(row["id"]),
        "name": row.get("name") or "",
        "revision": int(row.get("revision") or 1),
        "status": row.get("status") or "draft",
        "schema": spec["schema"],
        "bucket": spec["bucket"],
        "bucketLabel": bucket_label(spec["bucket"]),
        "allowedActions": spec["allowed_actions"],
        "forbiddenActions": spec["forbidden_actions"],
        "sizing": spec["sizing"],
        "freq": spec["freq"],
        "requiredPoints": spec["required_points"],
        "forbiddenPoints": spec["forbidden_points"],
        "rationale": spec["rationale"],
        "note": row.get("note") or "",
        "supersedes": (int(row["supersedes"])
                       if row.get("supersedes") else None),
        "createdAt": iso(row.get("created_at")),
        "revisionCount": (int(revision_count)
                          if revision_count is not None else None),
        "activeRevisionId": (int(active_revision_id)
                             if active_revision_id is not None else None),
    }


def meta() -> dict:
    """前端表单的词表：维度/取值/动作/单位/状态都由后端给，避免前端硬编码。"""
    return {
        "schema": SPEC_SCHEMA,
        "dimensions": [
            {
                "key": dimension,
                "label": BUCKET_VOCAB[dimension][0],
                "values": [
                    {"value": value, "label": label}
                    for value, label in BUCKET_VOCAB[dimension][1].items()
                ],
            }
            for dimension in BUCKET_ORDER
        ],
        "actions": [
            {"value": action, "label": label}
            for action, label in ACTIONS.items()
        ],
        "sizingUnits": [
            {"value": unit, "label": label}
            for unit, label in SIZING_UNITS.items()
        ],
        "statuses": [
            {"value": status, "label": SPEC_STATUS_LABELS[status]}
            for status in SPEC_STATUSES
        ],
    }
