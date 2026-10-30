#!/usr/bin/env python3
"""手牌 ID：名单 + 位置 + 日期/开始时间的摘要。

    .venv/bin/python tests/test_hand_id.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.hand_id import (  # noqa: E402
    HAND_ID_RE, TABLE_SIGNATURE_RE, build_hand_identity,
)

FAILS: list[str] = []
TZ = timezone(timedelta(hours=8))


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def players(*rows):
    return [
        {"seat_id": seat, "name": name, "position": position,
         "status": "playing", "in_hand": True}
        for seat, name, position in rows
    ]


BASE = players(
    ("upR", "LowR", "CO"),
    ("midR", "芒果与西米", "BTN"),
    ("hero", "我", "BB"),
)


def test_format_and_determinism() -> None:
    started = datetime(2026, 10, 30, 21, 3, 11, tzinfo=TZ)
    a = build_hand_identity(BASE, started_at=started, table_size=9,
                            dealer="midR")
    b = build_hand_identity(BASE, started_at=started, table_size=9,
                            dealer="midR")
    check(HAND_ID_RE.match(a.hand_id), f"ID 格式不对：{a.hand_id}")
    check(TABLE_SIGNATURE_RE.match(a.table_signature),
          f"桌签名格式不对：{a.table_signature}")
    check(a.hand_id == b.hand_id, "同一名单/时间应得到同一 ID")
    check(a.table_signature == b.table_signature, "同一名单/日期应得到同一签名")
    check(a.hand_id.startswith("20261030-"), f"ID 应以日期开头：{a.hand_id}")


def test_roster_changes_id_but_date_groups_signature() -> None:
    started = datetime(2026, 10, 30, 21, 3, 11, tzinfo=TZ)
    base = build_hand_identity(BASE, started_at=started, table_size=9)
    renamed = [dict(row) for row in BASE]
    renamed[0]["name"] = "LowR2"
    changed = build_hand_identity(renamed, started_at=started, table_size=9)
    check(base.hand_id != changed.hand_id, "换名字应换 ID")

    repositioned = [dict(row) for row in BASE]
    repositioned[0]["position"] = "UTG"
    moved = build_hand_identity(repositioned, started_at=started, table_size=9)
    check(base.hand_id != moved.hand_id, "换位置应换 ID")

    later = build_hand_identity(
        BASE, started_at=started + timedelta(minutes=1), table_size=9)
    check(base.hand_id != later.hand_id, "不同开始时间应换 ID（保证唯一）")
    check(base.table_signature == later.table_signature,
          "同一天同一名单的桌签名应相同，便于按桌分组")

    other_dealer = build_hand_identity(BASE, started_at=started,
                                       table_size=9, dealer="upR")
    check(other_dealer.hand_id != base.hand_id,
          "换庄位应换 ID（不同的牌局）")
    check(other_dealer.table_signature == base.table_signature,
          "换庄位不应改变桌签名（同一桌同一天）")


def test_collision_suffix() -> None:
    started = datetime(2026, 10, 30, 21, 3, 11, tzinfo=TZ)
    first = build_hand_identity(BASE, started_at=started, table_size=9)
    second = build_hand_identity(BASE, started_at=started, table_size=9,
                                 taken={first.hand_id})
    check(second.hand_id == f"{first.hand_id}-2",
          f"碰撞应追加 -2：{second.hand_id}")


def test_empty_seats_are_ignored_and_names_are_normalized() -> None:
    started = datetime(2026, 10, 30, 21, 3, 11, tzinfo=TZ)
    roster = [
        {"seat_id": "a", "name": "  LowR ", "position": "CO",
         "status": "playing", "in_hand": True},
        {"seat_id": "b", "name": None, "position": "BTN",
         "status": "away", "in_hand": False},
    ]
    normalized = [
        {"seat_id": "a", "name": "LowR", "position": "CO",
         "status": "playing", "in_hand": True},
    ]
    a = build_hand_identity(roster, started_at=started, table_size=9)
    b = build_hand_identity(normalized, started_at=started, table_size=9)
    check(a.hand_id == b.hand_id, "空座应被忽略、名字空白应归一")


def test_readable_example() -> None:
    """文档示例：LowR 这种位置标签也参与摘要。"""
    started = datetime(2026, 10, 30, 21, 3, 11, tzinfo=TZ)
    ident = build_hand_identity(
        players(("lowR", "LowR", "LowR")), started_at=started, table_size=9)
    check(ident.hand_id.startswith("20261030-"),
          f"示例 ID 应以日期开头：{ident.hand_id}")
    check(len(ident.hand_id) == len("20261030-") + 10,
          f"示例 ID 长度不对：{ident.hand_id}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)
             and getattr(value, "__module__", "") == __name__]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {test.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n✅ 手牌 ID 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
