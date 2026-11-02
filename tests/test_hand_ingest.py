"""自动入库 worker：成功才移走、坏手隔离、网络错误保留。"""

from __future__ import annotations

import tempfile

import pytest

from poker_ocr import hand_ingest
from poker_ocr.hand_ingest import HandIngestWorker, IngestConfig
from poker_ocr.hand_outbox import HandOutbox


def make_worker(tmp):
    outbox = HandOutbox(tmp)
    config = IngestConfig(url="http://example.invalid", batch=20,
                          max_failures=2, timeout=1)
    return outbox, HandIngestWorker(outbox, config)


def test_success_moves_to_sent(monkeypatch, tmp_path):
    outbox, worker = make_worker(tmp_path)
    outbox.enqueue({"uid": "h1", "hand_id": "h1", "hero_cards": ["Ah", "Kd"]})
    outbox.enqueue({"uid": "h2", "hand_id": "h2", "hero_cards": ["Qs", "Qd"]})

    def fake_post(config, records):
        return {"received": len(records), "inserted": len(records),
                "errors": 0, "duplicates": 0}

    monkeypatch.setattr(hand_ingest, "_post_ingest", fake_post)
    result = worker.process_pending()
    assert result["sent"] == 2
    assert outbox.pending() == []


def test_batch_error_falls_back_to_single(monkeypatch, tmp_path):
    outbox, worker = make_worker(tmp_path)
    outbox.enqueue({"uid": "good", "hand_id": "good"})
    outbox.enqueue({"uid": "bad", "hand_id": "bad"})
    calls = []

    def fake_post(config, records):
        calls.append([r["uid"] for r in records])
        if len(records) > 1:
            return {"errors": 1, "issues": [{"index": 1, "reason": "bad"}]}
        if records[0]["uid"] == "bad":
            return {"errors": 1, "issues": [{"index": 0, "reason": "bad"}]}
        return {"errors": 0}

    monkeypatch.setattr(hand_ingest, "_post_ingest", fake_post)
    result = worker.process_pending()
    assert result["sent"] == 1
    assert result["failed"] == 1
    assert len(outbox.pending()) == 1
    assert any(len(call) == 1 for call in calls)


def test_transient_error_keeps_pending(monkeypatch, tmp_path):
    outbox, worker = make_worker(tmp_path)
    outbox.enqueue({"uid": "h1", "hand_id": "h1"})

    def fake_post(config, records):
        raise hand_ingest._Transient("timeout")

    monkeypatch.setattr(hand_ingest, "_post_ingest", fake_post)
    with pytest.raises(hand_ingest._Transient):
        worker.process_pending()
    assert len(outbox.pending()) == 1
