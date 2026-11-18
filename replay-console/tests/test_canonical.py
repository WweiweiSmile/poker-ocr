"""canonical 归组规则（纯逻辑）：底牌归一、30 分钟窗、primary 优先级。

设计 §3.4：
  · 同内容 = 底牌相同 且 公共牌相同 且 发生时间差 ≤ 30 分钟；
  · 命中后挂同一 canonical id，content_group = sha1(底牌|公共牌|日期)；
  · primary 默认 review > live > synthetic，同来源取 source_hand_id 最小。
"""
from __future__ import annotations

import datetime as dt

import pytest

from replay.canonical import (
    CANONICAL_HAND_ID_BASE,
    MATCH_WINDOW,
    SOURCES,
    content_group_key,
    normalize_board,
    normalize_hero_cards,
    pick_candidate,
    primary_row,
    validate_source,
)
from replay.errors import ApiError
from replay.repository import IMPORTED_HAND_ID_BASE


def at(hour: int, minute: int = 0, second: int = 0, day: int = 17) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, second)


# ── 底牌 / 公共牌归一 ──────────────────────────────────────────────

def test_hero_cards_are_sorted_so_suits_and_order_do_not_matter():
    assert normalize_hero_cards("QhKh") == "KhQh"
    assert normalize_hero_cards("KdAh") == "AhKd"
    assert normalize_hero_cards("AhKd") == normalize_hero_cards("KdAh")


def test_hero_cards_tolerate_separators_and_empty():
    assert normalize_hero_cards("Kh, Qh") == "KhQh"
    assert normalize_hero_cards("") == ""
    assert normalize_hero_cards(None) == ""
    assert normalize_hero_cards("Ah") == "Ah"


def test_board_keeps_deal_order():
    # 公共牌的顺序本身是事实（flop 先后），不排序
    assert normalize_board("Ks7d2c3h9s") == "Ks7d2c3h9s"
    assert normalize_board(" 2c 7d Ks ") == "2c7dKs"
    assert normalize_board(None) == ""


# ── content_group ────────────────────────────────────────────────

def test_same_cards_same_day_share_one_group():
    first = content_group_key("QhKh", "Ks7d2c3h9s", at(14, 0))
    second = content_group_key("KhQh", "Ks7d2c3h9s", at(21, 30))

    assert first == second
    assert first is not None and len(first) == 40  # sha1 hex


def test_group_differs_by_board_or_day():
    base = content_group_key("KhQh", "Ks7d2c3h9s", at(14, 0))

    assert content_group_key("KhQh", "Ks7d2c3h9c", at(14, 0)) != base
    assert content_group_key("KhQh", "Ks7d2c3h9s", at(14, 0, day=18)) != base


def test_group_is_empty_without_started_at():
    assert content_group_key("KhQh", "Ks7d2c3h9s", None) is None


# ── 30 分钟窗 ────────────────────────────────────────────────────

def test_window_is_inclusive_at_exactly_thirty_minutes():
    rows = [{"id": 5, "started_at": at(10, 0)}]

    assert pick_candidate(rows, at(10, 30)) is rows[0]
    assert pick_candidate(rows, at(9, 30)) is rows[0]
    assert pick_candidate(rows, at(10, 31)) is None


def test_window_prefers_the_nearest_candidate():
    rows = [
        {"id": 5, "started_at": at(10, 0)},
        {"id": 6, "started_at": at(10, 25)},
    ]

    assert pick_candidate(rows, at(10, 20))["id"] == 6


def test_window_tie_breaks_on_smaller_canonical_id():
    rows = [
        {"id": 9, "started_at": at(10, 10)},
        {"id": 4, "started_at": at(10, 30)},
    ]

    assert pick_candidate(rows, at(10, 20))["id"] == 4


def test_rows_without_started_at_never_match():
    rows = [{"id": 3, "started_at": None}, {"id": 4}]

    assert pick_candidate(rows, at(10, 0)) is None
    assert pick_candidate(rows, None) is None


def test_match_window_is_half_an_hour():
    assert MATCH_WINDOW == dt.timedelta(minutes=30)


# ── primary ──────────────────────────────────────────────────────

def test_primary_prefers_review_over_live_over_synthetic():
    rows = [
        {"source": "synthetic", "source_hand_id": 1},
        {"source": "live", "source_hand_id": 2},
        {"source": "review", "source_hand_id": 3},
    ]

    assert primary_row(rows)["source"] == "review"
    assert primary_row(rows[:2])["source"] == "live"
    assert primary_row(rows[:1])["source"] == "synthetic"
    assert primary_row([]) is None


def test_primary_tie_breaks_on_smallest_source_hand_id():
    rows = [
        {"source": "review", "source_hand_id": 90},
        {"source": "review", "source_hand_id": 12},
    ]

    assert primary_row(rows)["source_hand_id"] == 12


# ── 来源校验与 id 段 ─────────────────────────────────────────────

def test_sources_are_the_three_known_kinds():
    assert SOURCES == ("review", "live", "synthetic")


def test_validate_source_normalizes_and_rejects_unknown():
    assert validate_source(" Review ") == "review"
    with pytest.raises(ApiError) as exc:
        validate_source("manual")
    assert exc.value.status_code == 422


def test_canonical_ids_start_above_imported_hand_ids():
    assert CANONICAL_HAND_ID_BASE > IMPORTED_HAND_ID_BASE
    assert CANONICAL_HAND_ID_BASE == 2_000_000_000_000
