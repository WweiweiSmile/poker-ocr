"""评分运行 worker（#57）：按「版本 × 冻结案例集」跑模型并落分。

复用 replay/engine.py 的 execute_unit()（与旧 job 链路同一条模型调用路径），
只把结果写进 replay_runs / replay_run_items。一次 run 冻结：
案例集 revision、spec revisions、version_id、judge_version、reconstruct_version。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from . import cases as case_lib
from . import judge, scoring
from .config import Settings
from .db import Database
from .engine import RateLimiter, execute_unit
from .repository import (
    CaseRepository, DecisionRepository, LiveAdviceRepository,
    SpotSpecRepository, VersionRepository,
)
from .run_repository import (
    JudgeOverrideRepository, RunItemRepository, RunRepository,
)
from .runner import _previous_stored_advice, decision_from_row
from poker_ocr.buckets import bucket_label
from .specs import spec_from_row
from .util import json_dumps, json_load
from .versioning import (
    CURRENT_RECONSTRUCT_VERSION, build_settings, run_blockers,
)

log = logging.getLogger("replay.run_worker")

#: P0 案例在首次之外再跑两遍（设计 §7.4：固定样本，不随机抽）。
P0_EXTRA_REPEATS = 2
#: 稳定维固定样本数 = 1 + P0_EXTRA_REPEATS；不足就不进分母。
STABLE_SAMPLES = P0_EXTRA_REPEATS + 1

#: 没有历史 token 时的估算兜底（每次调用）。
DEFAULT_TOKENS = {"tokensIn": 2000.0, "tokensOut": 300.0}


class RunAbort(Exception):
    """run 无法开始（案例/版本/spec 缺失），整条 run 直接 failed。"""


# ─────────────── 纯函数（估算，便于单测） ───────────────

def plan_counts(total: int, p0: int) -> dict:
    """案例数 / P0 数 → 调用次数：全量 1 次 + P0 额外 P0_EXTRA_REPEATS 次。"""
    return {
        "cases": int(total),
        "p0": int(p0),
        "calls": int(total) + int(p0) * P0_EXTRA_REPEATS,
        "extraPerP0": P0_EXTRA_REPEATS,
    }


def plan_calls(cases: list[dict]) -> dict:
    return plan_counts(len(cases),
                       sum(1 for case in cases
                           if int(case.get("priority") or 0) == 0))


def price_for(model: str | None, table: dict | None) -> dict | None:
    """价目表（元/百万 token）：`{"model": {"in": 1.0, "out": 2.0}}`。"""
    if not model or not isinstance(table, dict):
        return None
    entry = table.get(model)
    if not isinstance(entry, dict):
        return None
    try:
        return {"in": float(entry["in"]), "out": float(entry["out"])}
    except (KeyError, TypeError, ValueError):
        return None


def estimate_cost(calls: int, tokens: dict | None, price: dict | None) -> dict:
    """调用次数 × 单次 token × 单价；不知道单价时 cost=None。"""
    tokens = tokens or DEFAULT_TOKENS
    tokens_in = float(tokens.get("tokensIn") or 0.0)
    tokens_out = float(tokens.get("tokensOut") or 0.0)
    out = {
        "calls": int(calls),
        "tokensIn": round(tokens_in * calls, 0),
        "tokensOut": round(tokens_out * calls, 0),
        "cost": None,
    }
    if price:
        out["cost"] = round(
            calls * (tokens_in / 1_000_000 * price["in"]
                     + tokens_out / 1_000_000 * price["out"]), 4)
        out["currency"] = "CNY"
    return out


class RunWorker:
    """轮询 replay_runs 的 queued 行，逐条执行并评分。"""

    def __init__(self, settings: Settings, db: Database,
                 wake_event: threading.Event | None = None):
        self.settings = settings
        self.db = db
        self.wake_event = wake_event or threading.Event()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_heartbeat = time.time()
        self.current_run_id: int | None = None
        self.runs = RunRepository(db)
        self.items = RunItemRepository(db)
        self.overrides = JudgeOverrideRepository(db)
        self.case_repo = CaseRepository(db)
        self.spec_repo = SpotSpecRepository(db)
        self.version_repo = VersionRepository(db, settings.console_user_id)
        self.decision_repo = DecisionRepository(db)
        self.live_advice_repo = LiveAdviceRepository(db)
        self.limiter = RateLimiter(settings.rate_limit_per_second)

    # ─────────────── 生命周期 ───────────────

    def start(self) -> None:
        if not self.settings.worker_enabled:
            log.info("评分 worker 已禁用（REPLAY_WORKER_ENABLED=0）")
            return
        try:
            recovered = self.runs.recover_interrupted()
            if recovered:
                log.warning("恢复：%d 条中断的评分运行已标记 partial", recovered)
        except Exception:  # noqa: BLE001
            log.exception("恢复中断的评分运行失败；worker 仍启动")
        self.thread = threading.Thread(target=self._loop,
                                       name="replay-score-worker",
                                       daemon=True)
        self.thread.start()
        log.info("评分 worker 已启动 id=%s workers=%d",
                 self.settings.worker_id, self.settings.unit_workers)

    def stop(self, timeout: float = 8.0) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def wake(self) -> None:
        self.wake_event.set()

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def status(self) -> dict:
        return {
            "alive": self.alive or not self.settings.worker_enabled,
            "enabled": self.settings.worker_enabled,
            "workerId": self.settings.worker_id,
            "currentRunId": self.current_run_id,
            "lastHeartbeat": self.last_heartbeat,
            "workers": self.settings.unit_workers,
        }

    # ─────────────── 主循环 ───────────────

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.last_heartbeat = time.time()
            try:
                run = self.runs.next_queued()
            except Exception as exc:  # noqa: BLE001
                log.exception("领取评分运行失败：%s", exc)
                run = None
            if run is None:
                self.wake_event.wait(1.0)
                self.wake_event.clear()
                continue
            claimed = None
            try:
                claimed = self.runs.claim(int(run["id"]))
            except Exception:  # noqa: BLE001
                log.exception("认领评分运行失败 #%s", run.get("id"))
            if claimed is None:
                continue
            self.current_run_id = int(claimed["id"])
            log.info("开始评分运行 #%s", self.current_run_id)
            try:
                self._execute(claimed)
            except RunAbort as exc:
                log.warning("评分运行 #%s 无法开始：%s", claimed["id"], exc)
                self._safe_finish(int(claimed["id"]), "failed",
                                  error_msg=str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("评分运行 #%s 异常退出：%s", claimed["id"], exc)
                self._safe_finish(int(claimed["id"]), "failed",
                                  error_msg=f"{type(exc).__name__}: {exc}")
            finally:
                self.current_run_id = None
                self.last_heartbeat = time.time()

    def _safe_finish(self, run_id: int, status: str, *,
                     error_msg: str = "") -> None:
        try:
            self.runs.finish(run_id, status, error_msg=error_msg)
        except Exception:  # noqa: BLE001
            log.exception("标记评分运行 %s 失败时再次异常", run_id)

    # ─────────────── 单条 run ───────────────

    def _judge_setup(self, run: dict, config: dict,
                     version_model: str | None) -> tuple[dict, Any]:
        """run 里冻结的 judge 配置优先；凭据缺失时返回 None（照常评分，按项 fail open）。"""
        cfg = judge.judge_config(self.settings)
        frozen = config.get("judge")
        if isinstance(frozen, dict):
            cfg.update({key: value for key, value in frozen.items()
                        if value is not None})
        settings = None
        try:
            settings = judge.build_settings(cfg)
        except judge.JudgeUnavailable as exc:
            log.warning("评分运行 #%s 的 judge 不可用：%s", run.get("id"), exc)
        return cfg, settings

    def _execute(self, run: dict) -> None:
        run_id = int(run["id"])
        case_set_id = int(run["case_set_id"])
        reconstruct_version = str(
            run.get("reconstruct_version") or CURRENT_RECONSTRUCT_VERSION)
        version_id = int(run["version_id"])

        cases = self.case_repo.items(case_set_id)
        if not cases:
            raise RunAbort("案例集为空，没有可评分的案例")
        version = self.version_repo.get(version_id)
        if version is None:
            raise RunAbort(f"版本 {version_id} 不存在")
        blockers = run_blockers(version)
        if blockers:
            raise RunAbort("版本不可运行：" + "、".join(
                str(b.get("code") or b) for b in blockers))
        try:
            ai_settings = build_settings(version)
        except Exception as exc:  # noqa: BLE001
            raise RunAbort(f"版本配置不可用：{exc}") from exc

        spec_ids = [int(s) for s in (json_load(run.get("spec_ids_json"), [])
                                     or [])]
        specs: dict[int, dict] = {}
        for spec_id in spec_ids:
            row = self.spec_repo.get(spec_id)
            if row is not None:
                specs[spec_id] = spec_from_row(row)
        active_specs = [spec_from_row(row)
                        for row in self.spec_repo.list_latest()
                        if (row.get("status") or "active") == "active"]

        config = json_load(run.get("config_snapshot"), {}) or {}
        judge_cfg, judge_settings = self._judge_setup(run, config,
                                                     version.get("model"))
        case_specs = {int(key): int(value) for key, value in
                      (config.get("caseSpecs") or {}).items()}
        # 覆盖率分母按设计 §7.3：有 active spec 的桶数，而不是本次 run 恰好
        # 冻结到的那些 spec——否则只挑 2 个桶跑也能报 100%。
        active_buckets = {bucket_label(spec.get("bucket"))
                          for spec in active_specs if spec.get("bucket")}
        p0_total = len({int(case["id"]) for case in cases
                        if int(case.get("priority") or 0) == 0})
        units = self._build_units(cases, specs, active_specs,
                                  reconstruct_version, case_specs)
        total_cases = len(cases)
        existing = self.items.existing(run_id)
        pending = [u for u in units
                   if (int(u["case"]["id"]), int(u["repeat"])) not in existing]

        per_case = {int(u["case"]["id"]): len(u["repeats"]) for u in units}
        done_per_case: dict[int, int] = {}
        for case_id, repeat in existing:
            done_per_case[case_id] = done_per_case.get(case_id, 0) + 1
        done_cases = sum(1 for case_id, total in per_case.items()
                         if done_per_case.get(case_id, 0) >= total)
        self.runs.set_done(run_id, done_cases)

        if self.runs.cancel_requested(run_id):
            # 还没开跑就被取消：也要带上案例总数，否则「完成 0/0」看不出规模。
            self._finalize(run, reconstruct_version, specs,
                           canceled=True, total_cases=total_cases,
                           done_cases=done_cases,
                           bucket_total=len(active_buckets),
                           p0_total=p0_total)
            return

        lock = threading.Lock()
        cancelled = threading.Event()
        last_progress = time.time()

        def task(unit: dict):
            if cancelled.is_set() or self.stop_event.is_set():
                return None
            if self.runs.cancel_requested(run_id):
                cancelled.set()
                return None
            return self._run_unit(run, unit, ai_settings, version,
                                  spec_ids, judge_cfg, judge_settings)

        with ThreadPoolExecutor(max_workers=max(1, self.settings.unit_workers),
                                thread_name_prefix="score-unit") as pool:
            futures = {pool.submit(task, unit): unit for unit in pending}
            while futures:
                finished, _ = wait(set(futures), timeout=0.5,
                                   return_when=FIRST_COMPLETED)
                if self.runs.cancel_requested(run_id):
                    cancelled.set()
                for future in finished:
                    unit = futures.pop(future)
                    try:
                        ok = future.result()
                    except Exception as exc:  # noqa: BLE001
                        log.exception("评分 unit 异常 case=%s repeat=%s：%s",
                                      unit["case"]["id"], unit["repeat"], exc)
                        ok = False
                    if ok is None:
                        # 取消/停机时 task 没跑就返回 None：不能算「已完成」，
                        # 否则进度会到 N/N 却标 partial。
                        continue
                    with lock:
                        case_id = int(unit["case"]["id"])
                        done_per_case[case_id] = done_per_case.get(case_id, 0) + 1
                        if done_per_case[case_id] >= per_case.get(case_id, 0):
                            done_cases += 1
                now = time.time()
                if now - last_progress >= 1.0:
                    try:
                        self.runs.set_done(run_id, done_cases)
                    except Exception:  # noqa: BLE001
                        log.exception("更新评分进度失败")
                    last_progress = now

        self._finalize(run, reconstruct_version, specs,
                       canceled=cancelled.is_set() or bool(
                           self.runs.cancel_requested(run_id)),
                       total_cases=total_cases, done_cases=done_cases,
                       bucket_total=len(active_buckets), p0_total=p0_total)

    def _build_units(self, cases: list[dict], specs: dict[int, dict],
                     active_specs: list[dict], reconstruct_version: str,
                     case_specs: dict[int, int] | None = None) -> list[dict]:
        """案例 → (decision, spec, prompt, 重复序号) 的执行单元。"""
        prompt_cache: dict[int, dict] = {}
        context_cache: dict[int, dict] = {}
        advice_cache: dict[int, list[dict]] = {}
        units: list[dict] = []
        for case in cases:
            canonical_id = case.get("canonical_id")
            decision_key = case.get("decision_key")
            if canonical_id is None or not decision_key:
                continue
            row = self._decision_row(int(canonical_id), str(decision_key),
                                     reconstruct_version)
            if row is None:
                # 不静默丢：落一条 error run_item，让「案例没跑到」在页面上看得见。
                log.warning("案例 %s 找不到决策点 %s（%s），记 error",
                            case.get("id"), decision_key, reconstruct_version)
                units.append({
                    "case": case, "decision": None, "prompt": None,
                    "spec": None, "specId": None,
                    "specError": (f"找不到决策点 {decision_key}"
                                  f"（{reconstruct_version}）"),
                    "repeats": [0],
                })
                continue
            decision = decision_from_row(row)
            hand_id = int(row["hand_id"])
            if hand_id not in prompt_cache:
                prompt_cache[hand_id] = (
                    self.live_advice_repo.baseline_prompts_for_hand(hand_id))
                context_cache[hand_id] = (
                    self.live_advice_repo.context_for_hand(hand_id))
                advice_cache[hand_id] = (
                    self.live_advice_repo.list_for_hand(hand_id))
            key = (decision.street, decision.action_index)
            prompt = prompt_cache[hand_id].get(key)
            if prompt:
                decision.baseline_prompt = (prompt["system"], prompt["user"])
                decision.baseline_prompt_sha = prompt.get("sha")
                decision.baseline_options = prompt.get("options")
            decision.context = context_cache[hand_id].get(key, {})
            decision.previous_advice = _previous_stored_advice(
                advice_cache[hand_id], decision)

            frozen_spec_id = (case_specs or {}).get(int(case["id"]))
            spec_id = frozen_spec_id or case.get("spec_id")
            spec = specs.get(int(spec_id)) if spec_id else None
            spec_error = None
            if spec is None and frozen_spec_id:
                # 写死的 spec 不在了（被删）：宁可这条落 error，也不跨 run
                # 换成「当前的 active spec」——那会让两次 run 口径不同却看不出来。
                spec_error = f"案例冻结的 spec #{frozen_spec_id} 已不存在"
            elif spec is None and case_specs and not frozen_spec_id:
                # run 有冻结映射但独缺这一条：快照不完整，不替它挑一个。
                spec_error = "案例没有冻结的 spec（run 快照不完整）"
            elif spec is None and not case_specs:
                # 兼容 caseSpecs 之前建的 run：按当前 active spec 匹配。
                spec = case_lib.best_spec(case_lib.case_bucket(case),
                                          active_specs)
                if spec is None:
                    spec_error = "案例没有可匹配的 spec"
                else:
                    log.warning("案例 %s 没有冻结 spec，按当前 active spec #%s "
                                "匹配（旧 run 兼容）", case.get("id"),
                                spec.get("id"))
            repeats = [0]
            if int(case.get("priority") or 0) == 0:
                repeats += list(range(1, P0_EXTRA_REPEATS + 1))
            spec_row_id = (int(spec["id"]) if spec and spec.get("id")
                           else (int(spec_id) if spec_id else None))
            units.append({
                "case": case,
                "decision": decision,
                "prompt": decision.baseline_prompt,
                "spec": spec,
                "specId": spec_row_id,
                "specError": spec_error,
                "repeats": repeats,
            })
        expanded: list[dict] = []
        for unit in units:
            for repeat in unit["repeats"]:
                expanded.append({**unit, "repeat": repeat})
        return expanded

    def _decision_row(self, canonical_id: int, decision_key: str,
                      reconstruct_version: str) -> dict | None:
        street, _, seq = str(decision_key).partition(":")
        if not street or seq == "":
            return None
        return self.db.one(
            """SELECT * FROM replay_decisions
                WHERE canonical_id=%s AND street=%s
                  AND seq=CAST(%s AS UNSIGNED) AND reconstruct_version=%s
                ORDER BY id LIMIT 1""",
            (canonical_id, street, seq, reconstruct_version))

    def _run_unit(self, run: dict, unit: dict, ai_settings, version: dict,
                  spec_ids: list[int], judge_cfg: dict | None = None,
                  judge_settings: Any = None) -> bool:
        """跑一个单元并落 run_item；返回是否成功（含 HF_* 也算跑成功）。"""
        run_id = int(run["id"])
        case = unit["case"]
        decision = unit["decision"]
        spec = unit["spec"]
        repeat = int(unit["repeat"])
        case_id = int(case["id"])
        prompt = unit.get("prompt")

        if decision is None or spec is None:
            # 决策点/spec 缺：不花调用，直接落 error 行（仍算「跑到」）。
            # #58：HF_ERROR 也要带 evidence，否则页面上看不到为什么判它错。
            error_msg = unit.get("specError") or "案例没有可匹配的 spec"
            self.items.upsert(
                run_id=run_id, case_id=case_id,
                spec_id=unit.get("specId") or 0, repeat_index=repeat,
                item={
                    "source": "error",
                    "errorMsg": error_msg,
                    "hardFail": scoring.HF_ERROR, "scoresJson": "{}",
                    "score": None, "kind": None, "amount": None,
                    "optionId": None, "reason": "",
                    "factsJson": "{}",
                    "promptJson": None,
                    "judgeJson": json_dumps(
                        judge.error_judge_json(error_msg)),
                    "durationMs": 0,
                })
            return False

        payload: dict
        try:
            outcome = execute_unit(
                decision=decision, version=version, settings=ai_settings,
                timeout=self.settings.model_timeout,
                retries=self.settings.transient_retries,
                limiter=self.limiter,
                store_prompts=self.settings.store_prompts,
                prompt_override=prompt,
            )
            payload = outcome.payload
        except Exception as exc:  # noqa: BLE001
            log.exception("评分 unit 失败 case=%s", case_id)
            payload = _error_payload(f"{type(exc).__name__}: {exc}")

        facts = json_load(payload.get("facts_json"), {}) or {}
        option_ids = [str(option.get("id")) for option in
                      (facts.get("options") or [])
                      if isinstance(option, dict) and option.get("id")]
        advice = {
            "option_id": payload.get("option_id"),
            "kind": payload.get("option_kind"),
            "amount": payload.get("option_amount"),
            "reason": payload.get("reason_full") or payload.get("reason") or "",
            "source": payload.get("source") or "error",
        }
        # #58：先让 judge 逐条判要点/幻觉（温度 0、证据留档），再走同一套
        # scoring 合成——失败时 judge 自己退化成 #57 的规则口径，不伪造 HF。
        # #73：核对事实以模型当时实际看到的 prompt 原文为准（store_prompts=false
        # 时 payload 无这两个字段，judge 侧 fail open，不产生幻觉判定）。
        verdict = judge.evaluate(
            judge_cfg or {}, settings=judge_settings, spec=spec,
            advice=advice, facts=facts, option_ids=option_ids,
            model_prompt=payload.get("prompt_user"),
            model_prompt_sha=payload.get("prompt_sha256"),
            timeout=(judge_cfg or {}).get("timeout"))
        result = judge.score_verdict(
            verdict, spec=spec, advice=advice, option_ids=option_ids,
            pot=decision.pot)
        judge_json = {
            **verdict,
            "optionIds": option_ids,
            "evidence": judge.evidence(
                result["hardFail"], verdict, advice=advice,
                option_ids=option_ids, spec=spec,
                error=payload.get("error_msg") or ""),
        }
        prompt_json = None
        if self.settings.store_prompts:
            prompt_json = json_dumps({
                "system": payload.get("prompt_system"),
                "user": payload.get("prompt_user"),
                "sha": payload.get("prompt_sha256"),
                "optionIds": option_ids,
            })
        self.items.upsert(
            run_id=run_id, case_id=case_id,
            spec_id=unit.get("specId") or 0, repeat_index=repeat,
            item={
                "optionId": payload.get("option_id"),
                "kind": payload.get("option_kind"),
                "amount": payload.get("option_amount"),
                "reason": payload.get("reason_full")
                          or payload.get("reason") or "",
                "source": payload.get("source") or "error",
                "factsJson": payload.get("facts_json") or "{}",
                "promptJson": prompt_json,
                "judgeJson": json_dumps(judge_json),
                "scoresJson": json_dumps(result["scores"]),
                "score": result["score"],
                "hardFail": result["hardFail"],
                "errorMsg": payload.get("error_msg") or "",
                "durationMs": payload.get("duration_ms") or 0,
            })
        return payload.get("source") != "error"

    # ─────────────── 收尾：稳定维 + 聚合 ───────────────

    def _finalize(self, run: dict, reconstruct_version: str,
                  specs: dict[int, dict], *, canceled: bool = False,
                  total_cases: int = 0, done_cases: int = 0,
                  bucket_total: int = 0, p0_total: int = 0) -> None:
        run_id = int(run["id"])
        rows = self.items.list(run_id, reconstruct_version, limit=10000)
        by_case: dict[int, list[dict]] = {}
        for row in rows:
            by_case.setdefault(int(row["case_id"]), []).append(row)

        # 稳定维：同一案例的多次重复动作一致率。设计 §7.4 固定 3 次样本；
        # partial run 只跑到 2 次时不进分母，否则两次 run 的稳定维不可比。
        for case_rows in by_case.values():
            kinds = [row.get("option_kind") for row in case_rows
                     if row.get("option_kind")]
            if len(kinds) < STABLE_SAMPLES:
                continue
            stable = scoring.stable_score(kinds)
            if stable is None:
                continue
            for row in case_rows:
                scores = json_load(row.get("scores_json"), {}) or {}
                scores["stable"] = round(stable, 4)
                score, _weight = scoring.weighted_total(scores)
                if row.get("hard_fail"):
                    score = min(score, scoring.HF_CAP)
                self.items.update_scores(
                    run_id=run_id, case_id=int(row["case_id"]),
                    repeat_index=int(row["repeat_index"]), scores=scores,
                    score=round(score, 2), hard_fail=row.get("hard_fail"))

        rows = self.items.list(run_id, reconstruct_version, limit=10000)
        # 覆盖率按「本次 run 冻结的 spec」划桶：桶标签取 spec 自己的桶，而不是
        # 案例的决策点桶。否则案例桶往往比 spec 桶更细（多两个维度），两边
        # 标签永远对不上，覆盖率会静默恒为 0。
        spec_buckets = {
            int(spec_id): bucket_label(spec.get("bucket") or {})
            for spec_id, spec in specs.items() if spec.get("bucket")
        }
        if not bucket_total:
            bucket_total = len(set(spec_buckets.values()))
        overrides = self.overrides.list(run_id)
        agg, agg_items, errors = aggregate_rows(
            rows, spec_buckets=spec_buckets, bucket_total=bucket_total,
            p0_total=p0_total or None, overrides=overrides)

        if canceled:
            status = "partial"
        elif errors and errors >= max(len(rows), 1):
            status = "failed"
        elif errors:
            status = "partial"
        else:
            status = "succeeded"
        error_msg = ""
        if status == "partial":
            error_msg = f"完成 {len(agg_items)}/{total_cases or len(agg_items)} 个案例"
            if errors:
                error_msg += f"；{errors} 次调用失败"
            if canceled:
                error_msg += "；已按要求中断"
        elif status == "failed":
            error_msg = "全部调用失败"
        self.runs.finish(
            run_id, status,
            score=agg["score"]["mean"],
            p0_pass_rate=agg["p0"]["rate"],
            coverage_rate=agg["coverage"]["rate"],
            scored_cases=agg["scored"],
            metrics={**agg, "canceled": canceled,
                     "doneCases": done_cases, "errors": errors,
                     "units": len(rows), "overrideCount": len(overrides)},
            error_msg=error_msg,
        )
        log.info("评分运行 #%s 结束 status=%s score=%s scored=%s",
                 run_id, status, agg["score"]["mean"], agg["scored"])


def apply_override(scores: dict, hard_fail: str | None, score: float | None,
                   override: dict | None) -> tuple[float | None, dict, str | None]:
    """把人工改判叠加到一次机器判定上；返回 (score, scores, hardFail)。

    只影响展示与聚合：调用方不得把它写回 replay_run_items（原始分不覆盖）。
    override 的形状与 JudgeOverrideRepository 的 public 行一致。
    """
    merged = dict(scores or {})
    fail = hard_fail or None
    if not override:
        return score, merged, fail
    changed = False
    given = override.get("scores") or {}
    for dim in scoring.DIMENSIONS:
        if dim in given:
            merged[dim] = given[dim]
            changed = True
    if "hardFail" in override:
        new_fail = override.get("hardFail") or None
        if new_fail != fail:
            fail = new_fail
            changed = True
    if not changed:
        return score, merged, fail
    if score is None and not any(merged.get(dim) is not None
                                 for dim in scoring.DIMENSIONS):
        return None, merged, fail
    total, _weight = scoring.weighted_total(merged)
    if fail:
        total = min(total, scoring.HF_CAP)
    return round(total, 2), merged, fail


def aggregate_rows(rows: list[dict], *, spec_buckets: dict[int, str],
                   bucket_total: int = 0, p0_total: int | None = None,
                   overrides: dict[int, dict] | None = None
                   ) -> tuple[dict, list[dict], int]:
    """run_items（+人工改判）→ 聚合指标；worker 收尾与改判重算共用。"""
    overrides = overrides or {}
    agg_items: list[dict] = []
    errors = 0
    for row in rows:
        if row.get("source") == "error":
            errors += 1
        if int(row["repeat_index"]) != 0:
            continue
        case_id = int(row["case_id"])
        score = (float(row["score"]) if row.get("score") is not None else None)
        score, scores, hard_fail = apply_override(
            json_load(row.get("scores_json"), {}) or {},
            row.get("hard_fail") or None, score, overrides.get(case_id))
        agg_items.append({
            "priority": int(row.get("case_priority") or 0),
            "bucket": spec_buckets.get(int(row.get("spec_id") or 0)),
            "score": score,
            "scores": scores,
            "hardFail": hard_fail,
        })
    agg = scoring.aggregate(agg_items, bucket_total=bucket_total,
                            p0_total=p0_total or None)
    return agg, agg_items, errors


def _error_payload(message: str) -> dict:
    return {
        "option_id": None, "option_kind": None, "option_amount": None,
        "reason": None, "source": "error", "duration_ms": 0,
        "facts_json": "{}", "prompt_system": None, "prompt_user": None,
        "prompt_sha256": None, "error_msg": message[:500],
    }
