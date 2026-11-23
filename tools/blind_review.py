#!/usr/bin/env python3
"""盲评 CLI：按街分层抽 advice 决策点，先盲卡片、后揭示，幂等续评。

用法：

    nv tools/blind_review.py                        # 默认读 logs/live.states.jsonl
    nv tools/blind_review.py logs/live.states.jsonl logs/old.states.jsonl
    nv tools/blind_review.py -n 50 --seed 7 -o logs/blind_review.jsonl
    nv tools/blind_review.py --summary-only          # 只打印已有结果的分街汇总

口径：

  · 只读 `type == "advice"` 的行；一行坏 JSON 跳过并汇总提示，不中断。
  · 每条 advice 决策点按**决策键**去重。历史日志没有 `decision_key`，这里从
    既有字段退化派生一个稳定键（见 `derive_key`）：
        - 有 `decision_key`           → `dk:<键按 | 拼起来>`
        - 有 `action_index`           → `<hand_id>|<street>|a<n>`
        - 否则                        → `<hand_id>|<street>|<手牌>|<公共牌>|<需跟BB>`
    同一键出现多次（历史日志里同街同牌同需跟的两次决策）按读取顺序加
    `#2`、`#3` 后缀消歧——顺序稳定，键就稳定。输入文件的顺序因此也是身份
    的一部分：别随意重排日志文件再续评。
  · 抽样按街分层：每条街先保底 1 点，余下名额按各街点数比例分配（受该街
    实际条数上限约束）。同一 seed + 同一输入 ⇒ 同一批点；换 seed 会换点。
    不足 50 点时按实际数量抽，并在输出里说明。
  · 盲卡片只给事实与给模型的 `options`（白名单字段），不给程序 `option`、
    `reason`、`plan`、书中计划、偏移、上一街建议；提交后才揭示。位置字段
    旧日志不存，退化从 `prompt_user` 的「## 当前局面 → 位置：」行取（该段
    只放事实，不放建议）。
  · 结果按决策键幂等写 JSONL：重跑跳过已评点，支持断点续评；重复提交同一
    批点不会产生重复记录。
  · 工具不调模型、不连数据库、不做 Web UI。一致率只说明「与程序当时的
    判断差多少」，不证明盈利，也不证明程序更优。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_INPUT = Path("logs/live.states.jsonl")
DEFAULT_RESULTS = Path("logs/blind_review.jsonl")
DEFAULT_SAMPLE = 50
DEFAULT_SEED = 0

# 街的固定顺序：抽样分配与汇总都按它排，保证输出稳定。
STREET_ORDER = ("preflop", "flop", "turn", "river")
UNKNOWN_STREET = "unknown"


def _street_of(record: dict) -> str:
    street = str(record.get("street") or "").strip().lower()
    return street or UNKNOWN_STREET


def _fmt_bb(value: float) -> str:
    return f"{round(float(value), 2):g}"


def _join_cards(cards) -> str:
    if not isinstance(cards, (list, tuple)):
        return ""
    return "".join(str(card) for card in cards)


def derive_key(record: dict, occurrence: int = 1) -> str:
    """派生稳定决策键。

    `occurrence` 是同一基础键在输入里的第几次出现（1 起）：历史日志里同一
    街、同一手牌、同一公共牌、同一需跟额可能对应两次决策（例如过牌后对手
    下注又轮到我），没有 `action_index` 时只能靠出现顺序消歧。
    """
    explicit = record.get("decision_key")
    if explicit not in (None, "", [], ()):
        if isinstance(explicit, (list, tuple)):
            base = "dk:" + "|".join(str(part) for part in explicit)
        else:
            base = f"dk:{explicit}"
    else:
        hand = str(record.get("hand_id") or "").strip() or "?"
        street = _street_of(record)
        action_index = record.get("action_index")
        if action_index is not None:
            base = f"{hand}|{street}|a{action_index}"
        else:
            hero = _join_cards(record.get("hero_cards")) or "-"
            board = _join_cards(record.get("board")) or "-"
            try:
                to_call = _fmt_bb(record.get("to_call") or 0.0)
            except (TypeError, ValueError):
                to_call = "?"
            base = f"{hand}|{street}|{hero}|{board}|{to_call}"
    if occurrence > 1:
        return f"{base}#{occurrence}"
    return base


@dataclass(frozen=True)
class Point:
    """一个待评决策点：稳定键 + 原始 advice 记录（只读，不改形状）。"""

    key: str
    record: dict


@dataclass(frozen=True)
class Sample:
    """一次抽样的结果：抽到的点、名额分配、以及「不足 n」的说明。"""

    points: tuple[Point, ...] = ()
    requested: int = DEFAULT_SAMPLE
    available: int = 0
    by_street: dict = field(default_factory=dict)
    note: str = ""

    @property
    def short(self) -> bool:
        return self.available < self.requested


def _ordered_streets(counts: dict) -> list[str]:
    """已知街按固定顺序在前，未知/新街按名字排在最后。"""
    known = [s for s in STREET_ORDER if counts.get(s, 0) > 0]
    rest = sorted(s for s in counts if counts.get(s, 0) > 0 and s not in STREET_ORDER)
    return known + rest


def _allocate(counts: dict, n: int) -> tuple[dict, bool]:
    """分层名额：每街保底 1，余下按各街点数比例分（受实际上限约束）。

    返回 `(名额, 是否够分)`；`是否够分` 为 False 表示街数多于样本数。
    """
    present = _ordered_streets(counts)
    total = sum(counts[s] for s in present)
    if total <= n:
        return {s: counts[s] for s in present}, True
    if len(present) > n:
        # 极端退化：街比样本还多，只能按固定顺序每街 1 点。
        return {s: 1 for s in present[:n]}, False

    quota = {s: 1 for s in present}
    remaining = n - len(present)
    weights = {s: counts[s] for s in present}
    total_weight = sum(weights.values()) or 1
    exact = {s: remaining * weights[s] / total_weight for s in present}
    for s in present:
        quota[s] = min(counts[s], quota[s] + int(exact[s]))
    left = n - sum(quota.values())
    # 先按小数余数补（余数相同按街顺序），再按街顺序把剩余名额塞进还有空位的街。
    by_remainder = sorted(present,
                          key=lambda s: (-(exact[s] - int(exact[s])),
                                         present.index(s)))
    for s in by_remainder:
        if left <= 0:
            break
        if quota[s] < counts[s]:
            quota[s] += 1
            left -= 1
    for s in present:
        if left <= 0:
            break
        add = min(left, counts[s] - quota[s])
        quota[s] += add
        left -= add
    return quota, True


def sample(points, *, n: int = DEFAULT_SAMPLE,
           seed: int = DEFAULT_SEED) -> Sample:
    """按街分层抽 `n` 个点；同 seed + 同输入结果完全一致。

    每条街单独用一个由 `(seed, 街名)` 派生的 RNG，这样某一街的日志变多不会
    打乱其他街的抽样；抽中的点再按键排序，输出顺序也稳定。
    """
    groups: dict[str, list[Point]] = {}
    for item in points:
        groups.setdefault(_street_of(item.record), []).append(item)
    for group in groups.values():
        group.sort(key=lambda p: p.key)
    present = _ordered_streets({s: len(g) for s, g in groups.items()})
    total = len(points)
    if total <= n or not present:
        picked = [p for s in present for p in groups[s]]
        note = ""
        if total < n:
            note = (f"输入只有 {total} 个决策点，不足要求的 {n} 个，"
                    f"按实际 {total} 个抽样。")
        return Sample(points=tuple(picked), requested=n, available=total,
                      by_street={s: len(groups[s]) for s in present}, note=note)

    quota, enough = _allocate({s: len(groups[s]) for s in present}, n)
    picked = []
    for street in present:
        take = quota.get(street, 0)
        if take <= 0:
            continue
        rng = random.Random(f"blind-review:{seed}:{street}")
        picked.extend(sorted(rng.sample(groups[street], take),
                             key=lambda p: p.key))
    note = ""
    if not enough:
        note = (f"输入里有 {len(present)} 条街，多于样本数 {n}，"
                f"只按固定顺序各取 1 点（{ '、'.join(present[:n]) }）。")
    return Sample(points=tuple(picked), requested=n, available=total,
                  by_street={s: quota.get(s, 0) for s in present}, note=note)


def assign_points(records: list[dict]) -> list[Point]:
    """给每条 advice 记录配稳定键；同键按出现顺序加 `#n` 后缀。"""
    seen: dict[str, int] = {}
    points: list[Point] = []
    for record in records:
        base = derive_key(record)
        seen[base] = seen.get(base, 0) + 1
        points.append(Point(key=derive_key(record, seen[base]), record=record))
    return points


def load(paths) -> tuple[list[dict], list[tuple[str, int, str]]]:
    """读一个或多个 JSONL，返回 (advice 记录, 坏行)。

    坏行 = `(文件, 行号, 原因)`；非 advice 的行（hand/session_start 等）静默
    跳过——同一个日志文件里本来就混着这些记录。
    """
    records: list[dict] = []
    bad_lines: list[tuple[str, int, str]] = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            bad_lines.append((str(path), 0, "文件不存在或读不到"))
            continue
        with path.open(encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    bad_lines.append((str(path), lineno, f"JSON 解析失败：{exc.msg}"))
                    continue
                if not isinstance(record, dict):
                    bad_lines.append((str(path), lineno, "不是 JSON 对象"))
                    continue
                if record.get("type") == "advice":
                    records.append(record)
    return records, bad_lines


# ── 盲卡片：只给事实与给模型的 options ──────────────────────────────────

MISSING = "（日志没记）"


def _facts(record: dict) -> dict:
    facts = record.get("facts")
    return facts if isinstance(facts, dict) else {}


def _cards(cards) -> list[str]:
    if not isinstance(cards, (list, tuple)):
        return []
    return [str(card) for card in cards if str(card).strip()]


def _number(*values) -> float | None:
    """取第一个能转成数字的值；缺数据 → None（fail open，不猜）。"""
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            continue
    return None


def _integer(*values) -> int | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _text(*values) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _as_text(*values) -> str:
    """把提示词里可能出现的结构（字符串/列表/字典）压成可读文本。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            return "；".join(str(item) for item in value)
        if isinstance(value, dict) and value:
            return json.dumps(value, ensure_ascii=False)
    return ""


