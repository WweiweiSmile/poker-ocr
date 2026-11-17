"""手牌库归组/筛选纯逻辑（#53）。"""
from __future__ import annotations

from replay import library
from replay.service import ReplayService


def test_canonical_source_maps_legacy_db_to_review():
    assert library.canonical_source({"source": "db"}) == "review"
    assert library.canonical_source({}) == "review"
    assert library.canonical_source({"source": "live"}) == "live"
    assert library.canonical_source({"source": "synthetic"}) == "synthetic"


def test_build_groups_drops_deleted_sources_and_empty_groups():
    canonical_rows = [
        {"id": 1, "content_group": "g1", "title": "A"},
        {"id": 2, "content_group": "g2", "title": "B"},
    ]
    source_rows = [
        {"canonical_id": 1, "source": "review", "source_hand_id": 10,
         "is_primary": 1},
        {"canonical_id": 1, "source": "live", "source_hand_id": 20,
         "is_primary": 0},
        {"canonical_id": 2, "source": "live", "source_hand_id": 30,
         "is_primary": 1},
    ]
    visible = {("live", 20)}

    groups = library.build_groups(canonical_rows, source_rows, visible)

    # canonical 2 的唯一来源已删除 → 整组不出现。
    assert [group["canonicalId"] for group in groups] == [1]
    group = groups[0]
    # 只剩 live 一条；primary 的 review 已删除，代表落到可见来源。
    assert [s["source_hand_id"] for s in group["sources"]] == [10, 20]
    assert [s["source_hand_id"] for s in group["visibleSources"]] == [20]
    assert group["representative"]["source_hand_id"] == 20


def test_matches_filters_source_reads_item_sources():
    item = {"sources": [{"source": "review"}]}
    assert library.matches_filters(item, {"sources": {"review"}})
    assert not library.matches_filters(item, {"sources": {"live"}})
    # 多来源组：命中任意一个来源即可。
    multi = {"sources": [{"source": "review"}, {"source": "live"}]}
    assert library.matches_filters(multi, {"sources": {"live"}})


def test_matches_filters_buckets_and_case_count():
    item = {
        "heroPosition": "CO",
        "result": "win",
        "quality": "exact",
        "caseCount": 2,
        "buckets": {"street": ["flop"], "hand_tier": ["TPTK"]},
    }
    assert library.matches_filters(item, {"positions": {"CO"}})
    assert not library.matches_filters(item, {"positions": {"BB"}})
    assert library.matches_filters(item, {"results": {"win"}})
    assert library.matches_filters(item, {"qualities": {"exact"}})
    assert library.matches_filters(item, {"hasCase": True})
    assert not library.matches_filters(item, {"hasCase": False})


def test_decision_quality_three_states():
    assert library.decision_quality(0, 0) == "invalid"
    assert library.decision_quality(3, 1) == "approx"
    assert library.decision_quality(3, 0) == "exact"


def test_meta_vocab_is_bucket_single_source():
    vocab = library.meta_vocab()
    assert [item["key"] for item in vocab] == list(library.DIMENSIONS)
    facing = next(item for item in vocab if item["key"] == "facing")
    assert facing["label"] == "面对下注"
    assert {"value": "1", "label": "面对下注"} in facing["options"]
    hand_tier = next(item for item in vocab if item["key"] == "hand_tier")
    assert {"value": "TPTK", "label": "顶对强踢"} in hand_tier["options"]


def test_bucket_label_of_coerces_facing_to_int():
    assert library.bucket_label_of("facing", "1") == "面对下注"
    assert library.bucket_label_of("facing", 0) == "未面对下注"
    assert library.bucket_label_of("hand_tier", "TPTK") == "顶对强踢"
    # 词表外的脏值原样显示，不抛异常。
    assert library.bucket_label_of("hand_tier", "??") == "??"


def test_bucket_values_keeps_dimension_order():
    chips = library.bucket_values({
        "hand_tier": {"TPTK"},
        "street": {"flop", "preflop"},
    })
    assert [chip["dimension"] for chip in chips] == ["street", "hand_tier"]
    assert [value["value"] for value in chips[0]["values"]] == ["flop", "preflop"]
    assert chips[1]["values"][0] == {"value": "TPTK", "label": "顶对强踢"}


def test_dedupe_source_decisions_prefers_primary_source():
    service = object.__new__(ReplayService)
    rows = [
        {"id": 1, "hand_id": 20, "street": "flop", "seq": 1, "quality": "approx"},
        {"id": 2, "hand_id": 10, "street": "flop", "seq": 1, "quality": "exact"},
        {"id": 3, "hand_id": 10, "street": "turn", "seq": 2, "quality": "exact"},
    ]
    picked = service._dedupe_source_decisions(rows, {10: 0, 20: 1})
    assert picked[("flop", 1)]["id"] == 2
    assert picked[("turn", 2)]["id"] == 3


