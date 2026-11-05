#!/usr/bin/env python3
"""倒查实时日志：哪些手牌的识别可能有问题。

为什么需要它：
  `live.states.jsonl` 里每手牌都带 `data_quality` / `cards_warnings`，事件
  日志里每一帧还带 `hand_id` 和丢弃原因。但人不可能手翻几十万行 JSONL 去
  找「哪几手牌读错了」。这个工具把两类日志对齐到手牌粒度，按可疑程度打分，
  再用 `--explain` 展开某一手的逐帧时间线。

用法：

    # 默认读 logs/live.states.jsonl + 同目录 live.events.jsonl
    .venv/bin/python tools/hand_recognition_audit.py

    # 只看前 10 名，并生成可点击的 HTML 报告
    .venv/bin/python tools/hand_recognition_audit.py --top 10 --html logs/audit.html

    # 展开某一手：逐帧 kind / errors / cards warnings / 建议兜底 / 疑惑帧目录
    .venv/bin/python tools/hand_recognition_audit.py --explain 20261105-abcdef1234

    # 导出 JSON / CSV 给别的脚本
    .venv/bin/python tools/hand_recognition_audit.py --json logs/audit.json
    .venv/bin/python tools/hand_recognition_audit.py --csv logs/audit.csv

评分口径（0–100，越高越可疑）：

| 信号 | 分值 |
|---|---|
| 手牌截断（partial） | 10 |
| 牌面近似（approx / cards_unreliable） | 6–8 |
| `cards_warnings` 每条（去重，封顶） | 2 |
| 推断动作每条（封顶） | 1.5 |
| hero 手牌张数不是 2 | 8–10 |
| hero+board 重复牌 | 12 |
| 公共牌张数不合法 | 6 |
| 事件日志里丢弃帧占比 | 4–12 |
| 疑惑帧目录每份（封顶） | 5 |
| 建议兜底每条（封顶） | 3 |
| 建议未调用模型每条（封顶） | 2 |
| 名字告警每条（封顶） | 1 |
| 有手牌但完全没有行动线 | 3 |

分数只用于**排序/分流**，不是「识别正确率」。真正的证据在
`--explain` 的逐帧记录和 `logs/incidents/` 的截图里。
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_STATES = Path("logs/live.states.jsonl")
DEFAULT_EVENTS = Path("logs/live.events.jsonl")
DEFAULT_INCIDENTS = Path("logs/incidents")

LEGAL_BOARD_LENGTHS = (0, 3, 4, 5)


# ─────────────────────────── 数据结构 ───────────────────────────

@dataclass
class Issue:
    code: str
    label: str
    points: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "label": self.label,
                "points": round(self.points, 1), "detail": self.detail}


@dataclass
class HandAudit:
    uid: str
    hand_id: str
    session: int
    ts: str
    partial: bool
    hero_cards: list[str]
    board: list[str]
    pot: float | None
    hero_position: str | None
    verdict: str
    warnings: list[str]
    inferred_actions: int
    action_count: int
    advice_count: int
    fallback_count: int
    prompt_missing_count: int
    advice_skips: int
    frame_count: int
    accepted_frames: int
    discard_count: int
    stale_count: int
    discard_reasons: dict[str, int]
    incidents: list[str]
    name_warnings: int
    issues: list[Issue] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    score: float = 0.0
    severity: str = "clean"

    @property
    def discard_rate(self) -> float:
        return self.discard_count / self.frame_count if self.frame_count else 0.0

    def to_dict(self) -> dict:
        return {
            **{k: getattr(self, k) for k in (
                "uid", "hand_id", "session", "ts", "partial", "hero_cards",
                "board", "pot", "hero_position", "verdict", "warnings",
                "inferred_actions", "action_count", "advice_count",
                "fallback_count", "prompt_missing_count", "advice_skips",
                "frame_count",
                "accepted_frames", "discard_count", "stale_count",
                "discard_reasons", "incidents", "name_warnings", "score",
                "severity")},
            "discard_rate": round(self.discard_rate, 3),
            "issues": [i.to_dict() for i in self.issues],
            "frames": self.frames,
        }


@dataclass
class AuditReport:
    sessions: int
    hands: list[HandAudit]
    bad_lines: list[dict]
    state_files: list[str]
    event_files: list[str]
    incident_files: int

    @property
    def flagged(self) -> list[HandAudit]:
        return [h for h in self.hands if h.score > 0]

    def summary(self) -> dict:
        severity = Counter(h.severity for h in self.hands)
        return {
            "sessions": self.sessions,
            "hands": len(self.hands),
            "flagged": len(self.flagged),
            "severity": dict(severity),
            "bad_lines": len(self.bad_lines),
            "state_files": self.state_files,
            "event_files": self.event_files,
            "incident_files": self.incident_files,
        }


# ─────────────────────────── 读日志 ───────────────────────────

def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict | None, str]]:
    """逐行读 JSONL；坏行不抛异常，返回 (行号, None, 原因)。"""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        yield 0, None, f"打不开：{exc}"
        return
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                yield lineno, None, f"坏行：{exc.msg}"
                continue
            if isinstance(obj, dict):
                yield lineno, obj, ""
            else:
                yield lineno, None, "不是 JSON 对象"


def _iter_events(path: Path) -> Iterable[tuple[int, dict | None, str]]:
    for lineno, obj, err in _iter_jsonl(path):
        if obj is None:
            yield lineno, None, err
            continue
        if obj.get("type") != "event":
            continue
        event = obj.get("event")
        data = obj.get("data")
        if not isinstance(event, str) or not isinstance(data, dict):
            continue
        yield lineno, {"event": event, "data": data,
                       "session_id": obj.get("session_id")}, ""


def _hand_id(hand: dict) -> str:
    return str(hand.get("hand_id") or hand.get("uid") or "").strip()


def _cards(warnings: Iterable[str]) -> list[str]:
    out: list[str] = []
    for w in warnings:
        text = str(w).strip()
        if text and text not in out:
            out.append(text)
    return out


def _count_inferred(hand: dict) -> int:
    return sum(1 for street in hand.get("streets") or []
               for action in street.get("actions") or []
               if action.get("inferred"))


def _count_actions(hand: dict) -> int:
    return sum(len(street.get("actions") or ())
               for street in hand.get("streets") or [])


def _duplicate_cards(hero: Iterable[str], board: Iterable[str]) -> list[str]:
    cards = list(hero) + list(board)
    seen: set[str] = set()
    dup: list[str] = []
    for card in cards:
        if card in seen and card not in dup:
            dup.append(card)
        seen.add(card)
    return dup


def _parse_time(text: str | None) -> str:
    """只用于排序/显示；解析失败就原样保留。"""
    if not text:
        return ""
    return str(text)


# ─────────────────────────── 对齐 ───────────────────────────

def collect(states_paths: list[Path], event_paths: list[Path],
            incidents_dir: Path | None = None) -> AuditReport:
    hands_raw: list[tuple[int, dict]] = []
    advices_raw: dict[tuple[int, str], list[dict]] = defaultdict(list)
    states_raw: dict[tuple[int, str], list[dict]] = defaultdict(list)
    session_meta: dict[int, dict] = {}
    session_ids: dict[str, int] = {}
    current_session = 0
    pending_states: list[dict] = []
    bad_lines: list[dict] = []
    state_files: list[str] = []

    # ── states：手牌 / 建议 / 属于这一手的 state 帧 ──
    for path in states_paths:
        if not path.exists():
            bad_lines.append({"file": str(path), "line": 0,
                              "error": "文件不存在"})
            continue
        state_files.append(str(path))
        for lineno, obj, err in _iter_jsonl(path):
            if obj is None:
                if err:
                    bad_lines.append({"file": str(path), "line": lineno,
                                      "error": err})
                continue
            kind = obj.get("type")
            if kind == "session_start":
                current_session += 1
                session_meta[current_session] = obj
                sid = obj.get("session_id")
                if sid:
                    session_ids[str(sid)] = current_session
                pending_states = []
            elif kind == "state":
                pending_states.append(obj)
            elif kind == "advice":
                hid = str(obj.get("hand_id") or "")
                if hid:
                    advices_raw[(current_session, hid)].append(obj)
            elif kind == "hand":
                hid = _hand_id(obj)
                if not hid:
                    continue
                key = (current_session, hid)
                states_raw[key].extend(pending_states)
                pending_states = []
                hands_raw.append((current_session, obj))

    # ── events：帧 / 疑惑帧（按 hand_id 对齐） ──
    frames_by_hand: dict[tuple[int, str], list[dict]] = defaultdict(list)
    incidents_by_hand: dict[tuple[int, str], list[str]] = defaultdict(list)
    event_hands: dict[tuple[int, str], dict] = {}
    event_advices: dict[tuple[int, str], list[dict]] = defaultdict(list)
    advice_skips: dict[tuple[int, str], int] = defaultdict(int)
    event_files: list[str] = []
    incident_files = 0

    for path in event_paths:
        if not path.exists():
            continue
        event_files.append(str(path))
        for lineno, obj, err in _iter_events(path):
            if obj is None:
                if err:
                    bad_lines.append({"file": str(path), "line": lineno,
                                      "error": err})
                continue
            sid = str(obj.get("session_id") or "")
            session = session_ids.get(sid)
            if session is None:
                if sid:
                    current_session += 1
                    session = current_session
                    session_ids[sid] = session
                    session_meta.setdefault(session, {"session_id": sid})
                else:
                    session = current_session
            event = obj["event"]
            data = obj["data"]
            if event == "session_start":
                session_meta.setdefault(session, data)
            elif event == "frame":
                hid = str(data.get("hand_id") or "")
                if hid:
                    frames_by_hand[(session, hid)].append(data)
            elif event == "incident":
                hid = str(data.get("hand_id") or "")
                folder = data.get("folder")
                if hid and folder:
                    incidents_by_hand[(session, hid)].append(str(folder))
                if folder:
                    incident_files += 1
            elif event == "hand":
                hid = str(data.get("hand_id") or "")
                if hid:
                    event_hands.setdefault((session, hid), data)
            elif event == "advice":
                hid = str(data.get("hand_id") or "")
                if hid:
                    event_advices[(session, hid)].append(data)
            elif event == "advice_skip":
                hid = str(data.get("hand_id") or "")
                if hid:
                    advice_skips[(session, hid)] += 1

    # 没有 states hand 记录时（只给了事件日志），用事件里的 hand 兜底。
    if not hands_raw:
        hands_raw = [(session, data) for (session, _hid), data
                     in event_hands.items()]

    hands: list[HandAudit] = []
    seen: set[tuple[int, str]] = set()
    for session, hand in hands_raw:
        hid = _hand_id(hand)
        key = (session, hid)
        if not hid or key in seen:
            continue
        seen.add(key)
        quality = hand.get("data_quality") or {}
        warnings = _cards(quality.get("warnings")
                          or hand.get("cards_warnings") or [])
        inferred = int(quality.get("inferred_actions")
                       or _count_inferred(hand))
        advices = list(hand.get("advices") or ())
        if not advices:
            advices = advices_raw.get(key) or event_advices.get(key) or []
        fallback = sum(1 for a in advices
                       if a.get("source") not in (None, "model"))
        prompt_missing = sum(1 for a in advices if a.get("prompt_missing"))
        hero = [str(c) for c in (hand.get("hero_cards") or [])]
        board = [str(c) for c in (hand.get("board") or [])]
        frames = frames_by_hand.get(key, [])
        states = states_raw.get(key, [])

        discard_reasons: Counter = Counter()
        stale = 0
        accepted = 0
        for frame in frames:
            kind = str(frame.get("kind") or "")
            if kind == "discard":
                for error in frame.get("errors") or ():
                    discard_reasons[str(error).split(":", 1)[0] or "unknown"] += 1
            elif kind == "stale":
                stale += 1
            else:
                accepted += 1
        frame_count = len(frames)
        discard_count = sum(1 for f in frames if f.get("kind") == "discard")
        if frame_count == 0:
            # 没有事件日志时，用 states 帧数当分母；丢弃数未知。
            frame_count = len(states)
            accepted = len(states)

        name_warnings = sum(len(s.get("name_warnings") or ())
                            for s in states)
        incidents = incidents_by_hand.get(key, [])
        verdict = ("partial" if hand.get("partial") else
                   str(quality.get("status") or
                       ("approx" if hand.get("cards_unreliable") else
                        ("exact" if hand.get("hero_cards") else "unknown"))))

        h = HandAudit(
            uid=str(hand.get("uid") or f"s{session:02d}-{hid}"),
            hand_id=hid, session=session,
            ts=_parse_time(hand.get("ts")), partial=bool(hand.get("partial")),
            hero_cards=hero, board=board, pot=hand.get("pot"),
            hero_position=hand.get("hero_position"), verdict=verdict,
            warnings=warnings, inferred_actions=inferred,
            action_count=_count_actions(hand), advice_count=len(advices),
            fallback_count=fallback, prompt_missing_count=prompt_missing,
            advice_skips=advice_skips.get(key, 0),
            frame_count=frame_count, accepted_frames=accepted,
            discard_count=discard_count, stale_count=stale,
            discard_reasons=dict(discard_reasons), incidents=incidents,
            name_warnings=name_warnings,
            frames=[_compact_frame(f) for f in frames[:240]],
        )
        _score(h)
        hands.append(h)

    hands.sort(key=lambda h: (h.ts or "", h.hand_id))

    if incidents_dir is not None and incidents_dir.exists():
        incident_files = max(incident_files,
                             sum(1 for p in incidents_dir.iterdir()
                                 if p.is_dir() and (p / "meta.json").exists()))

    return AuditReport(sessions=max(current_session, 1) if hands else 0,
                       hands=hands, bad_lines=bad_lines,
                       state_files=state_files, event_files=event_files,
                       incident_files=incident_files)


def _compact_frame(frame: dict) -> dict:
    cards = frame.get("cards") or {}
    return {
        "seq": frame.get("seq"),
        "frame_id": frame.get("frame_id"),
        "t": frame.get("t"),
        "kind": frame.get("kind"),
        "proc_ms": frame.get("proc_ms"),
        "errors": list(frame.get("errors") or ()),
        "cards_reliable": cards.get("reliable"),
        "cards_warnings": list(cards.get("warnings") or ()),
    }


def _score(hand: HandAudit) -> None:
    issues: list[Issue] = []

    def add(code: str, label: str, points: float, detail: str = "") -> None:
        issues.append(Issue(code, label, points, detail))

    if hand.partial or hand.verdict == "partial":
        add("partial", "手牌截断（退出时未换手）", 10)
    elif hand.verdict == "approx":
        add("approx", "牌面近似（cards_unreliable）", 6)
    elif hand.verdict == "unknown":
        add("unknown", "手牌记录缺 data_quality", 2)

    warnings = _cards(hand.warnings)
    if warnings:
        add("cards_warnings", f"牌面警告 {len(warnings)} 条", min(12, 2 * len(warnings)),
            "；".join(warnings[:4]))

    if hand.inferred_actions:
        add("inferred_actions", f"推断动作 {hand.inferred_actions} 条",
            min(10, 1.5 * hand.inferred_actions))

    n_hero = len(hand.hero_cards)
    if n_hero == 0:
        add("no_hero", "没读到自己手牌", 10)
    elif n_hero == 1:
        add("one_hero", "只读到 1 张手牌", 8)
    elif n_hero > 2:
        add("hero_count", f"手牌张数异常（{n_hero}）", 10)

    board_n = len(hand.board)
    if board_n not in LEGAL_BOARD_LENGTHS:
        add("board_count", f"公共牌张数异常（{board_n}）", 6)

    dup = _duplicate_cards(hand.hero_cards, hand.board)
    if dup:
        add("duplicate_cards", f"重复牌：{'/'.join(dup)}", 12)

    if hand.frame_count >= 3:
        rate = hand.discard_rate
        if rate >= 0.7:
            add("discard_rate", f"丢弃帧 {rate * 100:.0f}%", 12,
                _reason_text(hand.discard_reasons))
        elif rate >= 0.4:
            add("discard_rate", f"丢弃帧 {rate * 100:.0f}%", 8,
                _reason_text(hand.discard_reasons))
        elif rate >= 0.2:
            add("discard_rate", f"丢弃帧 {rate * 100:.0f}%", 4,
                _reason_text(hand.discard_reasons))
    elif hand.frame_count == 0:
        add("no_frames", "没有关联到帧事件", 3)

    if hand.incidents:
        add("incidents", f"疑惑帧 {len(hand.incidents)} 份",
            min(15, 5 * len(hand.incidents)))

    if hand.fallback_count:
        add("fallback", f"建议兜底 {hand.fallback_count} 条",
            min(9, 3 * hand.fallback_count))
    if hand.prompt_missing_count:
        add("prompt_missing", f"未调用模型 {hand.prompt_missing_count} 条",
            min(6, 2 * hand.prompt_missing_count))
    if hand.advice_skips:
        add("advice_skip", f"牌面不可靠跳过建议 {hand.advice_skips} 次",
            min(6, 2 * hand.advice_skips))

    if hand.name_warnings:
        add("name_warnings", f"名字告警 {hand.name_warnings} 条",
            min(4, hand.name_warnings))

    if hand.hero_cards and hand.action_count == 0:
        add("no_actions", "有手牌但没有行动线", 3)

    hand.issues = issues
    hand.score = round(min(100.0, sum(i.points for i in issues)), 1)
    hand.severity = ("high" if hand.score >= 35 else
                     "medium" if hand.score >= 15 else
                     "low" if hand.score > 0 else "clean")


def _reason_text(reasons: dict[str, int]) -> str:
    return "  ".join(f"{k}={v}" for k, v in
                     sorted(reasons.items(), key=lambda kv: -kv[1])[:5])


# ─────────────────────────── 输出 ───────────────────────────

_ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m",
    "cyan": "\033[36m",
}


def _color(text: str, name: str, enabled: bool) -> str:
    return f"{_ANSI[name]}{text}{_ANSI['reset']}" if enabled else text


def _short_issues(hand: HandAudit, limit: int = 4) -> str:
    labels = [i.label for i in hand.issues[:limit]]
    if len(hand.issues) > limit:
        labels.append(f"…另 {len(hand.issues) - limit} 项")
    return "；".join(labels) if labels else "—"


def _time_short(ts: str) -> str:
    if not ts:
        return "—"
    text = str(ts)
    try:
        return datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        return text[-16:]


def print_report(report: AuditReport, *, top: int = 20, show_all: bool = False,
                 verbose: bool = False, color: bool = True) -> None:
    enabled = color and sys.stdout.isatty()
    summary = report.summary()
    print(_color(f"识别质量排查 · {summary['sessions']} 个会话 / "
                 f"{summary['hands']} 手 / {summary['flagged']} 手可疑",
                 "bold", enabled))
    print(f"  states: {', '.join(report.state_files) or '—'}")
    print(f"  events: {', '.join(report.event_files) or '—'}")
    print(f"  疑惑帧目录: {report.incident_files} 份"
          + (f" · 坏行 {summary['bad_lines']}" if summary["bad_lines"] else ""))
    print()

    rows = report.hands if show_all else report.flagged
    rows = sorted(rows, key=lambda h: (-h.score, h.ts or ""))
    if top > 0:
        rows = rows[:top]
    if not rows:
        print("没有发现可疑手牌（或日志里还没有 hand 记录）。")
        return

    header = (f"{'#':>3}  {'分数':>5}  {'等级':<4}  {'手牌 ID':<26}  "
              f"{'时间':<12}  {'hero':<6}  {'公共牌':<10}  主要问题")
    print(_color(header, "dim", enabled))
    print("─" * min(120, len(header) + 8))
    for i, hand in enumerate(rows, 1):
        sev = {"high": "高", "medium": "中", "low": "低", "clean": "—"}[hand.severity]
        sev_color = {"high": "red", "medium": "yellow", "low": "cyan",
                     "clean": "green"}[hand.severity]
        hero = " ".join(hand.hero_cards) or "—"
        board = " ".join(hand.board) or "翻前"
        line = (f"{i:>3}  {hand.score:>5.1f}  "
                f"{_color(f'{sev:<4}', sev_color, enabled)}  "
                f"{hand.uid:<26}  {_time_short(hand.ts):<12}  "
                f"{hero:<6}  {board:<10}  {_short_issues(hand)}")
        print(line)
        if verbose and hand.issues:
            for issue in hand.issues:
                detail = f"：{issue.detail}" if issue.detail else ""
                print(f"      · {issue.label} (+{issue.points:g}){detail}")

    print()
    print(_color("下一步：", "bold", enabled)
          + "`--explain <hand_id>` 看逐帧时间线；`--html logs/audit.html` "
            "生成可点击报告；`--json` 导出给脚本。")


def explain(report: AuditReport, needle: str, *, color: bool = True) -> int:
    enabled = color and sys.stdout.isatty()
    needle = needle.strip()
    matches = [h for h in report.hands
               if needle == h.hand_id or needle == h.uid
               or needle in h.hand_id or needle in h.uid]
    if not matches:
        print(f"没找到手牌：{needle}")
        return 1
    for hand in matches:
        print(_color(f"手牌 {hand.uid}", "bold", enabled),
              f"分数 {hand.score:g} / {hand.severity}")
        print(f"  时间 {hand.ts or '—'} · session {hand.session} · "
              f"verdict={hand.verdict} · 位置 {hand.hero_position or '—'}")
        print(f"  hero {(' '.join(hand.hero_cards) or '—')} · "
              f"board {(' '.join(hand.board) or '翻前')} · pot {hand.pot}")
        print(f"  帧 {hand.frame_count}（接受 {hand.accepted_frames} / "
              f"丢弃 {hand.discard_count} / stale {hand.stale_count}） · "
              f"建议 {hand.advice_count}（兜底 {hand.fallback_count} / "
              f"跳过 {hand.advice_skips}）")
        if hand.issues:
            print("  可疑信号：")
            for issue in hand.issues:
                detail = f"：{issue.detail}" if issue.detail else ""
                print(f"    · {issue.label} (+{issue.points:g}){detail}")
        if hand.warnings:
            print("  牌面警告：")
            for warning in hand.warnings[:12]:
                print(f"    · {warning}")
        if hand.discard_reasons:
            print("  丢弃原因：", _reason_text(hand.discard_reasons))
        if hand.incidents:
            print("  疑惑帧：")
            for folder in hand.incidents:
                print(f"    · {folder}")
        if hand.frames:
            print("  逐帧（最近 80 帧）：")
            for frame in hand.frames[-80:]:
                kind = str(frame.get("kind") or "?")
                errors = "；".join(frame.get("errors") or ())
                warnings = "；".join(frame.get("cards_warnings") or ())
                tail = " · ".join(x for x in (errors, warnings) if x)
                print(f"    #{frame.get('seq')} {kind:<9} "
                      f"{frame.get('proc_ms') or 0:>5.0f}ms "
                      f"{tail}")
        print()
    return 0


# ─────────────────────────── 导出 ───────────────────────────

def write_json(report: AuditReport, path: Path) -> None:
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": report.summary(),
        "hands": [h.to_dict() for h in report.hands],
        "bad_lines": report.bad_lines,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def write_csv(report: AuditReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["uid", "hand_id", "session", "ts", "severity", "score",
              "verdict", "hero_cards", "board", "pot", "hero_position",
              "warnings", "inferred_actions", "action_count", "advice_count",
              "fallback_count", "prompt_missing_count", "advice_skips",
              "frame_count", "accepted_frames", "discard_count",
              "discard_rate", "stale_count",
              "discard_reasons", "incidents", "name_warnings", "issues"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for h in report.hands:
            row = {
                "uid": h.uid, "hand_id": h.hand_id, "session": h.session,
                "ts": h.ts, "severity": h.severity, "score": h.score,
                "verdict": h.verdict,
                "hero_cards": " ".join(h.hero_cards),
                "board": " ".join(h.board), "pot": h.pot,
                "hero_position": h.hero_position or "",
                "warnings": " | ".join(h.warnings),
                "inferred_actions": h.inferred_actions,
                "action_count": h.action_count,
                "advice_count": h.advice_count,
                "fallback_count": h.fallback_count,
                "prompt_missing_count": h.prompt_missing_count,
                "advice_skips": h.advice_skips,
                "frame_count": h.frame_count,
                "accepted_frames": h.accepted_frames,
                "discard_count": h.discard_count,
                "discard_rate": round(h.discard_rate, 3),
                "stale_count": h.stale_count,
                "discard_reasons": json.dumps(h.discard_reasons,
                                              ensure_ascii=False),
                "incidents": " | ".join(h.incidents),
                "name_warnings": h.name_warnings,
                "issues": "；".join(i.label for i in h.issues),
            }
            writer.writerow(row)


def write_html(report: AuditReport, path: Path) -> None:
    esc = html.escape
    summary = report.summary()
    rows = []
    for hand in report.hands:
        sev = hand.severity
        issues = "；".join(f"{i.label} +{i.points:g}" for i in hand.issues) or "—"
        warnings = "；".join(hand.warnings[:8]) or "—"
        frames = []
        for frame in hand.frames[-80:]:
            errors = "；".join(frame.get("errors") or ())
            cards = "；".join(frame.get("cards_warnings") or ())
            tail = " · ".join(x for x in (errors, cards) if x)
            frames.append(
                f"<li><code>#{esc(str(frame.get('seq')))}</code> "
                f"<b>{esc(str(frame.get('kind')))}</b> "
                f"{frame.get('proc_ms') or 0:.0f}ms {esc(tail)}</li>")
        incidents = "".join(
            f"<li><code>{esc(folder)}</code></li>" for folder in hand.incidents)
        rows.append(f"""
