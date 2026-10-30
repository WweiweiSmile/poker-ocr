#!/usr/bin/env python3
"""把 ai_advice_dev_hands.py --json 的输出渲染成按街对比的静态 HTML 报告。

用法：
    .venv/bin/python tools/ai_advice_report.py /tmp/ai_advice_compare.json \
        -o /tmp/ai_advice_compare.html [--analysis verdict.json]

报告内容：
  · 概览 KPI（决策点数、三档计数、跳过）
  · 按街对比（翻牌/转牌/河牌各一条堆叠条 + 数字表）
  · 逐决策点明细（按街分组，动作冲突优先）
  · 综合评价（可选，来自 --analysis 的 JSON；不给就只有数据）

`--analysis` 的结构（都可选）：

    {
      "verdict": "模型更优 / 我更好 / 各有胜负",
      "summary": ["第一段", "第二段"],
      "streets": {"flop": "翻牌街的点评", "turn": "...", "river": "..."},
      "highlights": [{"hand_id": 990023, "street": "river",
                      "title": "河牌弃坚果", "note": "…"}],
      "caveats": ["数据口径的额外说明"]
    }

刻意不引模板引擎、不引 JS 框架：单文件、离线可看、双击就开。
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

STREETS = ("flop", "turn", "river")
STREET_ZH = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}
BUCKETS = ("一致", "尺度差", "动作不同")
# 三档是「状态」不是「系列」：用固定的状态色，且永远配文字标签，不靠颜色单独表意
BUCKET_TONE = {"一致": "good", "尺度差": "warning", "动作不同": "critical"}
AGG = ("bet", "raise", "allin")

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface-1: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --good: #0ca30c;
  --warning: #fab219;
  --critical: #d03b3b;
  --accent: #2a78d6;
  --chip-good-bg: rgba(12,163,12,0.12);
  --chip-warn-bg: rgba(250,178,25,0.18);
  --chip-crit-bg: rgba(208,59,59,0.12);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --accent: #3987e5;
    --chip-good-bg: rgba(12,163,12,0.20);
    --chip-warn-bg: rgba(250,178,25,0.20);
    --chip-crit-bg: rgba(208,59,59,0.22);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;
  --surface-1: #1a1a19;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --accent: #3987e5;
  --chip-good-bg: rgba(12,163,12,0.20);
  --chip-warn-bg: rgba(250,178,25,0.20);
  --chip-crit-bg: rgba(208,59,59,0.22);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", sans-serif;
  font-size: 14px;
  line-height: 1.55;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 72px; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
h1 { font-size: 22px; font-weight: 650; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--text-secondary); font-size: 13px; margin: 0; }
.sub code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
button.theme {
  flex: none; border: 1px solid var(--border); background: var(--surface-1);
  color: var(--text-secondary); border-radius: 8px; padding: 6px 12px;
  font: inherit; font-size: 12px; cursor: pointer;
}
button.theme:hover { color: var(--text-primary); }
section { margin-top: 36px; }
h2 { font-size: 15px; font-weight: 650; margin: 0 0 14px; letter-spacing: 0.01em; }
h2 .hint { font-weight: 400; color: var(--text-muted); font-size: 12px; margin-left: 8px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 18px;
}
.tile .label { color: var(--text-secondary); font-size: 12px; }
.tile .value { font-size: 30px; font-weight: 600; line-height: 1.15; margin-top: 4px; }
.tile .note { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.tile.hero .value { font-size: 44px; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); margin-bottom: 18px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.street-block {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 12px; padding: 18px 20px; margin-bottom: 12px;
}
.street-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.street-name { font-size: 15px; font-weight: 650; }
.street-meta { color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.bar {
  display: flex; height: 22px; border-radius: 6px; overflow: hidden;
  margin: 14px 0 10px; background: var(--grid); gap: 2px;
}
.bar .seg { height: 100%; min-width: 0; display: flex; align-items: center;
  justify-content: center; color: #fff; font-size: 11px; font-weight: 600;
  font-variant-numeric: tabular-nums; }
.bar .seg.good { background: var(--good); }
.bar .seg.warning { background: var(--warning); color: #3a2a00; }
.bar .seg.critical { background: var(--critical); }
.counts { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary);
  font-variant-numeric: tabular-nums; }
.counts b { color: var(--text-primary); font-weight: 600; }
.street-note { margin-top: 12px; font-size: 13px; color: var(--text-secondary);
  border-left: 3px solid var(--accent); padding-left: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--text-secondary); font-size: 12px;
  padding: 8px 10px; border-bottom: 1px solid var(--baseline); white-space: nowrap; }
td { padding: 9px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
.chip { display: inline-flex; align-items: center; gap: 5px; border-radius: 999px;
  padding: 2px 9px; font-size: 12px; font-weight: 600; white-space: nowrap; }
.chip .dot { width: 7px; height: 7px; border-radius: 50%; }
.chip.good { background: var(--chip-good-bg); color: var(--text-primary); }
.chip.good .dot { background: var(--good); }
.chip.warning { background: var(--chip-warn-bg); color: var(--text-primary); }
.chip.warning .dot { background: var(--warning); }
.chip.critical { background: var(--chip-crit-bg); color: var(--text-primary); }
.chip.critical .dot { background: var(--critical); }
.muted { color: var(--text-muted); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.action-ai { color: var(--accent); font-weight: 600; }
.action-me { font-weight: 600; }
.reason { color: var(--text-secondary); font-size: 12px; margin-top: 3px; }
details { margin-top: 6px; }
details summary { cursor: pointer; color: var(--text-muted); font-size: 12px; }
details pre { background: var(--page); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; overflow-x: auto; font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; }
.verdict { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px 22px; }
.verdict .headline { font-size: 18px; font-weight: 650; margin-bottom: 10px; }
.verdict p { margin: 0 0 10px; color: var(--text-secondary); }
.verdict p:last-child { margin-bottom: 0; }
.highlight { border-left: 3px solid var(--critical); padding: 2px 0 2px 12px; margin: 14px 0; }
.highlight .t { font-weight: 600; color: var(--text-primary); }
.highlight .n { color: var(--text-secondary); font-size: 13px; }
@media print {
  body { background: #fff; }
  button.theme { display: none; }
  .wrap { max-width: none; padding: 0; }
}
"""

