"""偏差热点（#59）：severity 排序、样本门槛与区间口径。"""
from __future__ import annotations

import pytest

from replay import hotspots, scoring


def _spec(spec_id, *, freq=None, label=None, bucket=None):
    return {
        "id": spec_id,
        "name": f"spec-{spec_id}",
        "bucket": bucket or {"street": ["flop"]},
        "bucketLabel": label or f"桶{spec_id}",
        "freq": freq,
    }


def _item(spec_id, score, *, kind="raise", hard_fail=None, comparable=True):
    return {
        "specId": spec_id, "score": score, "kind": kind,
        "comparable": comparable, "hardFail": hard_fail,
    }


def test_hotspot_sorts_by_severity_and_keeps_ci():
    specs = {
        1: _spec(1, freq={"action": "raise", "min": 0.3, "max": 0.5},
                 label="A"),
        2: _spec(2, label="B"),
    }
    items = [_item(1, 40 if i < 20 else 90) for i in range(20)]
    items += [_item(2, 90) for _ in range(20)]
    rows = hotspots.hotspot_rows(items, specs, global_mean=85.0)
    assert [row["specId"] for row in rows] == [1, 2]
    top = rows[0]
    assert top["n"] == 20
    assert top["insufficient"] is False
    # 20 条里 20 条 raise：obs=1.0，带中点 0.4，半带宽 0.1 → dev 封顶 3
    assert top["freq"]["status"] == "missed"
    assert top["dev"] == 3.0
    assert top["severity"] == pytest.approx(3.0 * (20 ** 0.5), abs=1e-3)
    # 口径必须随行下发：有可判定频次就用频次，否则退回均分
    assert top["basis"] == "freq"
    assert top["freq"]["ci"] is not None and top["freq"]["ci"][1] == 1.0
    assert top["scoreCi"]["n"] == 20
    assert top["scoreCi"]["lo"] is not None and top["scoreCi"]["hi"] is not None
    assert top["delta"] == pytest.approx((sum([40] * 20 + [90] * 0) / 20) - 85.0)
    # 均值 90、无频次目标 → dev 封顶 3，但 n 更大才排得前；这里同 severity,
    # 按 n 降序后 20 对 20 打平，用 spec id 兜底
    assert rows[1]["dev"] == pytest.approx(0.2)
    assert rows[1]["basis"] == "mean"


def test_small_sample_is_insufficient_and_excluded_from_attainment():
    specs = {1: _spec(1, freq={"action": "raise", "min": 0.3, "max": 0.5})}
    items = [_item(1, 80) for _ in range(5)]
    rows = hotspots.hotspot_rows(items, specs, global_mean=80.0)
    row = rows[0]
    assert row["insufficient"] is True
    assert row["freq"]["status"] == "insufficient"
    assert row["freq"]["score"] is None
    # 频次不可判定 → dev 退回均值口径（80 分 → 0.4），并在行里标出来
    assert row["dev"] == pytest.approx(0.4)
    assert row["basis"] == "mean"
    # 样本再小，比例也有区间；只是不作为结论
    assert row["freq"]["ci"] is not None
    summary = hotspots.summarize(items, rows)
    assert summary["insufficient"] == 1
    assert summary["attainment"]["met"] == 0
    assert summary["attainment"]["missed"] == 0
    assert summary["attainment"]["rate"] is None
    assert summary["attainment"]["ci"] is None


def test_freq_inside_band_has_zero_deviation():
    specs = {1: _spec(1, freq={"action": "call", "min": 0.9, "max": 1.0})}
    items = [_item(1, 70, kind="call") for _ in range(16)]
    rows = hotspots.hotspot_rows(items, specs, global_mean=70.0)
    row = rows[0]
    assert row["freq"]["status"] == "met"
    assert row["freq"]["observed"] == 1.0
    # 带内但压边：dev 仍是「离带中点几分之一半带宽」，不因 met 归零
    assert row["dev"] == pytest.approx(1.0)
    assert row["severity"] == pytest.approx(4.0)
    summary = hotspots.summarize(items, rows)
    assert summary["attainment"]["met"] == 1
    assert summary["attainment"]["missed"] == 0
    assert summary["attainment"]["rate"] == 1.0
    # 比例类带 Wilson 区间（设计 §7.6）
    assert summary["attainment"]["judged"] == 1
    assert summary["attainment"]["ci"] == pytest.approx([0.2065, 1.0], abs=1e-4)


def test_mean_deviation_uses_50_point_scale():
    specs = {1: _spec(1)}
    items = [_item(1, 0) for _ in range(16)]
    rows = hotspots.hotspot_rows(items, specs, global_mean=0.0)
    # (100-0)/50 = 2.0；均值口径到不了 3（封顶只对频次有意义）
    assert rows[0]["dev"] == 2.0
    assert rows[0]["basis"] == "mean"
    assert rows[0]["delta"] == 0.0


def test_spec_without_scores_reports_null_mean_and_null_severity():
    specs = {1: _spec(1)}
    items = [{"specId": 1, "score": None, "kind": None,
              "comparable": False, "hardFail": None}]
    rows = hotspots.hotspot_rows(items, specs)
    assert rows[0]["mean"] is None
    assert rows[0]["severity"] is None
    assert rows[0]["hardFail"]["rate"] is None
    assert rows[0]["hardFail"]["ci"] is None


def test_unscored_items_still_count_n_but_not_scored():
    specs = {1: _spec(1)}
    items = [_item(1, 50) for _ in range(14)] + [
        {"specId": 1, "score": None, "kind": None,
         "comparable": False, "hardFail": None}]
    rows = hotspots.hotspot_rows(items, specs, global_mean=50.0)
    assert rows[0]["n"] == 15
    assert rows[0]["scored"] == 14
    assert rows[0]["insufficient"] is False


def test_severity_uses_same_sample_as_deviation():
    """均分口径的 n 只数计分条目：error/未计分不能把严重度放大。"""
    specs = {1: _spec(1)}
    items = [_item(1, 20) for _ in range(20)] + [
        {"specId": 1, "score": None, "kind": None,
         "comparable": False, "hardFail": None} for _ in range(5)]
    row = hotspots.hotspot_rows(items, specs, global_mean=20.0)[0]
    assert row["n"] == 25 and row["scored"] == 20
    # dev=(100-20)/50=1.6，样本按 20 条算
    assert row["severity"] == pytest.approx(round(1.6 * (20 ** 0.5), 3))


def test_freq_min_n_goes_through_scoring_constant():
    assert hotspots.scoring.FREQ_MIN_N == 15
