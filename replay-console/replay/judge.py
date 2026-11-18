"""LLM judge（#58）：要点逐条判定 + evidence，人工可改判。

设计依据（docs/手牌库-AB案例-一体化设计-2026-11-17.md §7.2 / §7.4 / §13）：

- judge 默认与被测版本同档（deepseek-flash），温度 0；#74 起允许 judge 与被测
  模型相同（省成本，同模型自评的偏差由人工改判兜底），实际型号写进
  judge_version（随 run 冻结）；
- required_points / forbidden_points 逐条判命中 / 漏判；引用事实一律以模型
  当时实际看到的 prompt 原文为准，结构化 facts 只作 options / id 与结构参考，
  冲突以 prompt 为准；prompt 里完全没有的事实才算 missing（幻觉），prompt 有
  但数值/对象不符算 contradicted（扣 s_fact，不算幻觉）。judge 只产出「判定」，
  动作/选项/回落/调用失败仍由 scoring 的规则判——判官不重算分；
- 六个硬失败码每条都必须在 judge_json.evidence 里给出「引用的原句 /
  缺失的事实项 / 命中的禁令」，人工推翻走 replay_judge_overrides，不覆盖
  run_items 的原始分；
- judge 调用失败按项 fail open：退化到 #57 的规则口径（mode=rules），只记
  status/error，绝不伪造硬失败码。

本模块分两层：build_messages / parse_verdict / evidence / input_digest 都是
纯函数（可测、可复现）；evaluate 只负责一次模型调用。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Mapping, Sequence

from . import scoring
from .util import json_dumps, sha256_text

#: judge 提示词版本；改提示词必须 bump（judge_version 会变，旧 run 保留旧值）。
JUDGE_PROMPT_VERSION = "j2"
#: 判定必须可复现：温度固定 0（不能被配置改掉）。
JUDGE_TEMPERATURE = 0.0
#: 判定输出 schema 版本。
JUDGE_SCHEMA = "judge.v1"
#: 默认判定模型：与被测版本同档；#74 起允许同模型自评（不再硬拦 JUDGE_SAME_MODEL）。
DEFAULT_JUDGE_MODEL = "deepseek-flash"
#: judge 回复上限：逐条要点 + 引用原句，1200 足够，也防跑飞。
JUDGE_MAX_TOKENS = 1200
#: 塞给 judge 的 facts 上限（字符）；超出截断并在 prompt 里注明。
FACTS_CHAR_LIMIT = 6000
#: 塞给 judge 的模型输入 prompt 上限（字符）；正常一条 2–4K，留足余量。
PROMPT_CHAR_LIMIT = 12000
#: 理由原文上限（字符）；正常 reason 在 300 以内。
REASON_CHAR_LIMIT = 2000

MODE_LLM = "llm"
MODE_RULES = "rules"

#: 判定失败但不伪造硬失败的两种状态。
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNAVAILABLE = "unavailable"

_HINTS = {
    scoring.HF_OPTION: "建议的选项不在当时的选项集里",
    scoring.HF_FALLBACK: "建议来自回退（不是模型输出）",
    scoring.HF_FORBIDDEN_ACTION: "动作命中了禁令",
    scoring.HF_FORBIDDEN_POINT: "理由依赖了被禁止的要点",
    scoring.HF_HALLUCINATION: "理由引用了模型输入里不存在的事实并当作依据",
    scoring.HF_ERROR: "这次调用失败",
}


class JudgeUnavailable(Exception):
    """judge 凭据/配置不可用（可预期，不是模型调用失败）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ─────────────── 配置与版本 ───────────────

def judge_version(model: str | None) -> str:
    """判定口径标识：llm-<提示词版本>:<模型>；进 replay_runs.judge_version(32)。

    列宽 32：型号太长时不截断到无法区分——保留前缀 + 6 位哈希，
    不同型号不会撞成同一个 judge_version。
    """
    name = str(model or "").strip() or "unknown"
    tag = f"llm-{JUDGE_PROMPT_VERSION}:"
    if len(tag) + len(name) <= 32:
        return tag + name
    digest = sha256_text(name)[:6]
    room = 32 - len(tag) - len(digest) - 1
    return f"{tag}{name[:room]}~{digest}"


