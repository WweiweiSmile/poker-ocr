#!/usr/bin/env python3
"""把 data/raw 下的 .done / .gto.json.gz 灌进 SQLite。

- 只认 .done（最后写的完成标记）；.tmp / 孤儿文件会被忽略
- 幂等：同一 job_id 重复入库会先删旧节点再写
- 可随时重跑；.gto 才是源数据，库坏了删掉重灌即可

用法：
    python gto/scripts/ingest.py
    python gto/scripts/ingest.py --rebuild
    python gto/scripts/ingest.py --stats
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from array import array
from pathlib import Path

GTO = Path(__file__).resolve().parents[1]

SCHEMA = """
CREATE TABLE IF NOT EXISTS solves (
  solve_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id         TEXT UNIQUE NOT NULL,
  flop           TEXT NOT NULL,
  config_hash    TEXT NOT NULL,
  starting_pot   INTEGER,
  effective_stack INTEGER,
  iterations     INTEGER,
  exploitability REAL,
  n_hands        INTEGER,
  n_nodes        INTEGER,
  engine_version TEXT,
  runner_version TEXT,
  created_at     INTEGER,
  source_host    TEXT,
  elapsed_secs   REAL
);
CREATE TABLE IF NOT EXISTS hands (
  solve_id INTEGER NOT NULL,
  idx      INTEGER NOT NULL,
  combo    TEXT NOT NULL,
  w_oop    REAL,
  w_ip     REAL,
  PRIMARY KEY (solve_id, idx)
);
CREATE TABLE IF NOT EXISTS nodes (
  solve_id  INTEGER NOT NULL,
  street    TEXT NOT NULL,
  turn      TEXT NOT NULL,
  key       TEXT NOT NULL,
  actor     INTEGER,
  pot       INTEGER,
  actions   TEXT,
  range_mix TEXT,
  strategy  BLOB,
  ev        BLOB,
  PRIMARY KEY (solve_id, street, turn, key)
);
CREATE INDEX IF NOT EXISTS idx_nodes_lookup ON nodes(street, turn, key);
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(GTO / "data" / "raw"))
    ap.add_argument("--db", default=str(GTO / "data" / "gto.sqlite"))
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    if args.rebuild:
        for p in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
            p.unlink(missing_ok=True)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()

    if args.stats:
        for t in ("solves", "nodes", "hands"):
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"{t}: {n}")
        return

    raw = Path(args.raw)
    dones = sorted(raw.glob("*.done"))
    if not dones:
        sys.exit(f"no .done under {raw}")

    n_new = n_skip = n_bad = 0
    total_nodes = 0
    for done in dones:
        try:
            meta = json.loads(done.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[bad] {done.name}: {e}")
            n_bad += 1
            continue
        gz_path = raw / meta["file"]
        if not gz_path.exists():
            print(f"[bad] {done.name}: missing {gz_path.name}")
            n_bad += 1
            continue
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[bad] {gz_path.name}: {e}")
            n_bad += 1
            continue

        row = conn.execute("SELECT solve_id FROM solves WHERE job_id=?", (data["job_id"],)).fetchone()
        if row:
            solve_id = row[0]
            conn.execute("DELETE FROM nodes WHERE solve_id=?", (solve_id,))
            conn.execute("DELETE FROM hands WHERE solve_id=?", (solve_id,))
            conn.execute(
                """UPDATE solves SET flop=?, config_hash=?, starting_pot=?, effective_stack=?,
                   iterations=?, exploitability=?, n_hands=?, n_nodes=?, engine_version=?,
                   runner_version=?, created_at=?, source_host=?, elapsed_secs=? WHERE solve_id=?""",
                (
                    data["flop"], data["config_hash"], data["starting_pot"], data["effective_stack"],
                    data["iterations"], data["exploitability"], len(data["hands"]), len(data["nodes"]),
                    data["engine_version"], data["runner_version"], meta.get("created_at_unix", 0),
                    meta.get("source_host", ""), meta.get("elapsed_secs", 0.0), solve_id,
                ),
            )
            n_skip += 1
        else:
            cur = conn.execute(
                """INSERT INTO solves (job_id, flop, config_hash, starting_pot, effective_stack,
                   iterations, exploitability, n_hands, n_nodes, engine_version, runner_version,
                   created_at, source_host, elapsed_secs)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["job_id"], data["flop"], data["config_hash"], data["starting_pot"],
                    data["effective_stack"], data["iterations"], data["exploitability"],
                    len(data["hands"]), len(data["nodes"]), data["engine_version"],
                    data["runner_version"], meta.get("created_at_unix", 0),
                    meta.get("source_host", ""), meta.get("elapsed_secs", 0.0),
                ),
            )
            solve_id = cur.lastrowid
            n_new += 1

        w_oop = data.get("weights_oop", [])
        w_ip = data.get("weights_ip", [])
        conn.executemany(
            "INSERT INTO hands (solve_id, idx, combo, w_oop, w_ip) VALUES (?,?,?,?,?)",
            [(solve_id, i, h, w_oop[i] if i < len(w_oop) else 0.0, w_ip[i] if i < len(w_ip) else 0.0)
             for i, h in enumerate(data["hands"])],
        )
        rows = []
        for nd in data["nodes"]:
            strat = bytes(nd["strategy"])
            ev = array("i", nd["ev"]).tobytes()
            rows.append((
                solve_id, nd["street"], nd["turn"], nd["key"], nd["actor"], nd["pot"],
                json.dumps(nd["actions"], separators=(",", ":")),
                json.dumps([round(x, 4) for x in nd["range_mix"]], separators=(",", ":")),
                strat, ev,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO nodes (solve_id, street, turn, key, actor, pot, actions, range_mix, strategy, ev) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        total_nodes += len(rows)

    print(f"ingested: new={n_new} replaced={n_skip} bad={n_bad} nodes={total_nodes}")
    print(f"db: {db_path}")


if __name__ == "__main__":
    main()
