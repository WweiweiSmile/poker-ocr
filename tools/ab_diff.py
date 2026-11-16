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
import difflib
import json
import sys
from collections import Counter
from pathlib import Path

# 单独跑 nv tools/ab_diff.py 时 sys.path[0] 是 tools/，读不到仓库包；
# 只有认 witness 前缀时才用得到 poker_ocr（见 witness_prefix）。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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



# ─────────────────── 段内行级：witness 行归一类 ───────────────────
#
# 段落级只能回答「哪一段变了」。witness（摊牌证据）行落在「## 对手画像」
# 段内部，和别的画像行混在一起：画像里 n 从 142 变 150、和多出一条证据行，
# 在段落级看起来都是「对手画像变了」。§15.3 要的是行级——段内新增了几行、
# 以及事实块的变化是不是**只**来自 witness 行：
#
#   翻转率差值 = 「事实块只因 witness 行而变」的翻转率 − 「事实块没变」的翻转率
#
# witness 行按前缀归一（默认「摊牌证据：」，与渲染层读同一份词表），所以
# N 从 1 次变 2 次仍算同一类变化，不会因为文案不同被算成「其它行」。

_WITNESS_PREFIX = "摊牌证据："


def witness_prefix() -> str:
    """「摊牌证据：」前缀：优先读渲染层词表，读不到回退内置常量。

    词表里改文案是允许的（AGENTS.md 三步走），工具必须跟着同一份来源走；
    否则改了前缀之后归因会静默把 witness 行算成「其它行」，A/B 主指标失真。
    """
    try:
        from poker_ocr.witness.render import load_phrasing

        prefix = str(load_phrasing().get("prefix") or "").strip()
        if prefix:
            return prefix
    except Exception:  # noqa: BLE001 - 词表不可用不该挡住对比
        pass
    # 回退到内置常量：不报错，但必须让人看见，否则改了词表后归因会静默算错。
    print(f"⚠ 读不到 witness 词表前缀，回退内置「{_WITNESS_PREFIX}」；"
          "若词表已改文案，本次行级归因不可信。", file=sys.stderr)
    return _WITNESS_PREFIX


