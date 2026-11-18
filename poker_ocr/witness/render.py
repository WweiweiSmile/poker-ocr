"""摊牌证据行渲染（#15 §14.4 / #17 措辞 / #38）。

输入 witness.rows（每行一手一座位），输出每座位 ≤2 条中文提示词行：

    摊牌证据：他在河牌IP过牌时亮过弱A顶对（3 次）

三条硬口径：
- 只数 status == "admitted"（主动秀 tagged 不进 N）；每座位 ≤2 条。
- 预算 max_chars 只算新增行（每行 len+1），硬上限优先于「2 条」；座位按入参顺序
  由近到远填，耗尽即停，所以远座位先丢、同座位 N 小的先丢。
- 纯函数：零 IO（词表首次加载一次，可注入），不调模型，不抛异常到调用方。

词表 poker_ocr/tables/witness_phrasing.json 的 version 与 witness 数据版本
（admission.WITNESS_SCHEMA）任一失配 → 返回 {}，让提示词整段不出现（§14.5）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .admission import (
    ACTION_BET,
    ACTION_CHECK,
    ACTION_FACING_BET,
    ACTION_UNKNOWN,
    FAMILIES,
    FAMILY_RENDER_GROUP,
    POSITION_IP,
    POSITION_OOP,
    STATUS_ADMITTED,
    STREET_RIVER,
    STREET_TURN,
    WITNESS_SCHEMA,
)

log = logging.getLogger(__name__)

PHRASING_PATH = Path(__file__).resolve().parent.parent / "tables" / "witness_phrasing.json"

POOL_MIN_COUNT = 20  # §10.2：池级比例线 n≥20 才出
SEATS_MAX_LINES = 2
# 池级证据的保留键：它不是座位，整段只出一次，调用方独立成段、不缩进。
POOL_KEY = "__pool__"
ENV_FLAG = "WITNESS_PROMPT"

DEFAULT_PHRASING: dict[str, Any] = {
    "version": "witness-phrasing-2026-11-14a",
    "prefix": "摊牌证据：",
    "line": "他在{street}{position}{action}时亮过{family}（{count} 次）",
    "pool_line": "这个池子在{street}的亮牌里{family}占 {pct}（{count} 次，本池）",
    "street": {"turn": "转牌", "river": "河牌"},
    "position": {"ip": "IP", "oop": "OOP"},
    "action": {
        "facing_bet": "面对下注",
        "check": "过牌",
        "bet": "主动下注",
        "unknown": "行动不明",
    },
    "family": {
        "weak_ace_top_pair": "弱A顶对",
        "strong_ace_top_pair": "A顶对强踢",
        "non_ace_top_pair": "非A顶对",
        "overpair": "超对",
        "middle_pair": "中等对子",
        "small_pair": "小对子",
        "air_high_card": "高张无对",
        "suited_connector": "同花连张",
        "busted_draw": "听牌破产",
        "two_pair_plus": "两对+",
        "set": "暗三",
        "playing_board": "打牌面",
    },
    "forbidden": [
        "经常", "频繁", "总是", "往往", "通常", "一贯", "老是",
        "必然", "一定", "肯定", "铁定",
        "建议", "应该", "可以", "必须", "最好",
        "对手有", "对手拿着", "他手里是",
    ],
}

_CACHE: dict[str, Any] = {}


def _valid_phrasing(data: Any) -> bool:
    if not isinstance(data, Mapping):
        return False
    for key in ("version", "prefix", "line", "pool_line"):
        if not isinstance(data.get(key), str) or not data.get(key):
            return False
    for key, expected in (
        ("street", {"turn", "river"}),
        ("position", {"ip", "oop"}),
        ("action", {"facing_bet", "check", "bet", "unknown"}),
    ):
        table = data.get(key)
        if not isinstance(table, Mapping) or not expected <= set(table) or not all(
            isinstance(v, str) and v for v in table.values()
        ):
            return False
    family = data.get("family")
    if not isinstance(family, Mapping) or set(FAMILIES) - set(family):
        return False
    if not all(isinstance(family[f], str) and family[f] for f in FAMILIES):
        return False
    forbidden = data.get("forbidden")
    if not isinstance(forbidden, list) or not all(isinstance(w, str) and w for w in forbidden):
        return False
    return True


def load_phrasing(path: Path | str | None = None) -> dict[str, Any]:
    """读词表；坏文件 / 缺字段 / 缺族 → 内存默认值 + log 一次（不静默出行）。"""
    target = Path(path) if path is not None else PHRASING_PATH
    key = str(target)
    if key in _CACHE:
        return _CACHE[key]
    data: Any = None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 只允许回退默认值
        log.warning("witness phrasing 读取失败，回退内置默认值：%s (%s)", target, exc)
        data = None
    if data is not None and not _valid_phrasing(data):
        log.warning("witness phrasing 字段不全，回退内置默认值：%s", target)
        data = None
    resolved = dict(data) if data is not None else DEFAULT_PHRASING
    _CACHE[key] = resolved
    return resolved


def enabled() -> bool:
    """渲染开关（§15.3 的 A/B 开关）：WITNESS_PROMPT=0 关闭，默认开。"""
    return (os.environ.get(ENV_FLAG) or "1").strip().lower() not in (
        "0", "false", "no", "off")


def forbidden_words(phrasing: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    table = phrasing or load_phrasing()
    return tuple(str(w) for w in table.get("forbidden") or ())


# ---------------------------------------------------------------- 行校验


def _row_ok(row: Any) -> bool:
    """只接受能渲染的行；坏行跳过（调用方 log 一次），不猜。"""
    if not isinstance(row, Mapping):
        return False
    if row.get("status") != STATUS_ADMITTED:
        return False
    if row.get("family") not in FAMILIES:
        return False
    if row.get("street") not in (STREET_TURN, STREET_RIVER):
        return False
    if row.get("action") not in (ACTION_BET, ACTION_CHECK, ACTION_FACING_BET, ACTION_UNKNOWN):
        return False
    if row.get("position") not in (POSITION_IP, POSITION_OOP):
        return False
    cards = row.get("cards")
    if not isinstance(cards, (list, tuple)) or len(cards) != 2:
        return False
    if not str(row.get("player") or ""):
        return False
    if not str(row.get("ts") or ""):
        return False
    return True


# ---------------------------------------------------------------- 聚合


def _cells_for(rows: Iterable[Mapping[str, Any]], player: str) -> list[dict[str, Any]]:
    cells: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("player")) != player:
            continue
        key = (
            str(row["family"]),
            str(row["street"]),
            str(row["action"]),
            str(row["position"]),
        )
        cell = cells.get(key)
        ts = str(row.get("ts") or "")
        if cell is None:
            cells[key] = {"family": key[0], "street": key[1], "action": key[2],
                          "position": key[3], "n": 1, "ts": ts}
        else:
            cell["n"] += 1
            if ts > cell["ts"]:
                cell["ts"] = ts
    return _order_cells(cells.values())


def _order_cells(cells: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """同 spot 优先 → 最近 → N 大；spot 之间按最近一次出现倒序。"""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for cell in cells:
        spot = (cell["street"], cell["action"], cell["position"])
        groups.setdefault(spot, []).append(dict(cell))
    ordered: list[dict[str, Any]] = []
    spot_order = sorted(
        groups,
        key=lambda s: (max(c["ts"] for c in groups[s]), s),
        reverse=True,
    )
    for spot in spot_order:
        ordered.extend(
            sorted(groups[spot], key=lambda c: (c["n"], c["ts"], c["family"]), reverse=True)
        )
    return ordered


def _pick(cells: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    """族不重复（按 6 个渲染组），最多 limit 条。"""
    picked: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for cell in cells:
        group = FAMILY_RENDER_GROUP.get(str(cell["family"]))
        if group is None or group in seen:
            continue
        picked.append(cell)
        seen.add(group)
        if len(picked) >= limit:
            break
    return picked


def _pool_stats(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    counts: dict[tuple[str, str], int] = {}
    totals: dict[str, int] = {}
    for row in rows:
        family = str(row["family"])
        street = str(row["street"])
        counts[(family, street)] = counts.get((family, street), 0) + 1
        totals[street] = totals.get(street, 0) + 1
    return counts, totals


def _personal_lines(
    rows: Iterable[Mapping[str, Any]], player: str, phrasing: Mapping[str, Any], limit: int
) -> list[str]:
    cells = _pick(_cells_for(rows, player), limit)
    lines: list[str] = []
    for cell in cells:
        lines.append(
            phrasing["prefix"]
            + phrasing["line"].format(
                street=phrasing["street"][cell["street"]],
                position=phrasing["position"][cell["position"]],
                action=phrasing["action"][cell["action"]],
                family=phrasing["family"][cell["family"]],
                count=cell["n"],
            )
        )
    return lines


def _pool_lines(
    counts: Mapping[tuple[str, str], int],
    totals: Mapping[str, int],
    phrasing: Mapping[str, Any],
    limit: int,
) -> list[str]:
    candidates = [
        {"family": family, "street": street, "n": n}
        for (family, street), n in counts.items()
        if n >= POOL_MIN_COUNT and totals.get(street)
    ]
    candidates.sort(key=lambda c: (-c["n"], c["family"], c["street"]))
    lines: list[str] = []
    seen: set[str] = set()
    for cell in candidates:
        group = FAMILY_RENDER_GROUP.get(cell["family"])
        if group is None or group in seen:
            continue
        seen.add(group)
        pct = "{:.0f}%".format(100.0 * cell["n"] / totals[cell["street"]])
        lines.append(
            phrasing["prefix"]
            + phrasing["pool_line"].format(
                street=phrasing["street"][cell["street"]],
                family=phrasing["family"][cell["family"]],
                pct=pct,
                count=cell["n"],
            )
        )
        if len(lines) >= limit:
            break
    return lines


def _seat_items(seats: Mapping[str, Any] | Iterable[Sequence[str]]) -> list[tuple[str, str]]:
    if isinstance(seats, Mapping):
        items = [(str(seat), str(player)) for seat, player in seats.items()]
    else:
        items = [(str(pair[0]), str(pair[1])) for pair in seats]
    return [(seat, player) for seat, player in items if seat and player]


# ---------------------------------------------------------------- 入口


def render_witness_lines(
    rows: Any,
    *,
    seats: Mapping[str, Any] | Iterable[Sequence[str]],
    hero_seat: str | None = None,
    max_chars: int = 600,
    version: str | None = None,
    phrasing: Mapping[str, Any] | None = None,
) -> dict[str, tuple[str, ...]]:
    """witness.rows → {seat: (行, 行)}；0 条返回 {}（§14.5 不写占位）。

    个人摊牌证据按座位分组；没有个人证据的座位**不再**借用池级句——池级证据
    描述全桌，整段只出一次，放在保留键 `POOL_KEY` 下（调用方独立成段、不缩进）。

    seats 是「由近到远」的有序 seat→player（调用方按 hero 下注后行动顺序给）。
    rows 可以是行列表，也可以是 {"version": ..., "rows": [...]} 的 profiles.witness。
    """
    if isinstance(rows, Mapping):
        wrapper = rows
        if version is None:
            version = wrapper.get("version")
        rows = wrapper.get("rows") or ()
    data_version = version if version is not None else WITNESS_SCHEMA
    if data_version != WITNESS_SCHEMA:
        return {}
    table = phrasing if phrasing is not None else load_phrasing()
    if not _valid_phrasing(table):
        log.warning("witness phrasing 不可用，本次不出摊牌证据行")
        return {}
    items = _seat_items(seats)
    if not items or not rows:
        return {}
    admitted = [row for row in rows if _row_ok(row)]
    if not admitted:
        return {}
    counts, totals = _pool_stats(admitted)
    hero = str(hero_seat) if hero_seat is not None else None
    out: dict[str, tuple[str, ...]] = {}
    used = 0
    exhausted = False
    for seat, player in items:
        if hero is not None and seat == hero:
            continue
        try:
            lines = _personal_lines(admitted, player, table, SEATS_MAX_LINES)
        except Exception:  # noqa: BLE001 - 单座位异常只丢这一座位
            log.warning("witness 渲染失败，跳过座位 %s", seat, exc_info=True)
            continue
        emitted: list[str] = []
        for line in lines:
            cost = len(line) + 1
            if used + cost > max_chars:
                exhausted = True
                break
            emitted.append(line)
            used += cost
        if emitted:
            out[seat] = tuple(emitted)
        if exhausted:
            break
    # 池级证据描述的是这张桌子整体，与具体座位无关：全局只出一次，放在
    # POOL_KEY 这个保留键下，由调用方独立成段；挂到每个座位上会把同一对
    # 句子按座位数重复（#38 验收里 42% 的证据行是这么来的）。
    if not exhausted:
        try:
            pool = _pool_lines(counts, totals, table, SEATS_MAX_LINES)
        except Exception:  # noqa: BLE001 - 池级失败只丢池级
            log.warning("witness 池级证据渲染失败", exc_info=True)
            pool = ()
        emitted = []
        for line in pool:
            cost = len(line) + 1
            if used + cost > max_chars:
                break
            emitted.append(line)
            used += cost
        if emitted:
            out[POOL_KEY] = tuple(emitted)
    return out
