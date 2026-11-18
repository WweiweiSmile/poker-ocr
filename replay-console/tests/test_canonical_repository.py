"""CanonicalHandRepository：幂等 upsert、同内容归并、多来源并列、primary 切换。

用内存替身库（FakeDB）而不是 MySQL：替身按 SQL 形状分派并维护真状态，
任何没被识别的语句直接报错，所以 SQL 写错表/列/参数顺序都会被抓到。
"""
from __future__ import annotations

import datetime as dt

import pytest

from replay.canonical import content_group_key
from replay.errors import ApiError
from replay.repository import CanonicalHandRepository

FIRST_ID = 2_000_000_000_001


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, 17, hour, minute)


class FakeDB:
    """够用的内存 MySQL 替身：canonical 行 + source 行。"""

    def __init__(self, canonicals=(), sources=(), next_id: int | None = None):
        self.canonicals = {int(r["id"]): dict(r) for r in canonicals}
        self.sources = {(r["source"], int(r["source_hand_id"])): dict(r) for r in sources}
        self.next_id = next_id or (
            max(self.canonicals) + 1 if self.canonicals else FIRST_ID)
        self.calls: list[tuple[str, str, tuple | None]] = []

    # ── 分派 ──
    def one(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("one", text, params))
        if "FROM replay_hand_sources hs" in text:
            row = self.sources.get((params[0], int(params[1])))
            if row is None:
                return None
            canonical = self.canonicals[row["canonical_id"]]
            return {**row, "content_group": canonical["content_group"],
                    "primary_source": canonical["primary_source"]}
        if "FROM replay_canonical_hands WHERE id=%s" in text:
            canonical = self.canonicals.get(int(params[0]))
            return dict(canonical) if canonical else None
        if "SELECT source_hand_id FROM replay_hand_sources" in text:
            canonical_id, source = int(params[0]), params[1]
            ids = sorted(row["source_hand_id"] for row in self.sources.values()
                         if row["canonical_id"] == canonical_id and row["source"] == source)
            return {"source_hand_id": ids[0]} if ids else None
        if "SELECT 1 AS ok FROM replay_hand_sources" in text:
            canonical_id, source, hand_id = int(params[0]), params[1], int(params[2])
            for row in self.sources.values():
                if (row["canonical_id"], row["source"], row["source_hand_id"]) == (
                        canonical_id, source, hand_id):
                    return {"ok": 1}
            return None
        raise AssertionError(f"unexpected SELECT: {text}")

    def query(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("query", text, params))
        if "FROM replay_canonical_hands" in text:
            hero, board, synthetic = params[0], params[1], int(params[2])
            low, high = params[3], params[4]
            return [dict(row) for row in self.canonicals.values()
                    if row["hero_cards"] == hero and row["board"] == board
                    and int(row["is_synthetic"]) == synthetic
                    and row["merged_into"] is None
                    and row["started_at"] is not None and low <= row["started_at"] <= high]
        if "SELECT source, source_hand_id, is_primary FROM replay_hand_sources" in text:
            canonical_id = int(params[0])
            return [{"source": row["source"], "source_hand_id": row["source_hand_id"],
                     "is_primary": row["is_primary"]}
                    for row in self.sources.values() if row["canonical_id"] == canonical_id]
        if text.startswith("SELECT * FROM replay_hand_sources"):
            canonical_id = int(params[0])
            rows = [dict(row) for row in self.sources.values()
                    if row["canonical_id"] == canonical_id]
            rows.sort(key=lambda r: (-int(r["is_primary"]), r["source"], r["source_hand_id"]))
            return rows
        raise AssertionError(f"unexpected SELECT: {text}")

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("execute", text, params))
        if text.startswith("INSERT INTO replay_canonical_hands"):
            (group, primary_source, title, hero, board, position,
             started, synthetic) = params
            canonical_id = self.next_id
            self.next_id += 1
            self.canonicals[canonical_id] = {
                "id": canonical_id, "content_group": group,
                "primary_source": primary_source, "title": title,
                "hero_cards": hero, "board": board, "hero_position": position,
                "started_at": started, "is_synthetic": int(synthetic),
                "merged_into": None,
            }
            return canonical_id
        if text.startswith("INSERT INTO replay_hand_sources"):
            canonical_id, source, hand_id, fingerprint, primary, linked_by = params
            self.sources[(source, int(hand_id))] = {
                "canonical_id": int(canonical_id), "source": source,
                "source_hand_id": int(hand_id), "fingerprint": fingerprint,
                "is_primary": int(primary), "linked_by": linked_by,
            }
            return 1
        if text.startswith("UPDATE replay_canonical_hands SET content_group=%s"):
            group, canonical_id = params
            row = self.canonicals[int(canonical_id)]
            if row["content_group"] is None:
                row["content_group"] = group
            return 1
        if text.startswith("UPDATE replay_canonical_hands SET primary_source=%s"):
            source, canonical_id = params
            self.canonicals[int(canonical_id)]["primary_source"] = source
            return 1
        if text.startswith("UPDATE replay_hand_sources SET is_primary = CASE"):
            source, hand_id, canonical_id = params
            for row in self.sources.values():
                if row["canonical_id"] != int(canonical_id):
                    continue
                row["is_primary"] = int(row["source"] == source
                                        and row["source_hand_id"] == int(hand_id))
            return 1
        if text.startswith("UPDATE replay_hand_sources SET canonical_id=%s"):
            canonical_id, source, hand_id = params
            self.sources[(source, int(hand_id))]["canonical_id"] = int(canonical_id)
            return 1
        if text.startswith("UPDATE replay_hand_sources SET fingerprint=%s"):
            fingerprint, source, hand_id = params
            row = self.sources[(source, int(hand_id))]
            row["fingerprint"] = fingerprint
            return 1
        raise AssertionError(f"unexpected write: {text}")

    # ── 断言辅助 ──
    def source_rows(self) -> list[dict]:
        return sorted(self.sources.values(), key=lambda r: (r["source"], r["source_hand_id"]))

    def primary_of(self, canonical_id: int) -> dict | None:
        rows = [r for r in self.sources.values()
                if r["canonical_id"] == canonical_id and r["is_primary"]]
        assert len(rows) <= 1, f"canonical {canonical_id} 有多个 primary：{rows}"
        return rows[0] if rows else None


