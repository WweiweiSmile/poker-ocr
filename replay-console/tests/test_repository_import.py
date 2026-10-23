from __future__ import annotations

import datetime as dt

from replay.repository import IMPORTED_HAND_ID_BASE, ImportedHandRepository


class FakeDB:
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []

    def one(self, sql, params=None):
        return self.existing

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return IMPORTED_HAND_ID_BASE + 2


def sample_row(**overrides):
    row = {
        "source": "review",
        "external_id": "990001",
        "fingerprint": "f" * 64,
        "title": "BTN KQs",
        "hero_cards": "KhQh",
        "board": "Ks7d2c3h9s",
        "hero_position": "BTN",
        "table_size": 6,
        "hero_stack_bb": 100.0,
        "small_blind_bb": 0.5,
        "big_blind_bb": 1.0,
        "ante_bb": 0.0,
        "bomb_pot_bb": 0.0,
        "villain_count": 2,
        "villains": [{"position": "CO", "stackBb": 100.0}],
        "streets": [{"street": "preflop", "actions": []}],
        "result": "win",
        "result_amount": 12.5,
        "pot_type": None,
        "hero_tags": [],
        "source_created_at": dt.datetime(2026, 10, 23, 3, 6, 53),
        "raw": {"id": 990001},
    }
    row.update(overrides)
    return row


def test_upsert_insert_binds_all_columns():
    db = FakeDB()
    repo = ImportedHandRepository(db)
    hand_id, created = repo.upsert(sample_row(), batch_id=7)
    assert created is True
    assert hand_id == IMPORTED_HAND_ID_BASE + 2
    sql, params = db.calls[0]
    assert sql.lstrip().startswith("INSERT INTO replay_imported_hands")
    assert sql.count("%s") == len(params) == 27


def test_upsert_existing_updates_in_place():
    db = FakeDB(existing={"id": 42, "source": "review", "fingerprint": "f" * 64})
    repo = ImportedHandRepository(db)
    hand_id, created = repo.upsert(sample_row(), batch_id=8)
    assert created is False
    assert hand_id == 42
    sql, params = db.calls[0]
    assert sql.lstrip().startswith("UPDATE replay_imported_hands")
    assert sql.count("%s") == len(params) == 25
    assert params[-2:] == (42, 0)
