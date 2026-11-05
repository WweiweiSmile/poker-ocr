"""实时链路的日志底座：结构化事件 JSONL + 控制台文本落盘。

为什么单独一层：
  · `logs/live.states.jsonl` 是**数据契约**（state / hand / advice），下游
    `export_live_hands` / 手牌库 / 复盘控制台都按它读。排查识别问题时需要的
    是另一类信息：每一帧为什么被丢、读牌警告、决策点提示词哈希、建议是模型
    还是兜底、耗时、事件先后。把这些塞进 states 会污染契约，也会让同一批
    数据被重复解析。
  · 事件日志是**排查索引**：一行一个事件，按 `session_id + event_seq` 排序，
    `frame` 事件带 `hand_id`，可以回答「这手牌哪几帧被丢了、丢在哪」。
  · 控制台文本落盘保留人肉可读的原始日志（带墙钟时间、去掉 ANSI），
    方便 `grep` / `less`，不用先把 JSONL 拼回人话。

事件 schema（`live.event.v1`）：

    {"schema":"live.event.v1","type":"event","event":"frame",
     "event_seq": 128, "session_id":"20261105-231500-a1b2c3",
     "logged_at":"2026-11-05T23:15:01.123+08:00", "t": 12.34,
     "data": {...}}

`event` 取值：
  session_start / status / frame / decision / advice / click / hand /
  incident / session_end

`data` 里刻意不重复大字段：
  · frame 不带完整 state（用 `state_index` 与 states 文件对齐）；
  · advice 不带 prompt 原文（用 `prompt_sha256` / `prompt_chars` 定位，
    需要原文时从 states 的 advice 记录取）；
  · hand 不带 `advices`（只留 `advice_count`）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "live.event.v1"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 事件日志里剔除的字段：这些内容已经在 states 文件里，事件日志只留索引。
_DROP_BY_EVENT: dict[str, tuple[str, ...]] = {
    "frame": ("state", "hand"),
    "advice": ("prompt_system", "prompt_user", "raw_response",
               "reason_full", "facts", "context"),
    "hand": ("advices",),
}


def strip_ansi(text: str) -> str:
    """去掉终端颜色，写文件时保留可读文本。"""
    return _ANSI_RE.sub("", text)


def derive_event_path(out: Path) -> Path:
    """`logs/live.states.jsonl` → `logs/live.events.jsonl`。

    自定义 `--out` 时按同样的规则派生；不是 `.states.jsonl` 就在原名后加
    `.events.jsonl`，不覆盖输入文件。
    """
    out = Path(out)
    name = out.name
    if name.endswith(".states.jsonl"):
        return out.with_name(name[: -len(".states.jsonl")] + ".events.jsonl")
    if name.endswith(".jsonl"):
        return out.with_name(name[: -len(".jsonl")] + ".events.jsonl")
    return out.with_name(name + ".events.jsonl")


def derive_console_path(out: Path) -> Path:
    """`logs/live.states.jsonl` → `logs/live.log`（控制台文本）。"""
    out = Path(out)
    name = out.name
    if name.endswith(".states.jsonl"):
        return out.with_name(name[: -len(".states.jsonl")] + ".log")
    if name.endswith(".jsonl"):
        return out.with_name(name[: -len(".jsonl")] + ".log")
    return out.with_name(name + ".log")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    """日志永不因为一个 numpy / 自定义对象挂掉；不能序列化的就转字符串。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


class ConsoleLog:
    """把 `_log()` 的终端输出按行追加到文本文件。

    只负责落盘，不做格式化之外的判断：终端输出什么，文件里就是什么，只多
    一个墙钟时间前缀、去掉 ANSI。这样 `grep [建议]` / `grep [警告]` 的
    习惯不用改。
    """

    def __init__(self, path: Path, *, append: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "a" if append else "w",
                        encoding="utf-8", newline="\n")

    def write(self, message: str) -> None:
        line = f"{_now()} {strip_ansi(message)}\n"
        with self._lock:
            if self._fh.closed:
                return
            self._fh.write(line)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()


class LiveEventLog:
    """一行一个事件，立即 flush；硬杀最多丢最后半行。

    追加是默认行为：同一场会话重启后仍保留历史事件。读取方按
    `session_id + event_seq` 分组即可；坏行（硬杀留下的半行）跳过。
    """

    def __init__(self, path: Path, *, session_id: str,
                 started_mono: float | None = None,
                 append: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.started_mono = (time.monotonic() if started_mono is None
                             else started_mono)
        self._lock = threading.Lock()
        self._seq = 0
        needs_newline = False
        if append and self.path.is_file() and self.path.stat().st_size > 0:
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_newline = fh.read(1) != b"\n"
        self._fh = open(self.path, "a" if append else "w",
                        encoding="utf-8", newline="\n")
        if needs_newline:
            self._fh.write("\n")

    @property
    def event_seq(self) -> int:
        return self._seq

    def write(self, event: str, payload: dict | None = None) -> dict:
        """写一个事件，返回落盘的记录（测试/调用方可直接检查）。"""
        data = _compact(event, payload or {})
        with self._lock:
            self._seq += 1
            record = {
                "schema": SCHEMA,
                "type": "event",
                "event": event,
                "event_seq": self._seq,
                "session_id": self.session_id,
                "logged_at": _now(),
                "t": round(max(0.0, time.monotonic() - self.started_mono), 3),
                "data": data,
            }
            self._fh.write(json.dumps(_jsonable(record), ensure_ascii=False)
                           + "\n")
            self._fh.flush()
            return record

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()


def _compact(event: str, payload: dict) -> dict:
    """按事件类型去掉重复大字段，并补上索引字段。"""
    data = dict(payload)
    for key in _DROP_BY_EVENT.get(event, ()):
        data.pop(key, None)
    if event == "frame":
        # 完整 state 不在事件日志里重复；用 state_index 与 states 对账。
        state = payload.get("state") or {}
        if state.get("index") is not None:
            data["state_index"] = state.get("index")
        data.pop("state", None)
    elif event == "advice":
        system = payload.get("prompt_system") or ""
        user = payload.get("prompt_user") or ""
        data["prompt_chars"] = len(system) + len(user)
    elif event == "hand":
        data.setdefault("advice_count",
                        len(payload.get("advices") or ()))
    return data
