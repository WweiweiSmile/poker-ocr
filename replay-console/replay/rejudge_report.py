"""批量重判一次评分运行的 judge（只读），汇总 HF 与 s_fact 变化（#73）。

用途：judge 升到 j2 后，旧 run 与新建 run 不再同口径。维护者在手动 ab 前，
用这个脚本把旧 run 全量重判到当前 judge，再比较同一口径下的：

    HF_HALLUCINATION 条数 / 有 prompt 的条目
    s_fact 均值（只算 before/after 都有值、且该条 prompt 未缺失的记录）

它不写库、不改 run，调用的就是页面上「重判」按钮用的
ReplayService.rejudge_score_run_item。

    cd replay-console
    ../nv -m replay.rejudge_report --run-id 8                 # 预演：只数条目
    ../nv -m replay.rejudge_report --run-id 8 --execute       # 真调 judge
    ../nv -m replay.rejudge_report --run-id 8 --execute --repeat 0 --json /tmp/run8.json

不传 --execute 时不会调用模型，只报告计划重判多少条、其中多少条留存了
prompt。--execute 才逐条调模型（temp 0），因此会产生 API 费用。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import judge, scoring
from .config import Settings
from .db import Database
from .service import ReplayService
from .util import json_load
from .versioning import CURRENT_RECONSTRUCT_VERSION

_PAGE = 200


def _fact_of(scores: Mapping[str, Any] | None) -> float | None:
    value = (scores or {}).get("fact")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(sum(items) / len(items), 4) if items else None


def _sample(record: Mapping[str, Any], kind: str) -> dict:
    return {
        "kind": kind,
        "caseId": record.get("caseId"),
        "repeatIndex": record.get("repeatIndex"),
        "beforeFact": record.get("beforeFact"),
        "afterFact": record.get("afterFact"),
        "beforeHardFail": record.get("beforeHardFail"),
        "afterHardFail": record.get("afterHardFail"),
    }


def summarize(records: Iterable[Mapping[str, Any]]) -> dict:
    """把逐条 before/after 汇总成可写回票面的数字（纯函数）。

    - compared：有 prompt、且 judge 真的返回了判定（afterStatus=ok）的条目
      ——HF 率的分母；judge 调用失败 / 不可用不算「重判成功」。
    - promptMissing：judge 成功但没有 prompt 的条目，单列，不混入 HF / s_fact。
    - judgeFailed：judge 没跑成（status != ok）的条目，单列；它们会 fail open
      把 s_fact 记 1，混进均值会虚高。
    - errors：重判抛异常的条目，单列，不参与任何比例。
    """
    rows = [dict(r) for r in records]
    errors = [r for r in rows if r.get("error")]

    def judge_ok(record: Mapping[str, Any]) -> bool:
        return str(record.get("afterStatus") or judge.STATUS_OK) == judge.STATUS_OK

    judge_failed = [r for r in rows if not r.get("error") and not judge_ok(r)]
    prompt_missing = [r for r in rows
                      if not r.get("error") and judge_ok(r)
                      and r.get("inputPrompt") is False]
    compared = [r for r in rows
                if not r.get("error") and judge_ok(r)
                and r.get("inputPrompt") is not False]

    def is_hf(record: Mapping[str, Any], key: str) -> bool:
        return str(record.get(key) or "") == scoring.HF_HALLUCINATION

    before_hf = [r for r in compared if is_hf(r, "beforeHardFail")]
    after_hf = [r for r in compared if is_hf(r, "afterHardFail")]
    fixed = [r for r in compared
             if is_hf(r, "beforeHardFail") and not is_hf(r, "afterHardFail")]
    new = [r for r in compared
           if is_hf(r, "afterHardFail") and not is_hf(r, "beforeHardFail")]

    fact_pairs = []
    for record in compared:
        before = _fact_of({"fact": record.get("beforeFact")})
        after = _fact_of({"fact": record.get("afterFact")})
        if before is not None and after is not None:
            fact_pairs.append((before, after))

    return {
        "total": len(rows),
        "compared": len(compared),
        "promptMissing": len(prompt_missing),
        "judgeFailed": len(judge_failed),
        "errors": len(errors),
        "versionChanged": sum(1 for r in rows
                              if r.get("judgeVersionChanged") is True),
        "beforeHf": len(before_hf),
        "afterHf": len(after_hf),
        "fixed": len(fixed),
        "new": len(new),
        "hfRateBefore": (round(len(before_hf) / len(compared), 4)
                         if compared else None),
        "hfRateAfter": (round(len(after_hf) / len(compared), 4)
                        if compared else None),
        "factBeforeMean": _mean(before for before, _ in fact_pairs),
        "factAfterMean": _mean(after for _, after in fact_pairs),
        "samples": ([_sample(r, "fixed") for r in fixed[:10]]
                    + [_sample(r, "new") for r in new[:10]]),
    }


def _load_rows(service: ReplayService, run_id: int, *,
               repeat: int | None, limit: int) -> tuple[dict, list[dict]]:
    run = service.run_repo.require(int(run_id))
    version = str(run.get("reconstruct_version")
                  or CURRENT_RECONSTRUCT_VERSION)
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        page = service.run_item_repo.list(
            int(run_id), version, repeat_index=repeat,
            limit=min(_PAGE, limit - len(rows)), offset=offset)
        rows.extend(page)
        if len(page) < _PAGE:
            break
        offset += len(page)
    return run, rows


def run(service: ReplayService, run_id: int, *, execute: bool = False,
        limit: int = 1000, repeat: int | None = None) -> dict:
    """预演或逐条重判；返回可直接打印/落盘的 JSON（不写库）。"""
    _run, rows = _load_rows(service, run_id, repeat=repeat, limit=limit)
    with_prompt = 0
    for row in rows:
        prompt = json_load(row.get("prompt_json"), {}) or {}
        if str(prompt.get("user") or "").strip():
            with_prompt += 1
    plan = {
        "runId": int(run_id),
        "execute": bool(execute),
        "planned": len(rows),
        "withPrompt": with_prompt,
        "withoutPrompt": len(rows) - with_prompt,
        "repeat": repeat,
    }
    if not execute:
        return plan

    records: list[dict] = []
    for index, row in enumerate(rows, start=1):
        case_id = int(row["case_id"])
        repeat_index = int(row["repeat_index"])
        before_scores = json_load(row.get("scores_json"), {}) or {}
        base = {
            "caseId": case_id,
            "repeatIndex": repeat_index,
            "beforeHardFail": row.get("hard_fail") or None,
            "beforeFact": _fact_of(before_scores),
        }
        try:
            result = service.rejudge_score_run_item(
                int(run_id), case_id, repeat_index)
        except Exception as exc:  # noqa: BLE001
            records.append({**base, "error": str(exc)[:300]})
        else:
            after = result.get("after") or {}
            records.append({
                **base,
                "afterHardFail": result.get("hardFail"),
                "afterFact": _fact_of(result.get("scores")),
                "afterStatus": after.get("status"),
                "inputPrompt": after.get("inputPrompt"),
                "degraded": after.get("degraded") or "",
                "judgeVersionChanged": bool(
                    result.get("judgeVersionChanged")),
            })
        if index % 10 == 0 or index == len(rows):
            print(f"  {index}/{len(rows)}", file=sys.stderr, flush=True)
    return {**plan, **summarize(records), "records": records}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="批量重判一次评分运行并汇总 HF / s_fact（只读，不写库）")
    parser.add_argument("--run-id", type=int, required=True,
                        help="评分运行 id（页面 /runs 的 runId）")
    parser.add_argument("--execute", action="store_true",
                        help="真正调用 judge（会产生 API 费用）；缺省只预演")
    parser.add_argument("--limit", type=int, default=1000,
                        help="最多重判多少条，默认 1000")
    parser.add_argument("--repeat", type=int, default=None,
                        help="只重判某个 repeat（缺省全部）")
    parser.add_argument("--json", type=Path, default=None,
                        help="把完整 JSON 另存到文件")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = Database(settings)
    try:
        service = ReplayService(settings, db)
        out = run(service, args.run_id, execute=args.execute,
                  limit=max(1, args.limit), repeat=args.repeat)
    finally:
        db.close()

    text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if args.json is not None:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
