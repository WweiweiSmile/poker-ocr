"""AI 版本的语义与不可变配置哈希。

一个版本不是「模型名」，而是：
模型 + 接口地址 + 采样参数 + 提示词风格 + 提示词版本 + 重建版本 + 桌风快照。
这里的 `config_hash` 就是这组字段的指纹；同一用户下哈希相同即视为同一个版本。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .config import REPO_ROOT, git_state
from .errors import ApiError
from .util import iso, json_dumps, sha256_text

from poker_ocr.advice import AISettings, PROMPT_VERSION  # noqa: E402
from poker_ocr.table_dynamics import (  # noqa: E402
    load_table_dynamics, parse_table_dynamics,
)

# 改动提示词、事实块、选项生成规则、价值指令或审计口径时**必须** bump。
# 2026-10-28.1：加入实时牌面范围/手牌强度事实块与 Hand vs ranges 优先级。
# 2026-10-28.2：在 Hand vs ranges 之后新增 Hand-strength read 一行事实。
# 2026-10-28.3：skills 提示词的 system 层注入实时牌力与桌面动态。
# 2026-10-28.4：skills 提示词再注入逐座位对手画像（Current opponent reads）。
# 2026-10-28.5：默认 lean 只注入状态/画像/实时牌力/桌风，移除范围表、翻后规则、
#               价值指令、Math gate、plan 回灌与 GTO；旧版完整 lean 改为 lean-rules。
# 2026-10-29.1：只保留一套中文 lean；新增中文事实块（SPR/位置/赔率门槛/爆炸底池/
#               转河变化/多人池实际人数）与 all-in 选项；skills/lean-rules 下线。
# 2026-10-30.9：system 拆 core + 前注条件模块；五级事实优先级与指标口径；
#               all-in 几何门控/有效筹码金额；缺失底池后手不再伪造下注；
#               选项 id 别名与 reason 截断；数据质量中文摘要；范围内部 id 下线。
# 2026-10-30.10：live 不再预计算/渲染 hand_strength 的「大/中/小」，
#                第 4 套牌力口径只留离线审计；prompt 只给牌型标签。
CURRENT_ADVICE_VERSION = "advice-2026-10-30.11"
# 改动 decisions.py 的重建语义时 bump。
# recon-3：翻前底池补上盲注（实时 pbs 只记前注，h39 4.5 → 6.0）。
CURRENT_RECONSTRUCT_VERSION = "recon-3"

# 新版本只允许 lean；旧版本里的 skills / lean-rules 只读，不再重跑。
PROMPT_STYLE_VALUES = ("lean",)
LEGACY_PROMPT_STYLES = ("lean-rules", "skills")
THINKING_VALUES = (None, "enabled", "disabled")
EFFORT_VALUES = (None, "none", "low", "high", "max")

_ENGINE: tuple[str | None, bool] | None = None


def engine_state(refresh: bool = False) -> tuple[str | None, bool]:
    global _ENGINE
    if _ENGINE is None or refresh:
        _ENGINE = git_state(REPO_ROOT)
    return _ENGINE


def provider_from_url(url: str) -> str:
    text = (url or "").lower()
    if "deepseek" in text:
        return "deepseek"
    if "openrouter" in text:
        return "openrouter"
    if "openai" in text:
        return "openai"
    if "anthropic" in text:
        return "anthropic"
    return "custom"


def normalize_table_dynamics(mode: str, text: str | None = None) -> str:
    """把「当前 / 粘贴 / 无」三种选择归一成快照文本。

    运行时只读版本里的文本，绝不重新读 `.table_dy`，否则一次改文件会让历史
    结果失去可解释性。
    """
    mode = (mode or "none").strip().lower()
    if mode == "none":
        return ""
    if mode == "text":
        return parse_table_dynamics(text or "")
    if mode == "current":
        dyn = load_table_dynamics()
        if dyn.error:
            raise ApiError("TABLE_DYNAMICS_ERROR", dyn.error, 422)
        return dyn.text if dyn.found else ""
    raise ApiError("INVALID_VERSION_CONFIG",
                   f"未知桌风来源：{mode}（current/text/none）", 422)


def table_dynamics_sha(text: str | None) -> str | None:
    return sha256_text(text) if text else None


def config_payload(
    *,
    provider: str,
    base_url: str,
    model: str,
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
    thinking: str | None,
    reasoning_effort: str | None,
    prompt_style: str,
    advice_version: str = CURRENT_ADVICE_VERSION,
    reconstruct_version: str = CURRENT_RECONSTRUCT_VERSION,
    api_key_ref: str,
    dynamics_sha: str | None,
    context_mode: str = "none",
    table_stats_mode: str = "none",
) -> dict[str, Any]:
    return {
        "provider": (provider or "").strip().lower(),
        "base_url": (base_url or "").strip().rstrip("/"),
        "model": (model or "").strip(),
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "prompt_style": prompt_style,
        "prompt_version": PROMPT_VERSION,
        "advice_version": advice_version,
        "reconstruct_version": reconstruct_version,
        "api_key_ref": (api_key_ref or "").strip(),
        "table_dynamics_sha": dynamics_sha,
        "context_mode": (context_mode or "none").strip().lower(),
        "table_stats_mode": (table_stats_mode or "none").strip().lower(),
    }


def config_hash(payload: dict[str, Any]) -> str:
    return sha256_text(json_dumps(payload))


def has_key(api_key_ref: str) -> bool:
    return bool((os.environ.get(api_key_ref or "") or "").strip())


def build_settings(version: dict) -> AISettings:
    """把版本行变成 poker_ocr.advice.AISettings。Key 只从环境变量读。"""
    ref = version.get("api_key_ref") or ""
    key = (os.environ.get(ref) or "").strip()
    if not key:
        raise ApiError("AI_KEY_MISSING",
                       f"环境变量 {ref or '（未配置）'} 里没有 API Key", 422,
                       {"apiKeyRef": ref})
    return AISettings(
        api_key=key,
        base_url=(version.get("base_url") or "").rstrip("/"),
        model=version.get("model") or "",
        source="replay",
        temperature=version.get("temperature"),
        top_p=version.get("top_p"),
        max_tokens=version.get("max_tokens"),
        thinking=version.get("thinking"),
        reasoning_effort=version.get("reasoning_effort"),
    )


def validate_config_values(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key_ref: str,
    prompt_style: str,
    thinking: str | None,
    reasoning_effort: str | None,
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
) -> None:
    if not (base_url or "").strip():
        raise ApiError("INVALID_VERSION_CONFIG", "base_url 不能为空", 422)
    if not (model or "").strip():
        raise ApiError("INVALID_VERSION_CONFIG", "model 不能为空", 422)
    if not (api_key_ref or "").strip():
        raise ApiError("INVALID_VERSION_CONFIG", "api_key_ref 不能为空", 422)
    if prompt_style not in PROMPT_STYLE_VALUES:
        raise ApiError("INVALID_VERSION_CONFIG",
                       f"prompt_style 只能是 {', '.join(PROMPT_STYLE_VALUES)}", 422)
    if thinking not in THINKING_VALUES:
        raise ApiError("INVALID_VERSION_CONFIG",
                       "thinking 只能是 enabled / disabled 或留空", 422)
    if reasoning_effort not in EFFORT_VALUES:
        raise ApiError("INVALID_VERSION_CONFIG",
                       "reasoning_effort 只能是 none/low/high/max 或留空", 422)
    if temperature is not None and not 0.0 <= float(temperature) <= 2.0:
        raise ApiError("INVALID_VERSION_CONFIG", "temperature 应在 0–2 之间", 422)
    if top_p is not None and not 0.0 <= float(top_p) <= 1.0:
        raise ApiError("INVALID_VERSION_CONFIG", "top_p 应在 0–1 之间", 422)
    if max_tokens is not None and int(max_tokens) <= 0:
        raise ApiError("INVALID_VERSION_CONFIG", "max_tokens 必须大于 0", 422)


def run_blockers(version: dict) -> list[dict]:
    """返回不能创建任务的原因；空列表表示可以重跑。"""
    blockers: list[dict] = []
    if int(version.get("archived") or 0):
        blockers.append({"code": "VERSION_ARCHIVED",
                         "message": f"版本 {version.get('name')} 已归档"})
    if (version.get("prompt_style") or "lean") != "lean":
        blockers.append({
            "code": "VERSION_LEGACY_PROMPT",
            "message": ("旧提示词风格 "
                        f"{version.get('prompt_style')} 已从 live 链路删除，"
                        "该版本只能只读查看，不能重跑"),
        })
    if version.get("advice_version") != CURRENT_ADVICE_VERSION:
        blockers.append({
            "code": "VERSION_STALE",
            "message": (f"版本提示词语义是 {version.get('advice_version')}，"
                        f"当前代码是 {CURRENT_ADVICE_VERSION}；请克隆成新版本再跑"),
        })
    if version.get("reconstruct_version") != CURRENT_RECONSTRUCT_VERSION:
        blockers.append({
            "code": "VERSION_STALE",
            "message": (f"版本重建语义是 {version.get('reconstruct_version')}，"
                        f"当前代码是 {CURRENT_RECONSTRUCT_VERSION}；请克隆成新版本再跑"),
        })
    if not has_key(version.get("api_key_ref") or ""):
        blockers.append({
            "code": "AI_KEY_MISSING",
            "message": f"环境变量 {version.get('api_key_ref')} 不存在或为空",
        })
    return blockers


def default_version_name(model: str, style: str, date_text: str) -> str:
    safe_model = "".join(ch if ch.isalnum() or ch in "-_." else "-"
                         for ch in (model or "model")).strip("-") or "model"
    return f"{safe_model}-{style or 'lean'}-{date_text}"


def public_version(row: dict, *, has_key_value: bool | None = None,
                   task_count: int = 0, last_used_at=None) -> dict:
    """版本行的 API 视图；永远不返回 Key。"""
    payload = row.get("config_payload") or {}
    if isinstance(payload, str):
        from .util import json_load
        payload = json_load(payload, {}) or {}
    name = row.get("name")
    return {
        "id": row.get("id"),
        "name": name,
        "provider": row.get("provider"),
        "baseUrl": row.get("base_url"),
        "model": row.get("model"),
        "apiKeyRef": row.get("api_key_ref"),
        "hasKey": has_key(row.get("api_key_ref") or "")
        if has_key_value is None else has_key_value,
        "temperature": row.get("temperature"),
        "topP": row.get("top_p"),
        "maxTokens": row.get("max_tokens"),
        "thinking": row.get("thinking"),
        "reasoningEffort": row.get("reasoning_effort"),
        "promptStyle": row.get("prompt_style"),
        "promptVersion": (payload or {}).get("prompt_version") if isinstance(payload, dict) else None,
        "adviceVersion": row.get("advice_version"),
        "reconstructVersion": row.get("reconstruct_version"),
        "engineCommit": row.get("engine_commit"),
        "engineDirty": bool(row.get("engine_dirty")),
        "tableDynamics": row.get("table_dynamics") or "",
        "tableDynamicsSha": row.get("table_dynamics_sha"),
        "tableDynamicsLength": len(row.get("table_dynamics") or ""),
        "contextMode": row.get("context_mode") or "none",
        "tableStatsMode": row.get("table_stats_mode") or "none",
        "configHash": row.get("config_hash"),
        "notes": row.get("notes") or "",
        "archived": bool(row.get("archived")),
        "createdAt": iso(row.get("created_at")),
        "taskCount": task_count,
        "lastUsedAt": last_used_at,
        "runnable": not run_blockers(row),
        "blockers": run_blockers(row),
        "config": payload,
    }
