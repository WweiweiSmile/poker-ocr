#!/usr/bin/env python3
"""生成提示词思维导图：自包含 HTML + Markdown 大纲。

数据源是 docs/prompt-mindmap.json；模板是 docs/prompt-mindmap.template.html。
改完数据后运行：

    python3 tools/build_prompt_mindmap.py
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "docs" / "prompt-mindmap.json"
TEMPLATE_FILE = ROOT / "docs" / "prompt-mindmap.template.html"
OUT_HTML = ROOT / "docs" / "提示词模块思维导图.html"
OUT_MD = ROOT / "docs" / "提示词模块思维导图.md"


def render_md(data: dict) -> str:
    lines: list[str] = []
    lines.append("# poker-ocr 提示词思维导图")
    lines.append("")
    lines.append(f"> {data.get('role', '')}")
    lines.append(">")
    lines.append("> 交互版：`docs/提示词模块思维导图.html` · 数据源：`docs/prompt-mindmap.json`")
    lines.append("")
    lines.append("## 结构总览")
    lines.append("")

    def walk(node: dict, depth: int) -> None:
        title = node.get("title", "")
        role = node.get("role", "")
        path = (node.get("detail") or {}).get("path")
        label = f"`{title}`" if path else f"**{title}**"
        suffix = f" — {role}" if role else ""
        lines.append("  " * depth + f"- {label}{suffix}")
        for child in node.get("children") or []:
            walk(child, depth + 1)

    walk(data, 0)
    lines.append("")
    lines.append("## Mermaid mindmap")
    lines.append("")
    lines.append("```mermaid")
    lines.append("mindmap")
    lines.append("  root((poker-ocr 提示词))")

    def esc(text: str) -> str:
        table = {
            "(": "（", ")": "）", "[": "【", "]": "】",
            "{": "｛", "}": "｝", "<": "＜", ">": "＞",
            "#": "＃", ";": "；", '"': "”", "`": "'",
        }
        return "".join(table.get(ch, ch) for ch in text).strip()

    def mermaid(node: dict, depth: int) -> None:
        lines.append("    " * depth + esc(node.get("title", "")))
        for child in node.get("children") or []:
            mermaid(child, depth + 1)

    for branch in data.get("children") or []:
        mermaid(branch, 1)
    lines.append("```")
    lines.append("")
    lines.append("> Mermaid 只保留标题层级；每个模块的作用见上方总览或交互版。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    OUT_HTML.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    print(f"wrote {OUT_HTML.relative_to(ROOT)} ({OUT_HTML.stat().st_size:,} bytes)")
    print(f"wrote {OUT_MD.relative_to(ROOT)} ({OUT_MD.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