class _FakeCanonicalRepo:
    def __init__(self, canonicals, edges):
        self.canonicals = {int(row["id"]): row for row in canonicals}
        self.edges = edges

    def get(self, canonical_id):
        return self.canonicals.get(int(canonical_id))

    def sources(self, canonical_id):
        return [edge for edge in self.edges
                if int(edge["canonical_id"]) == int(canonical_id)]

    def for_hand(self, hand_id):
        return [edge for edge in self.edges
                if int(edge["source_hand_id"]) == int(hand_id)]


class _FakeHandRepo:
    def __init__(self, visible_ids):
        self.visible = set(visible_ids)

    def get_row(self, hand_id):
        return {"id": int(hand_id)} if int(hand_id) in self.visible else None


def _service(canonicals, edges, visible):
    service = object.__new__(ReplayService)
    service.canonical_repo = _FakeCanonicalRepo(canonicals, edges)
    service.hand_repo = _FakeHandRepo(visible)
    return service


def test_canonical_for_hand_accepts_canonical_and_source_ids():
    canonicals = [{"id": 2_000_000_000_001, "content_group": "g"}]
    edges = [
        {"canonical_id": 2_000_000_000_001, "source": "review",
         "source_hand_id": 990001, "is_primary": 1},
        {"canonical_id": 2_000_000_000_001, "source": "live",
         "source_hand_id": 1_000_000_000_001, "is_primary": 0},
    ]
    service = _service(canonicals, edges, {990001, 1_000_000_000_001})

    canonical, sources = service._canonical_for_hand(2_000_000_000_001)
    assert canonical["content_group"] == "g"
    assert len(sources) == 2

    # 导入手牌 id 走 live 边，复盘 id 走 review 边。
    canonical, sources = service._canonical_for_hand(1_000_000_000_001)
    assert canonical["id"] == 2_000_000_000_001
    assert [s["source"] for s in sources] == ["review", "live"]
    canonical, _sources = service._canonical_for_hand(990001)
    assert canonical["id"] == 2_000_000_000_001


def test_resolve_hand_id_skips_deleted_sources_and_404s():
    canonicals = [{"id": 2_000_000_000_002}]
    edges = [
        {"canonical_id": 2_000_000_000_002, "source": "review",
         "source_hand_id": 990002, "is_primary": 1},
        {"canonical_id": 2_000_000_000_002, "source": "live",
         "source_hand_id": 1_000_000_000_002, "is_primary": 0},
    ]
    service = _service(canonicals, edges, {1_000_000_000_002})
    # review 源已删，回落到 live 源。
    assert service.resolve_hand_id(2_000_000_000_002) == 1_000_000_000_002
    # 源 id 原样返回，不额外查库。
    assert service.resolve_hand_id(990002) == 990002

    empty = _service(canonicals, edges, set())
    try:
        empty.resolve_hand_id(2_000_000_000_002)
    except Exception as exc:  # ApiError
        assert getattr(exc, "code", "") == "HAND_NOT_FOUND"
    else:
        raise AssertionError("expected ApiError")


def test_last_run_at_drives_sort_and_not_run_filter():
    import datetime as dt

    now = dt.datetime.now()
    recent = {"createdAt": now.isoformat(),
              "lastRunAt": (now - dt.timedelta(hours=2)).isoformat()}
    never = {"createdAt": now.isoformat(), "lastRunAt": None}

    assert library.sort_value(recent, "last_run") > library.sort_value(
        never, "last_run")
    # 最近跑过的手牌不应出现在「超过 7 天没跑」里。
    assert not library.matches_filters(recent, {"notRunDays": 7})
    assert library.matches_filters(never, {"notRunDays": 7})


def test_decision_stats_aggregates_quality_and_buckets():
    rows = [
        {"quality": "exact", "street": "flop", "position_bucket": "LP"},
        {"quality": "approx", "street": "turn", "position_bucket": "LP"},
    ]
    stats, buckets = library.decision_stats(rows)
    assert stats == {"count": 2, "approx": 1, "quality": "approx"}
    assert buckets["street"] == {"flop", "turn"}
    assert buckets["position_bucket"] == {"LP"}

    empty, no_buckets = library.decision_stats([])
    assert empty["quality"] == "invalid"
    assert no_buckets == {}
