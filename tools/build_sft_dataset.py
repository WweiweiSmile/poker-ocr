#!/usr/bin/env python3
"""把已导出的手牌 + 实时日志，转成 LLaMA-Factory 能直接吃的 SFT 数据集。

    # 先看看能转出多少条，不写文件
    .venv/bin/python tools/build_sft_dataset.py --dry-run

    # 真写：data/sft/{train,eval}.json + dataset_info.json + system.txt
    .venv/bin/python tools/build_sft_dataset.py --out data/sft

    # 带上理由一起学（输出更长、更慢，但能给复盘用）
    .venv/bin/python tools/build_sft_dataset.py --with-reason --out data/sft

## 为什么用「当时的提示词」而不是另写一套

训练集和线上推理必须**逐字节同构**：同一个 system（含 RFI 表）、同一份事实渲染。
只要有一处漂移，模型学到的就是另一个任务的解，线上立刻掉点。所以这里直接调
`advice.build_messages()`，不自己拼 prompt。

## 标签是什么

`advice` 记录里那次**实际被采纳的选择**（`option.id`，如 `raise-2/3`、`fold`）。
选项集是闭集（最多 7 个 id），所以这是一道分类题，不是 SpinGPT 那种自由生成金额。
好处：永远不会给出牌桌上做不出来的尺寸。

## 已知边界

· 这是**行为克隆的种子集**，百来条，只能验证格式、跑通训练、当 eval。
  真正的量要靠求解器/教师模型扩（见 docs/微调-8b-两阶段方案.md 第 4 节）。
· 标签是 deepseek-flash 当时的决定，不是专家决定。它含的错误会被一起学走。
· 跳过陈旧帧、跳过选项集已变化的样本（老日志里的 `open-3` 现在可能不存在）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.advice import build_messages, build_situation  # noqa: E402
from tools.replay_live_decisions import (  # noqa: E402
    build_snap, find_state, load,
)

SYSTEM_FILE = "system.txt"


def is_stale(snap) -> bool:
    """翻前已经投过钱、却又报 to_call=0 —— 同一决策在旧帧上重复建议。

    与 tools/replay_live_decisions.py 的口径一致：这类样本 hero 其实没有选择，
    拿来当标签等于教模型「对着已经结束的局面出牌」。
    """
    bb = snap.blinds.bb if snap.blinds else 1.0
    return (snap.street == "preflop" and snap.to_call == 0
            and ((snap.hero_position or "") not in ("BB",)
                 or (snap.pot or 0) > 12 * bb))


def iter_rows(log_path: Path, hands_path: Path, style: str):
    """逐条产出 (advice, snapshot, situation)，口径与线上完全一致。"""
    records = load(log_path)
    states = [r for r in records
              if r.get("type") == "state" and r.get("hero")]
    advices = [r for r in records if r.get("type") == "advice"]

    hands: dict[str, dict] = {}
    if hands_path.exists():
        for h in json.loads(hands_path.read_text(encoding="utf-8")):
            hands[str(h.get("hand_id"))] = h

    since = ""
    for adv in advices:
        state = find_state(adv, states, since)
        since = adv["ts"]
        if state is None:
            yield adv, None, None, "no-state"
            continue
        snap = build_snap(adv, state, hands.get(str(adv.get("hand_id"))))
        if is_stale(snap):
            yield adv, snap, None, "stale"
            continue
        sit = build_situation(snap)
        yield adv, snap, sit, ""


def build(log_path: Path, hands_path: Path, style: str,
          with_reason: bool, eval_frac: float, seed: int):
    rows, stats = [], Counter()
    seen = set()

    for adv, snap, sit, skip in iter_rows(log_path, hands_path, style):
        if skip:
            stats[skip] += 1
            continue
        label = str((adv.get("option") or {}).get("id") or "")
        legal = {o.id for o in sit.options}
        if not label:
            stats["no-label"] += 1
            continue
        if label not in legal:
            # 老日志里的 id 已经被现在的选项生成器淘汰（例如 open-3）。
            # 留着学 = 教模型输出一个不存在的 id。
            stats["label-not-in-options"] += 1
            continue
        key = (snap.hand_id, snap.street, round(float(snap.pot or 0.0), 2),
               round(float(snap.to_call or 0.0), 2),
               tuple(snap.hero_cards))
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)

        system, user = build_messages(sit, style)
        reason = (adv.get("reason") or "").strip().replace("\n", " ")
        target = {"option": label}
        if with_reason and reason:
            target["reason"] = reason[:200]
        rows.append({
            "row": {
                "instruction": user,
                "input": "",
                "output": json.dumps(target, ensure_ascii=False),
                # system 每行都带一份：LLaMA-Factory 的 dataset_info.json
                # 没有 `default_system` 这种全局键（查过 parser.py / converter.py），
                # 只能走 columns 里的 system 列。10 万条时改用 parquet，
                # 列式字典编码会把这段重复文本压掉。
                "system": system,
            },
            "meta": {
                "hand_id": snap.hand_id, "street": snap.street,
                "position": snap.hero_position,
                "cards": "".join(snap.hero_cards),
                "board": "".join(snap.board),
                "pot": snap.pot, "to_call": snap.to_call,
                "label": label, "legal": sorted(legal),
                "source": adv.get("model") or "unknown",
                "ts": adv.get("ts"),
            },
            "system": system,
            "hand_id": str(snap.hand_id),
        })

    # 按手牌切分，避免同一手的不同街同时出现在 train / eval（信息泄漏）。
    hand_ids = sorted({r["hand_id"] for r in rows})
    rng = random.Random(seed)
    rng.shuffle(hand_ids)
    n_eval = max(1, round(len(hand_ids) * eval_frac)) if eval_frac > 0 else 0
    eval_hands = set(hand_ids[:n_eval])
    train = [r["row"] for r in rows if r["hand_id"] not in eval_hands]
    evals = [r["row"] for r in rows if r["hand_id"] in eval_hands]
    return rows, train, evals, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出 SFT 数据集（LLaMA-Factory 格式）")
    ap.add_argument("--log", type=Path, default=Path("logs/live.states.jsonl"))
    ap.add_argument("--hands", type=Path, default=Path("hands.json"))
    ap.add_argument("--out", type=Path, default=Path("data/sft"))
    ap.add_argument("--style", default="lean", help="lean | skills（与线上一致）")
    ap.add_argument("--with-reason", action="store_true",
                    help="输出 {\"option\",\"reason\"}；默认只输出 option")
    ap.add_argument("--eval-frac", type=float, default=0.15,
                    help="按手牌切出的 eval 比例；0 = 不切")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"找不到日志：{args.log}", file=sys.stderr)
        return 1

    rows, train, evals, stats = build(
        args.log, args.hands, args.style, args.with_reason,
        args.eval_frac, args.seed)

    if not rows:
        print("一条都没转出来。检查日志里有没有 advice 记录。", file=sys.stderr)
        return 1

    labels = Counter(r["meta"]["label"] for r in rows)
    streets = Counter(r["meta"]["street"] for r in rows)
    print(f"样本 {len(rows)} 条  →  train {len(train)} / eval {len(evals)}"
          f"（按手牌切分）")
    print("跳过：" + ("，".join(f"{k} {v}" for k, v in sorted(stats.items()))
                     or "无"))
    print("\n标签分布：")
    for k, v in labels.most_common():
        print(f"  {k:12}{v:5}  {v / len(rows):5.1%}")
    print("\n按街：" + "，".join(f"{k} {v}" for k, v in streets.most_common()))

    system = rows[0]["system"]
    print(f"\nsystem {len(system)} 字符；平均 facts "
          f"{sum(len(r['row']['instruction']) for r in rows) // len(rows)} 字符")
    # 提示词几乎全是英文，按 3.5 字符/token 估；中文事实行会偏少，够用了。
    print(f"粗估 token/条 ≈ "
          f"{(len(system) + sum(len(r['row']['instruction']) for r in rows) // len(rows)
              + 24) * 2 // 7}（英文按 3.5 字符/token）")

    if args.dry_run:
        return 0

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / SYSTEM_FILE).write_text(system, encoding="utf-8")
    (out / "train.json").write_text(
        json.dumps(train, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "eval.json").write_text(
        json.dumps(evals, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "rows.jsonl").write_text(
        "\n".join(json.dumps(r["meta"], ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")

    # dataset_info.json：system 走 columns.system（alpaca 的官方口径）。
    # 推理时 advice.build_messages() 给的 system 必须与 system.txt 逐字节一致。
    cols = {"prompt": "instruction", "response": "output", "system": "system"}
    info = {
        "poker_sft": {
            "file_name": "train.json",
            "formatting": "alpaca",
            "columns": cols,
        },
        "poker_sft_eval": {
            "file_name": "eval.json",
            "formatting": "alpaca",
            "columns": cols,
        },
    }
    (out / "dataset_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n写出 {out}/{{train,eval}}.json、{SYSTEM_FILE}、dataset_info.json、rows.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
