#!/usr/bin/env python3
"""范围推导换表重算 + diff（只读）：同一批 trace 记录，两版 range_flow 表各算一遍。

为什么需要它
────────────
`poker_ocr/tables/range_flow.json` 里每张表都有 v0（手调）/ v1（推导）两个
变体，`STRATEGY_TABLES_PROFILE` 一换，实时链路的范围数字就变。但「变了多少、
从哪一步开始变、哪些 combo 在动」在实时链路里看不见：trace 只记**当前**表
算出来的那一版。

这个工具拿 `logs/live.range.jsonl`（契约 v1.2）里的记录，按记录自己的 facts
（先验 id + 事件链）在两版表下各重算一遍，输出逐决策 / 逐座位 / 逐步骤的
差异报告。**只读**：不写回 trace、不刷新看板、不联网、不调模型。

为什么必须子进程
────────────────
`range_flow` 的 `_FACING_PROBS` / `_SUPPORT_FLOOR` / `_DEFAULT_ALPHA` 等常量在
**import 期**从表里读死；`ranges.get()` 还有按进程的 lru_cache。同一进程里
切 `STRATEGY_TABLES_PROFILE` 不会生效。所以每个 profile 起一个子进程，子进程
只输出规范化 JSON，父进程做比较（与 `tools/strategy_table_diff.py` 同一套路）。

用法
────
    # 默认读当天 logs/live.range.jsonl.YYYYMMDD（没有就退回 logs/live.range.jsonl）
    nv tools/range_table_diff.py
    nv tools/range_table_diff.py --hand 104 --street flop
    nv tools/range_table_diff.py --decision-key 104:flop:0 --json
    nv tools/range_table_diff.py --file /tmp/range-e2e/live.range.jsonl --top 20

退出码
──────
    0  正常完成且两版无差异（含「不可复现但两版一致」：见下）
    2  检出差异
    1  输入不可读 / 没有可重算的记录 / 重建失败（原因打在 stderr 与报告里）

「不可复现」不等于失败
──────────────────────
记录自己的 `params.tables`（profile/variant/sha256）对不上本次两版表时，只把
`reproducible=false` 与 `reasons` 写进报告，**照常输出两版 diff**；退出码仍按
「有没有差异」给 0 / 2。这样老日志不会因为表换过就变成「跑不动」，
但报告不会假装那是这两版表算出来的数。

逐步比的是**份额**，不只是宽度
──────────────────────────────
`steps[].width_before/width_after` 是支撑集大小（存活 combo 数 / 1326）。换表
只改乘数、不改正负，所以宽度在两侧几乎恒定 —— 只比宽度会永远看不到分歧。
因此每步还带 `top`（本步变动最大的 combo 及其步后份额），父进程用份额并集
判 `share_delta`，`first_divergence` = 宽度或份额先不同的那一步。

报告字段（v1，见规格 #33 C 段）
──────────────────────────────
    source / filters / tables{a,b} / records[] / summary
    records[]: hand, street, decision_key, board, ts, session, params,
               reproducible, reasons, divergent, errors, seats[]
    seats[]:   seat, pos, archetype, path, mode, bucket, prior{a,b},
               recorded_prior, steps[], first_divergence, final_divergent,
               final{a,b,delta}, dropped{a,b}, top_combo_moves[], divergent
    steps[]:   i, street, label, width_before_a/b, width_after_a/b, delta,
               share_delta, divergent, first_divergence
    summary:   records, seats, divergent_records, divergent_seats,
               max_effective_width_delta, top_divergence, reproducible,
               rebuild_failures
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PATH = ROOT / "logs" / "live.range.jsonl"
PROFILE_A = "hand-tuned"
PROFILE_B = "sourced"

_STREET_ORDER = ("preflop", "flop", "turn", "river", "showdown")
_BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
_RANK = "23456789TJQKA"
# 子进程每座位保留的 final top combo 池：足够宽，父进程再从并集里取 top N 位移。
_TOP_POOL = 30
# 每步保留的 combo 份额（按本步 |dw| 排序的 top 行），用来定位「从哪一步分叉」。
_TOP_STEP = 15
_TOP_MOVES = 10
_EPS = 1e-9


# ─────────────────────────── 小工具 ───────────────────────────

def _card(text: str) -> tuple[int, str] | None:
    """'Ah' / '10s' → (点数下标, 'h'/'s')；认不出来返回 None（宁可漏，不猜）。

    牌面文本里十是**三字符** `10s`（见 `poker_ocr/cards.py` 的 RANKS），不能按
    固定两位切；这条在真实日志的牌面上天天出现（#37 端到端踩到）。
    """
    raw = str(text or "").strip()
    if len(raw) < 2:
        return None
    rank, suit = raw[:-1].upper(), raw[-1].lower()
    if rank == "10":
        rank = "T"
    if rank not in _RANK or suit not in "cdhs":
        return None
    return _RANK.index(rank), suit


def _board_tokens(text: str) -> list[str]:
    """牌面拼串 → 逐张：'Ks7c10d' → ['Ks', '7c', '10d']（十按三字符切）。"""
    raw = str(text or "")
    out: list[str] = []
    i = 0
    while i + 1 < len(raw):
        step = 3 if raw[i:i + 2] == "10" else 2
        out.append(raw[i:i + step])
        i += step
    return out


def _cards(values) -> tuple[tuple[int, str], ...]:
    out = []
    for value in values or ():
        card = _card(value)
        if card is not None:
            out.append(card)
    return tuple(out)


def _differs(a, b, tol: float = _EPS) -> bool:
    """None 与 None 视为相同；一侧 None 视为不同；数值按容差比。"""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    try:
        return abs(float(a) - float(b)) > tol
    except (TypeError, ValueError):
        return a != b


def _step_shares(step: dict | None) -> dict[str, float]:
    """一步的 combo 份额表 {combo: w1}（worker 只带本步变动最大的若干个）。"""
    return {str(row.get("c")): float(row.get("w") or 0.0)
            for row in (step or {}).get("top") or () if row.get("c")}


def _action_value(record: dict):
    """`decision_key.actions` 原值：实时链路写整数决策序号，旧载荷写 token 列表。"""
    return (record.get("decision_key") or {}).get("actions")


def _is_index(value) -> bool:
    """整数决策序号；`True`/`False` 是 int 的子类，不算序号。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _action_tokens(record: dict) -> tuple[str, ...]:
    """`decision_key.actions` 归一成 token 元组（旧载荷形态）。

    实时链路现在写的是**整数决策序号**（如 `actions: 0`），那不是 token
    列表，这里返回空元组，由 `_actions_text` / `_matches_actions` 单独处理。
    """
    value = _action_value(record)
    if value is None or _is_index(value):
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return (str(value),)


