#!/usr/bin/env python3
"""比较两次 `replay_live_decisions.py --model` 的输出：动作变了多少、变成什么。

和 `advice_scorecard.py` 的分工
───────────────────────────────
scorecard 比的是「复跑 vs 当时记录的建议」——里面混着**代码变化**、**模型噪声**、
以及**当时有画像/现在没画像**之类与改动无关的差异。要做归因得两边同配置：
同一份代码、同一批决策点，只改一处，跑两遍，再用这个工具比。

    # A：改动前
    cp poker_ocr/ranges.py /tmp/new.py
    git show HEAD:poker_ocr/ranges.py > poker_ocr/ranges.py
    nv tools/replay_live_decisions.py --model --since 2026-11-04 --json /tmp/ab-a.json
    cp /tmp/new.py poker_ocr/ranges.py

    # B：改动后
    nv tools/replay_live_decisions.py --model --since 2026-11-04 --json /tmp/ab-b.json

    nv tools/ab_diff.py /tmp/ab-a.json /tmp/ab-b.json

**噪声基线**：同一份代码连跑两遍，翻转率通常 3–5%（temperature + 上下文敏感）。
低于这个数的差异不要当信号。

决策点的对齐用 (手牌, 街, 底池, 需跟)——同一手牌在同一街可能有多个决策点，
单用 hand_id 会把它们压成一个。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


# ─────────────────── 段落级：哪一类事实导致了变化 ───────────────────
#
# 事实块是按 "## 段落名" 组织的（当前局面 / 行动线 / 翻前信息 / 对手画像 /
# 实时牌力 / 池子先验 / 范围弱点与弃牌率 / 数据质量 / 可选动作）。
# 只比「整块变没变」没法归因：画像里 n 涨了 1 也算变。按段落拆开之后，
# 才能回答「这次动作变化是被哪一类事实推动的」。

_SECTION_PREFIX = "## "
_NO_SECTION = "（头部）"


def sections(text: str) -> dict[str, str]:
    """把事实块切成 {段落名: 正文}。同名段落只保留第一段（不该出现重复）。"""
    out: dict[str, str] = {}
    if not text:
        return out
    current = _NO_SECTION
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith(_SECTION_PREFIX):
            if buf or current in out:
                out.setdefault(current, "\n".join(buf).strip())
            # 标题里的括号说明是渲染细节（如「（逐座位…）」），归因时去掉
            current = line[len(_SECTION_PREFIX):].strip().split("（")[0].strip()
            buf = []
        else:
            buf.append(line)
    out.setdefault(current, "\n".join(buf).strip())
    return out


def changed_sections(a: dict, b: dict) -> list[str]:
    """两个决策点之间，哪些段落的内容不同（顺序稳定：按 a 的出现顺序）。"""
    sa = sections(a.get("prompt_facts") or "")
    sb = sections(b.get("prompt_facts") or "")
    names = list(sa) + [n for n in sb if n not in sa]
    return [n for n in names if sa.get(n, "") != sb.get(n, "")]


def key_of(row: dict) -> tuple:
    return (str(row.get("hand")), str(row.get("street")),
            round(float(row.get("pot") or 0.0), 2),
            round(float(row.get("to_call") or 0.0), 2))


def load(path: Path) -> dict[tuple, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {key_of(r): r for r in rows if r.get("street")}


def main() -> int:
    ap = argparse.ArgumentParser(description="比较两次复跑的动作差异")
    ap.add_argument("a", help="基线（改动前）")
    ap.add_argument("b", help="对照（改动后）")
    ap.add_argument("--show", type=int, default=8, help="列出前 N 个翻转")
    ap.add_argument("--json-out", default=None, help="把翻转清单写成 JSON")
    args = ap.parse_args()

    a = load(Path(args.a))
    b = load(Path(args.b))
    common = [k for k in a if k in b]
    only = len(a) + len(b) - 2 * len(common)
    if not common:
        print("两次结果没有可比的决策点（检查 --since / --limit 是否一致）")
        return 2

    flips = [k for k in common if str(a[k].get("new")) != str(b[k].get("new"))]
    print(f"可比决策点 {len(common)}｜两次独有 {only}")
    print(f"动作变化 {len(flips)}/{len(common)} = {len(flips) / len(common):.1%}")
    print("（同配置重跑的噪声基线一般 3–5%，低于此不作结论）\n")

    print(f"{'街':8s}{'n':>6s}{'变化':>6s}{'比例':>8s}")
    for street in ("preflop", "flop", "turn", "river"):
        sub = [k for k in common if a[k].get("street") == street]
        if not sub:
            continue
        f = sum(1 for k in sub if k in flips)
        print(f"{street:8s}{len(sub):6d}{f:6d}{f / len(sub):8.0%}")

    # 事实块有没有变：变了才有归因意义；没变却翻了的，就是模型噪声。
    changed = [k for k in common
               if (a[k].get("prompt_facts") or "") != (b[k].get("prompt_facts") or "")]
    changed_set = set(changed)
    same = [k for k in common if k not in changed_set]
    def _rate(ks):
        f = sum(1 for k in ks if str(a[k].get("new")) != str(b[k].get("new")))
        return f, (f / len(ks) if ks else 0.0)
    fc, rc = _rate(changed)
    fs, rs = _rate(same)
    print(f"\n{'子集':14s}{'n':>6s}{'动作变化':>8s}{'比例':>8s}")
    print(f"{'事实块变了':14s}{len(changed):6d}{fc:8d}{rc:8.0%}")
    print(f"{'事实块没变':14s}{len(same):6d}{fs:8d}{rs:8.0%}")
    if len(changed):
        print(f"（事实块变化 {len(changed)} 个 = {len(changed)/len(common):.0%} 的决策点"
              f"真正看到了不同的输入）")

    # ── 段落级归因：只统计「恰好一个段落变了」的点，避免多因混杂 ──
    by_single: dict[str, list[tuple]] = {}
    multi: list[tuple] = []
    for k in common:
        ch = changed_sections(a[k], b[k])
        if len(ch) == 1:
            by_single.setdefault(ch[0], []).append(k)
        elif len(ch) >= 2:
            multi.append((k, ch))
    clean = [k for k in common if not changed_sections(a[k], b[k])]
    base_f, base_r = _rate(clean)
    if clean:
        print(f"\n段落级归因（只算「恰好一个段落变了」的点；未变的 {len(clean)} 个点"
              f"翻转 {base_r:.1%}，作为基线）")
    else:
        print("\n段落级归因（只算「恰好一个段落变了」的点；"
              "这次没有「未变」的点，基线不可算）")
    print(f"{'段落':22s}{'变化点数':>8s}{'翻转':>6s}{'翻转率':>8s}{'基线':>8s}{'超额':>8s}")
    rows_by_section = []
    for name, ks in by_single.items():
        f = sum(1 for k in ks if str(a[k].get("new")) != str(b[k].get("new")))
        rate = f / len(ks)
        rows_by_section.append((rate - base_r, name, len(ks), f, rate))
    for excess, name, n_s, f, rate in sorted(rows_by_section, reverse=True):
        base_txt = f"{base_r:.0%}" if clean else "—"
        excess_txt = f"{excess:+.0%}" if clean else "—"
        print(f"{name:22s}{n_s:8d}{f:6d}{rate:8.0%}{base_txt:>8s}{excess_txt:>8s}")
    if multi:
        print(f"\n（另有 {len(multi)} 个点同时变了 2 段以上，归因不唯一，未计入上表）")
        combo = Counter(tuple(ch) for _, ch in multi)
        for names, n in combo.most_common(5):
            f = sum(1 for k, ch in multi
                    if tuple(ch) == names and str(a[k].get("new")) != str(b[k].get("new")))
            print(f"  {' + '.join(names)}: {n} 个点，翻转 {f}")

    if flips:
        moves = Counter((str(a[k].get("new")), str(b[k].get("new"))) for k in flips)
        print("\n最常见的动作迁移：")
        for (x, y), n in moves.most_common(6):
            print(f"  {x:>12s} → {y:<12s} {n}")

        print(f"\n前 {min(args.show, len(flips))} 个翻转：")
        for k in flips[:args.show]:
            ra, rb = a[k], b[k]
            print(f"\n  {ra.get('hand')} {ra.get('street')} {ra.get('cards')} "
                  f"{ra.get('board') or ''}｜pot {ra.get('pot')} 需跟 {ra.get('to_call')}")
            print(f"    旧 {ra.get('new')}：{(ra.get('new_reason') or '')[:70]}")
            print(f"    新 {rb.get('new')}：{(rb.get('new_reason') or '')[:70]}")
            fa = ra.get("prompt_facts") or ""
            fb = rb.get("prompt_facts") or ""
            if fa != fb:
                print("    变化段落：" + "、".join(changed_sections(ra, rb)))
            else:
                print("    ⚠ 事实块完全相同 —— 这是模型噪声，不是改动带来的")
    if args.json_out:
        payload = []
        for k in flips:
            payload.append({
                "hand": a[k].get("hand"), "street": a[k].get("street"),
                "cards": a[k].get("cards"), "board": a[k].get("board"),
                "pot": a[k].get("pot"), "to_call": a[k].get("to_call"),
                "old_action": a[k].get("new"), "new_action": b[k].get("new"),
                "old_reason": a[k].get("new_reason"),
                "new_reason": b[k].get("new_reason"),
                "facts_changed": k in changed_set,
                "changed_sections": changed_sections(a[k], b[k]),
            })
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n翻转清单已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
