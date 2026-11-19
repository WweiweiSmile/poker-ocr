"""翻前手牌分类与位置分区的回归：书里每个牌型都要落到唯一一类。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from poker_ocr.playbook.hand_class import (
    POSITION_BUCKETS,
    PREFLOP_HAND_CLASSES,
    position_bucket,
    preflop_hand_class,
    preflop_suited,
)
from poker_ocr.positions import POSITIONS_BY_TABLE_SIZE


def _cls(a, b):
    return preflop_hand_class((a, b))


# 与 preflop_ranges 的 RANKS 同序（高到低），只是为了把 169 手牌枚举全。
RANKS = ("A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2")


@pytest.mark.parametrize("cards,expected", [
    (("Ah", "Ad"), "big_pair"),
    (("Kh", "Kd"), "big_pair"),
    (("Qh", "Qd"), "big_pair"),
    (("Jh", "Jd"), "medium_pair"),
    (("Th", "Td"), "medium_pair"),
    (("9h", "9d"), "medium_pair"),
    (("8h", "8d"), "low_pair"),
    (("7h", "7d"), "low_pair"),
    (("6h", "6d"), "low_pair"),
    (("5h", "5d"), "small_pair"),
    (("4h", "4d"), "small_pair"),
    (("2h", "2d"), "small_pair"),
    (("Ah", "Kh"), "ak"),
    (("Ah", "Kd"), "ak"),
    (("Ah", "Qh"), "aq"),
    (("Ah", "Qd"), "aq"),
    (("Ah", "Jh"), "ace_broadway"),
    (("Ah", "Td"), "ace_broadway"),
    (("Ah", "9h"), "weak_ace"),
    (("Ah", "7d"), "weak_ace"),
    (("Ah", "2h"), "weak_ace"),
    (("Kh", "Qh"), "trouble"),
    (("Kh", "Qd"), "trouble"),
    (("Kh", "Jh"), "trouble"),
    (("Qh", "Jh"), "trouble"),
    (("Kh", "Th"), "weak_trouble"),
    (("Kh", "Td"), "weak_trouble"),
    (("Qh", "Td"), "weak_trouble"),
    (("Jh", "Th"), "suited_connector"),
    (("Th", "9h"), "suited_connector"),
    (("5h", "4h"), "suited_connector"),
    (("7h", "5h"), "speculative"),
    (("9h", "7h"), "speculative"),
    (("9h", "6h"), "speculative"),
    (("Th", "9d"), "speculative"),
    (("8h", "7d"), "speculative"),
    (("4h", "3h"), "speculative"),
    (("3h", "2h"), "speculative"),
    (("Jh", "8h"), "speculative"),
    (("Kh", "9h"), "other"),
    (("Qh", "8h"), "other"),
    (("Jh", "7d"), "other"),
    (("7h", "2d"), "other"),
    (("10h", "10d"), "medium_pair"),  # '10' 记法归一到 T
])
def test_hand_class_matches_book_categories(cards, expected):
    assert _cls(*cards) == expected


def test_every_result_is_in_vocabulary():
    keys = [("Ah", "Ad"), ("Kh", "Qd"), ("Ah", "2c"), ("9h", "6h"),
            ("Jh", "7d"), ("7h", "2c"), ("Th", "9s")]
    for cards in keys:
        assert preflop_hand_class(cards) in PREFLOP_HAND_CLASSES


def test_hand_class_is_order_insensitive():
    assert preflop_hand_class(("Ks", "Ah")) == preflop_hand_class(("Ah", "Ks"))
    assert preflop_hand_class(["9d", "9c"]) == "medium_pair"


def test_suited_flag():
    assert preflop_suited(("Ah", "Kh")) is True
    assert preflop_suited(("Ah", "Kd")) is False
    assert preflop_suited(()) is None
    assert preflop_suited(None) is None
    assert preflop_suited(("Ah",)) is None
    assert preflop_suited(("Xh", "Kd")) is None
    assert preflop_suited(("Ah", "Kx")) is None


def test_hand_class_fails_open():
    assert preflop_hand_class(()) is None
    assert preflop_hand_class(None) is None
    assert preflop_hand_class(("Ah",)) is None
    assert preflop_hand_class(("Ah", "Kd", "Qc")) is None
    assert preflop_hand_class(("Xh", "Kd")) is None


@pytest.mark.parametrize("position,expected", [
    ("UTG", "early"), ("UTG+1", "early"), ("UTG+2", "early"),
    ("LJ", "middle"), ("HJ", "middle"),
    ("CO", "late"), ("BTN", "late"),
    ("SB", "blind"), ("BB", "blind"),
    ("btn", "late"), ("HJ ", "middle"),
])
def test_position_bucket(position, expected):
    assert position_bucket(position) == expected
    assert expected in POSITION_BUCKETS


def test_position_bucket_fails_open():
    assert position_bucket(None) is None
    assert position_bucket("") is None
    assert position_bucket("MP") is None


def test_every_canonical_seat_has_a_bucket():
    # 座位名只有 poker_ocr.positions 一份；那边新增座位而这里没跟上时，
    # 位置会静默落到 None，卡片退化成「书中无此局面」——用这条先红。
    seats = {seat for seats in POSITIONS_BY_TABLE_SIZE.values() for seat in seats}
    missing = sorted(seats - {s for s in seats if position_bucket(s)})
    assert missing == []


def test_every_hand_class_is_in_the_vocabulary():
    # 169 手牌全部枚举一遍：分类结果必须落在词表内，且每一类都真的可达。
    classes = set()
    for hi in RANKS:
        for lo in RANKS:
            if hi == lo:
                variants = ((hi + "h", lo + "d"),)          # 对子
            else:
                variants = ((hi + "h", lo + "h"),           # 同花
                            (hi + "h", lo + "d"))           # 非同花
            for cards in variants:
                got = preflop_hand_class(cards)
                assert got in PREFLOP_HAND_CLASSES, (cards, got)
                classes.add(got)
    assert classes == set(PREFLOP_HAND_CLASSES)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