def judge_config(settings: Any) -> dict:
    """从 Settings 解析 judge 配置。"""
    model = str(getattr(settings, "judge_model", "") or
                DEFAULT_JUDGE_MODEL).strip()
    base_url = str(getattr(settings, "judge_base_url", "") or "").strip()
    api_key_ref = str(getattr(settings, "judge_api_key_ref", "") or
                      "AI_SERVER_API_KEY").strip()
    timeout = _positive_float(getattr(settings, "judge_timeout", 30.0), 30.0)
    thinking = getattr(settings, "judge_thinking", None)
    return {
        "schema": JUDGE_SCHEMA,
        "model": model,
        "baseUrl": base_url,
        "apiKeyRef": api_key_ref,
        "timeout": timeout,
        "temperature": JUDGE_TEMPERATURE,
        "promptVersion": JUDGE_PROMPT_VERSION,
        "maxTokens": JUDGE_MAX_TOKENS,
        "thinking": thinking,
        "version": judge_version(model),
    }


def judge_blockers(cfg: Mapping[str, Any], *,
                   model_under_test: str | None = None) -> list[dict]:
    """judge 不可用/不合规的原因；空列表 = 可以建 run。

    这些不是「静默默认」：创建页会显示，create_score_run 直接 409。
    `model_under_test` 仍保留在签名里（调用方传，便于将来加提示），但 #74 起
    不再因「判官与被测同模型」拦截——同模型自评的偏差由人工改判兜底。
    """
    out: list[dict] = []
    model = str(cfg.get("model") or "").strip()
    if not model:
        out.append({"code": "JUDGE_MODEL_MISSING",
                    "message": "判定模型未配置（REPLAY_JUDGE_MODEL）"})
    if not str(cfg.get("baseUrl") or "").strip():
        out.append({"code": "JUDGE_BASE_URL_MISSING",
                    "message": "判定接口地址未配置"
                               "（REPLAY_JUDGE_BASE_URL 或 AI_SERVER_BASE_URL）"})
    ref = str(cfg.get("apiKeyRef") or "AI_SERVER_API_KEY").strip()
    if not (os.environ.get(ref) or "").strip():
        out.append({"code": "JUDGE_KEY_MISSING",
                    "message": f"判定模型凭据 {ref} 未设置"})
    return out


def build_settings(cfg: Mapping[str, Any]):
    """judge 配置 → AISettings（api key 从环境变量取；缺 key 抛 JudgeUnavailable）。"""
    from poker_ocr.advice import AISettings

    blockers = judge_blockers(cfg)
    if blockers:
        first = blockers[0]
        raise JudgeUnavailable(str(first["code"]), str(first["message"]))
    key = (os.environ.get(str(cfg.get("apiKeyRef") or "AI_SERVER_API_KEY"))
           or "").strip()
    thinking = cfg.get("thinking")
    return AISettings(
        api_key=key,
        base_url=str(cfg.get("baseUrl") or "").strip(),
        model=str(cfg.get("model") or "").strip(),
        source="replay-judge",
        temperature=JUDGE_TEMPERATURE,
        top_p=1.0,
        max_tokens=int(cfg.get("maxTokens") or JUDGE_MAX_TOKENS),
        thinking=(str(thinking) if thinking else None),
    )


# ─────────────── 纯函数：输入、提示词、解析、证据 ───────────────

def _points(spec: Mapping[str, Any] | None, field: str) -> list[dict]:
    raw = (spec or {}).get(field) or []
    out: list[dict] = []
    for point in raw:
        if not isinstance(point, Mapping):
            continue
        key = str(point.get("key") or "").strip()
        text = str(point.get("text") or "").strip()
        if not key or not text:
            continue
        item = {"key": key, "text": text}
        if field == "required_points":
            weight = point.get("weight")
            item["weight"] = (weight if isinstance(weight, int)
                              and not isinstance(weight, bool)
                              and weight > 0 else 1)
        out.append(item)
    return out


def _norm_text(value: Any) -> str:
    """引文比对用：去空白、统一大小写；不改变字符本身（错别字仍算不匹配）。"""
    text = str(value or "")
    text = text.replace("\u3000", "").replace("\u00a0", "")
    return re.sub(r"\s+", "", text).lower()


def _options_brief(facts: Mapping[str, Any],
                   option_ids: Iterable[Any] = ()) -> list[dict]:
    allowed = {str(oid) for oid in option_ids}
    out: list[dict] = []
    for option in (facts.get("options") or []):
        if not isinstance(option, Mapping):
            continue
        oid = option.get("id")
        if oid is None or (allowed and str(oid) not in allowed):
            continue
        out.append({
            "id": str(oid),
            "label": str(option.get("label") or ""),
            "kind": str(option.get("kind") or ""),
            "amount": option.get("amount"),
        })
    return out