def _prompt_section(prompt, prefix: str) -> str:
    """从 `prompt_user` 里只取指定 `## ` 段落（白名单，绝不整段回显）。"""
    if not isinstance(prompt, str):
        return ""
    out: list[str] = []
    active = False
    for line in prompt.splitlines():
        if line.startswith("## "):
            active = line.startswith(prefix)
            continue
        if active and line.strip():
            out.append(line.strip())
    return "\n".join(out)


def _options(record: dict, facts: dict) -> list[dict]:
    """模型当时看到的选项空间：优先 `facts["options"]`（实盘唯一存处）。"""
    raw = facts.get("options")
    if not isinstance(raw, list) or not raw:
        raw = record.get("options")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _position(record: dict, facts: dict) -> str:
    """位置：优先结构化字段，退而取 `当前局面` 段的「位置：」行（纯事实段）。"""
    direct = _text(record.get("hero_position"),
                   facts.get("hero_position"), facts.get("position"))
    if direct:
        return direct
    section = _prompt_section(record.get("prompt_user"), "## 当前局面")
    for line in section.splitlines():
        if line.startswith("位置："):
            # 「位置：…｜过牌后待行动…」——只取第一个事实分句，别把整段塞进卡片。
            return line[len("位置："):].split("｜")[0].strip()
    return ""


