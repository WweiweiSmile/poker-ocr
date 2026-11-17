#!/usr/bin/env python3
"""把「英雄动作缺录」的线上 advice 回填成 replay 决策点（票 #55）。

线上录制偶发：手牌行动线里英雄那一步没进 timeline，advice 行却完整记下了那次
决策的上下文（pot / to_call / board / options）。这些手牌在 canonical 里没有
决策点，seed 案例集只能跳过（SEED_DECISION_MISSING）。

本工具不做任何猜测：

  · 只补「整手最后一个英雄动作之后」的缺口 —— 之前的点都有真实动作可挂，
    补进来会改变已有决策点的 seq（seq 是 advice / 案例引用的身份）。
  · advice 记录的 pot/to_call 必须和行动线推算一致（容差 0.05），否则是误触发
    的建议，丢弃。
  · 补出来的决策点 actual 恒为 NULL、质量 approx，warnings 写明「英雄动作缺录」，
    绝不编一个 fold/check 进库。

    nv tools/backfill_advice_decisions.py --dry-run   # 只算，不写库
    nv tools/backfill_advice_decisions.py --hand-id 1000000000320

写完决策点后接着回填 replay_live_advices.decision_id 与决策行的
canonical_id / 七个桶（与 tools/013_backfill_canonical.py 同一份逻辑），
否则 seed 的 _seed_target 走 for_canonical 查不到这些行。桶回填按「还没有
canonical_id 的决策行」自愈，不只看本次新落的行：中途被杀或重跑都能补上。
决策点变了的手牌会重算时间线与 flags（否则 flags 停在下库时的「没有 hero
决策点」）；本项行为上线前回填过的批次用 --refresh-stale 补一次（按「flags 还是
INVALID、但已有关联决策点」这个签名精确重算，不动其它手牌）。

范围：默认扫全库（任何「有 advice 没挂决策点」的手牌都会补），不只 #55 的
7 手；只想看某几手用 --hand-id。已知限制：同一街只补一个边界点，且 advices
记录的 pot/to_call 必须与行动线推算一致（容差 0.05）；英雄缺的那一步如果是
下注/跟注，后续缺口会因底池少算而对不上，宁可不补（warning 里会写明丢弃）。
重复跑是安全的：决策点由确定性重建得到（同一手重算出同样的 seq 与内容），
已有 decision_id 不会被覆盖（只补空挂）；中途被打断留下的空挂 id 与
canonical/桶会在下一次运行时自愈。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "replay-console"
for path in (str(ROOT), str(CONSOLE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from poker_ocr.opponent_profile import load_profile_store  # noqa: E402
from replay.backfill import (fill_decisions,  # noqa: E402
                            refresh_stale_enrichment)
from replay.config import Settings  # noqa: E402
from replay.db import Database  # noqa: E402
from replay.decisions import (analyze_hand, hand_to_input,  # noqa: E402
                              missing_action_advices)
from replay.repository import (DecisionRepository,  # noqa: E402
                                LiveAdviceRepository)
from replay.service import ReplayService  # noqa: E402
from replay.versioning import CURRENT_RECONSTRUCT_VERSION  # noqa: E402

_CANDIDATE_SQL = """
    SELECT h.id AS hand_id, COUNT(*) AS missing
      FROM replay_imported_hands h
      JOIN replay_live_advices a ON a.hand_id = h.id
     WHERE h.deleted_at IS NULL
       AND a.decision_id IS NULL
       AND a.actual_action IS NULL
     GROUP BY h.id
     ORDER BY h.id
"""

def _hand_row(db: Database, hand_id: int) -> dict | None:
    return db.one("SELECT * FROM replay_imported_hands WHERE id=%s", (hand_id,))


_PENDING_MAP_SQL = """
    SELECT DISTINCT d.hand_id, hs.canonical_id
      FROM replay_decisions d
      JOIN replay_hand_sources hs
        ON hs.source = 'live' AND hs.source_hand_id = d.hand_id
     WHERE d.reconstruct_version = %s
       AND d.canonical_id IS NULL