def _clip_facts(facts: Mapping[str, Any]) -> dict:
    """把 facts 收进提示词预算，但**保持合法 JSON**。

    超长时按体积从大到小丢掉可选键，把丢掉的键名放进 truncatedKeys；
    options 是判定锚点，最后才退化成摘要。绝不把 JSON 从中间切断——
    那样 judge 会以为被切掉的事实不存在，误判幻觉。
    """
    data = dict(facts or {})
    if len(json_dumps(data)) <= FACTS_CHAR_LIMIT:
        return data
    dropped: list[str] = []
    order = sorted((k for k in data if k != "options"),
                   key=lambda k: len(json_dumps(data[k])), reverse=True)
    while order and len(json_dumps({**data, "truncatedKeys": dropped})) > FACTS_CHAR_LIMIT:
        key = order.pop(0)
        data.pop(key, None)
        dropped.append(key)
    if len(json_dumps({**data, "truncatedKeys": dropped})) > FACTS_CHAR_LIMIT:
        data = {"options": [
            {k: o.get(k) for k in ("id", "label", "kind", "amount")}
            for o in (data.get("options") or []) if isinstance(o, Mapping)
        ]}
        dropped = sorted(set(dropped) | {"其余事实"})
    if dropped:
        data["truncatedKeys"] = dropped
    return data


def _clip_prompt(text: Any, limit: int = PROMPT_CHAR_LIMIT) -> str:
    """模型输入原文单独限长；截断必须留标记，否则 judge 会把没看到的内容判 missing。

    优先在最后一个换行处切，避免把「63BB」这类数字从中间切断后读成另一个值。
    """
    prompt = str(text or "")
    if len(prompt) <= limit:
        return prompt
    mark = (f"\n…（prompt 原文超 {limit} 字，已截断；"
            "标记之后未展示的内容不要据此判 missing）")
    budget = max(0, limit - len(mark))
    head = prompt[:budget]
    cut = head.rfind("\n")
    if cut > budget // 2:
        head = head[:cut]
    return head + mark


