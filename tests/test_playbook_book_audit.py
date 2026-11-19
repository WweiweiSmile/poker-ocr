"""书页逐条回查台账（#85）：live 卡必须有页码、来源与可定位原文。

台账 `poker_ocr/playbook/book_audit.json` 是 #85 的产物：每张 live 书卡一条，
记印刷页 / PDF 页 / confidence / 原文短句 / 回查结论。本测试把「不许漏」钉住：
新加卡忘了补台账、改页忘了同步台账、explicit 卡没原文依据，都会在这里红。
"""

import json
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poker_ocr.playbook import load_cards

ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = ROOT / "poker_ocr/playbook/cards"
LEDGER = ROOT / "poker_ocr/playbook/book_audit.json"
VERDICTS = {"ok", "fixed", "compiled"}


@lru_cache(maxsize=1)
def _ledger():
    return json.loads(LEDGER.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _card_files():
    """id -> (card, 文件名)；一次加载，避免每个测试重复读 130 张卡。"""
    cards = {}
    for path in sorted(CARDS_DIR.glob("*.json")):
        for card in load_cards(path):
            cards[card.id] = (card, path.name)
    return cards


def _entries():
    return {e["id"]: e for e in _ledger()["cards"]}


def _offsets():
    return {int(volume): value for volume, value in _ledger()["offsets"].items()}


def test_ledger_covers_every_live_card_once():
    live = set(_card_files())
    ids = [e["id"] for e in _ledger()["cards"]]
    assert len(ids) == len(set(ids)), "台账有重复卡 id"
    assert set(ids) == live, "台账与 live 卡不一致：%s" % sorted(set(ids) ^ live)


def test_removed_cards_are_not_live_and_have_reason():
    live = set(_card_files())
    entries = _entries()
    removed = _ledger()["removed"]
    assert removed, "至少应记录本轮移出的卡"
    for item in removed:
        assert item["id"] not in live, item["id"]
        assert item["id"] not in entries, item["id"]
        assert item["reason"].strip(), item["id"]


def test_every_live_card_has_pages_source_and_verdict():
    entries = _entries()
    for card_id, (card, filename) in _card_files().items():
        entry = entries[card_id]
        assert card.pages, card_id
        assert card.pdf_pages, card_id
        assert card.book and card.volume and card.chapter, card_id
        source = entry["source"]
        assert source.strip(), card_id
        assert "第%d卷" % card.volume in source, card_id
        for page in card.pages:
            assert "p%s" % page in source, card_id
        assert entry["verdict"] in VERDICTS, card_id
        assert entry["note"].strip(), card_id
        assert entry["confidence"] == card.confidence, card_id
        assert entry["pages"] == list(card.pages), card_id
        assert entry["pdf_pages"] == list(card.pdf_pages), card_id
        assert entry["file"] == "poker_ocr/playbook/cards/%s" % filename, card_id


def test_pdf_pages_match_printed_pages():
    offsets = _offsets()
    for card_id, (card, _) in _card_files().items():
        assert list(card.pdf_pages) == [p + offsets[card.volume] for p in card.pages], card_id


def test_explicit_cards_have_locatable_quote():
    entries = _entries()
    for card_id, (card, _) in _card_files().items():
        if card.confidence != "explicit":
            continue
        quote = entries[card_id]["quote"].strip()
        assert len(quote) >= 6, "%s 的 explicit 依据太短：%r" % (card_id, quote)
        assert quote not in {"—", "-", "待补"}, card_id


def test_every_entry_has_quote():
    entries = _entries()
    for card_id in _card_files():
        assert len(entries[card_id]["quote"].strip()) >= 4, card_id


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
