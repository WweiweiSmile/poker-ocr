"""行级 prompt_refs 的旁路收集器（spec #33 A 部分 / issue #34）。

契约见 `docs/提示词-facts-文案映射-对照表.md` §6。三条硬口径：

  · **提示词字节零变化**：收集器未激活时 `fact()` 恒等返回原文本，`section()`
    只是个装行的容器；`build_messages()` 的签名与输出都不变。
  · **只做行级**：`section` 是段 id，`line` 是段内 1-based 行号（与
    `段.split("\\n")` 对齐）；不插锚点、不做字符区间。`abs` 是该行在整段
    prompt 里的 1-based 行号，只给看板跳行用，不属于契约语义。
  · **fail open**：行号对不上、段文本定位不到时跳过这条 ref，绝不抛异常。

用法（渲染函数里）：

    lines.append(fact(f"对当前范围权益：{hr.equity:.0%}",
                      {"ns": "facts", "path": "hand_ranges.equity"},
                      values=[f"{hr.equity:.0%}"]))

组装段时把整段放进 `section()`，这样段内的行号与段文本对得上：

    with section("strength") as sec:
        sec.extend(render_hand_strength_zh(sit))
    lines.extend(sec.lines)
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

# 当前线程/任务里激活的收集器；None = 未激活（旁路关闭）。
_ACTIVE: contextvars.ContextVar["_Collector | None"] = contextvars.ContextVar(
    "poker_ocr_prompt_refs", default=None)


def active() -> "_Collector | None":
    """当前激活的收集器（测试/诊断用）。"""
    return _ACTIVE.get()


class _Section:
    """一段提示词的容器：行文本 + 渲染期登记的 (行文本, kind, refs)。

    未激活收集器时同样可用（`lines` 照样收集），只是 `facts` 永远为空。
    """

    __slots__ = ("name", "lines", "facts")

    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: list[str] = []
        self.facts: list[tuple[str, str, list[dict]]] = []

    def extend(self, lines) -> list[str]:
        self.lines.extend(lines)
        return self.lines

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@contextmanager
def section(name: str) -> Iterator[_Section]:
    """声明一个渲染段；段内 `fact()` 登记的 ref 按段内行号落位。"""
    sec = _Section(name)
    collector = _ACTIVE.get()
    if collector is not None:
        collector.stack.append(sec)
    try:
        yield sec
    finally:
        if collector is not None:
            collector.stack.pop()
            collector.sections.append(sec)


def fact(text: str, *refs, values=None, kind: str = "fact") -> str:
    """登记一行提示词与它的来源；返回原文本（未激活时是纯恒等函数）。

    `refs` 每项是 `{"ns": "facts"|"trace", "path": ...}`（可选 `"deep"`），
    也可以直接给 facts 点路径字符串。`values` 与 refs 一一对应，用来把渲染
    后的字面量（`"≈2.0%"`）钉进 `text`，供看板做行内子串高亮。
    """
    collector = _ACTIVE.get()
    if collector is None:
        return text
    collector.add(text, kind, _normalize(refs, values))
    return text


def _normalize(refs, values) -> list[dict]:
    if values is None:
        vals: tuple[str, ...] = ()
    elif isinstance(values, str):
        vals = (values,)
    else:
        vals = tuple(str(value) for value in values)
    items: list[dict] = []
    for index, ref in enumerate(refs):
        if isinstance(ref, str):
            item: dict = {"ns": "facts", "path": ref}
        else:
            item = dict(ref)
        item.setdefault("ns", "facts")
        if "text" not in item and index < len(vals):
            item["text"] = vals[index]
        items.append(item)
    return items


class _Collector:
    """一次 `build_messages_refs()` 的收集状态。"""

    def __init__(self) -> None:
        self.stack: list[_Section] = []
        self.sections: list[_Section] = []

    def add(self, text: str, kind: str, refs: list[dict]) -> None:
        if self.stack:
            self.stack[-1].facts.append((text, kind, refs))

    def finish(self, system: str, user: str) -> list[dict]:
        """把段内 ref 落成契约形状；定位不到的段/行直接跳过（fail open）。"""
        out: list[dict] = []
        for sec in self.sections:
            if not sec.lines or not sec.facts:
                continue
            located = _locate(sec.name, sec.text, system, user)
            if located is None:
                continue
            start = located
            cursor = 0
            for line_text, kind, refs in sec.facts:
                index = _match_line(sec.lines, line_text, cursor)
                if index is None:
                    continue
                cursor = index + 1
                out.append({
                    "section": sec.name,
                    "line": index + 1,
                    "kind": kind,
                    "refs": refs,
                    "abs": start + index,
                })
        # 按屏上顺序输出；同段内保持行号顺序，跨段按绝对行号。
        out.sort(key=lambda item: (item["abs"], item["section"], item["line"]))
        return out


def _locate(name: str, text: str, system: str, user: str) -> int | None:
    """段文本 → 该段第一行在整段 prompt 里的 1-based 行号。

    `scenario:*` 只可能出现在 system，其余段只可能出现在 user；先查该查的，
    查不到再退化成两边都找（段文本恰好撞车时也不至于丢 ref）。
    """
    if not text:
        return None
    order = (system, user) if name.startswith("scenario:") else (user, system)
    for full in order:
        if not full:
            continue
        index = full.find(text)
        if index >= 0:
            return full.count("\n", 0, index) + 1
    return None


def _match_line(lines: list[str], text: str, cursor: int) -> int | None:
    """行文本 → 段内 0-based 下标；先精确匹配，再退化成子串匹配。"""
    if not text:
        return None
    for index in range(cursor, len(lines)):
        if lines[index] == text:
            return index
    for index, line in enumerate(lines):
        if text in line:
            return index
    return None


@contextmanager
def collecting() -> Iterator[_Collector]:
    """激活收集器（只在 `build_messages_refs()` / 测试里用）。"""
    collector = _Collector()
    token = _ACTIVE.set(collector)
    try:
        yield collector
    finally:
        _ACTIVE.reset(token)


__all__ = ["active", "collecting", "fact", "section"]