def _actions_text(record: dict) -> str:
    """决策点的人读形式：整数序号 → `"0"`，token → `:` 连起来（如 `bet:6:raise:20`）。"""
    value = _action_value(record)
    if _is_index(value):
        return str(value)
    return ":".join(_action_tokens(record))


def _record_key(record: dict) -> tuple:
    """记录身份 (hand, street, actions)。子进程载荷用 `actions` 文本，
    原始 trace 记录用 `decision_key.actions` 列表，两种都要认。"""
    actions = record.get("actions")
    text = actions if isinstance(actions, str) else _actions_text(record)
    return (record.get("hand"), record.get("street"), text)


def _sort_key(record: dict) -> tuple:
    street = record.get("street") or ""
    order = (_STREET_ORDER.index(street) if street in _STREET_ORDER
             else len(_STREET_ORDER))
    try:
        hand = int(record.get("hand") or 0)
    except (TypeError, ValueError):
        hand = 0
    value = _action_value(record)
    count = value if _is_index(value) else len(_action_tokens(record))
    return (order, hand, count, _actions_text(record))


def _keep_newer(bucket: dict, key: tuple, record: dict) -> None:
    previous = bucket.get(key)
    if previous is None or str(record.get("ts") or "") > str(
            previous.get("ts") or ""):
        bucket[key] = record


def default_source(base: Path = DEFAULT_PATH) -> Path:
    """默认读**当天**：按天轮转的 `live.range.jsonl.YYYYMMDD` 优先。

    实时链路开着 trace 时，当天文件叫 `live.range.jsonl.20261115`；只有没
    轮转（或手写夹具）时才是 `live.range.jsonl` 本身。
    """
    day = Path(base).parent / f"{Path(base).name}.{date.today():%Y%m%d}"
    return day if day.is_file() else Path(base)


def _parse_decision_key(spec: str | None) -> tuple:
    """`flop` / `flop:bet:6` / `104:flop:bet:6` / `flop:2` → (hand, street, actions)。

    actions 段两种形态：实时记录里 `decision_key.actions` 是**整数决策序号**
    （0 起），此时 `flop:2` 指该街第 3 个决策点；旧载荷/fixture 里是 token
    列表，此时 `flop:2` 指 token 个数、`flop:bet:6` 指 token 文本。
    """
    if not spec:
        return None, None, None
    parts = [part.strip() for part in str(spec).split(":")]
    if not all(parts):
        raise ValueError(
            f"--decision-key 里不能有空段：{spec!r}"
            f"（示例 flop / flop:bet:6 / 104:flop:bet:6 / flop:2）")
    hand = None
    if parts[0].lstrip("-").isdigit():
        hand = int(parts[0])
        parts = parts[1:]
        if not parts:
            raise ValueError(
                f"--decision-key 只给 hand 不够：{spec!r}"
                f"（示例 104:flop:bet:6）")
    street = parts[0]
    if street not in _STREET_ORDER:
        raise ValueError(
            f"--decision-key 的 street 不认识：{street!r}"
            f"（可选 {'/'.join(_STREET_ORDER)}）")
    actions = ":".join(parts[1:]) if len(parts) > 1 else None
    return hand, street, actions


def _matches_actions(record: dict, actions: str) -> bool:
    """`--decision-key` 的 actions 段。

    记录里是整数决策序号（实时链路）：数字段按序号相等匹配，token 文本段
    无从匹配。记录里是 token 列表（旧载荷/fixture）：数字段按 token 个数，
    文本段按 `:` 连接后精确比较。
    """
    value = _action_value(record)
    if _is_index(value):
        return actions.isdigit() and int(actions) == value
    if actions.isdigit():
        return len(_action_tokens(record)) == int(actions)
    return _actions_text(record) == actions.replace("|", ":")


# ─────────────────────────── 读 trace ───────────────────────────

