"""10 张 Harrington 试点卡的静态校验与解析。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pathlib import Path

from poker_ocr.playbook import load_cards, resolve, validate_cards

CARDS = Path(__file__).resolve().parents[1] / "poker_ocr/playbook/cards/harrington_cash.json"


def test_pilot_cards_validate():
    cards = load_cards(CARDS)
    assert len(cards) == 10
    assert validate_cards(cards) == []


def test_each_card_resolves_to_itself():
    cards = load_cards(CARDS)
    for card in cards:
        got = resolve(cards, card.spot)
        assert got.card is not None, card.id
        assert got.card.id == card.id, card.id


def test_unknown_spot_is_unknown():
    cards = load_cards(CARDS)
    got = resolve(cards, __import__("poker_ocr.playbook", fromlist=["Spot"]).Spot(street="river"))
    assert got.card is None
    assert got.unknown

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