def _action_line(record: dict, facts: dict) -> str:
    for raw in (record.get("action_line"), facts.get("action_line")):
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, (list, tuple)) and raw:
            return " → ".join(str(item) for item in raw)
    # advice 记录本身不存行动线，但捕获了 prompt_user 时「## 行动线」段在。
    return _prompt_section(record.get("prompt_user"), "## 行动线")


def _quality(record: dict, facts: dict, card: dict) -> list[str]:
    """数据质量：技术告警 + 由缺字段推出的可读缺口（去重保序）。"""
    notes: list[str] = []
    for warning in facts.get("warnings") or []:
        text = str(warning).strip()
        if text:
            notes.append(text)
    if not facts:
        notes.append("日志没有事实快照（旧记录），只能看基础字段")
    if not card["hero_cards"]:
        notes.append("手牌未记录")
    if not card["board"] and card["street"] != "preflop":
        notes.append("公共牌未记录")
    if card["hero_stack"] is None:
        notes.append("后手未记录")
    if not card["options"]:
        notes.append("这条记录没有给模型的 options（旧日志）")
    if record.get("source") == "fallback":
        notes.append("这条建议是兜底结果（source=fallback）")
    if record.get("error"):
        notes.append(f"模型调用出错：{record['error']}")
    if record.get("prompt_missing"):
        notes.append("提示词未捕获（prompt_missing）")
    return list(dict.fromkeys(notes))