def load_records(path: str | Path, *, hand: int | None = None,
                 street: str | None = None, decision_key: str | None = None,
                 lenient: bool = False) -> dict:
    """读 trace 文件 → 过滤后的 `status:"ok"` 记录。

    同一决策点（hand + street + actions）写过多条时只留 ts 最新的那条，
    与 `range_trace.records_for` 同口径；返回按街序 + 决策序号排好。
    """
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(
            f"trace 文件不存在：{file}\n"
            f"（实时链路要开 trace 才会写；也可用 --file 指定别的文件）")
    key_hand, key_street, key_actions = _parse_decision_key(decision_key)
    if key_hand is not None and hand is None:
        hand = key_hand
    if key_street and not street:
        street = key_street

    picked: dict[tuple, dict] = {}
    bad_lines: list[int] = []
    lines = 0
    status_ok = 0
    with file.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            lines += 1
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                bad_lines.append(number)
                continue
            if not isinstance(record, dict):
                bad_lines.append(number)
                continue
            if record.get("status") != "ok":
                continue
            status_ok += 1
            try:
                record_hand = int(record.get("hand"))
            except (TypeError, ValueError):
                continue
            if hand is not None and record_hand != int(hand):
                continue
            if street and record.get("street") != street:
                continue
            if key_actions is not None and not _matches_actions(record, key_actions):
                continue
            _keep_newer(picked, _record_key(record), record)

    records = sorted(picked.values(), key=_sort_key)
    if bad_lines and not lenient:
        raise ValueError(
            f"{file} 有 {len(bad_lines)} 行不是合法 JSON（首个在第 "
            f"{bad_lines[0]} 行）；确认文件完好，或用 --lenient 跳过坏行")
    return {
        "file": str(file),
        "lines": lines,
        "bad_lines": bad_lines,
        "status_ok": status_ok,
        "selected": len(records),
        "records": records,
    }


# ─────────────────────────── 子进程：按一套表重算 ───────────────────────────

def _tables_identity() -> dict:
    """当前进程这套表的身份四件套里的表部分（profile / variant / sha256）。"""
    from poker_ocr import range_trace, strategy_tables

    name = strategy_tables.profile()
    return {
        "profile": name,
        "variant": strategy_tables.variant_of(name),
        "sha256": range_trace.tables_sha(),
    }


def _prior_identity(prior) -> dict:
    """先验身份：id + version + source + 表 sha256（契约 §3.3.1 四件套）。"""
    from poker_ocr import range_trace

    return {
        "id": prior.id,
        "version": prior.version,
        "source": prior.source,
        "sha256": range_trace.tables_sha(),
        "width_pct": round(prior.width_pct, 2),
        "combo_count": prior.combo_count,
    }


def _rebuild_events(steps: list, board: str) -> tuple:
    """从 `steps[].event` 还原 ActionEvent 序列（契约 §3.3.2 是全字段）。

    记录里 step 只带 `street`、不带 `board`：按街取最终牌面的前 3/4/5 张，
    与实时链路 `field_ranges.build_events` 逐位一致。牌面按张切，十（`10s`）
    是三字符，不能按 `length*2` 截字符串。`spr_before=null` 是「拿不到筹码」的
    99 哨兵，还原成 99.0。
    """
    from poker_ocr.range_flow import ActionEvent

    tokens = _board_tokens(board)
    events = []
    for step in steps:
        event = step.get("event") or {}
        street = str(step.get("street") or "")
        length = _BOARD_LEN.get(street, 0)
        spr = event.get("spr_before")
        events.append(ActionEvent(
            street=street,
            board="".join(tokens[:length]),
            seat_id=str(event.get("actor") or ""),
            action=str(event.get("action") or ""),
            all_in=bool(event.get("all_in")),
            amount=event.get("amount"),
            facing_bet=bool(event.get("facing")),
            raises_before=int(event.get("raises_before") or 0),
            n_callers=int(event.get("n_callers") or 0),
            players_behind=int(event.get("players_behind") or 0),
            is_first_to_act=bool(event.get("is_first_to_act", True)),
            is_last_to_act=bool(event.get("is_last_to_act", True)),
            closes_action=bool(event.get("closes_action")),
            prev_street_aggressor=event.get("prev_aggressor"),
            is_donk=bool(event.get("is_donk")),
            spr=99.0 if spr is None else float(spr),
            pot=float(event.get("pot_before") or 0.0),
        ))
    return tuple(events)


def _final_fields(summary: dict) -> dict:
    keys = ("width_pct", "weighted_width", "effective_width_pct", "peak",
            "rho", "combo_count", "dropped")
    return {key: summary.get(key) for key in keys}


def _top_combos(built, limit: int = _TOP_POOL) -> list[dict]:
    """final 支撑集里权重最高的 combo 池（父进程用它算 A/B 位移）。"""
    from poker_ocr import ranges

    rows = sorted(
        ((ranges.combo_text(combo), float(weight))
         for combo, weight in built.combo_weights.items() if weight > 0),
        key=lambda item: (-item[1], item[0]))
    return [{"c": text, "w": round(weight, 6)}
            for text, weight in rows[:max(int(limit), 1)]]


