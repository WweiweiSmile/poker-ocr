"""小块工具：JSON、时间、哈希、卡片。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

STREETS = ("preflop", "flop", "turn", "river")
STREET_LABELS = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
CARD_RE = re.compile(r"^(?:[2-9TJQKA]|10)[shdc]$", re.IGNORECASE)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=_default)


def _default(value: Any):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def json_load(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(value: dt.datetime | dt.date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            # MySQL DATETIME 是本地时间（Asia/Shanghai）；前端按本地展示。
            value = value.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def now_cn() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)


def cards_of(text: str | None) -> tuple[str, ...]:
    text = (text or "").strip()
    return tuple(text[i:i + 2] for i in range(0, len(text) - 1, 2))


def split_board(board: str | None) -> dict[str, list[str]]:
    cards = cards_of(board)
    return {
        "flop": list(cards[:3]),
        "turn": list(cards[3:4]),
        "river": list(cards[4:5]),
    }


def card_text(card: str) -> str:
    """Kh -> K♥（用于前端之外的文本导出）。"""
    suits = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
    card = (card or "").strip()
    if len(card) >= 2:
        return card[:-1] + suits.get(card[-1].lower(), card[-1])
    return card


def pct(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    n = len(values)
    mid = n // 2
    return float(values[mid]) if n % 2 else (values[mid - 1] + values[mid]) / 2


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
