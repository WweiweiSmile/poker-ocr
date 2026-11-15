"""范围推导 trace 的落盘：记录组装、体积降级、按天轮转。

口径来自两份文档：

  · `docs/范围推导-trace-字段契约-v1.md`（一行一个 JSON 对象）；
  · `docs/范围推导-trace-采集与落盘-规格.md`（预算与轮转）。

三条硬约束：

  · **fail open**：任何异常都不许冒出 live 主循环，最多把 status 改成
    `write_failed`，AI 建议照常出；
  · **体积**：单条软上限 110 KB、硬顶 180 KB。超了按阶梯收缩 detail
    （400 → 200 → 100 → 空 → 再丢中间步 grid），每收缩一档把步号写进
    `detail_omitted`，绝不静默少写；
  · **轮转**：本地按天切 `.YYYYMMDD`，留 14 天、总量 1 GB、当天 200 MB 停写。

为什么不用 logging：这里的行是**数据**不是日志（下游要按 decision_key join、
要按版本前缀过滤），必须能整体 `json.loads`，且写盘失败不能让 live 掉帧。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from . import range_flow, strategy_tables
from .prompt import PROMPT_VERSION

# ─────────────────────────── 常量 ───────────────────────────

VERSION = "live.range.v1.2"
DEFAULT_PATH = "logs/live.range.jsonl"

# 单条记录的体积闸：软上限触发降级，硬顶保证「400 条/天 × 14 天 < 1 GB」。
SOFT_LIMIT = 110 * 1024
HARD_LIMIT = 180 * 1024

# 轮转与配额。
KEEP_DAYS = 14
TOTAL_LIMIT = 1024 ** 3
DAY_LIMIT = 200 * 1024 ** 2

# 行小于 PIPE_BUF 时一次 write 就是原子的；大的行才需要 fsync 兜底。
SMALL_WRITE = 4096

STATUS_ON = "on"
STATUS_OFF = "off"
STATUS_BUDGET = "budget_exceeded"
STATUS_WRITE_FAIL = "write_failed"
# 单条记录超硬顶：先剥到骨架照写（记录带 trace_truncated），剥完还超才丢。
# 这个状态不闸后续写入，只是让 /api/meta 能看出「最近有记录被截断/丢弃」。
STATUS_TOO_BIG = "record_too_big"

# 降级阶梯：(detail 上限, top 每侧上限, 是否保留每步 grid/cell_n)。
LEVELS: tuple[tuple[int, int, bool], ...] = (
    (range_flow.TRACE_DETAIL_MAX, 10, True),   # 400 / 10 / 全量
    (200, 10, True),
    (100, 10, True),
    (0, 10, True),
    (0, 5, False),
)

_TABLES_PATH = Path(__file__).resolve().parent / "tables" / "range_flow.json"
_sha_cache: dict[tuple[str, float], str] = {}


# ─────────────────────────── 记录组装 ───────────────────────────


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def tables_sha() -> str:
    """策略表的指纹：规范化 JSON 的 sha256 前 16 位（票 6 规格）。

    表换了指纹就变，读端据此判断「这条 trace 是哪套数算出来的」——
    和重算无关（重算一定对不上，见契约 §3.3.4）。
    """
    try:
        stat = _TABLES_PATH.stat()
    except OSError:
        return ""
    key = (str(_TABLES_PATH), stat.st_mtime)
    cached = _sha_cache.get(key)
    if cached is not None:
        return cached
    try:
        raw = json.loads(_TABLES_PATH.read_text(encoding="utf-8"))
        canon = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    except (OSError, ValueError):
        return ""
    _sha_cache[key] = digest
    return digest


def advice_version() -> str:
    """与 `replay-console/replay/versioning.py` 同一条派生规则。

    那边从 `PROMPT_VERSION` 派生 `CURRENT_ADVICE_VERSION`，这里算同一个值：
    trace 与沉淀层对「建议版本」必须用同一个字符串，否则 join 不上。
    """
    return "advice-" + PROMPT_VERSION.removeprefix("lean-zh-")


def params_payload(trace_params: dict | None = None,
                   profile: str | None = None) -> dict:
    """契约 §3.2 的 `params`。逐 seat 的 α 差异看每步 `alpha_k`。"""
    trail = trace_params or {}
    name = profile or strategy_tables.profile()
    return {
        "alpha_in": trail.get("alpha_in"),
        "alpha_eff": trail.get("alpha_eff"),
        "n_events": trail.get("n_events"),
        "n_applied": trail.get("n_applied"),
        "support_floor": trail.get("support_floor"),
        "min_likelihood": trail.get("min_likelihood"),
        "event_model": range_flow.enabled(),
        "tables": {
            "profile": name,
            "variant": strategy_tables.variant_of(name),
            "sha256": tables_sha(),
        },
        "prompt_version": PROMPT_VERSION,
        "advice_version": advice_version(),
    }


def _spr(snap) -> tuple[float | None, str]:
    """`stack / pot`；拿不到筹码就是 null，**不写 99 这类假值**。"""
    stack = snap.hero_stack
    pot = snap.pot
    try:
        if stack is None or pot is None or float(pot) <= 0:
            return None, "unavailable"
        return round(float(stack) / float(pot), 2), "snapshot"
    except (TypeError, ValueError):
        return None, "unavailable"


def _dead_cards(snap) -> list[str]:
    cards = {str(card) for card in tuple(snap.hero_cards or ()) + tuple(snap.board or ())}
    return sorted(cards)


def _decision_key(snap) -> dict:
    key = tuple(snap.decision_key or ())
    return {
        "hand": str(snap.hand_index or 0),
        "street": snap.street,
        "actions": key[2] if len(key) > 2 else None,
        "pot": None if snap.pot is None else round(float(snap.pot), 3),
        "to_call": round(float(snap.to_call or 0.0), 3),
        "hero": sorted(str(card) for card in snap.hero_cards or ()),
    }


def _hero(snap) -> dict:
    spr, spr_source = _spr(snap)
    return {
        "pos": snap.hero_position,
        "stack": None if snap.hero_stack is None else round(float(snap.hero_stack), 3),
        "pot": None if snap.pot is None else round(float(snap.pot), 3),
        "to_call": round(float(snap.to_call or 0.0), 3),
        "spr": spr,
        "spr_source": spr_source,
    }


def build_record(snap, seats: list[dict], *, params: dict | None = None,
                 session: str = "", equity: dict | None = None,
                 warnings: Iterable[str] = (), omitted: int = 0) -> dict:
    """契约 §3.1 的 `status:"ok"` 记录。`seats` 由 `hand_vs_ranges` 组装。"""
    return {
        "v": VERSION,
        "status": "ok",
        "ts": _now(),
        "session": session or _session,
        "hand": int(snap.hand_index or 0),
        "hand_name": snap.hand_id,
        "street": snap.street,
        "decision_key": _decision_key(snap),
        "board": "".join(str(card) for card in snap.board or ()),
        "dead": _dead_cards(snap),
        "hero": _hero(snap),
        "params": params or params_payload(),
        "seats": seats,
        "omitted": int(omitted),
        "hero_equity": equity,
        "warnings": list(warnings),
    }


def build_skipped(snap, reason: str, *, session: str = "",
                  warnings: Iterable[str] = ()) -> dict:
    """`status:"skipped"`：只带 reason，不带 seats（契约 §2）。

    翻前**不写**这一类记录（grilling 1A）：翻前根本没跑范围计算，硬造一条
    「只有先验」的记录会让看板分不清「算了但很宽」和「没算」。
    """
    record = {
        "v": VERSION,
        "status": "skipped",
        "ts": _now(),
        "session": session or _session,
        "reason": reason,
        "warnings": list(warnings),
    }
    if snap is not None:
        record.update({
            "hand": int(snap.hand_index or 0),
            "hand_name": snap.hand_id,
            "street": snap.street,
            "decision_key": _decision_key(snap),
            "board": "".join(str(card) for card in snap.board or ()),
            "dead": _dead_cards(snap),
            "hero": _hero(snap),
        })
    return record


# ─────────────────────────── 体积降级 ───────────────────────────


def _encode(record: dict) -> bytes:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return (line + "\n").encode("utf-8")


def _cap_top(top: list, cap: int) -> list:
    """top 存 lost 10 + gained 10 + 一条 others；降级时每侧截到 cap。"""
    if not top or cap <= 0:
        return top or []
    rows = [row for row in top if "others" not in row]
    sentinels = [row for row in top if "others" in row]
    if len(rows) <= cap:
        return top
    keep, dropped = rows[:cap], rows[cap:]
    if sentinels:
        sentinel = copy.deepcopy(sentinels[0])
        others = sentinel.setdefault("others", {})
        others["n"] = int(others.get("n") or 0) + len(dropped)
        others["sum_dw"] = round(float(others.get("sum_dw") or 0.0)
                                 + sum(float(row.get("dw") or 0.0) for row in dropped), 6)
        sentinels = [sentinel]
    else:
        sentinels = [{"others": {
            "n": len(dropped),
            "sum_dw": round(sum(float(row.get("dw") or 0.0) for row in dropped), 6),
        }}]
    return keep + sentinels


def _shrink(record: dict, detail_cap: int, top_cap: int, keep_grid: bool) -> dict:
    """按一档参数收缩记录；返回**新**记录（不改原对象）。"""
    out = dict(record)
    seats = []
    touched: set[int] = set()
    for seat_index, seat in enumerate(record.get("seats") or ()):
        seat = dict(seat)
        steps = []
        omitted = list(seat.get("detail_omitted") or ())
        raw_steps = list(seat.get("steps") or ())
        for step_index, step in enumerate(raw_steps):
            step = dict(step)
            detail = list(step.get("detail") or ())
            if detail_cap <= 0 and detail:
                step["detail"] = []
                if not step.get("skipped"):
                    omitted.append(step.get("i"))
                    touched.add(seat_index)
            elif detail_cap > 0 and len(detail) > detail_cap:
                step["detail"] = detail[:detail_cap]
                step["truncated"] = True
            if top_cap and step.get("top"):
                step["top"] = _cap_top(step["top"], top_cap)
            if not keep_grid and not step.get("skipped") and step_index < len(raw_steps) - 1:
                # 中间步的 169 格与 cell_n 是最大头；末步的 grid 必须留
                # （看板默认视图就靠它）。
                step["grid"] = None
                step["cell_n"] = None
            steps.append(step)
        seat["steps"] = steps
        if omitted:
            seat["detail_omitted"] = sorted({i for i in omitted if i is not None})
        seats.append(seat)
    out["seats"] = seats
    if touched:
        union: set[int] = set(out.get("detail_omitted") or ())
        for seat_index in touched:
            union |= set(seats[seat_index].get("detail_omitted") or ())
        out["detail_omitted"] = sorted(union)
    warnings = list(out.get("warnings") or ())
    dropped = any(not step.get("detail") and not step.get("skipped")
                  for seat in seats for step in seat.get("steps") or ())
    truncated = any(step.get("truncated") for seat in seats
                    for step in seat.get("steps") or ())
    if (dropped or truncated or not keep_grid) and "trace_truncated" not in warnings:
        warnings.append("trace_truncated")
    out["warnings"] = warnings
    return out


def downgrade(record: dict) -> dict:
    """超软上限就按阶梯收缩，直到装进软上限；硬顶再兜一次底。"""
    if len(_encode(record)) <= SOFT_LIMIT:
        return record
    payload = record
    for detail_cap, top_cap, keep_grid in LEVELS[1:]:
        payload = _shrink(record, detail_cap, top_cap, keep_grid)
        if len(_encode(payload)) <= SOFT_LIMIT:
            return payload
    if len(_encode(payload)) > HARD_LIMIT:
        # 最后一道：detail 全清、每侧 top 只留 3、中间步 grid 也不要。
        payload = _shrink(payload, 0, 3, False)
    return payload


def _squeeze(record: dict) -> dict:
    """超硬顶的最后一道：剥到骨架（标签 + 宽度），保证记录仍能落盘。

    看板没有 grid/detail 时退化成只看宽度曲线；`trace_truncated` 与
    `detail_omitted` 让读者知道「不是没采到，是被截掉了」——规格要求超硬顶
    不许静默少写，这一档就是不静默的那一档。
    """
    out = dict(record)
    seats = []
    union: set[int] = set()
    for seat in record.get("seats") or ():
        seat = dict(seat)
        steps = []
        omitted: set[int] = set()
        for step in seat.get("steps") or ():
            step = dict(step)
            if step.get("skipped"):
                # 时间闸跳过的步本来没有 detail，不算「被截掉」。
                steps.append(step)
                continue
            omitted.add(step.get("i"))
            step["detail"] = []
            if step.get("top"):
                step["top"] = _cap_top(step["top"], 3)
            step["grid"] = None
            step["cell_n"] = None
            step["truncated"] = True
            steps.append(step)
        seat["steps"] = steps
        if omitted:
            seat["detail_omitted"] = sorted(i for i in omitted if i is not None)
            union |= omitted
        seats.append(seat)
    out["seats"] = seats
    if union:
        out["detail_omitted"] = sorted(i for i in union if i is not None)
    warnings = list(out.get("warnings") or ())
    if "trace_truncated" not in warnings:
        warnings.append("trace_truncated")
    out["warnings"] = warnings
    return out


# ─────────────────────────── 写盘 ───────────────────────────


class TraceWriter:
    """按天切分 + 配额 + fail open 的追加写入器。线程安全（live 有后台线程）。"""

    def __init__(self, path: str | Path = DEFAULT_PATH, *,
                 keep_days: int = KEEP_DAYS, total_limit: int = TOTAL_LIMIT,
                 day_limit: int = DAY_LIMIT) -> None:
        self.path = Path(path)
        self.dir = self.path.parent
        self.base = self.path.name
        self.keep_days = int(keep_days)
        self.total_limit = int(total_limit)
        self.day_limit = int(day_limit)
        self._lock = threading.Lock()
        self._day = ""
        self._day_bytes = 0
        self._status = STATUS_ON
        self.last_error = ""
        self.written = 0
        self.truncated = 0
        self.dropped = 0

    # —— 查询 ——
    @property
    def status(self) -> str:
        return self._status

    @property
    def current_path(self) -> Path:
        day = self._day or datetime.now().strftime("%Y%m%d")
        return self.dir / f"{self.base}.{day}"

    def stats(self) -> dict:
        return {
            "path": str(self.path),
            "current": str(self.current_path),
            "status": self._status,
            "written": self.written,
            "truncated": self.truncated,
            "dropped": self.dropped,
            "day_bytes": self._day_bytes,
            "last_error": self.last_error,
        }

    # —— 轮转 ——
    def _rotated_files(self) -> list[Path]:
        try:
            files = [p for p in self.dir.glob(f"{self.base}.*")
                     if p.is_file() and p.name[len(self.base) + 1:].isdigit()]
        except OSError:
            return []
        return sorted(files, key=lambda p: p.name)

    def _prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=self.keep_days)).strftime("%Y%m%d")
        for path in self._rotated_files():
            if path.name[len(self.base) + 1:] < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
        files = [p for p in self._rotated_files() if p.exists()]
        total = sum(p.stat().st_size for p in files if p.exists())
        for path in files:
            if total <= self.total_limit:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                break

    def _rotate(self, now: datetime) -> None:
        day = now.strftime("%Y%m%d")
        if day == self._day:
            return
        self._day = day
        self._prune(now)
        try:
            self._day_bytes = self.current_path.stat().st_size
        except OSError:
            self._day_bytes = 0

    # —— 写 ——
    def write(self, record: dict) -> bool:
        with self._lock:
            if self._status in (STATUS_OFF, STATUS_BUDGET, STATUS_WRITE_FAIL):
                return False
            now = datetime.now()
            try:
                self._rotate(now)
                data = _encode(downgrade(record))
                if len(data) > HARD_LIMIT:
                    # 硬顶：先剥到骨架照写（带 trace_truncated），剥完仍装不下
                    # 才丢。两种都要留痕，绝不静默少写。
                    original = len(_encode(record))
                    squeezed = _encode(_squeeze(record))
                    self._status = STATUS_TOO_BIG
                    if len(squeezed) > HARD_LIMIT:
                        self.dropped += 1
                        self.last_error = (
                            f"record {original} bytes over hard cap {HARD_LIMIT}; "
                            f"squeezed to {len(squeezed)} still too big, dropped")
                        return False
                    self.truncated += 1
                    self.last_error = (
                        f"record {original} bytes over hard cap {HARD_LIMIT}; "
                        f"written squeezed to {len(squeezed)} bytes")
                    data = squeezed
                if self._day_bytes + len(data) > self.day_limit:
                    self._status = STATUS_BUDGET
                    self.last_error = (f"daily cap {self.day_limit} bytes reached "
                                       f"({self._day_bytes} written)")
                    return False
                self.dir.mkdir(parents=True, exist_ok=True)
                with self.current_path.open("ab") as handle:
                    handle.write(data)
                    if len(data) >= SMALL_WRITE:
                        handle.flush()
                        os.fsync(handle.fileno())
                self._day_bytes += len(data)
                self.written += 1
                return True
            except OSError as exc:
                self._status = STATUS_WRITE_FAIL
                self.last_error = f"{type(exc).__name__}: {exc}"
                return False
            except Exception as exc:  # noqa: BLE001 —— fail open 是硬要求
                self._status = STATUS_WRITE_FAIL
                self.last_error = f"{type(exc).__name__}: {exc}"
                return False


# ─────────────────────────── 读回（看板只读） ───────────────────────────
#
# 看板的手牌详情弹窗按 decision_key 取一条记录：一条记录里已经带全部
# seats，所以切座位/切步不再请求后端。查询只读，不碰 writer 状态。

_STREET_ORDER = ("preflop", "flop", "turn", "river", "showdown")


def _trace_base(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    trace = _writer
    return Path(trace.path) if trace is not None else Path(DEFAULT_PATH)


def _trace_files(base: Path) -> list[Path]:
    """当日文件 + 轮转文件，按 mtime 倒序（新的先扫）。"""
    files: list[Path] = []
    try:
        if base.is_file():
            files.append(base)
        for candidate in base.parent.glob(base.name + ".*"):
            suffix = candidate.name[len(base.name) + 1:]
            if suffix.isdigit() and candidate.is_file():
                files.append(candidate)
    except OSError:
        return []

    def _mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=_mtime, reverse=True)


def _record_actions(record: dict) -> int | None:
    value = (record.get("decision_key") or {}).get("actions")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def records_for(hand: int, street: str | None = None, actions: int | None = None,
                *, path: str | Path | None = None, max_files: int = 3) -> list[dict]:
    """按 hand/street/actions 捞记录；同一 (street, actions) 只留 ts 最新的一条。

    `hand` 是那一局在某个 live 会话里的序号，跨会话会重号——有当前会话的记录
    就优先用当前会话的，找不到才回落到历史会话。`actions` 省略时返回该手该街
    的全部决策点，按街序 + 决策序号排列。

    最多扫 `max_files` 个最新文件：一手牌的所有决策点都在几分钟内、不会跨天，
    而单日文件可能到 200 MB，扫全部 14 天不值得。
    """
    try:
        wanted = int(hand)
    except (TypeError, ValueError):
        return []
    needle = f'"hand":{wanted},'
    current_session = _session
    found: dict[tuple, dict] = {}
    found_session: dict[tuple, dict] = {}
    for scanned, file in enumerate(_trace_files(_trace_base(path))):
        if scanned >= max_files:
            break
        try:
            handle = file.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if needle not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                try:
                    if int(record.get("hand") or 0) != wanted:
                        continue
                except (TypeError, ValueError):
                    continue
                if street and record.get("street") != street:
                    continue
                if actions is not None and _record_actions(record) != actions:
                    continue
                key = (record.get("street") or "", _record_actions(record))
                _keep_newer(found, key, record)
                if current_session and record.get("session") == current_session:
                    _keep_newer(found_session, key, record)
        # 只查单个决策点时，找到当前会话的记录就可以停。
        if actions is not None and found_session:
            break
    picked = found_session or found

    def _sort_key(record: dict) -> tuple:
        street_name = record.get("street") or ""
        order = (_STREET_ORDER.index(street_name)
                 if street_name in _STREET_ORDER else len(_STREET_ORDER))
        act = _record_actions(record)
        return (order, -1 if act is None else act)

    return sorted(picked.values(), key=_sort_key)


def _keep_newer(bucket: dict, key: tuple, record: dict) -> None:
    previous = bucket.get(key)
    if previous is None or str(record.get("ts") or "") > str(previous.get("ts") or ""):
        bucket[key] = record


def lookup(hand: int, street: str | None = None, actions: int | None = None,
           *, path: str | Path | None = None) -> dict:
    """`GET /api/range` 的数据侧：找到记录就回 ok，找不到才用 writer 状态解释。

    先找记录再看开关：trace 关了/超预算了，历史记录仍然可读，这比直接回
    off 有用（看板还能看到昨天那手牌）。
    """
    records = records_for(hand, street=street, actions=actions, path=path)
    if records:
        ok = any(record.get("status") == "ok" for record in records)
        return {"status": "ok" if ok else "skipped", "records": records}
    state = status()
    if state in (STATUS_OFF, STATUS_BUDGET, STATUS_WRITE_FAIL):
        return {"status": state}
    return {"status": "missing"}

# ─────────────────────────── 模块级开关 ───────────────────────────

_writer: TraceWriter | None = None
_session = ""


def configure(*, enabled: bool = True, path: str | Path | None = None,
              keep_days: int = KEEP_DAYS, total_limit: int = TOTAL_LIMIT,
              day_limit: int = DAY_LIMIT) -> TraceWriter | None:
    """由 live 启动时调用一次。关闭时 `_writer=None`，`emit` 变成空操作。"""
    global _writer
    if not enabled:
        _writer = None
        return None
    _writer = TraceWriter(path or DEFAULT_PATH, keep_days=keep_days,
                          total_limit=total_limit, day_limit=day_limit)
    return _writer


def set_session(session_id: str | None) -> None:
    """live 启动时把会话 id 交给 trace，落盘时写进每一行。"""
    global _session
    _session = str(session_id or "")


def session() -> str:
    return _session


def writer() -> TraceWriter | None:
    return _writer


def enabled() -> bool:
    return _writer is not None


def status() -> str:
    """`/api/meta.range_trace` 的值：on｜off｜budget_exceeded｜write_failed｜
    record_too_big（最近有记录超硬顶被截断或丢弃，写入继续）。"""
    if _writer is None:
        return STATUS_OFF
    return _writer.status


def emit(record: dict) -> bool:
    """写一条记录；未开启或写失败都返回 False，绝不抛异常。"""
    trace = _writer
    if trace is None:
        return False
    return trace.write(record)


def reset() -> None:
    """测试用：清掉全局 writer。"""
    global _writer
    _writer = None


__all__ = [
    "DEFAULT_PATH", "DAY_LIMIT", "HARD_LIMIT", "KEEP_DAYS", "LEVELS",
    "SOFT_LIMIT", "STATUS_BUDGET", "STATUS_OFF", "STATUS_ON",
    "STATUS_TOO_BIG", "STATUS_WRITE_FAIL", "TOTAL_LIMIT", "TraceWriter",
    "VERSION",
    "advice_version", "build_record", "build_skipped", "configure",
    "downgrade", "emit", "enabled", "lookup", "params_payload", "records_for",
    "reset", "session", "set_session", "status", "tables_sha", "writer",
]