def _step_top(step: dict, limit: int = _TOP_STEP) -> list[dict]:
    """一步里变动最大的 combo 及其**本步之后**的份额（w1）。

    逐步宽度（支撑集大小）在换表时几乎不变：乘数永远 > 0，只有权重在动。
    所以「从哪一步开始分叉」只能看份额；这里取 trace 里按 |dw| 排好的
    top 行，父进程按 combo 取并集比 w1。
    """
    rows = []
    for row in step.get("top") or ():
        if "others" in row or not row.get("c"):
            continue
        weight = row.get("w1")
        if weight is None:
            continue
        rows.append({"c": str(row["c"]), "w": round(float(weight), 6)})
        if len(rows) >= max(int(limit), 1):
            break
    return rows


def _rebuild_seat(seat: dict, board: str, dead: tuple) -> dict:
    """一个座位：先验 → 事件链 → final，全部复用实时同一套 build 函数。

    刻意不传记录里的 `params.alpha_in` / `params.support_floor`：规格 C 要求
    两侧「唯一变量是表 profile」。实时链路（hand_vs_ranges）也不传这两个，
    让 range_flow 用当前 profile 的默认值；回放记录里的旧值会把两版都锁死
    在历史参数上，换表差异反而看不出来。
    """
    from poker_ocr import board_range, range_flow, ranges

    prior_id = str(((seat.get("prior") or {}).get("id")) or "")
    out = {
        "seat": seat.get("seat"),
        "pos": seat.get("pos"),
        "archetype": seat.get("archetype") or "unknown",
        "path": seat.get("path") or "",
        "mode": seat.get("mode") or "call",
        "bucket": seat.get("bucket") or "",
        "prior": {},
        "alpha_in": None,
        "alpha_eff": None,
        "steps": [],
        "final": {},
        "top": [],
        "error": None,
    }
    if not prior_id:
        out["error"] = "记录缺少先验 id（seats[].prior.id），无法重建"
        return out
    prior = ranges.get(prior_id)
    if prior is None:
        out["error"] = (f"先验 {prior_id} 在当前注册表里取不到"
                        f"（ranges.get 返回 None）")
        return out
    out["prior"] = _prior_identity(prior)

    steps = list(seat.get("steps") or ())
    if out["path"] == "board_range" or not steps:
        # 旧口径：一条街一次过滤，没有逐步留痕。两侧都调同一个函数，
        # 唯一变量仍是表 profile（board_range 不读 range_flow 表，通常无差）。
        built = board_range.build(board, out["mode"], prior)
        if built is None:
            out["error"] = (f"board_range.build 返回空"
                            f"（mode={out['mode']}, prior={prior_id}）")
            return out
        out["final"] = _final_fields(range_flow.final_summary(built))
        out["top"] = _top_combos(built)
        return out

    events = _rebuild_events(steps, board)
    built, trail = range_flow.build_range_traced(
        prior, events, archetype=out["archetype"], dead=dead)
    if built is None or not trail:
        out["error"] = (f"build_range_traced 返回空"
                        f"（{len(events)} 个事件，prior={prior_id}）")
        return out
    params = trail.get("params") or {}
    out["alpha_in"] = params.get("alpha_in")
    out["alpha_eff"] = params.get("alpha_eff")
    step_rows = []
    for index, step in enumerate(trail.get("steps") or ()):
        position = step.get("i")
        try:
            label = events[int(position)].label()
        except (IndexError, TypeError, ValueError):
            label = f"{step.get('street')}:{((step.get('event') or {}).get('action'))}"
        step_rows.append({
            "i": position,
            "street": step.get("street"),
            "label": label,
            "width_before": step.get("width_before"),
            "width_after": step.get("width_after"),
            "top": _step_top(step),
        })
    out["steps"] = step_rows
    out["final"] = _final_fields(trail.get("final") or {})
    out["top"] = _top_combos(built)
    return out


def rebuild_record(record: dict) -> dict:
    """一条 trace 记录 → 规范化重算载荷（单 profile，子进程内调用）。

    记录里的 `params`（alpha_in / support_floor）不参与重建：两侧都走当前
    profile 的默认值，唯一变量是表 profile（规格 C）。
    """
    board = str(record.get("board") or "")
    dead = _cards(record.get("dead") or ())
    seats = [_rebuild_seat(seat, board, dead)
             for seat in record.get("seats") or ()]
    return {
        "hand": record.get("hand"),
        "street": record.get("street"),
        "actions": _actions_text(record),
        "board": board,
        "seats": seats,
        "error": None,
    }


