"""手牌 ID：从「当前手牌全部玩家的名称 + 位置 + 日期/开始时间」摘要生成。

为什么不继续用 `h1` / `h2`：

  · `hand_id` 每次启动都从 `h1` 重新计数，跨 session 只能靠 `session` 前缀
    才能区分；手牌库里搜 `h5` 会搜到十几手完全不同的牌。
  · 复盘时想按「谁在这桌」找牌，顺序号没有任何信息量。

现在的 ID 形如 `20261030-3f2a91c8d0`：

  · 前 8 位是手牌开始日期（Asia/Shanghai 本地时区），肉眼可读；
  · 后 10 位是 `sha256(名单 + 位置 + 日期 + 开始时间 + 桌型 + 庄位)` 的摘要。
    名单用 `normalize` 后的名字；名字没读到时用座位号兜底，保证稳定。
  · 另外给一个 `table_signature`（只含名单/位置/日期，不含开始时间），
    同一桌同一天的多手牌可以按它分组，但 `hand_id` 本身仍然唯一。

**为什么摘要里要带开始时间**：只按「名单 + 日期」生成，同一桌同一天打 100 手
会得到同一个 ID。要唯一，必须至少有一个随时间变化的成分；开始时间是最自然、
也最方便人工核对的一个。格式仍以日期开头，满足「日期 + 名单摘要」的直觉。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

HAND_ID_RE = re.compile(r"^\d{8}-[0-9a-f]{10}(?:-\d+)?$")
TABLE_SIGNATURE_RE = re.compile(r"^\d{8}-[0-9a-f]{12}$")

_HAND_DIGEST_LEN = 10
_TABLE_DIGEST_LEN = 12
# 空座/离桌的人不算「这手牌的人」。playing/folded 都算在座。
_EMPTY_STATUS = ("away", "buying_in", "left")


def normalize_name(name: Any) -> str:
    """NFKC、去首尾空格、连续空白合并。与 opponent_profile.normalize_name 同口径。"""
    if name is None:
        return ""
    text = unicodedata.normalize("NFKC", str(name)).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _field(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _roster_rows(players: Iterable[Any] | None) -> list[dict[str, str]]:
    """抽出参与摘要的玩家行：位置、名字、座位。

    只排除明确空座/离桌的人；`in_hand=False` 但仍在座（例如刚坐下还没发牌）
    保留，因为「当前桌的名单」包含他。名字/位置读不到时用空串，座位号兜底。
    """
    rows: list[dict[str, str]] = []
    for index, player in enumerate(players or ()):
        status = str(_field(player, "status", "") or "")
        in_hand = bool(_field(player, "in_hand", False))
        if status in _EMPTY_STATUS and not in_hand:
            continue
        seat = str(_field(player, "seat_id", "") or f"seat{index}")
        name = normalize_name(_field(player, "name", None))
        position = str(_field(player, "position", "") or "")
        rows.append({"seat": seat, "position": position, "name": name})
    rows.sort(key=lambda row: (row["position"], row["name"], row["seat"]))
    return rows


@dataclass(frozen=True)
class HandIdentity:
    """一手牌的身份。`hand_id` 唯一；`table_signature` 用于按桌/日期分组。"""

    hand_id: str
    table_signature: str


def build_hand_identity(
    players: Iterable[Any] | None,
    *,
    started_at: datetime,
    table_size: int = 0,
    dealer: str | None = None,
    taken: Iterable[str] = (),
) -> HandIdentity:
    """生成 `YYYYMMDD-<digest>` 形式的手牌 ID。

    `players` 接受 `PlayerState`、`RosterSeat` 或带 seat_id/name/position/status
    的 mapping。`taken` 是本次会话已经用过的 ID；碰撞时追加 `-2`、`-3`。
    """
    date = started_at.strftime("%Y%m%d")
    roster = _roster_rows(players)
    # 桌签名只含「名单/位置/日期」：同一桌同一天换庄、换手都共用它，
    # 用于把多手牌分组。庄位/桌型/开始时间只进 hand_id，保证每手唯一。
    signature_payload = {"date": date, "players": roster}
    table_digest = hashlib.sha256(_canonical(signature_payload)).hexdigest()
    table_signature = f"{date}-{table_digest[:_TABLE_DIGEST_LEN]}"

    unique_payload = {
        "date": date,
        "table_size": int(table_size or 0),
        "dealer": str(dealer or ""),
        "players": roster,
        "started_at": started_at.isoformat(timespec="seconds"),
    }
    digest = hashlib.sha256(_canonical(unique_payload)).hexdigest()
    base_id = f"{date}-{digest[:_HAND_DIGEST_LEN]}"

    used = set(taken or ())
    candidate = base_id
    suffix = 2
    while candidate in used:
        candidate = f"{base_id}-{suffix}"
        suffix += 1
    return HandIdentity(hand_id=candidate, table_signature=table_signature)
