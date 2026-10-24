from __future__ import annotations

import json

from replay.config import Settings
from replay.importer import normalize_review_hand
from replay.service import ReplayService


class FakeHandRepo:
    def __init__(self, review_rows=None, active_rows=None):
        self.review_rows = review_rows or []
        self.active_rows = active_rows or []
        self.deleted: list[int] = []

    def list_review_rows(self):
        return self.review_rows

    def get_by_ids(self, hand_ids):
        wanted = {int(x) for x in hand_ids}
        return [r for r in self.active_rows if int(r["id"]) in wanted]

    def delete_hands(self, hand_ids):
        ids = [int(x) for x in hand_ids]
        self.deleted.extend(ids)
        imported = sum(1 for x in ids if x >= 1_000_000_000_000)
        return {"imported": imported, "reviewHidden": len(ids) - imported}


class FakeImportRepo:
    def __init__(self):
        self.rows: dict[tuple[str, str], int] = {}
        self.upserts: list[tuple[str, str]] = []
        self.saved_rows: list[dict] = []

    def fingerprint_map(self):
        return dict(self.rows)

    def upsert(self, row, *, batch_id=None):
        key = (row["source"], row["fingerprint"])
        self.upserts.append((row["source"], row["external_id"]))
        self.saved_rows.append(row)
        if key in self.rows:
            return self.rows[key], False
        hand_id = 1_000_000_000_000 + len(self.rows) + 1
        self.rows[key] = hand_id
        return hand_id, True


class FakeBatchRepo:
    def __init__(self):
        self.created = []
        self.finished = []

    def create(self, *, source, container, filename, total_records):
        self.created.append((source, container, filename, total_records))
        return 77

    def finish(self, batch_id, **counts):
        self.finished.append((batch_id, counts))


def make_service(review_rows=None, active_rows=None):
    service = ReplayService(Settings(db_user="", db_password="", db_name="nodb",
                                     worker_enabled=False), db=object())
    service.hand_repo = FakeHandRepo(review_rows, active_rows)
    service.import_repo = FakeImportRepo()
    service.batch_repo = FakeBatchRepo()
    return service


def review_content(**overrides):
    hand = {
        "id": 990001,
        "title": "BTN KQs",
        "heroCards": "KhQh",
        "board": "Ks7d2c3h9s",
        "heroPosition": "BTN",
        "tableSize": 6,
        "heroStackBb": 100.0,
        "smallBlindBb": 0.5,
        "bigBlindBb": 1.0,
        "anteBb": 0.0,
        "bombPotBb": 0.0,
        "villainCount": 2,
        "villains": [
            {"position": "CO", "stackBb": 100.0},
            {"position": "BB", "stackBb": 80.0},
        ],
        "streets": [
            {"street": "preflop", "actions": [
                {"actor": "CO", "action": "raise", "amountBb": 2.5},
                {"actor": "hero", "action": "call", "amountBb": 2.5},
                {"actor": "BB", "action": "call", "amountBb": 2.5},
            ]},
            {"street": "flop", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "bet", "amountBb": 4.0},
                {"actor": "hero", "action": "call", "amountBb": 4.0},
            ]},
            {"street": "turn", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "check"},
                {"actor": "hero", "action": "check"},
            ]},
            {"street": "river", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "check"},
                {"actor": "hero", "action": "check"},
            ]},
        ],
        "result": "win",
        "resultAmount": 12.5,
    }
    hand.update(overrides)
    return json.dumps([hand])


def test_dry_run_reports_without_writing():
    service = make_service()
    result = service.import_hands({
        "content": review_content(), "format": "auto", "dryRun": True,
    })
    assert result["source"] == "review"
    assert result["imported"] == 1
    assert result["updated"] == 0
    assert result["batchId"] is None
    assert service.import_repo.upserts == []
    assert service.batch_repo.created == []
    assert result["samples"][0]["status"] == "imported"
    assert result["samples"][0]["decisionCount"] == 4


def test_confirm_writes_and_records_batch():
    service = make_service()
    result = service.import_hands({
        "content": review_content(), "format": "auto", "dryRun": False,
        "filename": "review.json",
    })
    assert result["batchId"] == 77
    assert result["imported"] == 1
    assert service.import_repo.upserts == [("review", "990001")]
    assert service.batch_repo.created == [("review", "json", "review.json", 1)]
    assert service.batch_repo.finished[0][0] == 77
    assert service.batch_repo.finished[0][1]["status"] == "succeeded"


def test_duplicate_against_review_hands_is_skipped():
    row = normalize_review_hand(json.loads(review_content())[0])
    row["id"] = 1
    service = make_service(review_rows=[row])
    result = service.import_hands({
        "content": review_content(), "format": "auto", "dryRun": True,
    })
    assert result["imported"] == 0
    assert result["duplicates"] == 1
    assert result["samples"][0]["status"] == "duplicate"


