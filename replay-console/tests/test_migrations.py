"""迁移 012_canonical_and_cases.sql 的形状与幂等性。

本仓的迁移跑在 `Database.migrate()` 里：按文件名顺序应用、版本记进
replay_schema_migrations。这里用一个只记账的替身库把真迁移跑两遍，验证：

* 接在 011 之后，与 #54 的 012_spot_specs.sql 并存（两个 012 是两个 version）；
* 重复跑不重复应用（幂等来自版本表，不是 SQL 里的 information_schema）；
* 本文件只做 CREATE TABLE IF NOT EXISTS / 可空列 ADD COLUMN —— 旧代码只
  SELECT 已知列，读不到新列也不会被影响。
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from replay.config import Settings
from replay.db import Database

MIGRATIONS = Path(__file__).resolve().parents[1] / "replay" / "migrations"
CANONICAL = MIGRATIONS / "012_canonical_and_cases.sql"

EXPECTED_TABLES = {
    "replay_canonical_hands",
    "replay_hand_sources",
    "replay_case_sets",
    "replay_cases",
    "replay_runs",
    "replay_run_items",
    "replay_judge_overrides",
}

EXPECTED_DECISION_COLUMNS = {
    "canonical_id",
    "position_bucket",
    "hand_tier",
    "board_texture",
    "pot_type",
    "facing",
    "opponent_profile",
}


class MigrationStub:
    """只记账的连接：认 cursor/query/execute，不连 MySQL。"""

    _split_sql = Database._split_sql

    def __init__(self) -> None:
        self.migrations_dir = MIGRATIONS
        self.versions: dict[str, str] = {}
        self.executed: list[str] = []

    @contextmanager
    def cursor(self, *, commit: bool = False):
        yield self

    def execute(self, sql: str, params: tuple | None = None):
        statement = sql.strip()
        self.executed.append(statement)
        if statement.upper().startswith("INSERT INTO REPLAY_SCHEMA_MIGRATIONS"):
            assert params is not None
            self.versions[params[0]] = params[1]
        return 0

    def query(self, sql: str, params: tuple | None = None):
        if "SELECT version, checksum FROM replay_schema_migrations" in sql:
            return [{"version": v, "checksum": c} for v, c in self.versions.items()]
        return []


def _run_twice() -> tuple[MigrationStub, list[str], list[str]]:
    stub = MigrationStub()
    first = Database.migrate(stub)
    second = Database.migrate(stub)
    return stub, first, second


def _statements(text: str = "") -> list[str]:
    """按 runner 的切分方式看语句，而不是按肉眼换行。"""
    return Database(Settings())._split_sql(text or CANONICAL.read_text(encoding="utf-8"))


def test_migrate_applies_canonical_migration_after_011():
    stub, first, second = _run_twice()

    assert "011_version_channel" in first
    assert "012_canonical_and_cases" in first
    assert first.index("011_version_channel") < first.index("012_canonical_and_cases")
    assert second == []
    assert set(stub.versions) >= {
        "011_version_channel",
        "012_canonical_and_cases",
        "012_spot_specs",
    }


def test_two_012_migrations_coexist_and_each_owns_its_table():
    _stub, first, _second = _run_twice()

    assert "012_spot_specs" in first  # #54 的那份照旧应用
    canonical_text = CANONICAL.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS replay_spot_specs" not in canonical_text


def test_canonical_migration_only_creates_tables_and_adds_columns():
    for statement in _statements():
        head = statement.lstrip().upper()
        assert head.startswith("CREATE TABLE IF NOT EXISTS") or head.startswith(
            "ALTER TABLE"
        ), statement[:60]


def test_canonical_migration_creates_the_expected_tables():
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", CANONICAL.read_text(encoding="utf-8")))

    assert created == EXPECTED_TABLES


def test_canonical_hands_ids_start_above_imported_hands():
    text = CANONICAL.read_text(encoding="utf-8")

    assert "AUTO_INCREMENT=2000000000001" in text


def test_decision_alter_adds_nullable_columns_only():
    text = CANONICAL.read_text(encoding="utf-8")
    alter = text[text.index("ALTER TABLE replay_decisions") :]
    alter = alter[: alter.index(";")]

    added = dict(re.findall(r"ADD COLUMN (\w+)\s+([^,\n]+)", alter))
    assert set(added) == EXPECTED_DECISION_COLUMNS
    for name, definition in added.items():
        assert "NOT NULL" not in definition.upper(), f"{name} 必须可空，旧写路径才不受影响"

    assert "ADD KEY idx_dec_canonical" in alter
    assert "ADD KEY idx_dec_bucket" in alter