def blind_card(point: Point) -> dict:
    """盲评卡（提交前）：**白名单**取事实，模型结论一个都不带。

    只含手牌、公共牌、底池、需跟、后手、位置、在局对手数、行动线、数据质量
    和给模型的 `options`。程序 `option`、`reason`、`plan`、书中计划、偏移、
    上一街建议一律不进卡片——它们只在 `reveal()` 里出现。
    """
    record = point.record
    facts = _facts(record)
    card = {
        "decision_key": point.key,
        "hand_id": _text(record.get("hand_id")),
        "street": _street_of(record),
        "hero_cards": _cards(record.get("hero_cards")),
        "board": _cards(record.get("board")),
        "pot": _number(record.get("pot"), facts.get("pot")),
        "to_call": _number(record.get("to_call"), facts.get("to_call")),
        "hero_stack": _number(facts.get("hero_stack"),
                              record.get("hero_stack"),
                              facts.get("hero_total_stack")),
        "position": _position(record, facts),
        "opponents": _integer(facts.get("opponents_in_hand")),
        "action_line": _action_line(record, facts),
        "options": _options(record, facts),
    }
    card["quality"] = _quality(record, facts, card)
    return card


def _option_brief(option: dict) -> str:
    if not isinstance(option, dict):
        return ""
    oid = _text(option.get("id"))
    label = _text(option.get("label"))
    if oid and label:
        return f"{oid}（{label}）"
    if oid:
        amount = _number(option.get("amount_bb"))
        return f"{oid}（{_fmt_bb(amount)}BB）" if amount is not None else oid
    return label


def reveal(point: Point) -> dict:
    """提交后才能看的东西：程序选择、理由、书中计划、偏移、模型计划。

    书中计划/偏移在实盘记录里只活在 `prompt_user` 的对应段落（facts 不存），
    所以先取提示词原文段、再退回结构字段；两处都没有就空着（不编）。
    """
    record = point.record
    facts = _facts(record)
    chosen = record.get("option") if isinstance(record.get("option"), dict) else {}
    prompt = record.get("prompt_user")
    return {
        "model_option": _option_brief(chosen),
        "model_option_id": _text(chosen.get("id")),
        "model_reason": _text(record.get("reason")),
        "model_plan": _text(record.get("plan")),
        "book_plan": (_prompt_section(prompt, "## 书中计划")
                      or _as_text(facts.get("book_plan"), facts.get("playbook"))),
        "offsets": (_prompt_section(prompt, "## 偏移")
                    or _as_text(facts.get("offsets"))),
        "source": _text(record.get("source")),
        "prompt_version": _text(record.get("prompt_version")),
    }