def split_lines(text: str) -> list[str]:
    """事实块正文 → 归一化行（strip、去空行）。排版差异不算内容变化。"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def is_witness_line(line: str, prefix: str | None = None) -> bool:
    """这一行是不是 witness 行（渲染时缩进两格，所以先 strip）。"""
    mark = prefix if prefix is not None else witness_prefix()
    return line.strip().startswith(mark)


def line_diff(a_text: str, b_text: str) -> tuple[list[str], list[str]]:
    """段内逐行 diff → (新增行, 删除行)，都已归一化，按 b / a 的出现顺序。"""
    la = split_lines(a_text)
    lb = split_lines(b_text)
    added: list[str] = []
    removed: list[str] = []
    matcher = difflib.SequenceMatcher(a=la, b=lb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(la[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(lb[j1:j2])
    return added, removed


def line_attribution(a: dict, b: dict, *, prefix: str | None = None) -> dict:
    """两个决策点之间：段内逐行增删，以及变化是否只来自 witness 行。

    返回的 witness_only 就是 §15.3 主指标要的子集判定：事实块确实变了，
    且增删的每一行都是 witness 行。别的段变了、或同段里统计行也变了，
    都不算——那种点的动作翻转不能归给摊牌证据。
    """
    mark = prefix if prefix is not None else witness_prefix()
    sa = sections(a.get("prompt_facts") or "")
    sb = sections(b.get("prompt_facts") or "")
    added: list[str] = []
    removed: list[str] = []
    for name in changed_sections(a, b):
        add, rem = line_diff(sa.get(name, ""), sb.get(name, ""))
        added.extend(add)
        removed.extend(rem)
    witness_added = [line for line in added if is_witness_line(line, mark)]
    witness_removed = [line for line in removed if is_witness_line(line, mark)]
    other_added = [line for line in added if not is_witness_line(line, mark)]
    other_removed = [line for line in removed if not is_witness_line(line, mark)]
    facts_changed = ((a.get("prompt_facts") or "") != (b.get("prompt_facts") or ""))
    return {
        "prefix": mark,
        "changed_sections": changed_sections(a, b),
        "added_lines": len(added),
        "removed_lines": len(removed),
        "witness_added": len(witness_added),
        "witness_removed": len(witness_removed),
        "other_added": len(other_added),
        "other_removed": len(other_removed),
        "witness_only": (bool(witness_added or witness_removed)
                         and not (other_added or other_removed)),
        # 事实块变了、但归一化后一行都没变：只有排版差异（理论上不该出现）
        "reformat_only": facts_changed and not (added or removed),
    }

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
    ap.add_argument("--json-out", default=None,
                    help="把汇总数字与翻转清单写成 JSON（结论要能离线复核）")
    ap.add_argument("--csv-out", default=None,
                    help="逐点归因表（121 行那种），给人工复核/表格统计用")
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
    print("（同配置重跑（temperature>0）噪声基线常见 5–10%，"
          "差异不到噪声量级不作结论）\n")

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
    sec_base_ok = len(clean) >= 30
    if clean:
        print(f"\n段落级归因（只算「恰好一个段落变了」的点；未变的 {len(clean)} 个点"
              f"翻转 {base_r:.1%}" + ("，作为基线）" if sec_base_ok
                                       else "，但 n<30 只作参考、不算超额）"))
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
        base_txt = f"{base_r:.0%}" if (clean and sec_base_ok) else "—"
        excess_txt = f"{excess:+.0%}" if (clean and sec_base_ok) else "—"
        print(f"{name:22s}{n_s:8d}{f:6d}{rate:8.0%}{base_txt:>8s}{excess_txt:>8s}")
    if multi:
        print(f"\n（另有 {len(multi)} 个点同时变了 2 段以上，归因不唯一，未计入上表）")
        combo = Counter(tuple(ch) for _, ch in multi)
        for names, n in combo.most_common(5):
            f = sum(1 for k, ch in multi
                    if tuple(ch) == names and str(a[k].get("new")) != str(b[k].get("new")))
            print(f"  {' + '.join(names)}: {n} 个点，翻转 {f}")

    # ── 行级归因（§15.3 主指标）：变化是不是只来自 witness 行 ──
    #
    # witness 行住在「## 对手画像」段里，段落级会把「n 涨了 1」和「多了
    # 一条证据行」混成同一件事。这里把段内逐行 diff 出来，按前缀归一，
    # 只把「事实块只因 witness 行而变」的那批点当作改动的作用面。
    mark = witness_prefix()
    attr = {k: line_attribution(a[k], b[k], prefix=mark) for k in common}
    wit = [k for k in changed if attr[k]["witness_only"]]
    reformat = [k for k in changed if attr[k]["reformat_only"]]
    reformat_set = set(reformat)
    other = [k for k in changed
             if not attr[k]["witness_only"] and k not in reformat_set]
    fw, rw = _rate(wit)
    fo, ro = _rate(other)
    print(f"\n行级归因（witness 行按「{mark}」前缀归一；§15.3 主指标）")
    print(f"{'子集':26s}{'n':>6s}{'动作变化':>8s}{'翻转率':>8s}{'基线':>8s}{'超额':>8s}")

    # 基线 n 太小（witness 覆盖率高时常见 n=1）时，「超额」只是噪声：
    # §15.3 要求 n≥30 才读比例，基线一侧同理——不够就只打印 n，不算差值。
    base_ok = len(same) >= 30

    def _line_row(label: str, ks: list[tuple], f: int, r: float,
                  *, is_baseline: bool = False) -> None:
        if not ks:
            print(f"{label:26s}{0:6d}{0:8d}{'—':>8s}{'—':>8s}{'—':>8s}")
            return
        # 基线一侧同样受 n≥30 约束：不够就只给 n，不给比例，免得把 1 个点当基线
        base_txt = f"{rs:.0%}" if (base_ok and not is_baseline) else "—"
        excess_txt = (f"{(r - rs) * 100:+.0f}pp"
                      if (base_ok and not is_baseline) else "—")
        print(f"{label:26s}{len(ks):6d}{f:8d}{r:8.0%}{base_txt:>8s}{excess_txt:>8s}")

    _line_row("只因 witness 行而变", wit, fw, rw)
    _line_row("还有其它行变化", other, fo, ro)
    _line_row("事实块没变（基线）", same, fs, rs, is_baseline=True)
    if same and not base_ok:
        print(f"⚠ 基线子集 n={len(same)} < 30：超额差值不可算（witness 覆盖率高时"
              "对照组会塌掉）。改法：用同配置重跑一遍当噪声基线，"
              "超额 = 上表翻转率 − 重跑翻转率（见 docs/AB测试-手牌集.md §6）。")
    if wit:
        add_avg = sum(attr[k]["witness_added"] for k in wit) / len(wit)
        rem_avg = sum(attr[k]["witness_removed"] for k in wit) / len(wit)
        print(f"（witness 子集平均段内 +{add_avg:.1f} / −{rem_avg + 0.0:.1f} 行）")
    if reformat:
        print(f"（另有 {len(reformat)} 个点事实块变了、归一化后只有排版差异，未计入）")
    if not wit:
        print("⚠ 没有「只因 witness 行而变」的决策点：两遍的 witness 开关/画像一样，"
              "或者 profiles 里没有 witness 行——这一轮 A/B 对摊牌证据无话可说。")
    elif len(wit) < 30:
        print(f"⚠ witness 子集 n={len(wit)} < 30：§15.3 要求这批 n≥30 才读比例，"
              "现在只看方向。")

    if flips:
        moves = Counter((str(a[k].get("new")), str(b[k].get("new"))) for k in flips)
        print("\n最常见的动作迁移：")
        for (x, y), n in moves.most_common(6):
            print(f"  {x:>12s} → {y:<12s} {n}")

        shown = min(args.show, len(flips))
        if shown:
            print(f"\n前 {shown} 个翻转：")
        for k in flips[:shown]:
            ra, rb = a[k], b[k]
            print(f"\n  {ra.get('hand')} {ra.get('street')} {ra.get('cards')} "
                  f"{ra.get('board') or ''}｜pot {ra.get('pot')} 需跟 {ra.get('to_call')}")
            print(f"    旧 {ra.get('new')}：{(ra.get('new_reason') or '')[:70]}")
            print(f"    新 {rb.get('new')}：{(rb.get('new_reason') or '')[:70]}")
            fa = ra.get("prompt_facts") or ""
            fb = rb.get("prompt_facts") or ""
            if fa != fb:
                print("    变化段落：" + "、".join(attr[k]["changed_sections"]))
                at = attr[k]
                tail = "（只来自 witness 行）" if at["witness_only"] else ""
                print(f"    段内：+{at['added_lines']} −{at['removed_lines']} 行"
                      f"（witness +{at['witness_added']} −{at['witness_removed']}，"
                      f"其它 +{at['other_added']} −{at['other_removed']}）{tail}")
            else:
                print("    ⚠ 事实块完全相同 —— 这是模型噪声，不是改动带来的")
    if args.json_out:
        by_street = {}
        for street in ("preflop", "flop", "turn", "river"):
            sub = [k for k in common if a[k].get("street") == street]
            if sub:
                f = sum(1 for k in sub if k in flips)
                by_street[street] = {"n": len(sub), "flips": f,
                                     "rate": f / len(sub)}
        rows = []
        for k in flips:
            rows.append({
                "hand": a[k].get("hand"), "street": a[k].get("street"),
                "cards": a[k].get("cards"), "board": a[k].get("board"),
                "pot": a[k].get("pot"), "to_call": a[k].get("to_call"),
                "old_action": a[k].get("new"), "new_action": b[k].get("new"),
                "old_reason": a[k].get("new_reason"),
                "new_reason": b[k].get("new_reason"),
                "facts_changed": k in changed_set,
                "changed_sections": attr[k]["changed_sections"],
                "line_attribution": attr[k],
            })
        payload = {
            "summary": {
                "witness_prefix": mark,
                "common": len(common),
                "only_one_side": only,
                "flips": len(flips),
                "flip_rate": len(flips) / len(common),
                "by_street": by_street,
                "facts_changed": len(changed),
                "facts_same": len(same),
                # 基线子集够大才算得出「超额」；否则只能拿同配置重跑当噪声基线
                "baseline_reliable": base_ok,
                "witness_only": {"n": len(wit), "flips": fw, "rate": rw},
                "other_lines": {"n": len(other), "flips": fo, "rate": ro},
                "unchanged": {"n": len(same), "flips": fs, "rate": rs},
            },
            "flips": rows,
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n翻转清单已写入 {args.json_out}")
    if args.csv_out:
        import csv

        with open(args.csv_out, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "手牌", "街", "底池", "需跟", "手牌", "公共牌",
                "旧动作", "新动作", "动作变化", "事实块变化", "变化段落",
                "witness 新增", "witness 删除", "其它新增", "其它删除",
                "只因 witness 行而变", "仅排版差异",
            ])
            for k in common:
                ra, rb, at = a[k], b[k], attr[k]
                writer.writerow([
                    ra.get("hand"), ra.get("street"), ra.get("pot"),
                    ra.get("to_call"), ra.get("cards"), ra.get("board") or "",
                    ra.get("new"), rb.get("new"),
                    int(str(ra.get("new")) != str(rb.get("new"))),
                    int(k in changed_set), "、".join(at["changed_sections"]),
                    at["witness_added"], at["witness_removed"],
                    at["other_added"], at["other_removed"],
                    int(at["witness_only"]), int(at["reformat_only"]),
                ])
        print(f"逐点归因已写入 {args.csv_out}（{len(common)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
