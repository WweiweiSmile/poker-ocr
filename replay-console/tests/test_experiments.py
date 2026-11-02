from __future__ import annotations

from types import SimpleNamespace

from replay.config import Settings
from replay.service import ReplayService


class FakeExperimentRepo:
    def __init__(self, row, hands, results=None, reviews=None):
        self.row = row
        self.hand_rows = hands
        self.results = results or []
        self.reviews = reviews or []
        self.jobs = []

    def get(self, experiment_id):
        return self.row if int(experiment_id) == int(self.row["id"]) else None

    def hands(self, experiment_id, split=None):
        if split:
            return [h for h in self.hand_rows if h["split"] == split]
        return list(self.hand_rows)

    def result_rows(self, experiment_id, job_id, split=None):
        return [r for r in self.results
                if not split or r["split"] == split]

    def review_rows(self, experiment_id, split=None):
        return [r for r in self.reviews
                if not split or r["split"] == split]

    def set_job(self, experiment_id, job_id, status="queued"):
        self.jobs.append((experiment_id, job_id, status))


class FakeVersionRepo:
    def __init__(self, rows):
        self.rows = rows

    def get(self, version_id):
        return self.rows.get(int(version_id))


def make_service(row, hands, results=None, reviews=None):
    service = ReplayService(
        Settings(db_user="", db_password="", db_name="nodb",
                 worker_enabled=False), db=object())
    service.experiment_repo = FakeExperimentRepo(
        row, hands, results=results, reviews=reviews)
    service.version_repo = FakeVersionRepo({
        10: {"id": 10, "name": "A", "context_mode": "snapshot",
             "table_stats_mode": "snapshot"},
        11: {"id": 11, "name": "B", "context_mode": "none",
             "table_stats_mode": "none"},
    })
    return service


def experiment_row(**overrides):
    row = {
        "id": 1, "name": "ctx-snapshot-vs-none",
        "hypothesis": "injecting table stats improves agreement",
        "baseline_version_id": 10, "variant_version_id": 11,
        "filter": {}, "dev_ratio": 0.7, "repeat_count": 3,
        "status": "done", "job_id": 5,
        "created_at": None, "started_at": None, "finished_at": None,
    }
    row.update(overrides)
    return row


def test_split_is_deterministic_and_has_both_sides():
    included = [
        ({"id": i}, SimpleNamespace(fingerprint=f"fp-{i}"),
         [SimpleNamespace(street="flop")])
        for i in range(1, 21)
    ]
    first = ReplayService._split_experiment_hands(included, "exp", 0.7)
    second = ReplayService._split_experiment_hands(included, "exp", 0.7)
    assert first == second
    splits = {row[2] for row in first}
    assert splits == {"dev", "holdout"}
    assert len(first) == 20
    assert all(row[3] == "flop" for row in first)


def test_get_experiment_reports_hand_counts():
    service = make_service(
        experiment_row(),
        [{"hand_id": 1, "split": "dev"}, {"hand_id": 2, "split": "holdout"},
         {"hand_id": 3, "split": "dev"}],
    )
    item = service.get_experiment(1)
    assert item["hands"] == {"total": 3, "dev": 2, "holdout": 1}
    assert item["jobId"] == 5
    assert item["baselineVersionId"] == 10


def test_experiment_results_aggregate_bucket_and_review():
    results = [
        {"split": "dev", "version_id": 10, "bucket": "一致",
         "source": "model", "violation_tags": "[]",
         "decision_id": 1, "street": "flop", "actual_action": "call",
         "actual_amount": 4.0},
        {"split": "dev", "version_id": 10, "bucket": "动作不同",
         "source": "model", "violation_tags": '["有赔率却弃牌"]',
         "decision_id": 2, "street": "turn", "actual_action": "call",
         "actual_amount": 8.0},
        {"split": "holdout", "version_id": 11, "bucket": "一致",
         "source": "model", "violation_tags": "[]",
         "decision_id": 3, "street": "river", "actual_action": "check",
         "actual_amount": None},
    ]
    reviews = [
        {"split": "dev", "verdict": "good", "tags": "[]"},
        {"split": "dev", "verdict": "bad", "tags": "[]"},
    ]
    service = make_service(
        experiment_row(),
        [{"hand_id": 1, "split": "dev"}, {"hand_id": 2, "split": "holdout"}],
        results=results, reviews=reviews,
    )
    out = service.experiment_results(1)
    assert out["rows"] == 3
    baseline = out["versions"]["10"]
    assert baseline["n"] == 2
    assert baseline["bucket"]["一致"] == 1
    assert baseline["bucket"]["动作不同"] == 1
    assert baseline["agreement"] == 0.5
    assert baseline["violations"]["有赔率却弃牌"] == 1
    variant = out["versions"]["11"]
    assert variant["n"] == 1
    assert variant["agreement"] == 1.0
    assert out["review"] == {"good": 1, "bad": 1}