def _num_text(value) -> str:
    return MISSING if value is None else f"{_fmt_bb(value)}BB"


def _options_text(options: list[dict]) -> str:
    parts = [_option_brief(option) for option in options]
    parts = [part for part in parts if part]
    return "｜".join(parts) or MISSING


def render_card(card: dict, revealed: dict | None = None) -> str:
    """渲染盲评卡。`revealed` 为空 = 提交前（只有事实与 options）。"""
    street = card.get("street") or UNKNOWN_STREET
    board = " ".join(card.get("board") or [])
    if not board:
        board = "（翻前无公共牌）" if street == "preflop" else MISSING
    lines = [
        f"盲评卡 {card.get('decision_key') or '?'}"
        f"（{card.get('hand_id') or '?'} · {street}）",
        f"手牌：{' '.join(card.get('hero_cards') or []) or MISSING}",
        f"公共牌：{board}",
        f"底池：{_num_text(card.get('pot'))}    需跟：{_num_text(card.get('to_call'))}",
        f"后手：{_num_text(card.get('hero_stack'))}"
        f"    位置：{card.get('position') or MISSING}"
        f"    在局对手：{card.get('opponents') if card.get('opponents') is not None else MISSING}",
        f"行动线：{card.get('action_line') or MISSING}",
        f"数据质量：{'；'.join(card.get('quality') or []) or '（没有告警）'}",
        f"选项：{_options_text(card.get('options') or [])}",
    ]
    if revealed is None:
        return "\n".join(lines)
    lines.extend(["", "—— 揭示 ——",
                  f"程序选择：{revealed.get('model_option') or MISSING}",
                  f"理由：{revealed.get('model_reason') or MISSING}"])
    if revealed.get("book_plan"):
        lines.append(f"书中计划：{revealed['book_plan']}")
    if revealed.get("offsets"):
        lines.append(f"偏移：{revealed['offsets']}")
    if revealed.get("model_plan"):
        lines.append(f"模型计划：{revealed['model_plan']}")
    if revealed.get("source"):
        lines.append(f"来源：{revealed['source']}")
    return "\n".join(lines)

# ── 结果记录：按决策键幂等合并 ──────────────────────────────────────────

RESULT_TYPE = "blind_review"


def review_row(point: Point, blind_option: str, *, confidence: int | None = None,
               note: str = "", ts: str | None = None) -> dict:
    """把一个盲评结果压成一行记录（纯函数，时间由调用方给）。"""
    record = point.record
    revealed = reveal(point)
    choice = _text(blind_option)
    model = revealed["model_option_id"]
    agree = None
    if choice and model:
        agree = choice == model
    row = {
        "type": RESULT_TYPE,
        "decision_key": point.key,
        "hand_id": _text(record.get("hand_id")),
        "street": _street_of(record),
        "blind_option": choice,
        "blind_confidence": confidence,
        "model_option": model,                 # 机器可比的选项 id
        "model_option_label": revealed["model_option"],
        "agree": agree,
        "note": _text(note),
        "prompt_version": revealed["prompt_version"],
    }
    if ts:
        row["ts"] = ts
    return row


def merge_result(rows, row: dict) -> list[dict]:
    """按决策键合并：同键替换原行，不同键追加；重交同一批不产生重复。"""
    key = _text(row.get("decision_key"))
    if not key:
        raise ValueError("盲评结果必须带 decision_key")
    merged = [dict(existing) for existing in (rows or [])]
    for index, existing in enumerate(merged):
        if _text(existing.get("decision_key")) == key:
            merged[index] = dict(row)
            return merged
    merged.append(dict(row))
    return merged