JS = """
const root = document.documentElement;
const btn = document.getElementById('theme-toggle');
function label() {
  btn.textContent = root.dataset.theme === 'dark' ? '切到浅色' : '切到深色';
}
if (!root.dataset.theme) {
  root.dataset.theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
label();
btn.addEventListener('click', () => {
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  label();
});
"""


def e(v) -> str:
    return html.escape("" if v is None else str(v))


_KIND_ZH = {"fold": "弃牌", "check": "过牌", "call": "跟注",
            "bet": "下注", "raise": "加注", "allin": "全下"}


def action_text(kind: str | None, amount: float | None,
                to_call: float | None = None, ai: bool = False) -> str:
    """把动作渲染成中文。AI 的 `call` 在 to_call==0 时其实是过牌（选项 id 复用）。"""
    if not kind:
        return "—"
    amount = round(amount, 2) if isinstance(amount, (int, float)) else None
    if ai and kind == "call" and not to_call:
        return "过牌"
    if kind in ("bet", "raise", "allin"):
        # 库里与选项的 amount 都是「本街加到总额」
        if amount is None:
            return _KIND_ZH[kind]
        return f"{_KIND_ZH[kind]}到 {amount:g}BB" if kind != "allin" \
            else f"全下到 {amount:g}BB"
    if kind == "call" and amount:
        return f"跟注 {amount:g}BB"
    return _KIND_ZH.get(kind, kind)


