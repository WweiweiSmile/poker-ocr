"""013 回填：源记录 → canonical、决策点 → 七个桶（#52）。

用内存替身库（FakeDB）而不是 MySQL：替身维护真状态、按 SQL 形状分派，
没被识别的语句直接报错。测试只看回填的对外行为（对账数字、表里的行、
决策点的桶值），不碰内部函数。
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from replay.backfill import (
    ReadOnlyGuard,
    fill_decisions,
    group_lines,
    load_source_rows,
    materialize_sources,
    report_lines,
    run_backfill,
)
from replay.canonical import CANONICAL_HAND_ID_BASE
from replay.versioning import CURRENT_RECONSTRUCT_VERSION

IMPORTED = 1_000_000_001_001
REVIEW = 990_001


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, 17, hour, minute)


_KEEP = object()  # 「没传」和「显式传 None（缺时间）」要能区分开


def imported(hand_id: int, cards: str = "AhKh", board: str = "Qd7c2s",
             started=_KEEP, *, deleted: bool = False, source: str = "live"):
    return {"id": hand_id, "hero_cards": cards, "board": board,
            "hero_position": "CO", "title": f"#{hand_id}",
            "started_at": at(10, 0) if started is _KEEP else started,
            "fingerprint": f"fp{hand_id}", "source": source,
            "deleted_at": at(20, 0) if deleted else None}


def review(hand_id: int, cards: str = "AhKh", board: str = "Qd7c2s",
           started=_KEEP, *, deleted: bool = False):
    return {"id": hand_id, "hero_cards": cards, "board": board,
            "hero_position": "CO", "title": f"复盘 {hand_id}",
            "started_at": at(10, 2) if started is _KEEP else started,
            "fingerprint": f"hash{hand_id}",
            "deleted_at": at(20, 0) if deleted else None}


def decision(decision_id: int, hand_id: int, *,
             street: str = "flop", seq: int = 0,
             snapshot: dict | None = None,
             version: str = CURRENT_RECONSTRUCT_VERSION):
    return {"id": decision_id, "hand_id": hand_id, "street": street, "seq": seq,
            "snapshot_json": "" if snapshot is None else json.dumps(snapshot),
            "reconstruct_version": version}


def snapshot(*, street: str = "flop", to_call: float = 5.0,
             hero: tuple = ("Ah", "Ad"), board: tuple = ("Qd", "7c", "2s"),
             seat: str = "3", name: str = "对手A"):
    """一手有翻前加注、翻牌 continue-bet 的快照（camelCase，同 snapshot_to_dict）。"""
    return {
        "handId": str(IMPORTED), "street": street,
        "board": list(board), "heroCards": list(hero),
        "heroPosition": "CO", "toCall": to_call, "bombPot": False,
        "opponents": [{"seatId": seat, "name": name, "position": "BTN"}],
        "streets": [
            {"street": "preflop", "potStart": 0.0, "actions": [
                {"seatId": seat, "name": name, "position": "BTN",
                 "action": "raise", "amount": 6.0, "isHero": False},
                {"seatId": "hero", "position": "CO", "action": "call",
                 "amount": 6.0, "isHero": True},
            ]},
            {"street": "flop", "potStart": 12.0, "actions": [
                {"seatId": seat, "name": name, "position": "BTN",
                 "action": "bet", "amount": 5.0, "isHero": False},
            ]},
        ],
    }


class ProfileStore:
    """画像库的最小替身：只回答 read_for 的 seat → OpponentRead。"""

    def __init__(self, reads: dict | None = None):
        self.reads = reads or {}

    def read_for(self, opponents) -> dict:
        if "3" not in self.reads:
            return {}
        out = {}
        for o in opponents or ():
            seat = getattr(o, "seat_id", None)
            if seat is None and isinstance(o, Mapping):
                seat = o.get("seatId") or o.get("seat_id")
            if seat is not None:
                out[str(seat)] = self.reads["3"]
        return out


class NameProfileStore:
    """按名字回答画像（真画像库就是这么匹配的：座位号会跨手重复）。"""

    def __init__(self, by_name: dict):
        self.by_name = by_name

    def read_for(self, opponents) -> dict:
        out = {}
        for o in opponents or ():
            name = getattr(o, "name", None)
            if name in self.by_name:
                out[str(getattr(o, "seat_id", ""))] = self.by_name[name]
        return out


class FakeDB:
    """够用的内存 MySQL 替身：源表、canonical、sources、decisions。"""

    def __init__(self, *, imported_rows=(), review_rows=(), decisions=(),
                 canonicals=(), sources=()):
        self.imported = [dict(r) for r in imported_rows]
        self.review = [dict(r) for r in review_rows]
        self.decisions = {int(r["id"]): dict(r) for r in decisions}
        self.canonicals = {int(r["id"]): dict(r) for r in canonicals}
        self.sources = {(r["source"], int(r["source_hand_id"])): dict(r)
                        for r in sources}
        self.next_id = CANONICAL_HAND_ID_BASE + 1
        self.calls: list[tuple[str, str]] = []

    # ── 读 ──
    def one(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.calls.append(("one", text))
        if "FROM replay_hand_sources hs" in text:
            row = self.sources.get((params[0], int(params[1])))
            if row is None:
                return None
            canonical = self.canonicals[row["canonical_id"]]
            return {**row, "content_group": canonical["content_group"],
                    "primary_source": canonical["primary_source"],
                    "merged_into": canonical.get("merged_into")}
        if "SELECT merged_into FROM replay_canonical_hands WHERE id=%s" in text:
            row = self.canonicals.get(int(params[0]))
            return {"merged_into": row.get("merged_into")} if row else None
        raise AssertionError(f"unexpected SELECT: {text}")

    def query(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.calls.append(("query", text))
        if "FROM replay_imported_hands" in text:
            return [_source_row(r) for r in self._source_rows(self.imported, params)]
        if "FROM review_hands" in text:
            # 真实查询带 WHERE deleted_at IS NULL：用户删掉的复盘手牌不回填
            visible = [r for r in self.review if r.get("deleted_at") is None]
            return [_source_row(r) for r in self._source_rows(visible, params)]
        if "FROM replay_canonical_hands" in text and "hero_cards = %s" in text:
            hero, board, synthetic = params[0], params[1], int(params[2])
            low, high = params[3], params[4]
            return [{"id": row["id"], "started_at": row["started_at"],
                     "content_group": row["content_group"]}
                    for row in self.canonicals.values()
                    if row["hero_cards"] == hero and row["board"] == board
                    and int(row["is_synthetic"]) == synthetic
                    and row.get("merged_into") is None
                    and row["started_at"] is not None
                    and low <= row["started_at"] <= high]
        if "SELECT source, source_hand_id, is_primary FROM replay_hand_sources" in text:
            canonical_id = int(params[0])
            return [{"source": row["source"],
                     "source_hand_id": row["source_hand_id"],
                     "is_primary": row["is_primary"]}
                    for row in self.sources.values()
                    if row["canonical_id"] == canonical_id]
        if "FROM replay_decisions WHERE reconstruct_version = %s" in text:
            rows = [dict(r) for r in self.decisions.values()
                    if r["reconstruct_version"] == params[0]]
            rows.sort(key=lambda r: (r["hand_id"], r["street"], r["seq"]))
            return rows
        if "FROM replay_decisions WHERE reconstruct_version <> %s" in text:
            return [{"n": sum(1 for r in self.decisions.values()
                              if r["reconstruct_version"] != params[0])}]
        if "SELECT source, COUNT(*) AS n FROM replay_hand_sources GROUP BY source" in text:
            counts: dict[str, int] = {}
            for key in self.sources:
                counts[key[0]] = counts.get(key[0], 0) + 1
            return [{"source": s, "n": n} for s, n in sorted(counts.items())]
        if "SELECT COUNT(*) AS n FROM replay_hand_sources" in text:
            return [{"n": len(self.sources)}]
        if "FROM (SELECT canonical_id FROM replay_hand_sources" in text:
            per: dict[int, int] = {}
            for row in self.sources.values():
                per[row["canonical_id"]] = per.get(row["canonical_id"], 0) + 1
            return [{"n": sum(1 for n in per.values() if n > 1)}]
        if "FROM (SELECT content_group FROM replay_canonical_hands" in text:
            per_group: dict[str, int] = {}
            for row in self.canonicals.values():
                group = row.get("content_group")
                if group is None or row.get("merged_into") is not None:
                    continue
                per_group[group] = per_group.get(group, 0) + 1
            return [{"n": sum(1 for n in per_group.values() if n > 1)}]
        if "GROUP_CONCAT" in text:
            per: dict[int, list] = {}
            for row in self.sources.values():
                canonical = self.canonicals.get(row["canonical_id"])
                if canonical is None or canonical.get("merged_into") is not None:
                    continue
                per.setdefault(row["canonical_id"], []).append(row)
            groups = [(cid, rows) for cid, rows in per.items() if len(rows) > 1]
            groups.sort(key=lambda item: (-len(item[1]), item[0]))
            limit = int(params[0]) if params else None
            return [{"canonical_id": cid,
                     "hero_cards": self.canonicals[cid]["hero_cards"],
                     "board": self.canonicals[cid]["board"],
                     "started_at": self.canonicals[cid]["started_at"],
                     "primary_source": self.canonicals[cid]["primary_source"],
                     "n_sources": len(rows),
                     "sources": ",".join(
                         f"{r['source']}:{r['source_hand_id']}"
                         for r in sorted(rows, key=lambda r: (r["source"],
                                                              r["source_hand_id"])))}
                    for cid, rows in groups[:limit]]
        if "SELECT COUNT(*) AS n FROM replay_canonical_hands WHERE merged_into IS NULL" in text:
            return [{"n": sum(1 for r in self.canonicals.values()
                              if r.get("merged_into") is None)}]
        raise AssertionError(f"unexpected SELECT: {text}")

    @staticmethod
    def _source_rows(rows, params):
        """按时间排序（缺时间的排最后，与回填同口径），再套 LIMIT。"""
        ordered = sorted(rows, key=lambda r: (r["started_at"] is None,
                                              r["started_at"] or dt.datetime.min,
                                              r["id"]))
        limit = None if not params else params[0]
        return ordered[:limit]

    def scalar(self, sql, params=None):
        return self.query(sql, params)[0]["n"]

    # ── 写 ──
    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.calls.append(("execute", text))
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
            for key, row in self.sources.items():
                if row["canonical_id"] == int(canonical_id):
                    row["is_primary"] = int(key[0] == source
                                            and key[1] == int(hand_id))
            return 1
        if text.startswith("UPDATE replay_hand_sources SET fingerprint=%s"):
            fingerprint, source, hand_id = params
            self.sources[(source, int(hand_id))]["fingerprint"] = fingerprint
            return 1
        if text.startswith("UPDATE replay_hand_sources SET canonical_id=%s"):
            canonical_id, source, hand_id = params
            self.sources[(source, int(hand_id))]["canonical_id"] = int(canonical_id)
            return 1
        if text.startswith("UPDATE replay_decisions"):
            (canonical_id, position_bucket, hand_tier, board_texture, pot_type,
             facing, opponent_profile, decision_id) = params
            row = self.decisions[int(decision_id)]
            row.update({"canonical_id": canonical_id,
                        "position_bucket": position_bucket,
                        "hand_tier": hand_tier,
                        "board_texture": board_texture,
                        "pot_type": pot_type, "facing": facing,
                        "opponent_profile": opponent_profile})
            return 1
        raise AssertionError(f"unexpected write: {text}")

    def execute_many(self, sql, seq_params):
        return sum(self.execute(sql, params) for params in seq_params)


_SOURCE_COLUMNS = ("id", "hero_cards", "board", "hero_position", "title",
                   "started_at", "fingerprint", "deleted_at")


def _source_row(row: dict) -> dict:
    """替身 SELECT 的结果行；真实查询没有 source 列，这里按需带上以便断言。"""
    out = {key: row.get(key) for key in _SOURCE_COLUMNS}
    if "source" in row:
        out["source"] = row["source"]
    return out


def writes_to(db: FakeDB) -> list[str]:
    return [text for kind, text in db.calls
            if kind in ("execute", "execute_many") and "review_hands" in text]


# ─────────────────────────── 物化 ───────────────────────────

def test_materialize_merges_same_content_and_maps_every_source():
    db = FakeDB(imported_rows=[imported(IMPORTED), imported(IMPORTED + 1, started=at(10, 5)),
                               imported(IMPORTED + 2, cards="QdQc", board="", started=at(12, 0))],
                review_rows=[review(REVIEW)])

    stats = materialize_sources(db, load_source_rows(db))

    assert stats["rows"] == 4
    assert stats["created"] == 2 and stats["linked"] == 2
    assert len(db.canonicals) == 2
    assert len(db.sources) == 4
    merged = db.sources[("review", REVIEW)]["canonical_id"]
    assert db.sources[("live", IMPORTED)]["canonical_id"] == merged
    assert db.sources[("live", IMPORTED + 1)]["canonical_id"] == merged
    assert db.sources[("live", IMPORTED + 2)]["canonical_id"] != merged
    assert db.canonicals[merged]["primary_source"] == "review"
    assert db.sources[("review", REVIEW)]["is_primary"] == 1
    assert all(cid >= CANONICAL_HAND_ID_BASE for cid in db.canonicals)


def test_materialize_is_idempotent():
    db = FakeDB(imported_rows=[imported(IMPORTED)], review_rows=[review(REVIEW)])

    first = materialize_sources(db, load_source_rows(db))
    second = materialize_sources(db, load_source_rows(db))

    assert (first["created"], first["linked"]) == (1, 1)
    assert (second["created"], second["linked"]) == (0, 2)
    assert len(db.canonicals) == 1 and len(db.sources) == 2


def test_materialize_keeps_soft_deleted_sources_and_reports_them():
    db = FakeDB(imported_rows=[imported(IMPORTED, deleted=True)])

    stats = materialize_sources(db, load_source_rows(db))

    assert len(db.sources) == 1
    assert stats["sources"] == {"live": 1}
    assert stats["deleted"] == 1


# ─────────────────────────── 决策点补料 ───────────────────────────

def _materialized(*, decisions):
    db = FakeDB(imported_rows=[imported(IMPORTED), imported(IMPORTED + 1)],
                review_rows=[review(REVIEW)], decisions=decisions)
    mat = materialize_sources(db, load_source_rows(db))
    return db, mat


def test_fill_decisions_backfills_seven_buckets_and_skips_missing_snapshot():
    unmapped = 1_000_000_009_999
    db, mat = _materialized(decisions=[
        decision(1, IMPORTED, snapshot=snapshot()),
        decision(2, IMPORTED + 1, snapshot=None),
        decision(3, unmapped, snapshot=snapshot()),
        decision(4, IMPORTED, snapshot=snapshot(), version="recon-1"),
    ])

    stats = fill_decisions(db, mat["mapping"],
                           profile_store=ProfileStore({"3": SimpleNamespace(archetype="tag")}))

    row = db.decisions[1]
    assert row["canonical_id"] == db.sources[("live", IMPORTED)]["canonical_id"]
    assert row["position_bucket"] == "LP"
    assert row["hand_tier"] == "OVERPAIR"  # §3.3 要英文稳定键，不存 coach 文案
    assert row["pot_type"] == "single"
    assert row["facing"] == 1
    assert row["opponent_profile"] == "紧凶"
    assert row["board_texture"] is not None
    assert stats["updated"] == 1
    assert stats["skipped_no_snapshot"] == 1
    assert stats["unmapped_hand"] == 1
    # 旧版本按设计不动
    assert db.decisions[4].get("canonical_id") is None
    assert stats["other_versions"] == 1


def test_fill_decisions_without_profile_store_is_unknown_not_missing():
    db, mat = _materialized(decisions=[decision(1, IMPORTED, snapshot=snapshot())])

    stats = fill_decisions(db, mat["mapping"])

    assert db.decisions[1]["opponent_profile"] == "未知"
    assert stats["filled"]["opponent_profile"] == 1


def test_fill_decisions_is_idempotent():
    db, mat = _materialized(decisions=[decision(1, IMPORTED, snapshot=snapshot())])

    fill_decisions(db, mat["mapping"])
    first = dict(db.decisions[1])
    fill_decisions(db, mat["mapping"])

    assert db.decisions[1] == first


# ─────────────────────────── 对账 / 只读 ───────────────────────────

def test_run_backfill_writes_then_reports_full_coverage():
    db = FakeDB(imported_rows=[imported(IMPORTED), imported(IMPORTED + 1, cards="QdQc", board="")],
                review_rows=[review(REVIEW)],
                decisions=[decision(1, IMPORTED, snapshot=snapshot())])

    stats = run_backfill(db, profile_store=ProfileStore())

    assert stats["sources"] == {"live": 2, "review": 1}
    assert stats["sources_total"] == 3
    assert stats["mapped_total"] == 3
    assert stats["canonical_total"] == 2
    assert stats["multi_source_groups"] == 1
    assert stats["decisions"]["updated"] == 1
    lines = "\n".join(report_lines(stats))
    assert "3/3" in lines and "100.0%" in lines
    assert "100.0%" in lines and "多源组" in lines
    assert writes_to(db) == []


def test_run_backfill_twice_does_not_duplicate():
    db = FakeDB(imported_rows=[imported(IMPORTED)], review_rows=[review(REVIEW)],
                decisions=[decision(1, IMPORTED, snapshot=snapshot())])

    run_backfill(db)
    second = run_backfill(db)

    assert second["canonical_total"] == 1
    assert second["mapped_total"] == 2
    assert len(db.sources) == 2


def test_write_guard_rejects_review_hands_writes_and_allows_selects():
    db = FakeDB()
    guarded = ReadOnlyGuard(db)

    list(guarded.query("SELECT id FROM review_hands"))
    for sql in (
        "UPDATE review_hands SET title='x' WHERE id=1",
        # 把写动词藏在 CTE 后面也要拦住
        "WITH t AS (SELECT id FROM replay_hand_sources) DELETE FROM review_hands",
        "TRUNCATE TABLE review_hands",
        "ALTER TABLE review_hands ADD COLUMN x INT",
    ):
        with pytest.raises(RuntimeError, match="review_hands"):
            guarded.execute(sql)


# ─────────────────────── 口径：命名空间 / 缺字段 / 上限 / dry-run ───────────────────────

def test_materialize_forces_namespace_and_does_not_trust_source_column():
    """导入表里 source 列写 review 也仍算 live（§3.1 来源按表划分）。"""
    db = FakeDB(imported_rows=[imported(IMPORTED, source="review")])

    stats = materialize_sources(db, load_source_rows(db))

    assert stats["sources"] == {"live": 1}
    assert ("live", IMPORTED) in db.sources
    assert ("review", IMPORTED) not in db.sources


def test_materialize_counts_rows_missing_started_at_and_keeps_group_empty():
    db = FakeDB(imported_rows=[imported(IMPORTED, started=None)])

    stats = materialize_sources(db, load_source_rows(db))

    assert stats["missing_started_at"] == 1
    assert stats["rows"] == 1
    assert len(db.canonicals) == 1
    assert next(iter(db.canonicals.values()))["content_group"] is None


def test_limit_is_shared_by_both_source_tables():
    db = FakeDB(imported_rows=[imported(IMPORTED + i, started=at(10, i))
                               for i in range(3)],
                review_rows=[review(REVIEW), review(REVIEW + 1, started=at(11, 0))])

    rows = load_source_rows(db, limit=4)

    # 4 = 两张表合计：3 条 live + 1 条 review（先装满 live 再补 review）
    assert len(rows) == 4
    assert [r.source for r in rows].count("live") == 3
    assert [r.source for r in rows].count("review") == 1
    assert [r.started_at for r in rows] == sorted(r.started_at for r in rows)


def test_dry_run_writes_nothing_and_respects_the_thirty_minute_window():
    inside = FakeDB(imported_rows=[imported(IMPORTED, started=at(10, 0)),
                                   imported(IMPORTED + 1, started=at(10, 20))])
    outside = FakeDB(imported_rows=[imported(IMPORTED, started=at(10, 0)),
                                    imported(IMPORTED + 1, started=at(10, 45))])

    near = run_backfill(inside, dry_run=True)
    far = run_backfill(outside, dry_run=True)

    assert near["predicted_canonical"] == 1
    assert far["predicted_canonical"] == 2
    assert near["sources"] == {"live": 2}
    assert "dry-run" in "\n".join(report_lines(far))
    for db in (inside, outside):
        assert [k for k, _ in db.calls if k in ("execute", "execute_many")] == []
        assert db.canonicals == {}



def test_review_deleted_rows_are_not_materialized():
    """用户删掉的复盘手牌不再变成 canon（§11 的 1191 = 1139 + 52 就靠这条）。"""
    db = FakeDB(imported_rows=[imported(IMPORTED)],
                review_rows=[review(REVIEW, deleted=True), review(REVIEW + 1)])

    stats = materialize_sources(db, load_source_rows(db))

    assert stats["sources"] == {"live": 1, "review": 1}
    assert ("review", REVIEW) not in db.sources
    assert ("review", REVIEW + 1) in db.sources


def test_profile_reads_follow_the_hand_not_the_seat_number():
    """座位号跨手重复，画像按名字匹配；上一手的读牌不能漏到下一手。"""
    db = FakeDB(imported_rows=[imported(IMPORTED), imported(IMPORTED + 1)],
                decisions=[decision(1, IMPORTED, snapshot=snapshot(name="Alice")),
                           decision(2, IMPORTED + 1, snapshot=snapshot(name="Bob"))])
    mat = materialize_sources(db, load_source_rows(db))
    store = NameProfileStore({"Alice": SimpleNamespace(archetype="tag"),
                              "Bob": SimpleNamespace(archetype="maniac")})

    fill_decisions(db, mat["mapping"], profile_store=store)

    assert db.decisions[1]["opponent_profile"] == "紧凶"
    assert db.decisions[2]["opponent_profile"] == "松凶"


def test_group_lines_lists_multi_source_groups_for_spot_check():
    db = FakeDB(imported_rows=[imported(IMPORTED), imported(IMPORTED + 1),
                               imported(IMPORTED + 2, cards="QdQc", board="")],
                review_rows=[review(REVIEW)])
    materialize_sources(db, load_source_rows(db))

    text = "\n".join(group_lines(db, limit=20))

    assert "多源组明细" in text
    # 3 条同内容源合到一行（review 顶替 live 当 primary），≥3 源按 §3.4 规则 4 打标
    assert "[3 源]" in text and "待确认" in text
    assert "live:" in text and "review:" in text