def index_results(rows) -> dict[str, dict]:
    """结果集按决策键建索引；同一键后写的赢（幂等合并的读侧）。"""
    index: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _text(row.get("decision_key"))
        if key:
            index[key] = row
    return index


def unreviewed(points, rows) -> list[Point]:
    """断点续评：只留还没评过的点，顺序与抽样顺序一致。"""
    done = index_results(rows)
    return [point for point in points if point.key not in done]


# ── 汇总：按街一致率与分歧清单 ────────────────────────────────────────

CAVEAT = ("一致率只说明你的判断和程序当时的选择有多接近，"
          "不能当作盈利证明——样本小，也没看结算结果。")


def summarize(sample: Sample, rows) -> dict:
    """把抽样与结果对起来：按街给抽样数、已评数、一致率、分歧清单。"""
    reviewed = index_results(rows)
    groups: dict[str, list[Point]] = {}
    for point in sample.points:
        groups.setdefault(_street_of(point.record), []).append(point)

    streets = []
    for street in _ordered_streets({s: len(g) for s, g in groups.items()}):
        points = groups[street]
        picked = [reviewed[p.key] for p in points if p.key in reviewed]
        agree = [row for row in picked if row.get("agree") is True]
        disagree = [row for row in picked if row.get("agree") is False]
        judged = len(agree) + len(disagree)
        streets.append({
            "street": street,
            "sampled": len(points),
            "reviewed": len(picked),
            "agree": len(agree),
            "disagree": len(disagree),
            "agreement": round(len(agree) / judged, 4) if judged else None,
            "disagreements": [
                {"decision_key": _text(row.get("decision_key")),
                 "blind_option": _text(row.get("blind_option")),
                 "model_option": _text(row.get("model_option"),
                                       row.get("model_option_id")),
                 "note": _text(row.get("note"))}
                for row in disagree
            ],
        })

    total_reviewed = sum(street["reviewed"] for street in streets)
    total_agree = sum(street["agree"] for street in streets)
    total_judged = sum(street["agree"] + street["disagree"] for street in streets)
    return {
        "sampled": len(sample.points),
        "available": sample.available,
        "requested": sample.requested,
        "reviewed": total_reviewed,
        "agree": total_agree,
        "judged": total_judged,
        "agreement": round(total_agree / total_judged, 4) if total_judged else None,
        "pending": len(sample.points) - total_reviewed,
        "streets": streets,
        "note": sample.note,
    }


