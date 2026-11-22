"""多人池 Harrington 卡的静态校验与跨文件解析。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pathlib import Path

from poker_ocr.playbook import Spot, load_cards, resolve, validate_cards

ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = ROOT / "poker_ocr/playbook/cards"
MULTIWAY = CARDS_DIR / "harrington_cash_multiway.json"


def _all_cards():
    cards = []
    for path in sorted(CARDS_DIR.glob("*.json")):
        cards.extend(load_cards(path))
    return cards


def test_multiway_cards_validate():
    cards = load_cards(MULTIWAY)
    assert len(cards) == 49
    assert validate_cards(cards) == []


def test_each_multiway_card_resolves_to_itself():
    cards = load_cards(MULTIWAY)
    for card in cards:
        if card.id.endswith(".with_draw"):
            # #108：95 的带听牌覆盖卡故意让位给 100 的精确卡（draw=none），不参与自解析
            continue
        got = resolve(cards, card.spot)
        assert got.card is not None, card.id
        assert got.card.id == card.id, card.id


def test_multiway_beats_headsup_on_three_plus_players():
    cards = _all_cards()
    spot = Spot(street="flop", players="3+", made_hand="air", draw="none", facing="none")
    got = resolve(cards, spot)
    assert got.card is not None
    assert got.card.id == "flop.mw.air.check"


def test_all_cards_validate_together():
    cards = _all_cards()
    assert len(cards) == 154  # 9 单挑 + 49 多人 + 87 翻前 + 9 河牌（#83/#84/#85/#91/#108）
    assert validate_cards(cards) == []

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