def live_source(canonical_id, hand_id=1001, started=at(14, 0), **overrides):
    row = {
        "canonical_id": canonical_id, "source": "live", "source_hand_id": hand_id,
        "fingerprint": "a" * 64, "is_primary": 1, "linked_by": "auto",
    }
    row.update(overrides)
    return row


_UNSET = object()


def canonical_row(canonical_id=FIRST_ID, hero="KhQh", board="Ks7d2c",
                  started=at(14, 0), group=_UNSET, primary="live", synthetic=0,
                  merged_into=None):
    if group is _UNSET:
        group = content_group_key(hero, board, started)
    return {
        "id": canonical_id, "content_group": group, "primary_source": primary,
        "title": None, "hero_cards": hero, "board": board, "hero_position": None,
        "started_at": started, "is_synthetic": synthetic, "merged_into": merged_into,
    }


def upsert(repo, **overrides):
    params = {
        "source": "live", "source_hand_id": 1001, "hero_cards": "KhQh",
        "board": "Ks7d2c", "started_at": at(14, 0), "fingerprint": "a" * 64,
        "title": "BTN KQs", "hero_position": "BTN", "is_synthetic": False,
    }
    params.update(overrides)
    return repo.upsert_source(**params)


# ── 新建 ────────────────────────────────────────────────────────