<details class="hand {esc(sev)}">
  <summary>
    <span class="score">{hand.score:g}</span>
    <span class="sev">{esc({'high':'高','medium':'中','low':'低','clean':'—'}[sev])}</span>
    <span class="hid">{esc(hand.uid)}</span>
    <span class="time">{esc(_time_short(hand.ts))}</span>
    <span class="cards">{esc(' '.join(hand.hero_cards) or '—')}
      <i>/</i> {esc(' '.join(hand.board) or '翻前')}</span>
    <span class="issues">{esc(_short_issues(hand, 3))}</span>
  </summary>
  <div class="detail">
    <div class="facts">
      <span>verdict <b>{esc(hand.verdict)}</b></span>
      <span>帧 <b>{hand.frame_count}</b>（丢弃 {hand.discard_count}）</span>
      <span>建议 <b>{hand.advice_count}</b>（兜底 {hand.fallback_count} / 跳过 {hand.advice_skips}）</span>
      <span>推断动作 <b>{hand.inferred_actions}</b></span>
      <span>疑惑帧 <b>{len(hand.incidents)}</b></span>
      <span>名字告警 <b>{hand.name_warnings}</b></span>
    </div>
    <h4>可疑信号</h4>
    <p>{esc(issues)}</p>
    <h4>牌面警告</h4>
    <p>{esc(warnings)}</p>
    <h4>丢弃原因</h4>
    <p>{esc(_reason_text(hand.discard_reasons) or '—')}</p>
    {f'<h4>疑惑帧目录</h4><ul>{incidents}</ul>' if incidents else ''}
    {f'<h4>逐帧</h4><ul class="frames">{"".join(frames)}</ul>' if frames else ''}
  </div>
