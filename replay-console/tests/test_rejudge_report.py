"""批量重判报表（#73）：纯汇总与逐条重判接线。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from replay import rejudge_report, scoring


def _row(case_id: int, hard_fail, fact, *, prompt: str | None,
         repeat: int = 0) -> dict:
    return {
        "case_id": case_id,
        "repeat_index": repeat,
        "hard_fail": hard_fail,
        "scores_json": json.dumps({"fact": fact, "action": 1.0}),
        "prompt_json": (json.dumps({"user": prompt, "sha": "sha"})
                        if prompt else None),
    }


class _FakeService:
    def __init__(self, rows, results):
        self.rows = rows
        self.results = results
        self.require_calls = 0
        self.list_calls = []
        self.rejudge_calls = []
        self.run_repo = SimpleNamespace(require=self._require)
        self.run_item_repo = SimpleNamespace(list=self._list)

    def _require(self, run_id: int):
        self.require_calls += 1
        return {"id": run_id, "reconstruct_version": "v1"}

    def _list(self, run_id, version, *, repeat_index=None, limit=200,
              offset=0):
        self.list_calls.append({
            "run_id": run_id, "version": version, "repeat": repeat_index,
            "limit": limit, "offset": offset})
        rows = [r for r in self.rows
                if repeat_index is None or r["repeat_index"] == repeat_index]
        return rows[offset:offset + limit]

    def rejudge_score_run_item(self, run_id, case_id, repeat_index=0):
        self.rejudge_calls.append((run_id, case_id, repeat_index))
        result = self.results[case_id]
        if isinstance(result, Exception):
            raise result
        return result


def _result(*, hard_fail, fact, input_prompt, changed=False,
            status="ok"):
    return {
        "hardFail": hard_fail,
        "scores": {"fact": fact},
        "after": {"status": status, "inputPrompt": input_prompt,
                  "degraded": "" if input_prompt else "prompt_missing"},
        "judgeVersionChanged": changed,
    }


def test_dry_run_counts_prompt_coverage_without_calling_judge():
    service = _FakeService(
        [_row(1, None, 1.0, prompt="p1"),
         _row(2, None, 1.0, prompt=None)],
        {})
    out = rejudge_report.run(service, 8, execute=False)
    assert out["planned"] == 2
    assert out["withPrompt"] == 1
    assert out["withoutPrompt"] == 1
    assert service.rejudge_calls == []
    assert service.list_calls[0]["version"] == "v1"


def test_execute_summarizes_fixed_prompt_missing_and_errors():
    service = _FakeService(
        [_row(1, scoring.HF_HALLUCINATION, 0.5, prompt="p1"),
         _row(2, None, 1.0, prompt=None),
         _row(3, scoring.HF_HALLUCINATION, 0.5, prompt="p3")],
        {
            1: _result(hard_fail=None, fact=1.0, input_prompt=True,
                       changed=True),
            2: _result(hard_fail=None, fact=1.0, input_prompt=False),
            3: RuntimeError("judge timeout"),
        })
    out = rejudge_report.run(service, 8, execute=True, limit=10)
    assert out["total"] == 3
    assert out["compared"] == 1          # 只有 case 1 有 prompt 且成功
    assert out["promptMissing"] == 1     # case 2 单列
    assert out["judgeFailed"] == 0
    assert out["errors"] == 1            # case 3 单列
    assert out["beforeHf"] == 1 and out["afterHf"] == 0
    assert out["fixed"] == 1 and out["new"] == 0
    assert out["hfRateBefore"] == 1.0 and out["hfRateAfter"] == 0.0
    assert out["factBeforeMean"] == 0.5
    assert out["factAfterMean"] == 1.0
    assert out["versionChanged"] == 1
    assert out["samples"][0]["caseId"] == 1
    assert out["samples"][0]["kind"] == "fixed"
    assert [c[1] for c in service.rejudge_calls] == [1, 2, 3]


def test_summarize_ignores_new_hf_and_missing_fact_values():
    out = rejudge_report.summarize([
        {"caseId": 1, "beforeHardFail": None,
         "afterHardFail": scoring.HF_HALLUCINATION,
         "beforeFact": None, "afterFact": 0.5, "inputPrompt": True,
         "afterStatus": "ok"},
        {"caseId": 2, "beforeHardFail": None, "afterHardFail": None,
         "beforeFact": 0.5, "afterFact": 0.5, "inputPrompt": True,
         "afterStatus": "ok"},
    ])
    assert out["new"] == 1 and out["fixed"] == 0
    assert out["samples"][0]["kind"] == "new"
    assert out["samples"][0]["caseId"] == 1
    assert out["factBeforeMean"] == 0.5   # 只用两边都有值的 case 2
    assert out["factAfterMean"] == 0.5


def test_summarize_excludes_judge_failures_from_compared():
    out = rejudge_report.summarize([
        {"caseId": 1, "beforeHardFail": scoring.HF_HALLUCINATION,
         "afterHardFail": None, "beforeFact": 0.5, "afterFact": 1.0,
         "inputPrompt": True, "afterStatus": "ok"},
        {"caseId": 2, "beforeHardFail": scoring.HF_HALLUCINATION,
         "afterHardFail": None, "beforeFact": 0.5, "afterFact": 1.0,
         "inputPrompt": True, "afterStatus": "error",
         "degraded": "judge_error"},
    ])
    assert out["compared"] == 1
    assert out["judgeFailed"] == 1
    assert out["promptMissing"] == 0
    assert out["beforeHf"] == 1 and out["afterHf"] == 0
    assert out["factBeforeMean"] == 0.5
    assert out["factAfterMean"] == 1.0
    assert [s["caseId"] for s in out["samples"]] == [1]