def _worker(args) -> int:
    """子进程：按当前 profile 重算，把规范化 JSON 写到 --out。"""
    try:
        loaded = load_records(
            args.file, hand=args.hand, street=args.street,
            decision_key=args.decision_key, lenient=args.lenient)
        payload = {
            "tables": _tables_identity(),
            "records": [rebuild_record(record) for record in loaded["records"]],
            "bad_lines": loaded["bad_lines"],
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 —— 子进程只报诊断，不抛裸栈
        payload = {
            "tables": {},
            "records": [],
            "bad_lines": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    text = json.dumps(payload, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


# ─────────────────────────── 纯函数：比较 ───────────────────────────

def _identity(tables: dict) -> str:
    return (f"{tables.get('profile') or '?'}/"
            f"{tables.get('variant') or '?'}")


def _record_payloads(payload: dict) -> dict:
    """worker 载荷 → {记录键: 记录载荷}，并把表身份挂到每条记录上。"""
    tables = (payload or {}).get("tables") or {}
    out: dict[tuple, dict] = {}
    for record in (payload or {}).get("records") or ():
        item = dict(record)
        item["tables"] = tables
        out[_record_key(record)] = item
    return out


def _seat_map(payload: dict) -> dict:
    return {str(seat.get("seat")): seat for seat in (payload or {}).get("seats") or ()}


def _compare_seat(seat_id: str, seat_a: dict | None, seat_b: dict | None,
                  meta_seat: dict | None, *, top_n: int) -> tuple[dict, list, list]:
    """一个座位的 A/B 对比 → (seats[] 项, reasons, errors)。"""
    reasons: list[str] = []
    errors: list[str] = []
    meta_seat = meta_seat or {}
    recorded_prior = meta_seat.get("prior") or {}
    prior_a = (seat_a or {}).get("prior") or {}
    prior_b = (seat_b or {}).get("prior") or {}

    for tag, prior in (("A", prior_a), ("B", prior_b)):
        if not prior:
            continue
        for field in ("id", "version", "source"):
            if recorded_prior.get(field) is None:
                continue
            if prior.get(field) != recorded_prior.get(field):
                reasons.append(
                    f"座位 {seat_id} 先验 {recorded_prior.get('id')} 的 {field} "
                    f"漂移：记录 {recorded_prior.get(field)!r} → "
                    f"重算 {prior.get(field)!r}（{tag} 版）")
    for tag, seat in (("A", seat_a), ("B", seat_b)):
        if seat and seat.get("error"):
            errors.append(f"座位 {seat_id}（{tag} 版）重建失败：{seat['error']}")
        if seat is None:
            errors.append(f"座位 {seat_id} 只在一侧（{tag} 版）出现")
    if (seat_a is None) != (seat_b is None):
        ok_tag = "B" if seat_a is None else "A"
        errors.append(f"座位 {seat_id} 只有 {ok_tag} 版重建成功，另一侧缺失或失败")
    elif (bool((seat_a or {}).get("error"))
          != bool((seat_b or {}).get("error"))):
        ok_tag = "A" if (seat_a or {}).get("error") else "B"
        errors.append(f"座位 {seat_id} 只有 {ok_tag} 版重建成功")
    if seat_a is None and seat_b is None:
        errors.append(f"座位 {seat_id} 在两版里都没有重算结果")

    steps_a = list((seat_a or {}).get("steps") or ())
    steps_b = list((seat_b or {}).get("steps") or ())
    steps = []
    first = None
    for index in range(max(len(steps_a), len(steps_b))):
        step_a = steps_a[index] if index < len(steps_a) else None
        step_b = steps_b[index] if index < len(steps_b) else None
        base = step_a or step_b or {}
        shares_a = _step_shares(step_a)
        shares_b = _step_shares(step_b)
        share_delta = max(
            (abs(shares_b.get(text, 0.0) - shares_a.get(text, 0.0))
             for text in set(shares_a) | set(shares_b)), default=0.0)
        # 宽度（支撑集大小）换表时基本恒定（乘数永远 >0），真正会动的是份额；
        # 两者任一不同都算这一步分叉。
        divergent = (
            step_a is None or step_b is None
            or _differs(step_a.get("width_before"), step_b.get("width_before"))
            or _differs(step_a.get("width_after"), step_b.get("width_after"))
            or share_delta > _EPS
        )
        after_a = (step_a or {}).get("width_after")
        after_b = (step_b or {}).get("width_after")
        delta = None
        if after_a is not None and after_b is not None:
            delta = round(float(after_b) - float(after_a), 4)
        marker = bool(divergent and first is None)
        if marker:
            first = base.get("i", index)
        steps.append({
            "i": base.get("i", index),
            "street": base.get("street"),
            "label": base.get("label"),
            "width_before_a": (step_a or {}).get("width_before"),
            "width_before_b": (step_b or {}).get("width_before"),
            "width_after_a": after_a,
            "width_after_b": after_b,
            "delta": delta,
            "share_delta": round(share_delta, 6),
            "divergent": bool(divergent),
            "first_divergence": marker,
        })

    final_a = dict((seat_a or {}).get("final") or {})
    final_b = dict((seat_b or {}).get("final") or {})
    delta = {}
    final_divergent = False
    for field in ("width_pct", "weighted_width", "effective_width_pct",
                  "peak", "combo_count"):
        value_a = final_a.get(field)
        value_b = final_b.get(field)
        if _differs(value_a, value_b):
            final_divergent = True
        try:
            delta[field] = round(float(value_b) - float(value_a), 6)
        except (TypeError, ValueError):
            delta[field] = None
    dropped_a = final_a.get("dropped") or {}
    dropped_b = final_b.get("dropped") or {}
    dropped_divergent = any(
        _differs(dropped_a.get(key), dropped_b.get(key))
        for key in ("dead", "zero", "floor"))

    pool: dict[str, list[float]] = {}
    for row in (seat_a or {}).get("top") or ():
        pool.setdefault(str(row.get("c")), [0.0, 0.0])[0] = float(
            row.get("w") or 0.0)
    for row in (seat_b or {}).get("top") or ():
        pool.setdefault(str(row.get("c")), [0.0, 0.0])[1] = float(
            row.get("w") or 0.0)
    moves = [
        {"c": text, "a": round(weights[0], 6), "b": round(weights[1], 6),
         "delta": round(weights[1] - weights[0], 6)}
        for text, weights in pool.items()
        if _differs(weights[0], weights[1])
    ]
    moves.sort(key=lambda row: (-abs(row["delta"]), row["c"]))
    moves = moves[:max(int(top_n), 1)]

    divergent = bool(first is not None or final_divergent
                     or dropped_divergent or moves)
    entry = {
        "seat": seat_id,
        "pos": (seat_a or seat_b or {}).get("pos"),
        "archetype": (seat_a or seat_b or {}).get("archetype"),
        "path": (seat_a or seat_b or {}).get("path"),
        "mode": (seat_a or seat_b or {}).get("mode"),
        "bucket": (seat_a or seat_b or {}).get("bucket"),
        "prior": {"a": prior_a, "b": prior_b,
                  "same": bool(prior_a) and prior_a == prior_b},
        "recorded_prior": recorded_prior,
        "steps": steps,
        "first_divergence": first,
        "final_divergent": bool(final_divergent or dropped_divergent),
        "final": {"a": final_a, "b": final_b, "delta": delta},
        "dropped": {"a": dropped_a, "b": dropped_b},
        "top_combo_moves": moves,
        "divergent": divergent,
    }
    return entry, reasons, errors


def compare(a_payload: dict, b_payload: dict, record_meta: dict, *,
            top_n: int = _TOP_MOVES) -> dict:
    """一条记录的两版对比 → `records[]` 的一项（纯函数：不 spawn、不读文件）。

    `a_payload` / `b_payload` 是子进程对**同一条记录**的输出
    （`{tables, seats[], error}`）；`record_meta` 是原始 trace 记录（至少含
    `params` / `seats` / `board` / `decision_key`）。任何一侧缺失或重建失败
    都只写进 `reasons` / `errors`，**不抛异常**。
    """
    reasons: list[str] = []
    errors: list[str] = []
    tables_a = (a_payload or {}).get("tables") or {}
    tables_b = (b_payload or {}).get("tables") or {}
    recorded_tables = ((record_meta.get("params") or {}).get("tables")) or {}

    recorded_sha = str(recorded_tables.get("sha256") or "")
    known_sha = {str(tables_a.get("sha256") or ""),
                 str(tables_b.get("sha256") or "")} - {""}
    if not recorded_sha:
        reasons.append("记录缺少表指纹（params.tables.sha256），"
                       "无法确认是哪套表算出来的")
    elif known_sha and recorded_sha not in known_sha:
        reasons.append(
            f"记录表指纹 {recorded_sha} 与本次两版"
            f"（{'、'.join(sorted(known_sha))}）都不一致：表已变更")
    recorded_ident = _identity(recorded_tables)
    known_ident = {_identity(tables_a), _identity(tables_b)}
    if recorded_ident not in known_ident:
        reasons.append(
            f"记录由 {recorded_ident} 产出，不在本次两版"
            f"（{'、'.join(sorted(known_ident))}）之内")

    for tag, payload in (("A", a_payload), ("B", b_payload)):
        if payload is None:
            errors.append(f"{tag} 版没有这条记录的重算结果")
        elif payload.get("error"):
            errors.append(f"{tag} 版记录级重建失败：{payload['error']}")

    seats_a = _seat_map(a_payload)
    seats_b = _seat_map(b_payload)
    meta_seats = {str(seat.get("seat")): seat
                  for seat in record_meta.get("seats") or ()}
    order = list(meta_seats) or list(seats_a) or list(seats_b)
    for seat_id in list(seats_a) + list(seats_b):
        if seat_id not in order:
            order.append(seat_id)

    seats = []
    for seat_id in order:
        entry, seat_reasons, seat_errors = _compare_seat(
            seat_id, seats_a.get(seat_id), seats_b.get(seat_id),
            meta_seats.get(seat_id), top_n=top_n)
        seats.append(entry)
        reasons.extend(seat_reasons)
        errors.extend(seat_errors)

    return {
        "hand": record_meta.get("hand"),
        "street": record_meta.get("street"),
        "decision_key": record_meta.get("decision_key") or {},
        "board": record_meta.get("board") or "",
        "ts": record_meta.get("ts"),
        "session": record_meta.get("session"),
        "params": record_meta.get("params") or {},
        "reproducible": not reasons,
        "reasons": reasons,
        "divergent": any(seat["divergent"] for seat in seats),
        "errors": errors,
        "seats": seats,
    }


def summarize(records: list[dict]) -> dict:
    """records[] → 顶层 summary（记录数 / 座位数 / 分歧数 / 最大有效宽度差）。"""
    seats = [seat for record in records for seat in record.get("seats") or ()]
    worst = None
    worst_delta = 0.0
    for record in records:
        for seat in record.get("seats") or ():
            value = ((seat.get("final") or {}).get("delta") or {}).get(
                "effective_width_pct")
            if value is None:
                continue
            if worst is None or abs(float(value)) > abs(worst_delta):
                worst_delta = float(value)
                worst = {
                    "hand": record.get("hand"),
                    "street": record.get("street"),
                    "seat": seat.get("seat"),
                    "effective_width_delta": round(worst_delta, 4),
                }
    return {
        "records": len(records),
        "seats": len(seats),
        "divergent_records": sum(1 for r in records if r.get("divergent")),
        "divergent_seats": sum(1 for s in seats if s.get("divergent")),
        "max_effective_width_delta": round(abs(worst_delta), 4),
        "top_divergence": worst,
        "reproducible": all(record.get("reproducible") for record in records),
        "rebuild_failures": sum(1 for r in records if r.get("errors")),
    }


def _stub(payload: dict | None, key: tuple) -> dict:
    """worker 载荷里没有这条记录时的占位：把 worker 级错误带到记录级诊断里。"""
    return {
        "hand": key[0], "street": key[1], "actions": key[2], "board": "",
        "seats": [], "tables": (payload or {}).get("tables") or {},
        "error": ((payload or {}).get("error")
                  or f"没有这条记录（hand={key[0]} street={key[1]} "
                     f"actions={key[2] or '-'}）"),
    }


def build_report(a_payload: dict, b_payload: dict, metas: list[dict], *,
                 source: dict, filters: dict,
                 top_n: int = _TOP_MOVES) -> dict:
    """两个 worker 载荷 + 记录元数据 → 完整报告（纯函数）。"""
    records_a = _record_payloads(a_payload)
    records_b = _record_payloads(b_payload)
    records = []
    for meta in metas:
        key = _record_key(meta)
        records.append(compare(records_a.get(key) or _stub(a_payload, key),
                               records_b.get(key) or _stub(b_payload, key),
                               meta, top_n=top_n))
    return {
        "source": source,
        "filters": filters,
        "tables": {
            "a": (a_payload or {}).get("tables") or {},
            "b": (b_payload or {}).get("tables") or {},
        },
        "records": records,
        "summary": summarize(records),
    }


def exit_code(report: dict) -> int:
    """0 无差异；2 检出差异；1 有重建失败（报告已经打出来了）。"""
    summary = report.get("summary") or {}
    if summary.get("rebuild_failures"):
        return 1
    if summary.get("divergent_records"):
        return 2
    return 0


# ─────────────────────────── 文本报告 ───────────────────────────

def _num(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def render(report: dict) -> str:
    summary = report.get("summary") or {}
    tables_a = (report.get("tables") or {}).get("a") or {}
    tables_b = (report.get("tables") or {}).get("b") or {}
    source = report.get("source") or {}
    lines = [
        f"source: {source.get('file')}  "
        f"（读 {source.get('lines')} 行，ok {source.get('status_ok')} 条，"
        f"选中 {summary.get('records')} 条决策点 / {summary.get('seats')} 个座位）",
        f"A: {_identity(tables_a)}  sha {tables_a.get('sha256') or '-'}",
        f"B: {_identity(tables_b)}  sha {tables_b.get('sha256') or '-'}",
        f"分歧: {summary.get('divergent_records')} 条记录 / "
        f"{summary.get('divergent_seats')} 个座位；"
        f"最大有效宽度差 {_num(summary.get('max_effective_width_delta'), 4)}pt",
    ]
    worst = summary.get("top_divergence")
    if worst and summary.get("divergent_seats"):
        lines.append(
            f"  最大一处: hand {worst.get('hand')} {worst.get('street')} "
            f"seat {worst.get('seat')} Δ"
            f"{_num(worst.get('effective_width_delta'), 4)}pt")
    for record in report.get("records") or ():
        head = (f"\nhand {record.get('hand')} {record.get('street')} "
                f"[{record.get('board') or '-'}]")
        if not record.get("reproducible"):
            head += "  reproducible=false"
        if record.get("divergent"):
            head += "  ≠"
        lines.append(head)
        for reason in record.get("reasons") or ():
            lines.append(f"  ! {reason}")
        for error in record.get("errors") or ():
            lines.append(f"  ✗ {error}")
        for seat in record.get("seats") or ():
            prior = ((seat.get("prior") or {}).get("a") or {})
            recorded = seat.get("recorded_prior") or {}
            if prior:
                prior_line = (f"先验 {prior.get('id') or '-'}"
                              f"（v{prior.get('version')}，"
                              f"{prior.get('source') or '-'}）")
            else:
                prior_line = (f"先验 {recorded.get('id') or '-'}（记录值，"
                              f"重算缺失）")
            lines.append(
                f"  seat {seat.get('seat')} {seat.get('pos') or '-'} "
                f"[{seat.get('path') or '-'}] {prior_line}")
            if seat.get("first_divergence") is None and seat.get("final_divergent"):
                lines.append("    （逐步宽度/份额一致，分歧只出现在 final 剪枝/权重上）")
            for step in seat.get("steps") or ():
                mark = "  ← 首个分歧" if step.get("first_divergence") else ""
                share = ""
                if (step.get("share_delta") or 0) > 0:
                    share = f"  份额Δ{_num(step.get('share_delta'), 4)}"
                lines.append(
                    f"    {str(step.get('i')):>2} {str(step.get('label')):<18}"
                    f" A {_num(step.get('width_before_a'))} → "
                    f"{_num(step.get('width_after_a'))}"
                    f" | B {_num(step.get('width_before_b'))} → "
                    f"{_num(step.get('width_after_b'))}"
                    f"  Δ{_num(step.get('delta'), 4)}{share}{mark}")
            final = seat.get("final") or {}
            final_a = final.get("a") or {}
            final_b = final.get("b") or {}
            lines.append(
                f"    final A width {_num(final_a.get('width_pct'))}"
                f" eff {_num(final_a.get('effective_width_pct'))}"
                f" peak {_num(final_a.get('peak'), 6)}"
                f" combos {final_a.get('combo_count')}"
                f" | B width {_num(final_b.get('width_pct'))}"
                f" eff {_num(final_b.get('effective_width_pct'))}"
                f" peak {_num(final_b.get('peak'), 6)}"
                f" combos {final_b.get('combo_count')}")
            moves = seat.get("top_combo_moves") or ()
            if moves:
                lines.append("    top combo 位移: " + "，".join(
                    f"{row['c']} {row['delta']:+.4f}" for row in moves))
    return "\n".join(lines)


# ─────────────────────────── CLI ───────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="范围推导换表重算 + diff（两版 range_flow 表对照，只读）")
    ap.add_argument("--file", type=Path, default=None,
                    help="trace 文件（默认当天 logs/live.range.jsonl.YYYYMMDD）")
    ap.add_argument("--hand", type=int, default=None, help="只算这一手")
    ap.add_argument("--street", default=None,
                    help="只算这条街（preflop/flop/turn/river）")
    ap.add_argument("--decision-key", dest="decision_key", default=None,
                    help="决策点：flop / flop:bet:6 / 104:flop:bet:6 / flop:2"
                         "（最后一种是 token 个数）")
    ap.add_argument("--profile-a", default=PROFILE_A, help=f"A 版表（默认 {PROFILE_A}）")
    ap.add_argument("--profile-b", default=PROFILE_B, help=f"B 版表（默认 {PROFILE_B}）")
    ap.add_argument("--top", type=int, default=_TOP_MOVES,
                    help=f"top combo 位移条数（默认 {_TOP_MOVES}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--lenient", action="store_true",
                    help="跳过坏行继续（默认坏行即输入失败）")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--profile", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def _spawn(*, profile: str, source: Path, hand, street, decision_key,
           lenient: bool, out: Path) -> dict:
    """起一个子进程按 `profile` 重算（环境变量必须在 import 前生效）。"""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
           "--profile", profile, "--file", str(source), "--out", str(out)]
    if hand is not None:
        cmd += ["--hand", str(hand)]
    if street:
        cmd += ["--street", street]
    if decision_key:
        cmd += ["--decision-key", decision_key]
    if lenient:
        cmd += ["--lenient"]
    env = dict(os.environ, STRATEGY_TABLES_PROFILE=profile)
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{profile} 版子进程 120s 没跑完"
                           "（trace 文件太大或这手特别重）")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(
            f"{profile} 版子进程失败（exit {proc.returncode}）：{tail}")
    if not out.is_file():
        raise RuntimeError(f"{profile} 版子进程没有写出结果文件 {out}")
    return json.loads(out.read_text(encoding="utf-8"))


def _filters(args) -> dict:
    """`--decision-key` 里可能携带 hand/street，报告要把实际生效的过滤条件写全。"""
    try:
        key_hand, key_street, _actions = _parse_decision_key(args.decision_key)
    except ValueError:
        key_hand, key_street = None, None
    return {"hand": args.hand if args.hand is not None else key_hand,
            "street": args.street or key_street,
            "decision_key": args.decision_key}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.worker:
        return _worker(args)

    # 子进程的 cwd 固定成仓库根，所以相对路径必须在父进程里先解析成绝对路径，
    # 否则子进程按仓库根找不到文件、报告会假称「重建失败」（审查 P1）。
    try:
        source = (Path(args.file).expanduser().resolve() if args.file
                  else default_source())
        loaded = load_records(source, hand=args.hand, street=args.street,
                              decision_key=args.decision_key,
                              lenient=args.lenient)
    except (OSError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    if loaded["bad_lines"]:
        print(f"❌ {source} 有 {len(loaded['bad_lines'])} 行不是合法 JSON"
              f"（首个第 {loaded['bad_lines'][0]} 行）；"
              f"确认文件完好，或用 --lenient 跳过", file=sys.stderr)
        return 1
    if not loaded["records"]:
        print(f"❌ {source} 没有可重算的记录"
              f"（读 {loaded['lines']} 行，status=ok 共 {loaded['status_ok']} 条）；"
              f"检查 --hand / --street / --decision-key 过滤条件", file=sys.stderr)
        return 1

    try:
        with tempfile.TemporaryDirectory(prefix="range-diff-") as tmp:
            a_payload = _spawn(
                profile=args.profile_a, source=source, hand=args.hand,
                street=args.street, decision_key=args.decision_key,
                lenient=args.lenient, out=Path(tmp) / "a.json")
            b_payload = _spawn(
                profile=args.profile_b, source=source, hand=args.hand,
                street=args.street, decision_key=args.decision_key,
                lenient=args.lenient, out=Path(tmp) / "b.json")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"❌ 重算失败：{exc}", file=sys.stderr)
        return 1

    report = build_report(
        a_payload, b_payload, loaded["records"],
        source={"file": loaded["file"], "lines": loaded["lines"],
                "status_ok": loaded["status_ok"],
                "bad_lines": loaded["bad_lines"]},
        filters=_filters(args),
        top_n=max(int(args.top), 1))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
