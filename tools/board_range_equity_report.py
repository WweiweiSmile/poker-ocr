#!/usr/bin/env python3
"""从历史手牌库生成「牌面范围权益」HTML 报告。

    .venv/bin/python tools/board_range_equity_report.py
    .venv/bin/python tools/board_range_equity_report.py --ids h4,h29,h31
    .venv/bin/python tools/board_range_equity_report.py --out reports/xxx.html

口径（按当前需求）：
  · **不使用翻前范围**：不读 RFI / BB 防守 / 冷跟表，也不把历史翻前动作
    当成范围。每个对手的范围只由公共牌生成——`board_range.build(board,
    "call", prior=None)`，即连接度 >= 0.30 的 combo。
  · Hero 权益 = 手牌 vs N 个独立对手的该范围，Monte Carlo 发完剩余公共牌。
  · 对手数 N 从历史行动里的非 hero 在局座位推导（只取人数，不取范围）；
    最多 4 家，避免报告里出现 9 人桌的极端值。
  · 分别展示翻牌 / 转牌 / 河牌。Behind now 是当前成手牌口径，和权益分开。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poker_ocr import board_range, hand_vs_ranges  # noqa: E402
from poker_ocr.coach import parse_cards, read_hand  # noqa: E402

DEFAULT_HANDS = ROOT / "hands" / "1023.hands.json"
DEFAULT_OUT = ROOT / "reports" / "historical-board-range-equity.html"

# 9 手有完整 5 张公共牌的手牌，覆盖空气/顶对/两对/三条/顺子/公对面/花面。
DEFAULT_IDS = ("h4", "h29", "h31", "h55", "h71", "h73", "h86", "h113", "h119")
STREETS = ((3, "翻牌", "Flop"), (4, "转牌", "Turn"), (5, "河牌", "River"))

SUIT_SYMBOL = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
RED_SUITS = {"h", "d"}


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def card_html(text: str, *, ghost: bool = False) -> str:
    text = str(text or "").strip()
    if len(text) < 2:
        return '<span class="card ghost">—</span>'
    rank, suit = text[:-1], text[-1].lower()
    red = " red" if suit in RED_SUITS else ""
    ghost_cls = " ghost" if ghost else ""
    return (f'<span class="card{red}{ghost_cls}">'
            f'<span class="rank">{esc(rank)}</span>'
            f'<span class="suit">{esc(SUIT_SYMBOL.get(suit, suit))}</span></span>')


def cards_html(cards) -> str:
    return "".join(card_html(c) for c in cards)


def hand_read(hero: str, board: str) -> str:
    try:
        return read_hand(parse_cards(hero), parse_cards(board)).label
    except (ValueError, TypeError, IndexError):
        return "—"


def opponent_count(hand: dict) -> int:
    """从历史行动取非 hero 在局座位数；只用于权益的多人池，不做范围先验。"""
    last: dict[str, str] = {}
    for rec in hand.get("streets") or []:
        for a in rec.get("actions") or []:
            if a.get("is_hero"):
                continue
            seat = a.get("position") or a.get("seat_id") or "?"
            last[seat] = a.get("action") or ""
    active = [seat for seat, action in last.items() if action != "fold"]
    return max(1, min(len(active), 4))


def format_action(a: dict) -> str:
    pos = a.get("position") or a.get("seat_id") or "?"
    action = a.get("action") or "?"
    amount = a.get("amount")
    label = {"bet": "bet", "raise": "raise to", "call": "call",
             "check": "check", "fold": "fold"}.get(action, action)
    if amount is not None and action in ("bet", "raise", "call"):
        return f"{pos} {label} {amount:g}BB"
    return f"{pos} {label}"


def action_line(hand: dict) -> str:
    parts: list[str] = []
    for rec in hand.get("streets") or []:
        actions = [format_action(a) for a in rec.get("actions") or []]
        if actions:
            parts.append(f'<div><b>{esc(rec.get("street", ""))}</b> '
                         f'{" · ".join(esc(x) for x in actions)}</div>')
    return "".join(parts) or '<div class="muted">行动线未记录</div>'


def street_metrics(hand: dict, hero: str, board: list[str], n_opp: int,
                   hid: str) -> list[dict]:
    out: list[dict] = []
    for length, label_cn, label_en in STREETS:
        board_text = "".join(board[:length])
        wide = board_range.build(board_text, "call", None)
        samples = 2000 if n_opp <= 2 else 1500
        row = {
            "length": length,
            "label_cn": label_cn,
            "label_en": label_en,
            "board": board_text,
            "cards": board[:length],
            "label": hand_read(hero, board_text),
            "range_width": wide.width_pct if wide is not None else 0.0,
            "range_combos": wide.combo_count if wide is not None else 0,
            "n_opp": n_opp,
            # 多路（历史实际人数）
            "equity": 0.0,
            "behind": 0.0,
            "tie": 0.0,
            "ci": 0.0,
            "samples": samples,
            # 单挑（N=1）—— 判断手牌本身强弱的更直观口径
            "equity_1": 0.0,
            "behind_1": 0.0,
            "tie_1": 0.0,
            "ci_1": 0.0,
            "samples_1": 4000,
            "beaten": (),
        }
        if wide is not None:
            single = hand_vs_ranges.compute(
                hero, board_text, [("o0", None, wide, "call")],
                samples=row["samples_1"], seed_key=f"{hid}:{length}:1",
            )
            if single is not None:
                row.update(
                    equity_1=single.equity,
                    behind_1=single.opponents[0].behind,
                    tie_1=single.opponents[0].tie,
                    ci_1=single.equity_ci,
                )
            result = hand_vs_ranges.compute(
                hero, board_text,
                [(f"o{i}", None, wide, "call") for i in range(n_opp)],
                samples=samples,
                seed_key=f"{hid}:{length}",
            )
            if result is not None:
                row.update(
                    equity=result.equity,
                    behind=result.behind_any,
                    tie=result.tie_any,
                    ci=result.equity_ci,
                    samples=result.samples,
                    beaten=result.opponents[0].beaten_by[:3],
                )
        out.append(row)
    return out


def equity_class(value: float) -> str:
    if value >= 0.60:
        return "good"
    if value >= 0.40:
        return "mid"
    return "bad"


def behind_class(value: float) -> str:
    if value >= 0.60:
        return "bad"
    if value >= 0.35:
        return "mid"
    return "good"


def bar(value: float, kind: str) -> str:
    pct = max(0.0, min(1.0, float(value))) * 100
    return (f'<div class="bar"><span class="{kind}" '
            f'style="width:{pct:.1f}%"></span></div>')


def chips(groups) -> str:
    if not groups:
        return '<span class="chip muted">没有更大的牌型</span>'
    return "".join(
        f'<span class="chip">{esc(g.name)} {g.share:.1%}</span>' for g in groups
    )


def sparkline(metrics: list[dict]) -> str:
    width, height = 260, 72
    xs = [24, 130, 236]
    eqs = [m["equity_1"] for m in metrics]
    points = " ".join(
        f"{x},{height - 14 - eq * 44:.1f}" for x, eq in zip(xs, eqs)
    )
    dots = "".join(
        f'<circle cx="{x}" cy="{height - 14 - eq * 44:.1f}" r="4" '
        f'class="dot {equity_class(eq)}"/>'
        for x, eq in zip(xs, eqs)
    )
    labels = "".join(
        f'<text x="{x}" y="{height - 1}" text-anchor="middle" '
        f'class="spark-label">{esc(m["label_cn"])}</text>'
        for x, m in zip(xs, metrics)
    )
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="权益走势">'
            f'<line x1="16" y1="{height - 14 - 0.5 * 44:.1f}" '
            f'x2="{width - 12}" y2="{height - 14 - 0.5 * 44:.1f}" '
            f'class="spark-mid"/>'
            f'<polyline points="{points}" class="spark-line"/>{dots}{labels}</svg>')


def summary_row(hand: dict, hero: str, n_opp: int, metrics: list[dict]) -> str:
    hid = hand.get("hand_id")
    eqs = "".join(
        f'<td class="num {equity_class(m["equity_1"])}">{m["equity_1"]:.0%}</td>'
        for m in metrics
    )
    read = metrics[-1]["label"]
    return (
        f'<tr><td><a href="#{esc(hid)}"><b>{esc(hid)}</b></a></td>'
        f'<td>{cards_html(hand.get("hero_cards") or [])}</td>'
        f'<td>{esc(hand.get("hero_position") or "—")}</td>'
        f'<td>{cards_html(hand.get("board") or [])}</td>'
        f'<td class="num">{n_opp}</td>{eqs}'
        f'<td class="num {equity_class(metrics[-1]["equity"])}">{metrics[-1]["equity"]:.0%}</td>'
        f'<td class="num">{metrics[-1]["range_width"]:.0f}%</td>'
        f'<td>{esc(read)}</td></tr>'
    )


def street_panel(metric: dict) -> str:
    eq1 = metric["equity_1"]
    cls1 = equity_class(eq1)
    eq_m = metric["equity"]
    return f'''
      <section class="street-panel">
        <div class="street-head">
          <h3>{esc(metric["label_cn"])} <small>{esc(metric["label_en"])}</small></h3>
          <div class="mini-cards">{cards_html(metric["cards"])}</div>
        </div>
        <div class="eq-line"><strong class="{cls1}">{eq1:.0%}</strong>
          <span>单挑权益(N=1)</span></div>
        {bar(eq1, cls1)}
        <div class="sub">±{max(metric["ci_1"] * 100, 0.1):.1f}% · {metric["samples_1"]} 次抽样</div>
        <div class="multi-line">多路权益（N={metric["n_opp"]}）<b class="{equity_class(eq_m)}">{eq_m:.0%}</b></div>
        <dl>
          <div><dt>Behind now（多路）</dt><dd class="{behind_class(metric["behind"])}">{metric["behind"]:.1%}</dd></div>
          <div><dt>单挑 Behind</dt><dd class="{behind_class(metric["behind_1"])}">{metric["behind_1"]:.1%}</dd></div>
          <div><dt>Tie</dt><dd>{metric["tie"]:.1%}</dd></div>
          <div><dt>牌面范围宽度</dt><dd>{metric["range_width"]:.1f}% <small>({metric["range_combos"]} combos)</small></dd></div>
          <div><dt>当前读牌</dt><dd>{esc(metric["label"])}</dd></div>
        </dl>
        <div class="chips">{chips(metric["beaten"])}</div>
      </section>'''


def hand_card(hand: dict, hero: str, n_opp: int, metrics: list[dict]) -> str:
    hid = hand.get("hand_id")
    board = "".join(hand.get("board") or [])
    final_read = metrics[-1]["label"]
    return f'''
    <article class="hand-card" id="{esc(hid)}">
      <header class="hand-head">
        <div>
          <div class="hand-id">{esc(hid)}</div>
          <div class="hero-line">{cards_html(hand.get("hero_cards") or [])}
            <span class="pos">{esc(hand.get("hero_position") or "—")}</span>
            <span class="muted">· {esc(final_read)}</span>
          </div>
        </div>
        <div class="meta">
          <span class="badge">对手 {n_opp}</span>
          <span class="badge soft">无翻前范围</span>
        </div>
      </header>
      <div class="runout">
        <div class="runout-label">公共牌</div>
        <div class="runout-cards">{cards_html(hand.get("board") or [])}</div>
      </div>
      <div class="street-grid">{''.join(street_panel(m) for m in metrics)}</div>
      <div class="spark-wrap">
        <div class="spark-title">权益走势</div>
        {sparkline(metrics)}
      </div>
      <details class="actions">
        <summary>历史行动线</summary>
        {action_line(hand)}
      </details>
    </article>'''


CSS = """
:root{--ink:#18202f;--muted:#6b7486;--line:#e3e7f0;--bg:#f4f6fb;--card:#fff;
--green:#0f9f6e;--green-bg:#e5f8f0;--amber:#b7791f;--amber-bg:#fff5df;
--red:#cf3f4f;--red-bg:#fdecef;--blue:#3559d4;--shadow:0 12px 34px rgba(24,32,47,.08)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;
line-height:1.55}
.hero{background:radial-gradient(circle at 15% 10%,#2e4a9b 0,#16224a 48%,#0b1226 100%);
color:#fff;padding:52px 24px 44px}
.wrap{max-width:1280px;margin:0 auto}
.hero h1{margin:0 0 10px;font-size:clamp(28px,4vw,46px);letter-spacing:-.02em}
.hero p{margin:0;color:#c8d2f0;max-width:900px}
.hero .kicker{display:inline-block;font-size:12px;letter-spacing:.16em;text-transform:uppercase;
color:#9fb6ff;border:1px solid #3d57a3;border-radius:999px;padding:4px 11px;margin-bottom:16px}
.method{margin-top:-24px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:18px;
box-shadow:var(--shadow);padding:22px 24px}
.method h2{margin:0 0 8px;font-size:18px}
.method p{margin:6px 0;color:var(--muted);font-size:13.5px}
.method b{color:var(--ink)}
main{padding:26px 24px 64px}
.section-title{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
margin:34px 0 14px}
.section-title h2{margin:0;font-size:22px}
.section-title span{color:var(--muted);font-size:13px}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:16px;background:#fff;
box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:960px}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{background:#f8f9fd;color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.02em}
tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.hand-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.hand-card{background:#fff;border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);
padding:20px;scroll-margin-top:20px}
.hand-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;
padding-bottom:14px;border-bottom:1px solid var(--line)}
.hand-id{font-weight:800;font-size:18px;letter-spacing:.02em}
.hero-line{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:7px;font-size:13px}
.pos{background:#eef2ff;color:#3049a8;border-radius:8px;padding:1px 7px;font-size:12px;font-weight:700}
.meta{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.badge{background:#eef2ff;color:#3049a8;border-radius:999px;padding:3px 9px;font-size:11.5px;font-weight:700}
.badge.soft{background:#f1f3f8;color:#687184;font-weight:600}
.muted{color:var(--muted)}
.runout{display:flex;align-items:center;gap:14px;padding:14px 0 4px}
.runout-label{font-size:11px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}
.runout-cards{display:flex;gap:5px;flex-wrap:wrap}
.card{display:inline-flex;align-items:center;justify-content:center;gap:1px;min-width:30px;
height:40px;padding:0 5px;border-radius:7px;background:#fff;border:1px solid #cfd6e4;
box-shadow:0 1px 0 rgba(24,32,47,.06);font-weight:750;font-size:15px;color:#1b2333}
.card .suit{font-size:13px}
.card.red{color:#c53148;border-color:#f0c4cb;background:#fffafb}
.card.ghost{color:#aab1c0;background:#f8f9fc;border-style:dashed;min-width:26px}
.street-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}
.street-panel{background:#fbfcfe;border:1px solid var(--line);border-radius:14px;padding:12px}
.street-head{display:flex;justify-content:space-between;gap:8px;align-items:center;min-height:42px}
.street-head h3{margin:0;font-size:14px}
.street-head h3 small{font-weight:500;color:var(--muted);font-size:10px;text-transform:uppercase;
letter-spacing:.08em}
.mini-cards{display:flex;gap:3px}
.mini-cards .card{min-width:24px;height:30px;font-size:12px;padding:0 3px;border-radius:5px}
.mini-cards .card .suit{font-size:10px}
.eq-line{display:flex;align-items:baseline;gap:7px;margin-top:10px}
.eq-line strong{font-size:30px;letter-spacing:-.03em;line-height:1}
.eq-line span{font-size:10.5px;color:var(--muted)}
.good{color:var(--green)}.mid{color:var(--amber)}.bad{color:var(--red)}
.bar{height:7px;background:#edf0f6;border-radius:999px;overflow:hidden;margin:9px 0 4px}
.bar span{display:block;height:100%;border-radius:999px}
.bar .good{background:linear-gradient(90deg,#28b984,#0f9f6e)}
.bar .mid{background:linear-gradient(90deg,#e2b24d,#b7791f)}
.bar .bad{background:linear-gradient(90deg,#e66a79,#cf3f4f)}
.sub{font-size:10.5px;color:var(--muted)}
.multi-line{margin:2px 0 5px;font-size:11px;color:var(--muted)}
.multi-line b{font-weight:750;margin-left:4px}
dl{margin:10px 0 7px;display:grid;gap:4px;font-size:11.5px}
dl>div{display:flex;justify-content:space-between;gap:8px}
dt{color:var(--muted)}dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
dd small{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.chip{background:#f1f3f8;border:1px solid #e5e9f2;border-radius:999px;padding:2px 7px;
font-size:10px;color:#5f6879}
.chip.muted{background:#fafbfd;color:#9aa2b1}
.spark-wrap{display:flex;align-items:center;justify-content:space-between;gap:10px;
margin-top:12px;border-top:1px dashed var(--line);padding-top:10px}
.spark-title{font-size:10.5px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.spark{width:260px;max-width:70%;height:auto;overflow:visible}
.spark-line{fill:none;stroke:#3559d4;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.spark-mid{stroke:#dfe4ee;stroke-width:1;stroke-dasharray:3 4}
.spark-label{fill:#8a93a5;font-size:8px}
.dot.good{fill:#0f9f6e}.dot.mid{fill:#b7791f}.dot.bad{fill:#cf3f4f}
.actions{margin-top:10px;font-size:11.5px;color:var(--muted)}
.actions summary{cursor:pointer;color:#52607a;font-weight:600}
.actions div{margin-top:4px}
footer{padding:0 24px 40px;color:#8a93a5;font-size:11.5px;text-align:center}
@media(max-width:980px){.hand-grid{grid-template-columns:1fr}}
@media(max-width:720px){
  .hero{padding:38px 18px 36px}main{padding:20px 14px 50px}
  .method{margin-top:-18px}.panel{padding:18px}
  .street-grid{grid-template-columns:1fr}
  .spark-wrap{align-items:flex-start;flex-direction:column}
  .spark{max-width:100%}
}
@media print{
  body{background:#fff}.hero{background:#fff;color:#000;padding:20px 0}
  .hero p,.hero .kicker{color:#333}.panel,.hand-card,.table-wrap{box-shadow:none}
  .hand-card{break-inside:avoid}
}
"""


def build_html(hands: list[dict], metrics_by_id: dict[str, list[dict]],
               n_by_id: dict[str, int], source: Path) -> str:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    summary = "".join(
        summary_row(h, "".join(h.get("hero_cards") or []), n_by_id[h["hand_id"]],
                    metrics_by_id[h["hand_id"]])
        for h in hands
    )
    cards = "".join(
        hand_card(h, "".join(h.get("hero_cards") or []), n_by_id[h["hand_id"]],
                  metrics_by_id[h["hand_id"]])
        for h in hands
    )
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>历史手牌 · 牌面范围权益</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div class="kicker">Historical hand library · board-only ranges</div>
  <h1>牌面范围权益</h1>
  <p>从 <b>{esc(source.name)}</b> 挑选 {len(hands)} 手有完整 5 张公共牌的手牌，
  逐街展示 Hero 手牌对「牌面范围」的权益。主指标为单挑(N=1)，多路权益并列展示，
  避免成牌被多人底池摊薄后误判成弱牌。翻前范围不参与：范围只由公共牌生成。</p>
</div></header>
<main class="wrap">
  <section class="panel method">
    <h2>口径</h2>
    <p><b>牌面范围：</b>每个对手的范围 = 与当前公共牌连接度 ≥ 0.30 的 combo
    （<code>board_range</code> 的 call mode），不乘任何 RFI / BB 防守 / 冷跟先验。</p>
    <p><b>单挑权益（主指标）：</b>Hero 手牌对 1 个牌面范围的权益，用于判断
    这手牌本身强不强，与历史人数无关。</p>
    <p><b>多路权益（参考）：</b>Hero 对历史实际人数 N 个独立牌面范围的权益，
    Monte Carlo 发完剩余公共牌、平局按赢家数分池。N 家各自独立抽同一个牌面范围
    会把成牌摊薄，所以只作稀释参考，不单独当强弱判断。</p>
    <p><b>Behind now：</b>范围里当前成手牌严格大于 Hero 的加权比例，和「打到河牌的权益」不是同一个数。</p>
    <p><b>对手数：</b>只从历史行动里的非 hero 在局座位取人数（最多 4 家），
    不做翻前范围推断。N=1 表示单挑口径。</p>
    <p><b>0% 说明：</b>Monte Carlo 里 Hero 没有赢过任何一手；当 Behind now
    接近 100% 时这是正常结果，不是计算失败。</p>
  </section>

  <div class="section-title"><h2>总览</h2><span>权益按街对比（单挑口径）；「河牌 N路」列示历史人数稀释后的权益</span></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>手牌</th><th>Hero</th><th>位置</th><th>公共牌</th><th>对手</th>
      <th>翻牌(单挑)</th><th>转牌(单挑)</th><th>河牌(单挑)</th><th>河牌 N路</th><th>河牌范围宽度</th><th>河牌读牌</th></tr></thead>
      <tbody>{summary}</tbody>
    </table>
  </div>

  <div class="section-title"><h2>逐手明细</h2><span>翻牌 → 转牌 → 河牌</span></div>
  <div class="hand-grid">{cards}</div>
</main>
<footer>生成时间 {esc(generated)} · 数据源 {esc(source.name)} · 仅使用公共牌范围，不使用翻前范围</footer>
</body>
</html>
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hands", type=Path, default=DEFAULT_HANDS,
                    help="历史手牌 JSON（默认 hands/1023.hands.json）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="输出 HTML（默认 reports/historical-board-range-equity.html）")
    ap.add_argument("--ids", default=",".join(DEFAULT_IDS),
                    help="逗号分隔的 hand_id；默认 9 手完整河牌手牌")
    args = ap.parse_args()

    data = json.loads(args.hands.read_text(encoding="utf-8"))
    by_id = {h.get("hand_id"): h for h in data if isinstance(h, dict)}
    selected: list[dict] = []
    for hid in [x.strip() for x in args.ids.split(",") if x.strip()]:
        hand = by_id.get(hid)
        if hand is None:
            print(f"跳过 {hid}: 手牌库中没有", file=sys.stderr)
            continue
        hero = "".join(hand.get("hero_cards") or [])
        board = list(hand.get("board") or [])
        if len(hero) != 4 or len(board) != 5:
            print(f"跳过 {hid}: 不是完整 5 张公共牌（hero={hero!r}, board={board}）",
                  file=sys.stderr)
            continue
        selected.append(hand)

    if not selected:
        print("没有可生成的手牌", file=sys.stderr)
        return 1

    metrics_by_id: dict[str, list[dict]] = {}
    n_by_id: dict[str, int] = {}
    for hand in selected:
        hid = hand["hand_id"]
        hero = "".join(hand.get("hero_cards") or [])
        board = list(hand.get("board") or [])
        n_opp = opponent_count(hand)
        n_by_id[hid] = n_opp
        metrics_by_id[hid] = street_metrics(hand, hero, board, n_opp, hid)
        line = " ".join(
            f'{m["label_cn"]}={m["equity_1"]:.0%}单挑/{m["equity"]:.0%}多路'
            for m in metrics_by_id[hid]
        )
        print(f"{hid}: hero={hero} board={''.join(board)} opp={n_opp} {line}")

    text = build_html(selected, metrics_by_id, n_by_id, args.hands)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