</details>""")

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>识别质量排查 · 手牌审计</title>
<style>
:root{{--bg:#08110e;--panel:#0e1d17;--line:#1f3b30;--text:#e7f1ec;--muted:#8fa89e;
--gold:#e7c66d;--red:#f07d7d;--amber:#e6b45f;--green:#6fd3a3}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--text);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:var(--muted);font-size:12px;margin-bottom:18px}}
.summary{{display:flex;gap:22px;flex-wrap:wrap;margin:14px 0 22px;color:var(--muted);font-size:12px}}
.summary b{{display:block;color:var(--text);font-size:20px;font-family:monospace}}
input{{width:100%;max-width:420px;padding:8px 11px;border-radius:8px;border:1px solid var(--line);
background:#0b1813;color:var(--text);margin-bottom:14px}}
.hand{{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden}}
.hand.high{{border-color:#743b3b}} .hand.medium{{border-color:#6b5529}}
summary{{display:grid;grid-template-columns:56px 40px minmax(190px,1.4fr) 110px 150px 1fr;
gap:12px;align-items:center;padding:10px 13px;cursor:pointer;list-style:none}}
summary::-webkit-details-marker{{display:none}}
.score{{font-family:monospace;font-size:17px;color:var(--gold)}}
.sev{{font-size:11px;text-align:center;border:1px solid var(--line);border-radius:99px;padding:1px 0}}
.hand.high .sev{{color:var(--red)}} .hand.medium .sev{{color:var(--amber)}}
.hand.low .sev{{color:var(--green)}}
.hid,.time{{font-family:monospace;font-size:12px;color:var(--muted)}}
.cards{{font-family:monospace;font-size:12px}} .cards i{{color:#445b52;font-style:normal}}
.issues{{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.detail{{padding:4px 16px 16px;border-top:1px solid var(--line)}}
.facts{{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:10px 0}}
.facts b{{color:var(--text);font-family:monospace}}
h4{{margin:14px 0 4px;font-size:12px;color:var(--gold);letter-spacing:.08em}}
p{{margin:0;color:var(--muted);font-size:12px}}
ul{{margin:4px 0;padding-left:18px;color:var(--muted);font-size:12px}}
code{{font-family:monospace;color:var(--text)}}
.frames li{{margin:2px 0}}
@media (max-width: 900px){{summary{{grid-template-columns:50px 36px 1fr}} .cards,.issues{{display:none}}}}
</style></head><body>
<h1>识别质量排查 · 手牌审计</h1>
<div class="sub">生成于 {esc(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'))}
 · 分数只用于排序，逐帧证据在展开项里</div>
<div class="summary">
  <span><b>{summary['hands']}</b>手牌</span>
  <span><b>{summary['flagged']}</b>可疑</span>
  <span><b>{summary['severity'].get('high',0)}</b>高</span>
  <span><b>{summary['severity'].get('medium',0)}</b>中</span>
  <span><b>{report.sessions}</b>会话</span>
  <span><b>{report.incident_files}</b>疑惑帧目录</span>
  <span><b>{summary['bad_lines']}</b>坏行</span>
</div>
<input id="filter" placeholder="过滤 hand_id / 名字 / 分数…">
<div id="list">{''.join(rows) or '<p>没有手牌记录。</p>'}</div>
<script>
document.getElementById('filter').addEventListener('input', (e) => {{
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('details.hand').forEach((el) => {{
    el.hidden = q && !el.textContent.toLowerCase().includes(q);
  }});
}});
</script>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


# ─────────────────────────── CLI ───────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="倒查实时日志：按手牌找出识别可疑的记录")
    ap.add_argument("--states", nargs="+", type=Path, default=[DEFAULT_STATES],
                    help=f"states JSONL（默认 {DEFAULT_STATES}；可给多个）")
    ap.add_argument("--events", nargs="*", type=Path, default=None,
                    help="事件 JSONL；默认按 states 路径派生（*.states.jsonl "
                         "→ *.events.jsonl），不存在就只用 states")
    ap.add_argument("--incidents", type=Path, default=DEFAULT_INCIDENTS,
                    help=f"疑惑帧目录（默认 {DEFAULT_INCIDENTS}）")
    ap.add_argument("--top", type=int, default=20, help="控制台显示前 N 名（默认 20）")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="只显示分数 ≥ 该值的手牌（默认 0 = 全显示）")
    ap.add_argument("--all", action="store_true", help="连干净手牌也显示")
    ap.add_argument("--verbose", action="store_true", help="逐条列出可疑信号")
    ap.add_argument("--explain", default=None, metavar="HAND_ID",
                    help="展开某一手：逐帧 kind/errors/cards warnings/疑惑帧")
    ap.add_argument("--session", type=int, default=None, help="只看第几个会话")
    ap.add_argument("--since", default=None, help="只看该日期之后（ISO 前缀）")
    ap.add_argument("--json", type=Path, default=None, help="导出 JSON")
    ap.add_argument("--csv", type=Path, default=None, help="导出 CSV")
    ap.add_argument("--html", type=Path, default=None, help="导出可点击 HTML")
    ap.add_argument("--no-color", action="store_true", help="关闭终端颜色")
    return ap


def _derive_events(states: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in states:
        name = path.name
        if name.endswith(".states.jsonl"):
            candidate = path.with_name(name[:-len(".states.jsonl")]
                                       + ".events.jsonl")
        elif name.endswith(".jsonl"):
            candidate = path.with_name(name[:-len(".jsonl")] + ".events.jsonl")
        else:
            candidate = path.with_name(name + ".events.jsonl")
        if candidate.exists() and candidate not in out:
            out.append(candidate)
    if not out and DEFAULT_EVENTS.exists():
        out.append(DEFAULT_EVENTS)
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    event_paths = (args.events if args.events is not None
                   else _derive_events(args.states))
    report = collect(args.states, event_paths, args.incidents)

    if args.session is not None:
        report.hands = [h for h in report.hands if h.session == args.session]
    if args.since:
        report.hands = [h for h in report.hands
                        if str(h.ts) >= str(args.since)]
    if args.min_score > 0:
        report.hands = [h for h in report.hands if h.score >= args.min_score]

    if args.explain:
        return explain(report, args.explain, color=not args.no_color)

    print_report(report, top=args.top, show_all=args.all,
                 verbose=args.verbose, color=not args.no_color)
    if args.json:
        write_json(report, args.json)
        print(f"→ JSON {args.json}")
    if args.csv:
        write_csv(report, args.csv)
        print(f"→ CSV {args.csv}")
    if args.html:
        write_html(report, args.html)
        print(f"→ HTML {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