class FakeResultRepo:
    def __init__(self, rows):
        self.rows = rows

    def for_job(self, job_id, hand_id=None, version_ids=None):
        return [r for r in self.rows if (hand_id is None or r["hand_id"] == hand_id)]


class FakeSetRepo:
    def items(self, set_id):
        return [{"hand_id": 1, "expect_json": '{"assertions":[{"type":"option_kind","value":"call"}]}'}]


class FakePairRepo:
    def __init__(self):
        self.reviews = {}

    def for_experiment(self, experiment_id):
        return self.reviews

    def upsert(self, experiment_id, decision_id, *, preferred, tags=None, note=None):
        self.reviews[decision_id] = {"preferred": preferred, "tags": "[]",
                                     "note": note or ""}
        return self.reviews[decision_id]


def test_pick_result_takes_majority_and_reports_stability():
    service = make_service(experiment_row(), [])
    chosen = service._pick_result([
        {"option_kind": "call", "option_amount": 4, "reason": "a"},
        {"option_kind": "call", "option_amount": 4, "reason": "b"},
        {"option_kind": "fold", "option_amount": None, "reason": "c"},
    ])
    assert chosen["kind"] == "call"
    assert chosen["stable"] is False
    assert chosen["repeatCount"] == 3


def test_assertions_and_pairs_summary():
    service = make_service(
        experiment_row(hand_set_id=3, changed_scope=None),
        [{"hand_id": 1, "split": "dev"}],
        results=[
            {"decision_id": 11, "version_id": 10, "hand_id": 1,
             "street": "turn", "seq": 1, "quality": "exact",
             "actual_action": "call", "actual_amount": 8.0,
             "option_kind": "call", "option_amount": 8.0,
             "reason": "ok", "source": "model", "id": 1},
            {"decision_id": 11, "version_id": 11, "hand_id": 1,
             "street": "turn", "seq": 1, "quality": "exact",
             "actual_action": "call", "actual_amount": 8.0,
             "option_kind": "fold", "option_amount": None,
             "reason": "no", "source": "model", "id": 2},
        ],
    )
    # make_service 的 FakeExperimentRepo 不提供 result_repo，直接替换为可控数据。
    service.result_repo = FakeResultRepo([
        {"decision_id": 11, "version_id": 10, "hand_id": 1,
         "street": "turn", "seq": 1, "quality": "exact",
         "actual_action": "call", "actual_amount": 8.0,
         "option_kind": "call", "option_amount": 8.0,
         "reason": "ok", "source": "model", "id": 1},
        {"decision_id": 11, "version_id": 11, "hand_id": 1,
         "street": "turn", "seq": 1, "quality": "exact",
         "actual_action": "call", "actual_amount": 8.0,
         "option_kind": "fold", "option_amount": None,
         "reason": "no", "source": "model", "id": 2},
    ])
    service.hand_set_repo = FakeSetRepo()
    service.pair_review_repo = FakePairRepo()
    out = service.experiment_pairs(1)
    assert out["summary"]["n"] == 1
    assert out["summary"]["changed"] == 1
    assert out["summary"]["p0Regressions"] == 1
    assert out["rows"][0]["A"]["kind"] == "call"
    assert out["rows"][0]["B"]["kind"] == "fold"
    assert out["rows"][0]["auto"]["p0Pass"] is False
