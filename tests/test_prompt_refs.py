#!/usr/bin/env python3
"""行级 prompt_refs 的 6 条验收（规格 #33 A 部分 / issue #34）。

    nv tests/test_prompt_refs.py

只测旁路收集器的公开缝：`build_messages_refs()` / `fact()` / `Advice`。
提示词字节零变化是红线：第 1 条同时比对 `build_messages()` 与 golden hash。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "replay-console"))
sys.path.insert(0, str(ROOT / "tests"))

from poker_ocr import live_log  # noqa: E402
from poker_ocr.advice import (  # noqa: E402
    _facts_snapshot, advise, build_situation,
)
from poker_ocr.prompt import build_messages, build_messages_refs  # noqa: E402
from poker_ocr.prompt import refs as refs_mod  # noqa: E402
from test_advice_prompt_capture import make_sit, settings  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _golden():
    """按路径加载 replay-console 的 golden test（同一份 EXPECTED_PROMPT_HASH）。"""
    path = ROOT / "replay-console" / "tests" / "test_prompt_golden.py"
    spec = importlib.util.spec_from_file_location("_prompt_golden", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve(data, path: str):
    """facts 点路径（`a.b[0].c`）→ 值；解析不到返回 None。"""
    cur = data
    for part in path.split("."):
        name, _, rest = part.partition("[")
        if name:
            if not isinstance(cur, dict) or name not in cur:
                return None
            cur = cur[name]
        while rest:
            index, _, rest = rest.partition("]")
            try:
                cur = cur[int(index)]
            except (TypeError, ValueError, IndexError, KeyError):
                return None
            rest = rest.lstrip("[")
    return cur


def _line_of(ref: dict, system: str, user: str) -> str:
    text = system if ref["section"].startswith("scenario:") else user
    return text.split("\n")[ref["abs"] - 1]


def test_system_user_bytes_identical() -> None:
    """① 旁路激活不改变 (system, user)；golden hash 原样通过。"""
    sit = make_sit()
    system, user = build_messages(sit)
    system2, user2, refs = build_messages_refs(sit)
    check((system2, user2) == (system, user), "build_messages_refs 改了 prompt 字节")
    check(bool(refs), "build_messages_refs 没有产出 refs")

    golden = _golden()
    gsit = build_situation(golden._golden_snapshot(), table_dynamics="")
    gsystem, guser, _ = build_messages_refs(gsit)
    digest = hashlib.sha256(
        (gsystem + "\n---\n" + guser).encode("utf-8")).hexdigest()
    check(digest == golden.EXPECTED_PROMPT_HASH,
          f"golden prompt hash 变了：{digest}")


def test_inactive_collector_is_noop() -> None:
    """② 收集器未激活时 fact() 恒等返回，build_messages 无副作用。"""
    check(refs_mod.active() is None, "构建前收集器不应激活")
    check(refs_mod.fact("原样文本") == "原样文本", "未激活时 fact() 改写了文本")
    with refs_mod.section("x") as sec:
        sec.lines.append(refs_mod.fact("行"))
    check(sec.lines == ["行"], "未激活时 section 丢了行")
    check(refs_mod.active() is None, "build_messages 之外收集器不应激活")
    first = build_messages(make_sit())
    second = build_messages(make_sit())
    check(first == second, "build_messages 两次输出不一致")
    check(refs_mod.active() is None, "build_messages 泄漏了激活的收集器")


def test_fact_paths_resolve_and_text_is_substring() -> None:
    """③ kind=fact 的 facts 路径可解析，text 是该行子串；行号自洽。"""
    sit = make_sit()
    system, user, refs = build_messages_refs(sit)
    facts = _facts_snapshot(sit)
    check(bool(refs), "没有 refs")
    kinds = {ref["kind"] for ref in refs}
    check("fact" in kinds, "没有 kind=fact 的 ref")
    for ref in refs:
        check(ref["kind"] in ("fact", "derived", "static"),
              f"未知 kind：{ref['kind']}")
        line = _line_of(ref, system, user)
        check(isinstance(ref["line"], int) and ref["line"] >= 1,
              f"{ref['section']} 行号非法：{ref['line']}")
        check(isinstance(ref["abs"], int) and ref["abs"] >= 1,
              f"{ref['section']} abs 行号非法：{ref.get('abs')}")
        for item in ref["refs"]:
            check(item["ns"] in ("facts", "trace"),
                  f"未知 ns：{item['ns']}")
            check("text" in item and item["text"] in line,
                  f"{ref['section']}:{ref['line']} 的 text 不在行内：{item}")
            if ref["kind"] == "fact" and item["ns"] == "facts":
                value = _resolve(facts, item["path"])
                check(value is not None,
                      f"facts 路径解析不到：{item['path']}")


def test_refs_survive_without_range_trace() -> None:
    """④ --no-range-trace：refs 照常生成，deep 只是链接、缺失不抛错。"""
    with patch("poker_ocr.range_trace.enabled", return_value=False):
        system, user, refs = build_messages_refs(make_sit())
    check(bool(refs), "关 trace 后没有 refs")
    deep = [item["deep"] for ref in refs for item in ref["refs"]
            if item.get("deep")]
    check(bool(deep), "关 trace 后 deep 链接也丢了（应只降级、不生成失败）")
    for item in deep:
        check(item["ns"] == "trace", f"deep 只能指 trace：{item}")


def test_refs_are_deterministic() -> None:
    """⑤ 同一 sit 两次渲染，refs 逐字节相等。"""
    first = build_messages_refs(make_sit())[2]
    second = build_messages_refs(make_sit())[2]
    dump = lambda refs: json.dumps(refs, ensure_ascii=False, sort_keys=True)
    check(dump(first) == dump(second), "两次 refs 不一致")


def test_advice_carries_refs_and_events_drop_them() -> None:
    """⑥ prompt_refs 随 Advice 落 dict；events 日志不含该字段。"""
    advice = advise(make_sit(), settings(),
                    complete=lambda *_a, **_k: {"option": "call",
                                                "reason": "ok"})
    check(bool(advice.prompt_refs), "Advice 没带 prompt_refs")
    data = advice.to_dict()
    check(data.get("prompt_refs") == advice.prompt_refs,
          "to_dict 没写 prompt_refs")
    digest = hashlib.sha256(
        (advice.prompt_system + "\n---\n" + advice.prompt_user).encode()
    ).hexdigest()
    check(data["prompt_sha256"] == digest, "prompt_sha256 被 refs 影响")
    check("prompt_refs" in live_log._DROP_BY_EVENT["advice"],
          "events drop 表没剔除 prompt_refs")
    compact = live_log._compact("advice", data)
    check("prompt_refs" not in compact, "events 日志仍带 prompt_refs")
    check(compact["prompt_chars"] == len(advice.prompt_system) +
          len(advice.prompt_user), "prompt_chars 变了")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {test.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n✅ prompt_refs 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
