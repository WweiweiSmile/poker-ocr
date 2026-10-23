"""MySQL 访问层（PyMySQL）。

刻意不引 ORM：控制台只读 `review_hands`、读写固定几张 `replay_*` 表，
SQL 全部参数化，排序字段走白名单。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .errors import ApiError

log = logging.getLogger("replay.db")

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - 启动时给出明确提示
    pymysql = None
    DictCursor = None


class Database:
    """带连接池的 MySQL 访问层。

    到这台 MySQL 的新连接握手实测要 6–8 秒（查询本身只有几十毫秒），所以
    **不能每次 query 都新建连接**。池里的连接跨请求/跨 unit 复用；断线时丢弃重建。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.migrations_dir = Path(__file__).resolve().parent / "migrations"
        self._pool: queue.LifoQueue = queue.LifoQueue()
        self._pool_size = 0
        self._lock = threading.Lock()
        self._closed = False

    def _new_connection(self):
        if pymysql is None:
            raise ApiError("DB_UNAVAILABLE", "缺少 PyMySQL：pip install pymysql", 503)
        try:
            return pymysql.connect(
                host=self.settings.db_host,
                port=self.settings.db_port,
                user=self.settings.db_user,
                password=self.settings.db_password,
                database=self.settings.db_name,
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=False,
                connect_timeout=self.settings.db_connect_timeout,
                read_timeout=30,
                write_timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApiError(
                "DB_UNAVAILABLE",
                f"连不上 MySQL {self.settings.db_host}:{self.settings.db_port}/"
                f"{self.settings.db_name}：{exc}",
                503,
            ) from exc

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._pool_size < max(1, self.settings.db_pool_size):
                    self._pool_size += 1
                    create = True
                else:
                    create = False
            if create:
                try:
                    conn = self._new_connection()
                except Exception:
                    with self._lock:
                        self._pool_size -= 1
                    raise
            else:
                conn = self._pool.get(timeout=self.settings.db_connect_timeout + 30)
        try:
            yield conn
        except Exception:
            # 连接坏了就不要放回池里；下一次重建
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._pool_size = max(0, self._pool_size - 1)
            raise
        else:
            self._pool.put(conn)

    def connect(self):
        """保留旧接口：调用方负责 close()。新代码请用 cursor()。"""
        return self._new_connection()

    def prewarm(self, count: int = 4) -> int:
        """并发预热连接池。

        远程 MySQL 每次新建连接握手要 6–8 秒；如果等用户点页面时才建，
        首屏会非常慢。启动时并发建好几条，之后所有查询都走复用连接。
        """
        import concurrent.futures
        target = max(0, min(int(count), self.settings.db_pool_size) - self._pool_size)
        if target <= 0:
            return 0
        created = 0

        def open_one():
            return self._new_connection()

        with concurrent.futures.ThreadPoolExecutor(max_workers=target) as pool:
            futures = [pool.submit(open_one) for _ in range(target)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    conn = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("预热连接失败：%s", exc)
                    continue
                self._pool.put(conn)
                with self._lock:
                    self._pool_size += 1
                created += 1
        return created

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._pool_size = 0

    @contextmanager
    def cursor(self, *, commit: bool = False) -> Iterator[Any]:
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    yield cur
                if commit:
                    conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise

    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())

    def one(self, sql: str, params: tuple | list | None = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: tuple | list | None = None):
        row = self.one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    def execute(self, sql: str, params: tuple | list | None = None) -> int:
        with self.cursor(commit=True) as cur:
            cur.execute(sql, params or ())
            return cur.lastrowid or cur.rowcount

    def execute_many(self, sql: str, seq_params: list[tuple]) -> int:
        if not seq_params:
            return 0
        with self.cursor(commit=True) as cur:
            return cur.executemany(sql, seq_params)

    def ping(self) -> tuple[bool, str]:
        try:
            started = time.monotonic()
            self.query("SELECT 1")
            return True, f"{int((time.monotonic() - started) * 1000)}ms"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ─────────────── 迁移 ───────────────

    def _split_sql(self, text: str) -> list[str]:
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            lines.append(raw)
        body = "\n".join(lines)
        statements = []
        for chunk in re.split(r";\s*(?:\n|$)", body):
            chunk = chunk.strip()
            if chunk:
                statements.append(chunk)
        return statements

    def migrate(self) -> list[str]:
        """按编号应用 `replay/migrations/*.sql`。返回本次应用的版本。"""
        with self.cursor(commit=True) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS replay_schema_migrations (
                  version    VARCHAR(20) NOT NULL,
                  checksum   CHAR(64) NOT NULL,
                  applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                  PRIMARY KEY (version)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
            """)
        applied_rows = self.query("SELECT version, checksum FROM replay_schema_migrations")
        applied = {r["version"]: r["checksum"] for r in applied_rows}
        applied_now: list[str] = []
        for path in sorted(self.migrations_dir.glob("*.sql")):
            version = path.stem
            text = path.read_text(encoding="utf-8")
            from .util import sha256_text
            checksum = sha256_text(text)
            if version in applied:
                if applied[version] != checksum:
                    log.warning("迁移 %s 的 checksum 与已应用版本不同；不自动重跑", version)
                continue
            log.info("应用迁移 %s", version)
            for statement in self._split_sql(text):
                self.execute(statement)
            self.execute(
                "INSERT INTO replay_schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            applied_now.append(version)
        return applied_now

    def schema_versions(self) -> list[dict]:
        try:
            return self.query(
                "SELECT version, applied_at FROM replay_schema_migrations ORDER BY version")
        except Exception:  # noqa: BLE001
            return []


def json_col(value: Any) -> Any:
    """JSON 列在 PyMySQL 里是 str；统一在仓储层解。"""
    from .util import json_load
    return json_load(value)