def test_first_source_creates_one_canonical_and_one_source():
    db = FakeDB()
    result = upsert(CanonicalHandRepository(db))

    assert result["created"] is True
    assert result["canonical_id"] == FIRST_ID
    assert result["primary_source"] == "live"
    assert result["content_group"] == content_group_key("KhQh", "Ks7d2c", at(14, 0))
    assert len(db.canonicals) == 1
    assert db.primary_of(FIRST_ID)["source"] == "live"
    assert db.canonicals[FIRST_ID]["hero_cards"] == "KhQh"


def test_missing_started_at_creates_ungrouped_canonical():
    db = FakeDB()
    result = upsert(CanonicalHandRepository(db), started_at=None)

    assert result["created"] is True
    assert result["content_group"] is None
    assert db.canonicals[FIRST_ID]["content_group"] is None


# ── 幂等 ────────────────────────────────────────────────────────

def test_repeat_upsert_keeps_one_source_row_and_one_canonical():
    db = FakeDB()
    repo = CanonicalHandRepository(db)

    first = upsert(repo)
    second = upsert(repo, title="again")

    assert first["canonical_id"] == second["canonical_id"]
    assert second["created"] is False
    assert len(db.sources) == 1
    assert len(db.canonicals) == 1


def test_existing_source_only_fixes_fingerprint_and_keeps_manual_primary():
    db = FakeDB(
        canonicals=[canonical_row(primary="live")],
        sources=[live_source(FIRST_ID), {
            "canonical_id": FIRST_ID, "source": "review", "source_hand_id": 7,
            "fingerprint": "b" * 64, "is_primary": 0, "linked_by": "auto",
        }],
    )
    repo = CanonicalHandRepository(db)

    result = upsert(repo, fingerprint="c" * 64)

    assert result["created"] is False
    assert result["canonical_id"] == FIRST_ID
    assert db.sources[("live", 1001)]["fingerprint"] == "c" * 64
    assert db.primary_of(FIRST_ID)["source"] == "live"  # 人工切换不被回填覆盖
    assert not [call for call in db.calls if "SET is_primary = CASE" in call[1]]


# ── 同内容归并 ──────────────────────────────────────────────────