def _ratio(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def render_summary(summary: dict) -> str:
    """人读的分街汇总；分歧逐条列出，并明说一致率不是盈利证明。"""
    lines = [f"盲评汇总：抽样 {summary['sampled']}（可抽 {summary['available']}），"
             f"已评 {summary['reviewed']}，待评 {summary['pending']}"]
    if summary.get("note"):
        lines.append(summary["note"])
    lines.append("")
    for street in summary["streets"]:
        lines.append(f"  {street['street']:<8} 抽样 {street['sampled']:>2}  "
                     f"已评 {street['reviewed']:>2}  一致 {street['agree']:>2}  "
                     f"一致率 {_ratio(street['agreement'])}")
    lines.append(f"  {'合计':<8} 抽样 {summary['sampled']:>2}  "
                 f"已评 {summary['reviewed']:>2}  一致 {summary['agree']:>2}  "
                 f"一致率 {_ratio(summary['agreement'])}")

    misses = [(street["street"], miss)
              for street in summary["streets"]
              for miss in street["disagreements"]]
    if misses:
        lines.extend(["", "分歧清单（你 vs 程序）："])
        for street, miss in misses:
            note = f"（{miss['note']}）" if miss["note"] else ""
            lines.append(f"  [{street}] {miss['decision_key']}："
                         f"你选 {miss['blind_option'] or '—'}，"
                         f"程序选 {miss['model_option'] or '—'}{note}")
    lines.extend(["", CAVEAT])
    return "\n".join(lines)


# ── 输入答案与文件读写（薄壳；逻辑都在上面的纯函数里） ─────────────────

def parse_answer(text: str, default_confidence: int = 2) -> tuple[str, int, str]:
    """一行输入 → `(选项 id, 置信度 1-3, 备注)`；置信度缺省 2。"""
    parts = (text or "").strip().split(maxsplit=2)
    if not parts:
        return ("", default_confidence, "")
    choice = parts[0]
    if len(parts) == 1:
        return (choice, default_confidence, "")
    try:
        confidence = int(parts[1])
    except ValueError:
        return (choice, default_confidence, " ".join(parts[1:]))
    if not 1 <= confidence <= 3:
        confidence = default_confidence
    return (choice, confidence, parts[2] if len(parts) > 2 else "")


def load_results(path) -> tuple[list[dict], list[tuple[str, int, str]]]:
    """读结果 JSONL；文件不存在就当空的，坏行汇总返回。"""
    path = Path(path)
    rows: list[dict] = []
    bad: list[tuple[str, int, str]] = []
    if not path.exists():
        return rows, bad
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                bad.append((str(path), lineno, f"不是合法 JSON：{exc.msg}"))
                continue
            if not isinstance(row, dict):
                bad.append((str(path), lineno, "不是 JSON 对象"))
                continue
            if not _text(row.get("decision_key")):
                bad.append((str(path), lineno, "缺 decision_key，无法幂等续评"))
                continue
            rows.append(row)
    return rows, bad


def save_results(path, rows) -> None:
    """整集重写（先写临时文件再替换），保证中断不会留下半截结果。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── CLI：只做参数解析与文件读写 ───────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blind_review.py",
        description="从实盘 advice 日志分层抽决策点做盲评，结果按决策键幂等落盘。")
    parser.add_argument("inputs", nargs="*", default=[DEFAULT_INPUT],
                        help=f"advice JSONL，可给多个（缺省 {DEFAULT_INPUT}）")
    parser.add_argument("-n", "--count", type=int, default=DEFAULT_SAMPLE,
                        help=f"抽样点数（缺省 {DEFAULT_SAMPLE}）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"抽样随机种子（缺省 {DEFAULT_SEED}）")
    parser.add_argument("-o", "--out", default=DEFAULT_RESULTS,
                        help=f"结果 JSONL（缺省 {DEFAULT_RESULTS}）")
    parser.add_argument("--summary-only", action="store_true",
                        help="不进入盲评，只打印已有结果的分街汇总")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records, bad = load(Path(path) for path in args.inputs)
    for path, lineno, reason in bad:
        where = f"{path}:{lineno}" if lineno else str(path)
        print(f"[跳过] {where} {reason}", file=sys.stderr)
    if not records:
        print("没有可用的 advice 记录。", file=sys.stderr)
        return 1

    picked = sample(assign_points(records), n=args.count, seed=args.seed)
    if picked.note:
        print(picked.note)
    rows, result_bad = load_results(args.out)
    for path, lineno, reason in result_bad:
        print(f"[跳过] {path}:{lineno} {reason}", file=sys.stderr)

    todo = unreviewed(picked.points, rows)
    if not args.summary_only:
        print(f"抽样 {len(picked.points)} 个：已评 {len(picked.points) - len(todo)} 个，"
              f"待评 {len(todo)} 个（结果写 {args.out}）")
        for point in todo:
            card = blind_card(point)
            print()
            print(render_card(card))
            try:
                answer = input("你的选择（选项 id，回车跳过，q 结束）：")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            choice, confidence, note = parse_answer(answer)
            if choice in {"q", "quit", "exit"}:
                break
            if not choice:
                continue
            rows = merge_result(rows, review_row(point, choice,
                                                 confidence=confidence, note=note,
                                                 ts=_now()))
            save_results(args.out, rows)
            print()
            print(render_card(card, reveal(point)))

    print()
    print(render_summary(summarize(picked, rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