def build_messages(spec: Mapping[str, Any] | None,
                   advice: Mapping[str, Any],
                   facts: Mapping[str, Any],
                   option_ids: Iterable[Any] = (),
                   model_prompt: str | None = None) -> tuple[str, str]:
    """组装 judge 的 system/user。提示词改动必须 bump JUDGE_PROMPT_VERSION。

    model_prompt 是模型当时实际看到的 prompt 原文（prompt_json.user），是核对
    引用事实的权威来源；facts 只用于 options / id 与结构参考。
    """
    system = (
        "你是德州扑克复盘里的判定模型（judge），只做核对，不重新解题。\n"
        "输入：spot spec 的 required_points（必须说到的要点，带权重）与 "
        "forbidden_points（禁止依赖的要点）、被测模型给出的 advice（选项、动作、"
        "金额、理由原文）、模型当时实际看到的 prompt 原文"
        "（【模型当时看到的输入】，权威事实源）、当时的 optionIds、以及结构化 "
        "facts（只用于核对 options / id 与结构参考）。\n"
        "事实源优先级：【模型当时看到的输入】是权威；结构化 facts 与它冲突时"
        "一律以 prompt 原文为准。\n"
        "只按下面四条判，不允许补充外部知识：\n"
        "1. requiredPoints 逐条判理由有没有真的说出这个要点。hit 必须从理由原文"
        "里摘出连续一段 quote；理由没写就是 miss——不要替它补，也不要因为结论对"
        "就算 hit。\n"
        "2. forbiddenPoints 逐条判理由有没有依赖这条禁令。依赖了给 hit 并摘 "
        "quote，否则 clean。\n"
        "3. citedFacts 把理由里当作依据的数字/牌型/范围/人数/赔率等逐条列出："
        "quote 是理由原文；用【模型当时看到的输入】原文判三态——supported 表示"
        "原文有一致事实（原文没逐字给出、但能由原文数字直接算出且算对，也算 "
        "supported）；contradicted 表示原文有这条事实但数值/对象/方向实质不符；"
        "missing 表示原文完全没有这条事实。prompt 与结构化 facts 冲突时按 prompt "
        "判；结构化 facts 只用来核对 options / id。\n"
        "4. hallucinated 只有当存在 missing 的引用事实、且它确实被拿来支撑建议"
        "时才是 true；只有 contradicted、没有 missing 时必须 false。\n"
        "如果输入里没有【模型当时看到的输入】一节，说明该条没有留存模型输入："
        "citedFacts 必须为空、hallucinated 必须 false，只判 requiredPoints / "
        "forbiddenPoints。\n"
        "quote 必须一字不改地摘自 advice.reason（含标点），不要改写、翻译、拼接。\n"
        "只输出一个 JSON 对象，不要 markdown、不要解释，字段固定：\n"
        '{"requiredPoints":[{"key":"...","status":"hit|miss","quote":"..."}],'
        '"forbiddenPoints":[{"key":"...","status":"hit|clean","quote":"..."}],'
        '"citedFacts":[{"quote":"...","status":"supported|contradicted|missing",'
        '"fact":"..."}],"hallucinated":false}\n'
        "requiredPoints / forbiddenPoints 必须覆盖每个 key，各一条，不许漏。"
    )
    required = _points(spec, "required_points")
    forbidden = _points(spec, "forbidden_points")
    lines: list[str] = []
    lines.append("【required_points】")
    if required:
        lines.extend(f"- {p['key']} | 权重 {p.get('weight', 1)} | {p['text']}"
                     for p in required)
    else:
        lines.append("- 无（此时依据维按「理由是否支持该动作」退化判）")
    lines.append("")
    lines.append("【forbidden_points】")
    if forbidden:
        lines.extend(f"- {p['key']} | {p['text']}" for p in forbidden)
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("【optionIds】")
    brief = _options_brief(facts, option_ids)
    if brief:
        lines.extend(f"- {o['id']} | {o['label']} | {o['kind']} | 金额 {o['amount']}"
                     for o in brief)
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("【advice】")
    reason = str(advice.get("reason") or "")[:REASON_CHAR_LIMIT]
    lines.append(f"option_id: {advice.get('option_id')}")
    lines.append(f"kind: {advice.get('kind')}")
    lines.append(f"amount: {advice.get('amount')}")
    lines.append(f'reason: """{reason}"""')
    lines.append("")
    prompt_text = str(model_prompt or "")
    if prompt_text.strip():
        lines.append("【模型当时看到的输入（权威事实源）】")
        lines.append("以下是被测模型当时实际看到的 prompt 原文；引用事实一律以它"
                     "为准，与下方结构化 facts 冲突时也以它为准。")
        lines.append('"""')
        lines.append(_clip_prompt(prompt_text))
        lines.append('"""')
        lines.append("")
    lines.append("【facts】")
    lines.append("（结构化参考，仅用于核对 options / id 与结构；与上方 prompt "
                 "冲突时以 prompt 为准）")
    lines.append(json_dumps(_clip_facts(facts)))
    return system, "\n".join(lines)


