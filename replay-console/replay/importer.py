"""手牌导入：把「复盘手牌」和「实时日志手牌记录」归一成内部行。

支持的两类输入：

1. **复盘手牌**：`review_hands` 导出的 JSON 数组 / JSONL。字段可以是
   camelCase（`heroCards` / `bigBlindBb`）或 snake_case（`hero_cards` /
   `big_blind_bb`），行动线固定是 `{actor, action, amountBb}`。
2. **实时日志手牌记录**：`hands/1023.hands.json` 这样的 JSON 数组，或
   `logs/live.states.jsonl` 原始日志。后者会自动跳过 `state` 记录、按
   `session_start` 计会话；默认也会跳过 `partial` 和从翻后开始的中途接管手牌。

这里只做「解析 + 归一化」，不连库、不调模型。归一后的行与 `review_hands`
的 camelCase 字段一一对应，直接喂给 `replay.decisions.analyze_hand()`。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import ApiError
from .util import STREETS, json_load, now_cn, safe_float, safe_int

SOURCE_REVIEW = "review"
SOURCE_LIVE = "live"
IMPORT_SOURCES = (SOURCE_REVIEW, SOURCE_LIVE)
SOURCE_LABELS = {
    SOURCE_REVIEW: "导入·复盘",
    SOURCE_LIVE: "导入·实时",
}

MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_IMPORT_HANDS = 10000

_ACTION_ALIASES = {
    "all-in": "allin",
    "all_in": "allin",
    "all in": "allin",
    "allin": "allin",
    "bet": "bet",
    "raise": "raise",
    "call": "call",
    "check": "check",
    "fold": "fold",
    "muck": "fold",
}

_AGGREGATE_ACTORS = {"villain", "other", "unknown", "?"}
_POSITIONS = {"UTG", "UTG+1", "UTG+2", "UTG+3", "UTG+4", "LJ", "HJ", "CO",
              "BTN", "SB", "BB", "MP", "MP1", "MP2", "EP"}


@dataclass
class ParsedHand:
    index: int
    line: int | None
    source: str
    session: int
    row: dict
    raw: dict


@dataclass
class ParseResult:
    source: str
    container: str
    records: list[ParsedHand]
    errors: list[dict] = field(default_factory=list)
    scanned: int = 0
    skipped_non_hand: int = 0
    skipped_partial: int = 0
    skipped_mid_hand: int = 0
    session_count: int = 0
    # 已并进手牌的实时建议条数（type=advice 的行不单独成手）。
    advices: int = 0

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)


# ─────────────────────────── 读取与格式识别 ───────────────────────────

def parse_hand_text(text: str, *, source: str = "auto",
                    include_partial: bool = False,
                    include_mid_hand: bool = False) -> ParseResult:
    """解析文本。`source` 可以是 auto / review / live。"""
    if source not in ("auto", SOURCE_REVIEW, SOURCE_LIVE):
        raise ApiError("IMPORT_FORMAT_INVALID",
                       f"未知导入格式：{source}（可选 auto/review/live）", 422)
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        raise ApiError("IMPORT_EMPTY", "导入内容为空", 422)
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ApiError("IMPORT_TOO_LARGE",
                       f"导入内容超过 {MAX_IMPORT_BYTES // 1024 // 1024}MB 上限", 413)

    records, container, errors = _read_records(text)
    if not records:
        detail = "；".join(e["reason"] for e in errors[:3])
        raise ApiError("IMPORT_PARSE_ERROR",
                       f"没有解析到任何手牌记录{f'：{detail}' if detail else ''}", 422,
                       {"errors": errors[:20]})

    detected = (source if source in IMPORT_SOURCES
                else _detect_source(records))
    result = ParseResult(source=detected, container=container, records=[],
                         errors=list(errors), scanned=len(records))
    if detected == SOURCE_LIVE:
        _parse_live(records, result, include_partial=include_partial,
                    include_mid_hand=include_mid_hand)
    else:
        _parse_review(records, result)
    if len(result.records) > MAX_IMPORT_HANDS:
        raise ApiError("IMPORT_TOO_MANY",
                       f"一次最多导入 {MAX_IMPORT_HANDS} 手，当前 {len(result.records)} 手",
                       413)
    return result


def _read_records(text: str) -> tuple[list[tuple[int | None, dict]],
                                      str, list[dict]]:
    """返回 [(行号, 记录)], 容器类型, 坏记录。支持整段 JSON 与 JSONL。"""
    stripped = text.strip()
    # 整段 JSON（数组 / {"hands": [...]} / 单个对象）优先；但多行 JSONL 若以
    # `{` 开头，json.loads 会失败，自然落到逐行解析。
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        return _records_from_json(parsed)

    records: list[tuple[int | None, dict]] = []
    errors: list[dict] = []
    for lineno, raw in enumerate(stripped.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            errors.append({"line": lineno, "reason": "JSON 解析失败"})
            continue
        if not isinstance(record, dict):
            errors.append({"line": lineno, "reason": "不是 JSON 对象"})
            continue
        records.append((lineno, record))
    return records, "jsonl", errors


def _records_from_json(parsed: Any) -> tuple[list[tuple[int | None, dict]],
                                              str, list[dict]]:
    if isinstance(parsed, list):
        raw_records = parsed
    elif isinstance(parsed, dict):
        for key in ("hands", "records", "items", "data"):
            if isinstance(parsed.get(key), list):
                raw_records = parsed[key]
                break
        else:
            raw_records = [parsed]
    else:
        raise ApiError("IMPORT_PARSE_ERROR",
                       "JSON 顶层必须是数组或对象", 422)
    records: list[tuple[int | None, dict]] = []
    errors: list[dict] = []
    for index, record in enumerate(raw_records):
        if not isinstance(record, dict):
            errors.append({"index": index, "reason": "不是 JSON 对象"})
            continue
        records.append((None, record))
    return records, "json", errors


def _detect_source(records: Iterable[tuple[int | None, dict]]) -> str:
    live = 0
    review = 0
    for _line, rec in records:
        kind = rec.get("type")
        if kind in ("session_start", "state", "stream_info"):
            live += 3
            continue
        if kind == "hand":
            return SOURCE_LIVE
        if isinstance(rec.get("blinds"), dict) or rec.get("uid") or rec.get("hand_id"):
            live += 2
        if isinstance(rec.get("hero_cards"), list):
            live += 1
        if rec.get("heroCards") or isinstance(rec.get("hero_cards"), str):
            review += 1
        for street in _as_list(rec.get("streets")):
            for action in _as_list(street.get("actions") if isinstance(street, dict) else None):
                if not isinstance(action, dict):
                    continue
                if action.get("seat_id") or action.get("is_hero") is not None:
                    live += 1
                elif action.get("actor"):
                    review += 1
    return SOURCE_LIVE if live > review else SOURCE_REVIEW


# ─────────────────────────── 实时日志 ───────────────────────────

_STREET_RANK = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}


def _is_advice(rec: dict) -> bool:
    """是否是「行动建议」记录，而不是一手牌。

    `live.states.jsonl` 里 advice 是独立一行（`type=advice`），但语义上属于
    同一手牌；导入时要把它们并进 hand，而不是各算一手。没有 `type` 的旧导出
    也认：建议自带 option/reason/street，却没有行动线 `streets`。
    """
    if rec.get("type") == "advice":
        return True
    return (isinstance(rec.get("option"), dict)
            and _text(rec.get("street")) in STREETS
            and not rec.get("streets"))


def _advice_key(advice: dict) -> tuple:
    """同一决策的身份：街 + 选项 + 底池/需跟 + 理由。

    手牌记录里内嵌的 advice 与日志里独立的 advice 行内容相同，但前者没有
    `type`/`ts` 字段；按整段 JSON 去重会留下一份重复（实测每条建议都翻倍）。
    这里只比「决策身份」，两边能对上。
    """
    option = (advice.get("option")
              if isinstance(advice.get("option"), dict) else {})
    return (
        _text(advice.get("street")),
        _text(option.get("id")),
        option.get("amount_bb", option.get("amount")),
        safe_float(advice.get("pot")),
        safe_float(advice.get("to_call")),
        _text(advice.get("reason")),
    )


def _advice_richness(advice: dict) -> tuple:
    """重复时保留信息更全的一条：优先有 ts，其次字段更多。"""
    return (bool(advice.get("ts")), len(advice))


def _merge_advices(*groups) -> list[dict]:
    """合并手牌内嵌的建议与日志里独立的 advice 行，按街+时间去重排序。"""
    out: list[dict] = []
    index: dict[tuple, int] = {}
    for group in groups:
        for advice in group or ():
            if not isinstance(advice, dict):
                continue
            key = _advice_key(advice)
            pos = index.get(key)
            if pos is None:
                index[key] = len(out)
                out.append(advice)
                continue
            if _advice_richness(advice) > _advice_richness(out[pos]):
                out[pos] = advice
    out.sort(key=lambda a: (_STREET_RANK.get(_text(a.get("street")), 9),
                            str(a.get("ts") or "")))
    return out


def _parse_live(records: list[tuple[int | None, dict]], result: ParseResult,
                *, include_partial: bool, include_mid_hand: bool) -> None:
    # 建议行先按 (会话, hand_id) 归组：手牌记录可能在建议之后才写出，
    # 不能边扫边挂。会话号只在 session_start 时前进，保证 h1/h2 这种
    # 跨会话重复的 hand_id 不会串手。
    advices_by_hand: dict[tuple[int, str], list[dict]] = {}
    session = 0
    for _line, rec in records:
        if rec.get("type") == "session_start":
            session += 1
            continue
        if not _is_advice(rec):
            continue
        hand_id = _text(rec.get("hand_id"))
        if hand_id:
            advices_by_hand.setdefault((session, hand_id), []).append(rec)

    session = 0
    hand_seq = 0
    for index, (line, rec) in enumerate(records):
        kind = rec.get("type")
        if kind == "session_start":
            session += 1
            result.session_count += 1
            continue
        if _is_advice(rec):
            # 已归到对应手牌下面，不再单独成手。
            result.skipped_non_hand += 1
            continue
        if kind in ("state", "stream_info", "session_end"):
            result.skipped_non_hand += 1
            continue
        if kind not in (None, "hand"):
            result.skipped_non_hand += 1
            continue
        if not _looks_like_hand(rec):
            result.errors.append({
                "index": index, "line": line,
                "reason": "缺少 hand 记录字段（hero_cards / board / streets）",
            })
            continue
        hand_seq += 1
        if rec.get("partial") and not include_partial:
            result.skipped_partial += 1
            continue
        if not _starts_at_preflop(rec) and not include_mid_hand:
            result.skipped_mid_hand += 1
            continue
        rec_session = safe_int(rec.get("session"), session)
        hand_id = _text(rec.get("hand_id")) or f"h{hand_seq}"
        # 手牌记录自己可能已经内嵌 advices（live._write_hand 会带）；与日志里
        # 独立的 advice 行合并去重，两种来源都不丢。
        advices = _merge_advices(
            rec.get("advices") if isinstance(rec.get("advices"), list) else [],
            advices_by_hand.get((rec_session, hand_id), []),
        )
        if advices:
            rec = {**rec, "advices": advices}
            result.advices += len(advices)
        try:
            row = normalize_live_hand(rec, session=rec_session,
                                      hand_seq=hand_seq)
        except Exception as exc:  # noqa: BLE001
            result.errors.append({
                "index": index, "line": line,
                "reason": f"实时手牌归一失败：{type(exc).__name__}: {exc}",
            })
            continue
        result.records.append(ParsedHand(
            index=index, line=line, source=SOURCE_LIVE,
            session=rec_session, row=row, raw=rec,
        ))


def _looks_like_hand(rec: dict) -> bool:
    return bool(rec.get("streets") or rec.get("hero_cards") or rec.get("board"))


def _starts_at_preflop(hand: dict) -> bool:
    streets = _as_list(hand.get("streets"))
    return bool(streets) and isinstance(streets[0], dict) \
        and streets[0].get("street") == "preflop"


def normalize_live_hand(rec: dict, *, session: int = 0,
                        hand_seq: int = 1) -> dict:
    blinds = rec.get("blinds") if isinstance(rec.get("blinds"), dict) else {}
    sb = safe_float(blinds.get("sb"), 0.0) or 0.0
    bb = safe_float(blinds.get("bb"), 0.0) or 0.0
    ante = safe_float(blinds.get("ante"), 0.0) or 0.0
    table_size = safe_int(rec.get("table_size"), 0)
    hero_cards = _cards_text(rec.get("hero_cards"))[:4]
    board = _cards_text(rec.get("board"))[:10]
    hero_position = _text(rec.get("hero_position")) or None
    hand_id = _text(rec.get("hand_id")) or f"h{hand_seq}"
    external_id = (_text(rec.get("uid"))
                   or f"s{session:02d}-{hand_id}")
    villains = _live_villains(rec)
    bomb = 1.0 if rec.get("bomb_pot") else 0.0
    hero_net = safe_float(rec.get("hero_net"))
    result, result_amount = _result_of(hero_net)
    created = _parse_datetime(rec.get("ts") or rec.get("started_at")) or now_cn()
    row = {
        "id": 0,
        "source": SOURCE_LIVE,
        "external_id": external_id,
        "title": _text(rec.get("title")) or _default_title(hero_position, hero_cards),
        "hero_cards": hero_cards,
        "board": board,
        "hero_position": hero_position,
        "table_size": table_size,
        "hero_stack_bb": safe_float(_first(
            rec, "hero_stack_bb", "heroStackBb", "stack_bb", "hero_stack")),
        "small_blind_bb": sb,
        "big_blind_bb": bb,
        "ante_bb": ante,
        "bomb_pot_bb": bomb,
        "villain_count": max(len(villains), max(table_size - 1, 0)),
        "villains": villains,
        "streets": _live_streets(rec),
        "result": result,
        "result_amount": result_amount,
        "pot_type": None,
        "hero_tags": [],
        "source_created_at": created,
        "session": session,
        "hand_id": hand_id,
    }
    # 建议跟着手牌走：写进 payload_json，详情页可直接展示；没有建议时不加
    # 这个键，保持旧记录的 payload 形状不变。
    advices = _merge_advices(
        rec.get("advices") if isinstance(rec.get("advices"), list) else [])
    if advices:
        row["advices"] = advices
    return row


def _live_streets(rec: dict) -> list[dict]:
    out: list[dict] = []
    for raw in _as_list(rec.get("streets")):
        if not isinstance(raw, dict):
            continue
        street = _text(raw.get("street")).lower()
        if street not in STREETS:
            continue
        actions: list[dict] = []
        for item in _as_list(raw.get("actions")):
            if not isinstance(item, dict):
                continue
            action = _action(item.get("action"))
            if not action:
                continue
            actor = _text(item.get("seat_id")) or _text(item.get("position"))
            if item.get("is_hero") or actor == "hero":
                actor = "hero"
            if not actor:
                continue
            actions.append({
                "actor": actor,
                "action": action,
                "amountBb": safe_float(item.get("amount")),
            })
        out.append({
            "street": street,
            "potStartBb": safe_float(raw.get("pot_start")),
            "actions": actions,
        })
    return out


def _live_villains(rec: dict) -> list[dict]:
    found: dict[str, dict] = {}

    def add(position: Any, name: Any = None) -> dict | None:
        position = _text(position) or None
        if not position or position == "hero" or position.lower() in _AGGREGATE_ACTORS:
            return None
        item = found.setdefault(position, {
            "position": position, "name": None, "stackBb": None,
        })
        if name and not item.get("name"):
            item["name"] = _text(name) or None
        return item

    for street in _as_list(rec.get("streets")):
        if not isinstance(street, dict):
            continue
        for action in _as_list(street.get("actions")):
            if not isinstance(action, dict) or action.get("is_hero"):
                continue
            add(action.get("position") or _position_of(action.get("seat_id")),
                action.get("name"))
    for show in _as_list(rec.get("shows")):
        if isinstance(show, dict):
            add(show.get("position") or _position_of(show.get("seat_id")),
                show.get("name"))
    for all_in in _as_list(rec.get("all_ins")):
        if isinstance(all_in, dict):
            add(all_in.get("position") or _position_of(all_in.get("seat_id")),
                all_in.get("name"))
    # 实时手牌记录里的各座起始筹码。只补**行动线/亮牌/全下已经出现过**的
    # 位置：凭空多出来的座位（比如没留下弃牌徽章的人）会被当成仍在池里，
    # 让有效筹码变小；宁可少一个筹码，也不要凭空改变在局人数。
    hero_position = _text(rec.get("hero_position")) or None
    raw_stacks = rec.get("start_stacks")
    entries = (raw_stacks.values() if isinstance(raw_stacks, dict)
               else _as_list(raw_stacks))
    for item in entries:
        if not isinstance(item, dict):
            continue
        seat_id = _text(item.get("seat_id"))
        position = (_text(item.get("position"))
                    or _position_of(item.get("seat_id")))
        if seat_id == "hero" or (hero_position and position == hero_position):
            continue
        # 只给**已经由行动线/亮牌/全下确认在场**的位置补筹码，不新建座位：
        # 没留下动作或徽章的弃牌者如果被当成仍在池里，会把有效筹码压小。
        villain = found.get(position) if position else None
        if villain is None:
            continue
        stack = safe_float(item.get("stack"))
        if stack is not None and villain.get("stackBb") is None:
            villain["stackBb"] = stack
    return list(found.values())


# ─────────────────────────── 复盘手牌 ───────────────────────────

def _parse_review(records: list[tuple[int | None, dict]], result: ParseResult) -> None:
    for index, (line, rec) in enumerate(records):
        kind = rec.get("type")
        if kind in ("session_start", "state", "stream_info", "session_end"):
            result.skipped_non_hand += 1
            continue
        if not _looks_like_review_hand(rec):
            result.errors.append({
                "index": index, "line": line,
                "reason": "缺少复盘手牌字段（heroCards / streets）",
            })
            continue
        try:
            row = normalize_review_hand(rec, index=index)
        except Exception as exc:  # noqa: BLE001
            result.errors.append({
                "index": index, "line": line,
                "reason": f"复盘手牌归一失败：{type(exc).__name__}: {exc}",
            })
            continue
        result.records.append(ParsedHand(
            index=index, line=line, source=SOURCE_REVIEW,
            session=0, row=row, raw=rec,
        ))


def _looks_like_review_hand(rec: dict) -> bool:
    return bool(rec.get("heroCards") or rec.get("hero_cards")
                or rec.get("streets") or rec.get("id"))


def normalize_review_hand(rec: dict, *, index: int = 0) -> dict:
    hero_cards = _cards_text(_first(rec, "heroCards", "hero_cards"))[:4]
    board = _cards_text(_first(rec, "board") or "")[:10]
    hero_position = _text(_first(rec, "heroPosition", "hero_position")) or None
    table_size = safe_int(_first(rec, "tableSize", "table_size"), 0)
    villain_count = safe_int(_first(rec, "villainCount", "villain_count"), 0)
    if not table_size:
        table_size = max(villain_count + 1, 1) if villain_count else 0
    if not villain_count and table_size:
        villain_count = max(table_size - 1, 0)
    streets = _review_streets(rec)
    villains = _review_villains(rec, streets)
    sb = _number(_first(rec, "smallBlindBb", "small_blind_bb", "sb"), 0.0)
    bb = _number(_first(rec, "bigBlindBb", "big_blind_bb", "bb"), 0.0)
    ante = _number(_first(rec, "anteBb", "ante_bb", "ante"), 0.0)
    bomb = _number(_first(rec, "bombPotBb", "bomb_pot_bb", "bombPot", "bomb_pot"), 0.0)
    external_id = _text(_first(rec, "id", "hand_id", "uid")) or f"review-{index + 1}"
    result = _text(_first(rec, "result")) or "unknown"
    result_amount = _number(_first(rec, "resultAmount", "result_amount"), None)
    hero_tags = _as_list(_first(rec, "heroTags", "hero_tags"))
    created = _parse_datetime(_first(rec, "created_at", "createdAt", "ts")) or now_cn()
    return {
        "id": 0,
        "source": SOURCE_REVIEW,
        "external_id": external_id,
        "title": _text(_first(rec, "title")) or _default_title(hero_position, hero_cards),
        "hero_cards": hero_cards,
        "board": board,
        "hero_position": hero_position,
        "table_size": table_size,
        "hero_stack_bb": _number(_first(rec, "heroStackBb", "hero_stack_bb"), None),
        "small_blind_bb": sb,
        "big_blind_bb": bb,
        "ante_bb": ante,
        "bomb_pot_bb": bomb,
        "villain_count": villain_count,
        "villains": villains,
        "streets": streets,
        "result": result,
        "result_amount": result_amount,
        "pot_type": _text(_first(rec, "potType", "pot_type")) or None,
        "hero_tags": [str(t) for t in hero_tags if str(t).strip()],
        "source_created_at": created,
        "session": 0,
        "hand_id": _text(_first(rec, "id", "hand_id")) or external_id,
    }


def _review_streets(rec: dict) -> list[dict]:
    raw_streets = json_load(rec.get("streets"), rec.get("streets")) or []
    out: list[dict] = []
    for raw in _as_list(raw_streets):
        if not isinstance(raw, dict):
            continue
        street = _text(raw.get("street")).lower()
        if street not in STREETS:
            continue
        actions: list[dict] = []
        for item in _as_list(raw.get("actions")):
            if not isinstance(item, dict):
                continue
            action = _action(item.get("action"))
            actor = _text(item.get("actor"))
            if item.get("is_hero"):
                actor = "hero"
            if not actor or not action:
                continue
            actions.append({
                "actor": actor,
                "action": action,
                "amountBb": _number(
                    _first(item, "amountBb", "amount_bb", "amount"), None),
            })
        out.append({
            "street": street,
            "potStartBb": _number(
                _first(raw, "potStartBb", "pot_start", "potStart"), None),
            "actions": actions,
        })
    return out


def _review_villains(rec: dict, streets: list[dict] | None = None) -> list[dict]:
    found: dict[str, dict] = {}

    def add(position: Any, name: Any = None, stack: Any = None) -> None:
        position = _text(position) or None
        if not position or position == "hero" or position.lower() in _AGGREGATE_ACTORS:
            return
        item = found.setdefault(position, {
            "position": position, "name": None, "stackBb": None,
        })
        if name and not item.get("name"):
            item["name"] = _text(name) or None
        stack_value = safe_float(stack)
        if stack_value is not None and item.get("stackBb") is None:
            item["stackBb"] = stack_value

    raw_villains = json_load(_first(rec, "villains"), None) or []
    for item in _as_list(raw_villains):
        if isinstance(item, dict):
            add(item.get("position") or item.get("seat_id") or item.get("pos"),
                item.get("name"),
                _first(item, "stackBb", "stack_bb", "stack"))
    # 老导出不一定带 villains，从行动线的 actor/position 兜底。
    for street in streets or []:
        for action in _as_list(street.get("actions")):
            if not isinstance(action, dict) or action.get("is_hero"):
                continue
            add(action.get("position") or _position_of(action.get("actor")),
                action.get("name"))
    return list(found.values())


# ─────────────────────────── 小工具 ───────────────────────────

def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _first(rec: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in rec and rec[key] is not None:
            return rec[key]
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _cards_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "".join(_card_text(item) for item in value
                       if str(item).strip())
    text = _text(value)
    if not text:
        return ""
    # "Ah Kd" / "Ah,Kd" / "Ah-Kd" / "10d" 都归一成 "AhKd" / "Td"
    for sep in (" ", ",", "-", "/", "|"):
        text = text.replace(sep, "")
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index:index + 2] == "10" and index + 2 < len(text):
            out.append("T" + text[index + 2].lower())
            index += 3
        elif index + 1 < len(text):
            out.append(text[index].upper() + text[index + 1].lower())
            index += 2
        else:
            break
    return "".join(out)


def _card_text(value: Any) -> str:
    text = _text(value)
    if len(text) == 3 and text[:2] == "10":
        return "T" + text[2].lower()
    if len(text) >= 2:
        return text[0].upper() + text[1].lower()
    return text


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return safe_float(value, default)


def _action(value: Any) -> str:
    return _ACTION_ALIASES.get(_text(value).lower(), _text(value).lower())


def _position_of(value: Any) -> str | None:
    text = _text(value).upper()
    return text if text in _POSITIONS else None


def _default_title(position: str | None, cards: str) -> str:
    return f"{position or ''} {cards}".strip() or "导入手牌"


def _result_of(hero_net: float | None) -> tuple[str, float | None]:
    if hero_net is None:
        return "unknown", None
    if hero_net > 0:
        return "win", hero_net
    if hero_net < 0:
        return "lose", hero_net
    return "unknown", 0.0


def _parse_datetime(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(
                dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(
            dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
    return parsed