def stat(rows: list[dict]) -> dict:
    c = Counter(r["bucket"] for r in rows)
    total = sum(c[b] for b in BUCKETS)
    return {
        "total": total,
        **{b: c[b] for b in BUCKETS},
        "rate": (c["一致"] / total) if total else 0.0,
        "agree_any": ((c["一致"] + c["尺度差"]) / total) if total else 0.0,
    }


def render_street_bar(s: dict) -> str:
    total = s["total"]
    if not total:
        return '<div class="bar"></div>'
    segs = []
    for b in BUCKETS:
        n = s[b]
        if not n:
            continue
        pct = n / total * 100
        label = str(n) if pct >= 8 else ""
        segs.append(f'<div class="seg {BUCKET_TONE[b]}" style="flex:{n} 1 0" '
                    f'title="{b} {n} 个（{pct:.0f}%）">{label}</div>')
    return '<div class="bar">' + "".join(segs) + "</div>"


def render_rows(rows: list[dict]) -> str:
    order = {b: i for i, b in enumerate(("动作不同", "尺度差", "一致"))}
    rows = sorted(rows, key=lambda r: (order[r["bucket"]], -(r.get("pot") or 0)))
    out = []
    for r in rows:
        ai = r.get("ai") or {}
        ai_txt = action_text(ai.get("kind"), ai.get("amount"),
                             r.get("to_call"), ai=True)
        me_txt = action_text(r["actual"]["action"], r["actual"]["amount"])
        tone = BUCKET_TONE[r["bucket"]]
        facts = r.get("hand_facts") or ""
        reason = r.get("reason") or ""
        out.append(f"""
        <tr>
          <td class="mono">{e(r['hand_id'])}</td>
          <td>{e(STREET_ZH.get(r['street'], r['street']))}</td>
          <td><span class="mono">{e(r['hero_cards'])}</span><br>
              <span class="muted mono">{e(r['board'] or '—')}</span></td>
          <td class="num">{r['pot']:g}</td>
          <td class="num">{r['to_call']:g}</td>
          <td><span class="action-ai">{e(ai_txt)}</span>
              <div class="reason">{e(reason)}</div></td>
          <td><span class="action-me">{e(me_txt)}</span></td>
          <td><span class="chip {tone}"><span class="dot"></span>{e(r['bucket'])}</span></td>
          <td>{('<details><summary>牌况</summary><pre>' + e(facts) + '</pre></details>')
              if facts else ''}</td>
        </tr>""")
    return "".join(out)