"""


def _pending_map(db: Database) -> dict:
    """所有「还没有 canonical_id」的决策点所属手牌 → canonical。

    自愈式：不只看本次新落的决策点。实跑被中断、或上一次落到一半，重跑时
    这些行已不在 advice 候选里，靠这份映射补回来，否则它们永远 mapping 不到
    canonical，seed 也永远查不到（for_canonical 走 canonical 索引）。
    """
    rows = db.query(_PENDING_MAP_SQL, (CURRENT_RECONSTRUCT_VERSION,))
    return {"live": {int(r["hand_id"]): int(r["canonical_id"]) for r in rows}}


def _backfill_hand(db: Database, repository: DecisionRepository,
                   advice_repo: LiveAdviceRepository, service: ReplayService,
                   hand_id: int, *, dry_run: bool) -> dict | None:
    """回填一手；没有缺口返回 None（调用方据此分流 skip 原因）。"""
    row = _hand_row(db, hand_id)
    if not row:
        return None
    analysis = analyze_hand(row)
    backfilled = [d for d in analysis.decisions if d.backfilled]
    if not backfilled:
        return None
    entry = {
        "handId": hand_id,
        "quality": analysis.quality,
        "decisions": [
            {"street": d.street, "seq": d.seq, "actionIndex": d.action_index,
             "pot": d.pot, "toCall": d.snap.to_call}
            for d in backfilled],
    }
    if dry_run:
        return entry
    repository.persist(analysis)
    stored = repository.for_hand(hand_id, analysis.fingerprint)
    for decision in backfilled:
        hit = stored.get((decision.street, decision.seq))
        if not hit:
            continue
        advice_repo.link_decision(hand_id, decision.street,
                                  int(decision.action_index), int(hit["id"]))
    # 决策点变了，时间线与 flags 必须重算：438 回填前存的是
    # INVALID「没有 hero 决策点」，不重算就永远过期。
    service.refresh_enrichment(hand_id)
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="回填缺录英雄动作的决策点")
    ap.add_argument("--dry-run", action="store_true", help="只算，不写库")
    ap.add_argument("--hand-id", type=int, default=None,
                    help="只处理这一手（默认全部候选）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理几手")
    ap.add_argument("--profiles", default=None,
                    help="画像库路径；默认 data/opponents/profiles.json")
    ap.add_argument("--no-profiles", action="store_true",
                    help="不读画像库，opponent_profile 一律回填「未知」")
    ap.add_argument("--refresh-stale", action="store_true",
                    help="重算「标了 INVALID、其实已有决策点」的手牌"
                         "（历史回填留下的过期 flags 自愈用）")
    args = ap.parse_args()

    settings = Settings.from_env()
    db = Database(settings)
    repository = DecisionRepository(db)
    advice_repo = LiveAdviceRepository(db)
    service = ReplayService(settings, db)
    store = None
    if not args.no_profiles:
        store = load_profile_store(args.profiles)
        if store.load_error:
            print(f"画像库不可用（{store.load_error}），opponent_profile 将全为「未知」")
    try:
        sql = _CANDIDATE_SQL
        params: tuple = ()
        if args.hand_id:
            sql = sql.replace("WHERE h.deleted_at IS NULL",
                              "WHERE h.deleted_at IS NULL AND h.id=%s")
            params = (args.hand_id,)
        candidates = db.query(sql, params)
        if args.limit:
            candidates = candidates[: args.limit]
        if not candidates:
            print("没有缺 decision_id 的 advice 待回填")

        applied: list[dict] = []
        skipped: list[dict] = []
        for item in candidates:
            hand_id = int(item["hand_id"])
            entry: dict | None = None
            try:
                entry = _backfill_hand(db, repository, advice_repo, service,
                                       hand_id, dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001 —— 一手坏数据不能中断整批
                skipped.append({"handId": hand_id, "code": "ERROR",
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if entry is None:
                row = _hand_row(db, hand_id)
                planned = (missing_action_advices(hand_to_input(row))
                           if row else [])
                skipped.append({
                    "handId": hand_id,
                    "code": "NO_HAND" if not row
                            else ("NO_MATCH" if planned else "NO_GAP"),
                    "reason": ("导入行不存在" if not row
                               else ("advice 的 pot/to_call 与行动线对不上，丢弃 "
                                     f"{len(planned)} 条" if planned
                                     else "advice 全都有真实动作，或都在最后一个英雄动作之前")),
                })
                continue
            applied.append(entry)

        stale = {"refreshed": 0, "pending": [], "failed": []}
        if args.refresh_stale:
            stale = refresh_stale_enrichment(db, service, dry_run=args.dry_run)
            for item in stale["failed"]:
                print(f"  ✗ {item['handId']} REFRESH_ERROR {item['reason']}")

        filled = {}
        if not args.dry_run:
            filled = fill_decisions(db, _pending_map(db), profile_store=store)

        verb = "将回填" if args.dry_run else "已回填"
        total = sum(len(a["decisions"]) for a in applied)
        if candidates:
            print(f"候选 {len(candidates)} 手；{verb} {len(applied)} 手 / {total} 个决策点")
        for entry in applied:
            for d in entry["decisions"]:
                print(f"  ✓ {entry['handId']} {d['street']} seq={d['seq']} "
                      f"ai={d['actionIndex']} pot={d['pot']} toCall={d['toCall']}")
        for item in skipped:
            print(f"  ✗ {item['handId']} {item['code']} {item['reason']}")
        if args.refresh_stale:
            if args.dry_run:
                for hand_id in stale["pending"]:
                    print(f"  ~ {hand_id} enrichment 过期，需要重算")
                print(f"时间线/flags：需要重算 {len(stale['pending'])} 手"
                      "（flags 还写 INVALID，或时间线还没挂 decisionId）")
            else:
                print(f"时间线/flags：重算 {stale['refreshed']} 手"
                      + (f"，失败 {len(stale['failed'])} 手"
                         if stale["failed"] else ""))
        if filled:
            print(f"canonical/桶：更新 {filled.get('updated', 0)} 行，"
                  f"未映射 {filled.get('unmapped_hand', 0)} 手，"
                  f"缺快照 {filled.get('skipped_no_snapshot', 0)} 行")
            if filled.get("failed"):
                print(f"  桶推导失败 {len(filled['failed'])} 行（见日志）")
        mismatched = [s for s in skipped if s["code"] == "NO_MATCH"]
        if mismatched:
            print(f"有 {len(mismatched)} 手 advice 与行动线对不上（多半是英雄缺的那一步"
                  "本身是下注/跟注，底池少算后对不上），按约定跳过，不编动作")
        failed = [s for s in skipped if s["code"] in ("ERROR", "NO_HAND")]
        if failed and not args.dry_run:
            print(f"有 {len(failed)} 手本该回填却出错，见上面的 ✗")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