def test_same_content_in_another_source_joins_the_same_canonical():
    db = FakeDB(canonicals=[canonical_row()], sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    result = repo.upsert_source(source="review", source_hand_id=7,
                                hero_cards="QhKh", board="Ks7d2c",
                                started_at=at(14, 20), fingerprint="b" * 64)

    assert result["created"] is False
    assert result["canonical_id"] == FIRST_ID
    assert len(db.canonicals) == 1
    assert len(db.sources) == 2
    assert db.primary_of(FIRST_ID)["source"] == "review"  # 人工记录优先
    assert db.canonicals[FIRST_ID]["primary_source"] == "review"


def test_far_apart_or_different_board_starts_a_new_canonical():
    db = FakeDB(canonicals=[canonical_row()], sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    late = repo.upsert_source(source="review", source_hand_id=7, hero_cards="KhQh",
                              board="Ks7d2c", started_at=at(14, 31))
    other = repo.upsert_source(source="review", source_hand_id=8, hero_cards="KhQh",
                               board="Ks7d2c3h", started_at=at(14, 5))

    assert late["created"] is True and late["canonical_id"] == FIRST_ID + 1
    assert other["created"] is True and other["canonical_id"] == FIRST_ID + 2


def test_link_fills_a_missing_content_group():
    db = FakeDB(canonicals=[canonical_row(group=None)], sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    result = repo.upsert_source(source="review", source_hand_id=5, hero_cards="KhQh",
                                board="Ks7d2c", started_at=at(14, 25))

    assert result["content_group"] == content_group_key("KhQh", "Ks7d2c", at(14, 0))
    assert db.canonicals[FIRST_ID]["content_group"] == result["content_group"]


def test_synthetic_source_never_joins_a_real_canonical():
    # 构造手牌自成一列：即便调用方把 is_synthetic 传成 False，也不污染真实手牌
    db = FakeDB(canonicals=[canonical_row()], sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    result = repo.upsert_source(source="synthetic", source_hand_id=5, hero_cards="KhQh",
                                board="Ks7d2c", started_at=at(14, 5),
                                is_synthetic=False)

    assert result["created"] is True
    assert result["canonical_id"] == FIRST_ID + 1
    assert db.canonicals[result["canonical_id"]]["is_synthetic"] == 1


def test_resoft_merged_source_follows_the_pointer_to_the_kept_row():
    # 人工合并只改指针：已存在的源记录重跑时整体改挂到保留行，旧行不删
    db = FakeDB(canonicals=[canonical_row(merged_into=FIRST_ID + 10),
                            canonical_row(FIRST_ID + 10)],
                sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    result = upsert(repo)

    assert result["created"] is False
    assert result["canonical_id"] == FIRST_ID + 10
    assert db.sources[("live", 1001)]["canonical_id"] == FIRST_ID + 10
    assert FIRST_ID in db.canonicals  # 旧行保留


def test_merged_into_rows_are_not_auto_link_targets():
    db = FakeDB(canonicals=[canonical_row(merged_into=FIRST_ID + 10)],
                sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    result = repo.upsert_source(source="review", source_hand_id=7, hero_cards="KhQh",
                                board="Ks7d2c", started_at=at(14, 5))

    assert result["created"] is True


# ── primary 切换 ────────────────────────────────────────────────

def test_manual_primary_switch_survives_a_lower_ranked_link():
    db = FakeDB(canonicals=[canonical_row(primary="live")],
                sources=[live_source(FIRST_ID),
                         {"canonical_id": FIRST_ID, "source": "review", "source_hand_id": 7,
                          "fingerprint": None, "is_primary": 0, "linked_by": "auto"}])
    repo = CanonicalHandRepository(db)

    switched = repo.set_primary(FIRST_ID, "live")

    assert switched["primary_source"] == "live"
    assert db.primary_of(FIRST_ID)["source"] == "live"

    repo.upsert_source(source="synthetic", source_hand_id=9, hero_cards="KhQh",
                       board="Ks7d2c", started_at=at(14, 10))

    assert db.primary_of(FIRST_ID)["source"] == "live"


def test_set_primary_needs_an_existing_source_row():
    db = FakeDB(canonicals=[canonical_row()], sources=[live_source(FIRST_ID)])
    repo = CanonicalHandRepository(db)

    with pytest.raises(ApiError) as exc:
        repo.set_primary(FIRST_ID, "review")
    assert exc.value.status_code == 404


def test_set_primary_without_hand_id_picks_the_sources_only_row():
    db = FakeDB(canonicals=[canonical_row(primary="review")],
                sources=[live_source(FIRST_ID, is_primary=0),
                         {"canonical_id": FIRST_ID, "source": "review", "source_hand_id": 7,
                          "fingerprint": None, "is_primary": 1, "linked_by": "auto"}])
    repo = CanonicalHandRepository(db)

    result = repo.set_primary(FIRST_ID, "review")

    assert result["primary_source"] == "review"
    assert db.primary_of(FIRST_ID)["source_hand_id"] == 7


# ── 读 ──────────────────────────────────────────────────────────

def test_sources_lists_primary_first():
    db = FakeDB(canonicals=[canonical_row()],
                sources=[live_source(FIRST_ID),
                         {"canonical_id": FIRST_ID, "source": "review", "source_hand_id": 7,
                          "fingerprint": None, "is_primary": 0, "linked_by": "auto"}])
    repo = CanonicalHandRepository(db)

    rows = repo.sources(FIRST_ID)

    assert [row["source"] for row in rows] == ["live", "review"]


def test_unknown_source_is_rejected_before_touching_the_database():
    db = FakeDB()
    repo = CanonicalHandRepository(db)

    with pytest.raises(ApiError) as exc:
        repo.upsert_source(source="manual", source_hand_id=1)

    assert exc.value.status_code == 422
    assert db.calls == []