def build(data: dict, analysis: dict | None) -> str:
    rows = [r for r in data["rows"] if r["bucket"] != "跳过"]
    by_street = {s: [r for r in rows if r["street"] == s] for s in STREETS}
    overall = stat(rows)
    skipped = sum(1 for r in data["rows"] if r["bucket"] == "跳过")

    tiles = [
        ("决策点", str(overall["total"]), f"跳过 {skipped} 个", ""),
        ("完全一致", f"{overall['rate']:.0%}",
         f"{overall['一致']} / {overall['total']}", ""),
        ("动作方向一致", f"{overall['agree_any']:.0%}",
         "含尺度不同但方向相同", ""),
        ("动作冲突", str(overall["动作不同"]),
         f"尺度差 {overall['尺度差']} 个", ""),
    ]
    kpis = "".join(
        f'<div class="tile"><div class="label">{e(l)}</div>'
        f'<div class="value">{e(v)}</div>'
        f'<div class="note">{e(n)}</div></div>'
        for l, v, n, _ in tiles)

    street_blocks = []
    for s in STREETS:
        st = stat(by_street[s])
        note = ((analysis or {}).get("streets") or {}).get(s, "")
        street_blocks.append(f"""
        <div class="street-block">
          <div class="street-head">
            <span class="street-name">{STREET_ZH[s]}</span>
            <span class="street-meta">{st['total']} 个决策点 ·
              完全一致 {st['rate']:.0%} · 方向一致 {st['agree_any']:.0%}</span>
          </div>
          {render_street_bar(st)}
          <div class="counts">
            <span>一致 <b>{st['一致']}</b></span>
            <span>尺度差 <b>{st['尺度差']}</b></span>
            <span>动作不同 <b>{st['动作不同']}</b></span>
          </div>
          {f'<div class="street-note">{e(note)}</div>' if note else ''}
        </div>""")

    detail = []
    for s in STREETS:
        if not by_street[s]:
            continue
        detail.append(f"""
        <h2>{STREET_ZH[s]}<span class="hint">{len(by_street[s])} 个决策点，动作冲突优先</span></h2>
        <div class="street-block" style="padding:4px 16px">
          <table>
            <thead><tr>
              <th>手</th><th>街</th><th>底牌 / 公共牌</th>
              <th class="num">底池</th><th class="num">需跟</th>
              <th>模型</th><th>我</th><th>判定</th><th></th>
            </tr></thead>
            <tbody>{render_rows(by_street[s])}</tbody>
          </table>
        </div>""")

    verdict_html = ""
    if analysis:
        paras = "".join(f"<p>{e(p)}</p>" for p in analysis.get("summary") or [])
        hl = "".join(
            f'<div class="highlight"><div class="t">{e(h.get("title", ""))}'
            f'</div><div class="n">{e(h.get("note", ""))}</div></div>'
            for h in analysis.get("highlights") or [])
        verdict_html = f"""
        <section>
          <h2>综合评价</h2>
          <div class="verdict">
            <div class="headline">{e(analysis.get("verdict", ""))}</div>
            {paras}{hl}
          </div>
        </section>"""

    caveats = "".join(f"<p>{e(c)}</p>" for c in (analysis or {}).get("caveats") or [])

    s = data.get("summary") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 模型 vs 实际操作 · 按街对比</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>AI 模型 vs 我的实际操作</h1>
      <p class="sub">模型 <code>{e(data.get('model'))}</code> ·
         数据源 <code>{e(data.get('source'))}</code> ·
         生成于 {e(data.get('generated_at'))}</p>
    </div>
    <button class="theme" id="theme-toggle" type="button">切到深色</button>
  </header>

  <section>
    <h2>概览<span class="hint">判定口径：动作类别相同 = 一致；都是下注/加注但金额差超 1/4 池 = 尺度差</span></h2>
    <div class="kpis">{kpis}</div>
  </section>

  <section>
    <h2>按街对比</h2>
    <div class="legend">
      <span><i class="swatch" style="background:var(--good)"></i>一致</span>
      <span><i class="swatch" style="background:var(--warning)"></i>尺度差</span>
      <span><i class="swatch" style="background:var(--critical)"></i>动作不同</span>
    </div>
    {''.join(street_blocks)}
  </section>

  {verdict_html}

  <section>
    <h2>逐决策点明细</h2>
    {''.join(detail)}
  </section>

  <section>
    <h2>口径</h2>
    <p class="muted" style="font-size:12px;margin:0">
      只评翻后决策（模型不覆盖翻前）；底池由盲注 + 各街 amountBb 重建，误差 ≤1BB；
      allin 的 amountBb 按「本街加到总额」处理；模型理由为模型返回的原文。
      判定只看动作类别与尺度，不代表 EV 结论——谁更好见上面的综合评价。
    </p>
    {caveats}
  </section>
</div>
<script>{JS}</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="把 AI 对比结果渲染成按街对比的 HTML")
    ap.add_argument("json", help="ai_advice_dev_hands.py --json 的输出")
    ap.add_argument("-o", "--output", default="/tmp/ai_advice_compare.html")
    ap.add_argument("--analysis", default=None,
                    help="可选的综合评价 JSON（见模块 docstring）")
    args = ap.parse_args()

    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    analysis = (json.loads(Path(args.analysis).read_text(encoding="utf-8"))
                if args.analysis else None)
    out = Path(args.output)
    out.write_text(build(data, analysis), encoding="utf-8")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