def input_digest(spec: Mapping[str, Any] | None, advice: Mapping[str, Any],
                 facts: Mapping[str, Any],
                 option_ids: Iterable[Any] = (),
                 model_prompt: str | None = None,
                 model_prompt_sha: str | None = None) -> str:
    """同输入 → 同 digest；两次判定对不上时用它证明输入没变。

    prompt 是判定输入的一部分，必须进 digest。哈希的是 judge 实际看到的
    （_clip_prompt 之后）原文；只有原文缺失时才退回留存 sha。
    """
    if str(model_prompt or "").strip():
        prompt_tag = sha256_text(_clip_prompt(model_prompt))
    else:
        prompt_tag = str(model_prompt_sha or "").strip()
    payload = {
        "requiredPoints": _points(spec, "required_points"),
        "forbiddenPoints": _points(spec, "forbidden_points"),
        "optionIds": sorted(str(o) for o in option_ids),
        "advice": {
            "optionId": str(advice.get("option_id") or ""),
            "kind": str(advice.get("kind") or ""),
            "amount": advice.get("amount"),
            "reason": str(advice.get("reason") or "")[:REASON_CHAR_LIMIT],
        },
        "facts": facts or {},
        "prompt": prompt_tag,
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return sha256_text(text)[:16]


def parse_verdict(raw: Mapping[str, Any] | None, *, spec: Mapping[str, Any] | None,
                  advice: Mapping[str, Any]) -> dict:
    """模型回复 → 规范化判定（纯函数）。

    四条硬约束，防止「判官编证据」：
    - quote 必须能在 reason 里找到，否则强制 miss / clean，不认；
    - required_points 里没被提到的 key 一律 miss；
    - citedFacts 只认 supported / contradicted / missing，其余按 supported；
    - hallucinated=true 但没有 missing 的引用事实时强制 false（contradicted
      只计 wrongFacts 扣 s_fact，不升级成幻觉）。
    """
    data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    reason_norm = _norm_text(advice.get("reason") or "")

    def clean_quote(value: Any) -> str:
        quote = str(value or "").strip()
        if not quote or not reason_norm:
            return ""
        return quote if _norm_text(quote) in reason_norm else ""

    def by_key(values: Any) -> dict[str, Mapping[str, Any]]:
        out: dict[str, Mapping[str, Any]] = {}
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for entry in values:
                if not isinstance(entry, Mapping):
                    continue
                key = str(entry.get("key") or "").strip()
                if key and key not in out:
                    out[key] = entry
        return out

    raw_required = by_key(data.get("requiredPoints"))
    required_out: list[dict] = []
    matched: list[str] = []
    for point in _points(spec, "required_points"):
        entry = raw_required.get(point["key"]) or {}
        quote = clean_quote(entry.get("quote"))
        hit = (str(entry.get("status") or "").strip().lower() == "hit") and bool(
            quote)
        if hit:
            matched.append(point["key"])
        required_out.append({
            "key": point["key"], "text": point["text"],
            "weight": point.get("weight", 1),
            "status": "hit" if hit else "miss",
            "quote": quote, "quoteFound": bool(quote),
        })

    raw_forbidden = by_key(data.get("forbiddenPoints"))
    forbidden_out: list[dict] = []
    forbidden_hits: list[str] = []
    for point in _points(spec, "forbidden_points"):
        entry = raw_forbidden.get(point["key"]) or {}
        quote = clean_quote(entry.get("quote"))
        hit = (str(entry.get("status") or "").strip().lower() == "hit") and bool(
            quote)
        if hit:
            forbidden_hits.append(point["key"])
        forbidden_out.append({
            "key": point["key"], "text": point["text"],
            "status": "hit" if hit else "clean",
            "quote": quote, "quoteFound": bool(quote),
        })

    cited: list[dict] = []
    for entry in (data.get("citedFacts") or []):
        if not isinstance(entry, Mapping):
            continue
        quote = clean_quote(entry.get("quote"))
        if not quote:
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status not in ("supported", "contradicted", "missing"):
            status = "supported"
        cited.append({
            "quote": quote,
            "status": status,
            "fact": str(entry.get("fact") or "").strip()[:200],
        })
    missing = [item for item in cited if item["status"] == "missing"]
    contradicted = [item for item in cited if item["status"] == "contradicted"]
    hallucinated = bool(data.get("hallucinated")) and bool(missing)
    return {
        "schema": JUDGE_SCHEMA,
        "reasonMode": "points",
        "requiredPoints": required_out,
        "forbiddenPoints": forbidden_out,
        "matchedPoints": matched,
        "missingPoints": [p["key"] for p in required_out
                          if p["status"] == "miss"],
        "forbiddenHits": forbidden_hits,
        "citedFacts": cited,
        "wrongFacts": missing + contradicted,
        "hallucinated": hallucinated,
    }


def error_judge_json(message: str, *, source: str = "error") -> dict:
    """流程级错误（缺 decision/spec 等）也落一条带 evidence 的 judge_json。"""
    text = str(message or "")[:500]
    return {
        "schema": JUDGE_SCHEMA,
        "mode": MODE_RULES,
        "status": STATUS_ERROR,
        "judgeVersion": "",
        "model": "",
        "promptVersion": JUDGE_PROMPT_VERSION,
        "temperature": JUDGE_TEMPERATURE,
        "inputDigest": "",
        "optionIds": [],
        "inputPrompt": False,
        "degraded": "judge_error",
        "error": text,
        "evidence": {
            "code": scoring.HF_ERROR,
            "hint": _HINTS.get(scoring.HF_ERROR, ""),
            "quotes": [],
            "missingFacts": [],
            "forbiddenPoints": [],
            "forbiddenActions": [],
            "source": source,
            "error": text,
        },
    }


def evidence(code: str | None, verdict: Mapping[str, Any] | None, *,
             advice: Mapping[str, Any],
             option_ids: Iterable[Any] = (),
             spec: Mapping[str, Any] | None = None,
             error: str = "") -> dict:
    """硬失败的 evidence（设计 §7.2）：引用的原句 / 缺失的事实项 / 命中的禁令。"""
    if not code:
        return {}
    data = verdict or {}
    cited = [c for c in (data.get("citedFacts") or []) if isinstance(c, Mapping)]
    missing = [c for c in cited if c.get("status") == "missing"]
    forbidden = [p for p in (data.get("forbiddenPoints") or [])
                 if isinstance(p, Mapping) and p.get("status") == "hit"]
    quotes: list[str] = []
    if code == scoring.HF_FORBIDDEN_POINT:
        quotes = [str(p.get("quote")) for p in forbidden if p.get("quote")]
    else:
        quotes = [str(c.get("quote")) for c in missing if c.get("quote")]
    out = {
        "code": code,
        "hint": _HINTS.get(code, ""),
        "quotes": quotes,
        "missingFacts": [str(c.get("fact") or "") for c in missing
                         if c.get("fact")],
        "forbiddenPoints": [
            {"key": str(p.get("key") or ""), "text": str(p.get("text") or ""),
             "quote": str(p.get("quote") or "")}
            for p in forbidden
        ],
        "forbiddenActions": [],
        "optionId": advice.get("option_id"),
        "optionIds": [str(o) for o in option_ids],
        "source": advice.get("source"),
    }
    if code == scoring.HF_FORBIDDEN_ACTION:
        banned = [str(a) for a in (spec or {}).get("forbidden_actions") or ()]
        kind = str(advice.get("kind") or "")
        hits = [a for a in banned if a == kind]
        out["forbiddenActions"] = hits or ([kind] if kind else [])
    if code == scoring.HF_ERROR and error:
        out["error"] = str(error)[:500]
    return out


# ─────────────── 判定 + 打分 ───────────────

def fallback_verdict(cfg: Mapping[str, Any] | None, *,
                     error: str = "",
                     status: str = STATUS_ERROR) -> dict:
    """judge 不可用/失败：退化到规则口径，不伪造硬失败。"""
    cfg = cfg or {}
    return {
        "schema": JUDGE_SCHEMA,
        "mode": MODE_RULES,
        "status": status,
        "judgeVersion": cfg.get("version") or judge_version(cfg.get("model")),
        "model": cfg.get("model") or "",
        "promptVersion": cfg.get("promptVersion") or JUDGE_PROMPT_VERSION,
        "temperature": JUDGE_TEMPERATURE,
        "inputDigest": "",
        "reasonMode": "action-support",
        "requiredPoints": [],
        "forbiddenPoints": [],
        "matchedPoints": [],
        "missingPoints": [],
        "forbiddenHits": [],
        "citedFacts": [],
        "wrongFacts": [],
        "hallucinated": False,
        "inputPrompt": False,
        "degraded": "",
        "error": error[:500],
    }


def evaluate(cfg: Mapping[str, Any], *, settings: Any,
             spec: Mapping[str, Any] | None, advice: Mapping[str, Any],
             facts: Mapping[str, Any], option_ids: Iterable[Any] = (),
             model_prompt: str | None = None,
             model_prompt_sha: str | None = None,
             timeout: float | None = None) -> dict:
    """调一次 judge 并返回可落库的 judge_json；失败 fail open。

    model_prompt 是模型当时实际看到的 prompt 原文（prompt_json.user）。没有留存
    prompt 时（store_prompts=false / 旧行 prompt_json 为 NULL）不产生幻觉判定，
    只判 required / forbidden 要点，并写 inputPrompt=false / degraded。
    """
    from poker_ocr.advice import AIError, complete_json

    version = str(cfg.get("version") or judge_version(cfg.get("model")))
    option_list = [str(o) for o in option_ids]
    has_prompt = bool(str(model_prompt or "").strip())
    prompt_meta = {
        "inputPrompt": has_prompt,
        "degraded": "" if has_prompt else "prompt_missing",
    }
    digest = input_digest(spec, advice, facts, option_list,
                          model_prompt=model_prompt,
                          model_prompt_sha=model_prompt_sha)
    if settings is None:
        return {**fallback_verdict(cfg, status=STATUS_UNAVAILABLE,
                                   error="judge 凭据不可用"),
                **prompt_meta, "inputDigest": digest,
                "degraded": "judge_error"}
    system, user = build_messages(spec, advice, facts, option_list,
                                  model_prompt=model_prompt)
    try:
        raw = complete_json(settings, system, user,
                            timeout=_positive_float(timeout, 30.0))
    except AIError as exc:
        return {**fallback_verdict(cfg, error=f"{type(exc).__name__}: {exc}"),
                **prompt_meta, "inputDigest": digest,
                "degraded": "judge_error"}
    except Exception as exc:  # noqa: BLE001  judge 永不阻塞评分
        return {**fallback_verdict(cfg, error=f"{type(exc).__name__}: {exc}"),
                **prompt_meta, "inputDigest": digest,
                "degraded": "judge_error"}
    verdict = parse_verdict(raw, spec=spec, advice=advice)
    if not has_prompt:
        # 没有权威事实源就无法证明幻觉；fail open，不产生 HF_HALLUCINATION。
        verdict = {**verdict, "citedFacts": [], "wrongFacts": [],
                   "hallucinated": False}
    meta = raw.get("_meta") if isinstance(raw, Mapping) else None
    return {
        **verdict,
        "mode": MODE_LLM,
        "status": STATUS_OK,
        "judgeVersion": version,
        "model": str(cfg.get("model") or ""),
        "promptVersion": str(cfg.get("promptVersion") or JUDGE_PROMPT_VERSION),
        "temperature": JUDGE_TEMPERATURE,
        "inputDigest": digest,
        "optionIds": option_list,
        "error": "",
        **prompt_meta,
        **({"usage": meta} if isinstance(meta, Mapping) else {}),
    }


def score_verdict(verdict: Mapping[str, Any] | None, *,
                  spec: Mapping[str, Any] | None,
                  advice: Mapping[str, Any],
                  option_ids: Iterable[Any] = (),
                  pot: Any = None) -> dict:
    """判定 + 规则 → score_case 的结果（worker 与 rejudge 共用同一条路径）。"""
    data = verdict or {}
    llm = data.get("mode") == MODE_LLM
    kind = advice.get("kind")
    # 规则能判的硬失败（失败/越界选项/回退/禁动作）；要点与幻觉由 judge 判。
    hard = scoring.detect_hard_fail(
        source=str(advice.get("source") or "model"),
        option_id=advice.get("option_id"), option_ids=list(option_ids),
        kind=kind,
        forbidden_actions=(spec or {}).get("forbidden_actions") or (),
        forbidden_points=(), matched_points=(), hallucinated=False)
    if hard is None and llm:
        if data.get("forbiddenHits"):
            hard = scoring.HF_FORBIDDEN_POINT
        elif data.get("hallucinated"):
            hard = scoring.HF_HALLUCINATION
    score_spec = spec if llm else {**(spec or {}), "required_points": []}
    return scoring.score_case(
        spec=score_spec, advice=advice, option_ids=list(option_ids), pot=pot,
        matched_points=(data.get("matchedPoints") or []) if llm else [],
        cited_facts=len(data.get("citedFacts") or []) if llm else 0,
        wrong_facts=len(data.get("wrongFacts") or []) if llm else 0,
        repeat_kinds=None,
        hallucinated=bool(data.get("hallucinated")) if llm else False,
        hard_fail=hard)


def compare_verdict(before: Mapping[str, Any] | None,
                    after: Mapping[str, Any] | None) -> dict:
    """两次判定的可比部分是否一致（同输入复判用）；差异逐项列出。"""
    a = before or {}
    b = after or {}

    def statuses(data: Mapping[str, Any], field: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in data.get(field) or []:
            if isinstance(item, Mapping) and item.get("key"):
                out[str(item["key"])] = str(item.get("status") or "")
        return out

    diffs: list[dict] = []
    for field in ("requiredPoints", "forbiddenPoints"):
        left, right = statuses(a, field), statuses(b, field)
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                diffs.append({"field": field, "key": key,
                              "before": left.get(key), "after": right.get(key)})
    if bool(a.get("hallucinated")) != bool(b.get("hallucinated")):
        diffs.append({"field": "hallucinated", "key": "",
                      "before": bool(a.get("hallucinated")),
                      "after": bool(b.get("hallucinated"))})
    if str(a.get("mode") or "") != str(b.get("mode") or ""):
        diffs.append({"field": "mode", "key": "",
                      "before": a.get("mode"), "after": b.get("mode")})
    return {"consistent": not diffs, "diffs": diffs}


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
