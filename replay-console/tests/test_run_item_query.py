"""RunItemRepository.list 的下钻参数：只取首次建议、最差优先。"""
from __future__ import annotations

from replay.run_repository import RunItemRepository


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []

    def query(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return []


def test_repeat_filter_stays_in_sql():
    db = _Recorder()
    RunItemRepository(db).list(7, "v1", spec_id=3, repeat_index=0,
                               limit=10, offset=5)
    sql, params = db.calls[-1]
    assert "AND i.repeat_index = %s" in sql
    # 参数顺序必须跟 SQL 占位符一致：JOIN 的 rv → run → spec → repeat → 分页
    assert params == ("v1", 7, 3, 0, 10, 5)


def test_default_order_keeps_priority_and_has_no_repeat_filter():
    db = _Recorder()
    RunItemRepository(db).list(7, "v1", limit=5)
    sql, params = db.calls[-1]
    assert "i.repeat_index = %s" not in sql
    assert "ORDER BY c.priority ASC" in sql
    assert params == ("v1", 7, 5, 0)