def test_invalid_is_skipped_by_default_and_can_be_included():
    content = review_content(heroCards="")
    service = make_service()
    result = service.import_hands({
        "content": content, "format": "auto", "dryRun": True,
    })
    assert result["invalid"] == 1
    assert result["imported"] == 0

    included = service.import_hands({
        "content": content, "format": "auto", "dryRun": True,
        # exact 过滤会先挡掉 invalid；这里两个闸门都显式放开。
        "onlyExact": False, "allowInvalid": True,
    })
    assert included["invalid"] == 0
    assert included["imported"] == 1


def test_reimport_same_content_updates_instead_of_insert():
    service = make_service()
    payload = {"content": review_content(), "format": "auto"}
    first = service.import_hands({**payload, "dryRun": False})
    second = service.import_hands({**payload, "dryRun": False})
    assert first["imported"] == 1
    assert second["updated"] == 1
    assert len(service.import_repo.rows) == 1


def test_delete_imported_and_review_hands():
    imported = {"id": 1_000_000_000_009}
    review = {"id": 123}
    service = make_service(active_rows=[imported, review])
    result = service.delete_hands({
        "handIds": [imported["id"], review["id"]],
    })
    assert result["requested"] == 2
    assert result["deleted"] == 2
    assert result["imported"] == 1
    assert result["reviewHidden"] == 1
    assert result["missing"] == []
    assert service.hand_repo.deleted == [123, 1_000_000_000_009]


def test_delete_missing_hand_reports_missing():
    service = make_service(active_rows=[])
    result = service.delete_hands({"handIds": [42]})
    assert result["deleted"] == 0
    assert result["missing"] == [42]

    try:
        service.delete_hand(42)
    except Exception as exc:  # ApiError
        assert getattr(exc, "code", None) == "HAND_NOT_FOUND"
    else:  # pragma: no cover
        raise AssertionError("不存在的手牌应该返回 404")


def test_delete_hand_ids_validation():
    service = make_service()
    for payload in ({}, {"handIds": []}, {"handIds": "1,2"}, {"handIds": ["x"]}):
        try:
            service.delete_hands(payload)
        except Exception as exc:  # ApiError
            assert getattr(exc, "code", None) == "INVALID_HAND_IDS"
        else:  # pragma: no cover
            raise AssertionError(f"{payload} 应该校验失败")


def live_log_with_advice() -> str:
    """一条实时日志：advice 行 + 对应的 hand 行。"""
    session_start = {"type": "session_start",
                     "started_at": "2026-10-23T03:00:00+08:00"}
    hand = {
        "type": "hand", "ts": "2026-10-23T03:08:07+08:00",
        "hand_id": "h4", "uid": "s01-h4",
        "hero_cards": ["Ah", "Js"], "hero_position": "UTG+1",
        "table_size": 9,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5, "raw": "1/2（1）"},
        "board": ["7c", "7h", "10d", "8c", "6h"],
        "streets": [
            {"street": "preflop", "pot_start": 4.0, "actions": [
                {"seat_id": "hero", "position": "UTG+1", "action": "raise",
                 "amount": 7.5, "is_hero": True},
                {"seat_id": "topR", "position": "BTN", "action": "call",
                 "amount": 7.5, "is_hero": False},
            ]},
            {"street": "flop", "pot_start": 20.5, "actions": [
                {"seat_id": "hero", "position": "UTG+1", "action": "bet",
                 "amount": 6.5, "is_hero": True},
                {"seat_id": "topR", "position": "BTN", "action": "call",
                 "amount": 6.5, "is_hero": False, "inferred": True},
            ]},
        ],
    }
    advice = {
        "type": "advice", "ts": "2026-10-23T03:08:10+08:00",
        "hand_id": "h4", "street": "preflop",
        "option": {"id": "open-2.5", "label": "Raise to 2.5BB",
                   "kind": "raise", "amount_bb": 2.5},
        "reason": "open for value", "source": "model",
    }
    return "\n".join(json.dumps(x) for x in [session_start, advice, hand])


def test_live_log_import_attaches_advices_to_hand():
    service = make_service()
    result = service.import_hands({
        "content": live_log_with_advice(), "format": "auto", "dryRun": False,
        # 这个样本缺筹码 → approx；本用例只验证建议归手，放开 exact 过滤。
        "onlyExact": False,
    })
    assert result["source"] == "live"
    assert result["hands"] == 1
    assert result["advices"] == 1
    assert len(service.import_repo.saved_rows) == 1
    saved = service.import_repo.saved_rows[0]
    assert saved["advices"][0]["option"]["id"] == "open-2.5"
    # advice 没有变成第二手牌
    assert len(service.import_repo.rows) == 1


def test_import_defaults_to_exact_only():
    """默认只导入 exact：approx 手牌跳过，显式放开才进库。"""
    service = make_service()
    blocked = service.import_hands({
        "content": live_log_with_advice(), "format": "auto", "dryRun": True,
    })
    assert blocked["onlyExact"] is True
    assert blocked["imported"] == 0
    assert blocked["skippedNotExact"] == 1
    assert blocked["samples"][0]["status"] == "approx"

    allowed = service.import_hands({
        "content": live_log_with_advice(), "format": "auto", "dryRun": True,
        "onlyExact": False,
    })
    assert allowed["imported"] == 1
    assert allowed["skippedNotExact"] == 0
