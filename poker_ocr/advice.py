"""AI 建议的模型调用层：拼提示词 → 调 /chat/completions → 校验结果 → 兜底。

事实计算（`Situation`）在 `poker_ocr.situation`，选项空间在
`poker_ocr.options`，提示词文本在 `poker_ocr.prompt` —— 本模块只负责：

  · `AISettings` / `AIError`：凭据、采样参数与超时（OpenAI 兼容协议）；
  · `complete_json` / `chat_message`：非流式调用、连接复用、错误归一化；
  · `advise()`：构建 prompt、解析模型返回的 option id、截断 reason、
    失败时走 `fallback_option()`，并把 prompt/raw 一起带进 `Advice` 供复盘；
  · `Advice` / `AdviceRunner`：结果结构与后台线程执行。

为什么输出是「选项编号」而不是自由文本：选项把尺度约束死了（1/3、1/2、
2/3、1、1.2 倍池、all-in），金额由 `options.py` 按现场底池算好，模型只做
选择 —— 它不需要会算底池，也就不会算错。这一点和 coach.py「确定性规则不
交给模型」是同一条原则。

调用协议：OpenAI 兼容的 /chat/completions（与 call-back 的 AIClient 同口径），
凭据来自环境变量 AI_SERVER_API_KEY / AI_SERVER_BASE_URL / AI_SERVER_MODEL。
**非流式**：一次建议的正文只有几十个 token，流式只会增加复杂度；超时用
`socket` 级的总时长控制（默认 20 秒，实时链路等不起更久）。**连接复用**：
同一 host 的 HTTP/1.1 长连接会被缓存，省掉每次建议都要付的 TLS 握手
（国内到 OpenRouter 实测 0.4–1.4s，常常比模型本身还慢）。
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from .opponent_profile import (
    OpponentRead, TENDENCY_PCT_FIELDS, pooled_stats_to_dict,
)
from .cross_check import check as _cross_action

# ── 事实 / 选项：拆到独立模块，这里 re-export 保持旧调用口径 ──
from .options import (  # noqa: F401
    ALL_IN_MAX_POT_MULTIPLE, LOOSE_OPEN_BONUS, OPEN_POT_EXPONENT, OPTION_ORDER,
    PREFLOP_ALLIN_MAX_STACK_BB, RAISE_FRACTIONS, Option, _SIZE_ZH,
    _abnormal_options, _all_in_option, _cap, _raise_geometry, _round_bb,
    _unsized_options, build_options, build_preflop_options, preset_amounts,
    preset_targets,
)
from .situation import (  # noqa: F401
    HERO_ACTOR, Situation, _TEXTURE_EN, _coach_actions, _precise_texture_en,
    _range_conclusion_zh, _read, build_situation,
)

# ── 牌桌几何/赔率纯计算 ──
from .pot_math import (  # noqa: F401
    EXPLOSIVE_POT_BB_PER_PLAYER, EXPLOSIVE_POT_TOLERANCE,
    PREFLOP_MIN_RAISE_BB, _action_order, _big_blind_option_pending, _can_act,
    _effective_stack, _hero_total_stack, _no_opponent_anomaly,
    _opponent_total_stack, _playable_depth, _same_actor, _street_bet,
    _unknown_stack_opponents, behind_opponents, call_amount, count_limpers,
    hero_first_to_act, hero_in_position, is_explosive_pot, players_left_to_act,
    pot_per_player, pot_type, previous_street_pot, spr, uncalled_excess,
    winnable_pot,
)

# ── 提示词 ──
from .prompt import (  # noqa: F401
    PROMPT_LANGUAGE, PROMPT_VERSION, build_messages, build_system,
    render_facts_zh,
)
from .prompt.user.action_line import _fmt_actions_zh  # noqa: F401
from .prompt.user.attack import render_attack_zh  # noqa: F401
from .prompt.user.board import unmapped_gt_warnings  # noqa: F401
from .prompt.user.explosive import render_explosive_zh  # noqa: F401
from .prompt.user.notes import _notes_ace_weakness  # noqa: F401
from .prompt.user.options import render_options_zh  # noqa: F401
from .prompt.user.preflop import render_preflop_zh  # noqa: F401
from .prompt.user.quality import _gap_zh, _summarize_gaps  # noqa: F401
from .prompt.user.ranges import render_ranges_zh  # noqa: F401
from .prompt.user.strength import render_hand_strength_zh  # noqa: F401


# ─────────────────────────── 模型调用 ───────────────────────────

def _env_text(env: dict[str, str], key: str) -> str | None:
    """可选配置：空值等同于没配，避免 `.env` 里的 `KEY=` 覆盖掉默认。"""
    value = (env.get(key) or "").strip()
    return value or None


def _env_number(env: dict[str, str], key: str, cast, low: float,
                high: float) -> float | int | None:
    raw = _env_text(env, key)
    if raw is None:
        return None
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{key} 不是合法数字：{raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{key} 应在 {low}–{high} 之间，实际 {raw!r}")
    return value


@dataclass(frozen=True)
class AISettings:
    api_key: str
    base_url: str
    model: str
    source: str = "env"              # env | dotenv，只用于启动日志
    # OpenRouter 的可选排名头（走 OpenRouter 时用）
    referer: str = ""
    title: str = ""
    # 采样/思考参数。None = 不传，用服务端默认。
    # 注意：deepseek-flash 默认开启思考（effort=high），思考模式下 temperature
    # 不生效，见 AI_SERVER_THINKING 的说明。
    temperature: float | None = 0.2
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: str | None = None           # enabled | disabled（DeepSeek）
    reasoning_effort: str | None = None   # none | low | high | max
    # OpenRouter 推理开关：请求体里是 {"reasoning": {"enabled": bool}}。
    # 与 DeepSeek 的 thinking 是两套字段，各自按供应商填写。None = 不传。
    reasoning: bool | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None,
                 source: str = "env") -> "AISettings | None":
        """从环境变量读凭据与采样参数。缺凭据返回 None（= 没配置）。

        与 call-back 的 AI_SERVER_* 同口径：同一台机器上两边共用一套配置。
        `.env` 由 poker_ocr.env 先加载进环境变量，这里不关心值从哪来。

        采样参数非法时抛 ValueError（而不是静默退回默认）：
        「配了却没生效」是这类配置最难查的故障。
        """
        env = os.environ if env is None else env
        key = (env.get("AI_SERVER_API_KEY") or "").strip()
        base = (env.get("AI_SERVER_BASE_URL") or "").strip()
        model = (env.get("AI_SERVER_MODEL") or "").strip()
        if not (key and base and model):
            return None

        thinking = _env_text(env, "AI_SERVER_THINKING")
        if thinking is not None:
            thinking = thinking.lower()
            if thinking not in ("enabled", "disabled"):
                raise ValueError("AI_SERVER_THINKING 只能是 enabled 或 disabled")
        effort = _env_text(env, "AI_SERVER_REASONING_EFFORT")
        if effort is not None:
            effort = effort.lower()
            if effort not in ("none", "low", "high", "max"):
                raise ValueError(
                    "AI_SERVER_REASONING_EFFORT 只能是 none / low / high / max")

        reasoning_text = _env_text(env, "AI_SERVER_REASONING")
        if reasoning_text is not None:
            reasoning_text = reasoning_text.lower()
            if reasoning_text not in ("enabled", "disabled"):
                raise ValueError("AI_SERVER_REASONING 只能是 enabled 或 disabled")
        reasoning = (None if reasoning_text is None
                     else reasoning_text == "enabled")

        temperature = _env_number(env, "AI_SERVER_TEMPERATURE", float, 0.0, 2.0)
        # 推理模型（OpenRouter 的 GPT/o 系列）只接受默认 temperature，
        # 显式发 0.2 会被服务端拒绝。没配 temperature 时只在非推理模式
        # 保留原来的 0.2 默认；开了 reasoning 就不发。
        if temperature is None and not reasoning:
            temperature = 0.2

        return cls(api_key=key, base_url=base.rstrip("/"), model=model,
                   source=source,
                   referer=(env.get("AI_SERVER_REFERER") or "").strip(),
                   title=(env.get("AI_SERVER_TITLE") or "").strip(),
                   temperature=temperature,
                   top_p=_env_number(env, "AI_SERVER_TOP_P", float, 0.0, 1.0),
                   max_tokens=_env_number(env, "AI_SERVER_MAX_TOKENS", int,
                                          1, 393216),
                   thinking=thinking,
                   reasoning_effort=effort,
                   reasoning=reasoning)


class AIError(RuntimeError):
    """模型调用失败。消息面向用户，不含供应商原文（可能回显 Key 片段）。"""


# 一次建议的模型本身只要几百毫秒，但国内到 OpenRouter 的 TLS 握手实测
# 0.4–1.4s —— urllib 每次 urlopen 都新建连接，等于每给一次建议就多付一次
# 握手钱。这里按 host 缓存一条 HTTP/1.1 长连接。只在没配代理时用：
# http.client 不会读 HTTPS_PROXY，配了代理就退回 urllib（保持原有行为）。
# 连接按**线程**缓存：HTTP/1.1 连接不是线程安全的，旧实现用一把全局锁把
# 整个请求（含 read）锁住，replay 的 unit_workers 并行因此退化成串行。
# 每个线程一条长连接既保住了握手复用，也让多 worker 真正并发。
_CONNS = threading.local()


def _thread_conns() -> dict:
    conns = getattr(_CONNS, "conns", None)
    if conns is None:
        conns = {}
        _CONNS.conns = conns
    return conns


def close_thread_connections() -> None:
    """关闭当前线程缓存的连接（测试/退出用；不影响其它线程）。"""
    conns = getattr(_CONNS, "conns", None)
    if not conns:
        return
    for conn in conns.values():
        try:
            conn.close()
        except Exception:                 # noqa: BLE001 — 收尾尽力而为
            pass
    conns.clear()
_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy")


def _proxy_configured() -> bool:
    return any(os.environ.get(k) for k in _PROXY_ENV)


def _post_keepalive(url: str, data: bytes, headers: dict,
                    timeout: float) -> bytes:
    parts = urllib.parse.urlsplit(url)
    https = parts.scheme == "https"
    key = (parts.scheme, parts.hostname or "",
           parts.port or (443 if https else 80))
    conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    conns = _thread_conns()
    conn = conns.get(key)
    if conn is None:
        conn = conn_cls(key[1], key[2], timeout=timeout)
        conns[key] = conn
    elif conn.sock is not None:
        conn.sock.settimeout(timeout)
    try:
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
    except BaseException:
        # 连接坏了不能留在缓存里，下一次还得付握手钱
        conn.close()
        conns.pop(key, None)
        raise
    if resp.will_close:
        conn.close()
        conns.pop(key, None)
    if resp.status >= 400:
        # 与 urllib 的口径一致：HTTP 错误带着 code 原样抛出，调用方决定
        # 401/403 脱敏、400 退回非 JSON 模式重试
        raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                     resp.headers, io.BytesIO(body))
    return body


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> bytes:
    """POST 一段 JSON 并返回原始响应体。

    `HTTPError` 原样抛出，由调用方决定怎么脱敏（401/403 不回显 body）；
    网络层错误统一转 AIError。
    """
    data = json.dumps(payload).encode("utf-8")
    if not _proxy_configured():
        try:
            return _post_keepalive(url, data, headers, timeout)
        except TimeoutError as exc:
            # 超时不能悄悄落到 urllib 再等一轮：调用方需要 AIError 才能出兜底建议。
            raise AIError(f"调用模型超时：{exc}") from exc
        except (http.client.HTTPException, ConnectionError):
            # 长连接被服务端按空闲超时关掉是最常见的情况：丢掉它，走 urllib
            # 新建连接重试一次。模型请求是幂等的选择题，重试不会产生副作用。
            pass
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIError(f"调用模型失败：{exc}") from exc


def _http_error(exc: urllib.error.HTTPError) -> AIError:
    # 不回显 body：供应商常在里面回显 Key 片段
    if exc.code == 401:
        return AIError("模型鉴权失败，请检查 AI_SERVER_API_KEY")
    if exc.code == 403:
        # 403 不一定是 Key 错：OpenRouter 对 OpenAI 模型常因账号/地区/
        # 供应商条款拒绝，Key 本身仍然有效。两者分开报才查得动。
        return AIError("模型服务拒绝访问（HTTP 403）：可能是账号、地区或供应商"
                       "条款限制，不一定是 Key 写错。OpenRouter 可在 Activity "
                       "页查看被拒原因")
    return AIError(f"模型服务返回 HTTP {exc.code}")


def chat_headers(settings: AISettings) -> dict:
    """OpenAI 兼容 /chat/completions 的请求头。

    `HTTP-Referer` / `X-OpenRouter-Title` 只在配置了 `AI_SERVER_REFERER` /
    `AI_SERVER_TITLE` 时发送（OpenRouter 用它做可选排名，其他供应商会忽略）。
    """
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + settings.api_key}
    if settings.referer:
        headers["HTTP-Referer"] = settings.referer
    if settings.title:
        headers["X-OpenRouter-Title"] = settings.title
    return headers


def chat_payload(settings: AISettings, messages: list[dict],
                 json_mode: bool = False) -> dict:
    """组装 /chat/completions 请求体。只发显式配置过的参数。

    `thinking` 是 DeepSeek 的字段；`reasoning` 是 OpenRouter 的字段
    （`{"enabled": true}`）。两者互不替代，按 `.env` 里实际配置的发。
    `reasoning_effort` 在 OpenRouter 下嵌进 reasoning.effort，其余供应商
    仍发顶层字段（DeepSeek 的口径）。
    """
    body = {"model": settings.model, "messages": messages, "stream": False}
    if settings.temperature is not None:
        body["temperature"] = settings.temperature
    if settings.top_p is not None:
        body["top_p"] = settings.top_p
    if settings.max_tokens is not None:
        body["max_tokens"] = settings.max_tokens
    if settings.thinking is not None:
        body["thinking"] = {"type": settings.thinking}
    if settings.reasoning is not None:
        reasoning: dict = {"enabled": settings.reasoning}
        if settings.reasoning_effort is not None:
            reasoning["effort"] = settings.reasoning_effort
        body["reasoning"] = reasoning
    elif settings.reasoning_effort is not None:
        body["reasoning_effort"] = settings.reasoning_effort
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def chat_message(settings: AISettings, messages: list[dict],
                 timeout: float = 20.0, request=None,
                 json_mode: bool = False, return_meta: bool = False):
    """POST `{BASE_URL}/chat/completions`，返回 assistant message 原对象。

    返回的 message 可能带 `reasoning` / `reasoning_details`。**多轮续推**时把
    上一条 assistant message 原样放回 `messages` 即可（`reasoning_details`
    不要裁剪或改写，OpenRouter 需要它来续接推理）::

        first = chat_message(settings, messages)
        messages += [{"role": "assistant",
                      "content": first.get("content"),
                      "reasoning_details": first.get("reasoning_details")},
                     {"role": "user", "content": "Are you sure?"}]
        second = chat_message(settings, messages)

    `json_mode` 打开时先带 `response_format=json_object` 请求；兼容层不认它
    会回 400，此时自动退回不带该字段重试一次（保留原有行为）。
    """
    url = settings.base_url + "/chat/completions"
    post = request or _post_json
    headers = chat_headers(settings)
    try:
        try:
            raw = post(url, chat_payload(settings, messages, json_mode),
                       headers, timeout)
        except urllib.error.HTTPError as exc:
            # 有些兼容层不认 response_format（回 400）。它只是个增强，
            # 退一步重来，而不是把整次建议判失败
            if exc.code != 400 or not json_mode:
                raise
            raw = post(url, chat_payload(settings, messages, False),
                       headers, timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc

    try:
        body = json.loads(raw)
        choice = body["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError("模型响应结构不对") from exc
    if not isinstance(message, dict):
        raise AIError("模型响应结构不对")
    if not return_meta:
        return message
    return message, {
        "usage": body.get("usage") if isinstance(body, dict) else None,
        "finish_reason": choice.get("finish_reason"),
    }


def complete_json(settings: AISettings, system: str, user: str,
                  timeout: float = 20.0,
                  request=None, history: list[dict] | None = None) -> dict:
    """调用 OpenAI 兼容的 `/chat/completions` 并解析成 `{option, reason}`。

    失败抛 AIError。这里刻意不引 SDK、不走流式：一次只回几十个 token，
    urllib 足够，也避免给实时链路增加依赖。

    `history` 是多轮对话里位于 system 之后、本轮 user 之前的消息。要续接
    推理模型的思考，把上一次的 assistant message（含 `reasoning_details`）
    原样放进去；返回值也会带上本次的 `reasoning_details`。

    `response_format=json_object` 能显著降低「返回一段带 markdown 围栏的
    散文」的概率；供应商不认这个字段时会自动退回普通模式重试一次。
    """
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})

    message, meta = chat_message(settings, messages, timeout=timeout,
                                 request=request, json_mode=True,
                                 return_meta=True)
    content = message.get("content")
    if not isinstance(content, str):
        # 推理模型偶尔只回 reasoning、content 为空；这不能当成合法选项
        raise AIError("模型响应结构不对（content 为空）")

    data = parse_json_content(content)
    # 保留原样的 reasoning_details：调用方续推时直接回传，不做任何改写
    if message.get("reasoning_details") is not None:
        data["reasoning_details"] = message["reasoning_details"]
    # 观测字段：prompt/ completion token 与是否被 max_tokens 截断。
    data["_meta"] = meta
    return data


def parse_json_content(content: str) -> dict:
    """解析模型正文。允许被 ```json 围栏包住（有些供应商不认 response_format）。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        data = json.loads(text)
    except ValueError:
        # 最后兜一道：供应商偶尔在 JSON 前后加一句废话。只取第一个 { 到最后一个 }
        lo, hi = text.find("{"), text.rfind("}")
        if lo < 0 or hi <= lo:
            raise AIError("模型返回的不是合法 JSON")
        try:
            data = json.loads(text[lo:hi + 1])
        except ValueError as exc:
            raise AIError("模型返回的不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise AIError("模型返回的 JSON 不是对象")
    return data

# ─────────────────────────── 兜底 ───────────────────────────

def _rule_fallback(sit: Situation) -> tuple[str, str] | None:
    """翻后兜底：用 coach 规则引擎产出一个可执行的 option id。

    只在「模型不可用」时调用。翻前没有规则引擎（coach 只覆盖 flop/turn/river），
    底池/后手缺失、牌不合法时 coach 返回 ok=False，这里一律放弃、交给最保守口径。
    """
    snap = sit.snap
    if snap.street == "preflop":
        return None
    if not snap.hero_cards or len(snap.board) < 3:
        return None
    if snap.pot is None or snap.pot <= 0:
        return None
    from .coach import advise as rule_advise
    from .situation import _coach_actions

    try:
        d = rule_advise(
            list(snap.hero_cards),
            "".join(str(c) for c in snap.board[:5]),
            actions=_coach_actions(snap),
            street=snap.street,
            pot=float(snap.pot),
            hero_position=snap.hero_position,
            opponents=snap.opponents_in_hand,
            stack_bb=snap.hero_stack,
            hero_invested_bb=snap.hero_invested,
        )
    except (ValueError, KeyError):
        return None
    if not d.ok or not d.action:
        return None

    ids = {o.id for o in sit.options}
    if d.action == "fold":
        return ("fold", "规则引擎：牌力与赔率不支持继续") if "fold" in ids else None
    if d.action == "check":
        return ("call", "规则引擎：过牌看下一张") if "call" in ids else None
    if d.action == "call":
        return ("call", "规则引擎：赔率支持跟注") if "call" in ids else None
    # bet / raise：挑最接近 sizing 的档位；没有 sizing 就用 1/3 池
    sized = [o for o in sit.options
             if o.kind in ("bet", "raise") and o.amount is not None]
    if not sized:
        return None
    target = float(d.sizing_bb or 0.0) or float(snap.pot) / 3.0
    best = min(sized, key=lambda o: abs(float(o.amount) - target))
    return (best.id, "规则引擎：按牌力与底池尺度下注")


def fallback_option(sit: Situation) -> str:
    """模型不可用时的确定性兜底。

    刻意**只做最保守的选择**，不试图复刻技能库的完整规则（那是 coach.py 的事）：
    面对下注且读不出牌力 → 弃牌；否则 → 过牌。宁可给一个保守答案，
    也不给一个看着像样却是错的建议。
    """
    if sit.snap.to_call > 0:
        return "fold"
    return "call"           # to_call == 0 时 call 这个 id 就是「过牌」


# ─────────────────────────── 结果 ───────────────────────────

def _confidence(value) -> float | None:
    """模型自评置信度：只接受 0–1；别把 "72%" 这种字符串当数用。"""
    if isinstance(value, str) and value.strip().endswith("%"):
        value = value.strip()[:-1]
        try:
            value = float(value) / 100
        except ValueError:
            return None
    try:
        got = float(value)
    except (TypeError, ValueError):
        return None
    return round(got, 3) if 0.0 <= got <= 1.0 else None


@dataclass(frozen=True)
class Advice:
    hand_id: str
    street: str
    option: Option
    reason: str
    source: str                      # model | fallback
    model: str | None = None
    # 旧字段保留一版，恒为空：避免旧 JSONL 消费者读不到键而报错。
    skill_codes: tuple[str, ...] = ()
    duration_ms: int = 0
    error: str | None = None
    pot: float | None = None
    to_call: float = 0.0
    hero_cards: tuple[str, ...] = ()
    board: tuple[str, ...] = ()
    # 决策点身份与五个快捷档位的当前目标额。点击器用前者判过期、用后者做
    # 「翻前建议 → 最近底池档位」映射；不写进 JSONL，保持输出形状稳定。
    decision_key: tuple = ()
    preset_amounts: tuple[tuple[str, float], ...] = ()
    # 出这条建议时用到的对手画像快照（seat/type/conf/n/source）。空 = 没有画像。
    opponent_reads: tuple[OpponentRead, ...] = ()
    # 本手第几个 hero 决策点。与 replay 的 DecisionPoint.action_index 对齐。
    action_index: int = 0
    # 出这条建议时的桌况快照：table_stats/table_dynamics/roster 版本等。
    # 手牌库/A-B 用它在重跑时复现线上上下文；空 = 旧记录。
    context: dict = field(default_factory=dict)
    # 模型给的下一街意图与自评置信度。新 prompt 不再要求 plan，通常为空。
    plan: str = ""
    confidence: float | None = None
    # 生成这条建议时的中文 lean 提示词版本，复盘归因用。
    prompt_version: str = PROMPT_VERSION
    # 模型返回的结束原因与 token 用量；只有 chat/completions 路径有。
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # 模型用了别名（check/bet-1/3/allin）时记下原词，便于发现 id 歧义。
    alias_from: str | None = None
    # 模型动作与规则引擎的交叉校验结论：ok | mild | severe。
    # severe 时自动点击默认拦下（见 live._click_skip_reason）。
    cross_check: str | None = None

    # ── 复盘/手牌库：真正发给模型的 prompt 与原始响应 ──
    # prompt_version 只是版本号，下面的字段才是「模型当时看到了什么」。
    # 旧 JSONL 消费者会忽略这些键；空值不写入 to_dict()。
    prompt_system: str | None = None
    prompt_user: str | None = None
    prompt_sha256: str | None = None
    raw_response: str | None = None
    response_sha256: str | None = None
    reason_full: str | None = None
    facts: dict = field(default_factory=dict)
    model_params: dict = field(default_factory=dict)
    captured_at: str | None = None
    prompt_missing: bool = False
    prompt_missing_reason: str | None = None

    def to_dict(self) -> dict:
        reads = [_read_snapshot(r) for r in self.opponent_reads]
        return {
            "hand_id": self.hand_id,
            "street": self.street,
            "hero_cards": list(self.hero_cards),
            "board": list(self.board),
            "pot": self.pot,
            "to_call": round(self.to_call, 2),
            "option": self.option.to_dict(),
            "reason": self.reason,
            "source": self.source,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "prompt_lang": PROMPT_LANGUAGE,
            "skill_codes": list(self.skill_codes),
            "duration_ms": self.duration_ms,
            **({"error": self.error} if self.error else {}),
            # 复盘时能回答「当时用了哪个对手的什么类型/置信度/样本数」。
            **({"opponent_reads": reads} if reads else {}),
            **({"action_index": self.action_index}
               if self.action_index else {}),
            **({"context": self.context} if self.context else {}),
            **({"plan": self.plan} if self.plan else {}),
            **({"confidence": round(self.confidence, 3)}
               if self.confidence is not None else {}),
            **({"finish_reason": self.finish_reason}
               if self.finish_reason else {}),
            **({"prompt_tokens": self.prompt_tokens}
               if self.prompt_tokens is not None else {}),
            **({"completion_tokens": self.completion_tokens}
               if self.completion_tokens is not None else {}),
            **({"alias_from": self.alias_from}
               if self.alias_from else {}),
            **({"cross_check": self.cross_check}
               if self.cross_check else {}),
            # 只有真的捕获到才写，保持旧记录形状稳定。
            **({"prompt_system": self.prompt_system}
               if self.prompt_system else {}),
            **({"prompt_user": self.prompt_user}
               if self.prompt_user else {}),
            **({"prompt_sha256": self.prompt_sha256}
               if self.prompt_sha256 else {}),
            **({"raw_response": self.raw_response}
               if self.raw_response else {}),
            **({"response_sha256": self.response_sha256}
               if self.response_sha256 else {}),
            **({"reason_full": self.reason_full}
               if self.reason_full else {}),
            **({"facts": self.facts} if self.facts else {}),
            **({"model_params": self.model_params}
               if self.model_params else {}),
            **({"captured_at": self.captured_at}
               if self.captured_at else {}),
            **({"prompt_missing": True} if self.prompt_missing else {}),
            **({"prompt_missing_reason": self.prompt_missing_reason}
               if self.prompt_missing_reason else {}),
        }


# 模型偶尔会按语义造 id：过牌写成 check、下注写成 bet-1/3、全下写成 allin。
# 这些都是同一个动作，不该因为 id 拼写不同就退化成弃牌/过牌；记下别名，
# 用日志反过来找 prompt 里没写清楚的地方。
_OPTION_ALIASES = {
    "check": "call",
    "allin": "all-in",
    "all_in": "all-in",
    "bet-1/3": "raise-1/3",
    "bet-1/2": "raise-1/2",
    "bet-2/3": "raise-2/3",
    "bet-1": "raise-1",
    "bet-1.2": "raise-1.2",
}


_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bb)?", re.IGNORECASE)


def _action_hint(text: str) -> str | None:
    """从自然语言里认出动作类别（用于金额匹配时消歧）。"""
    key = (text or "").lower().replace(" ", "")
    if any(word in key for word in ("全下", "allin", "all-in", "shove", "jam")):
        return "allin"
    if any(word in key for word in ("弃牌", "fold")):
        return "fold"
    if any(word in key for word in ("过牌", "check")):
        return "check"
    if any(word in key for word in ("跟注", "补齐", "平跟", "call", "limp")):
        return "call"
    if any(word in key for word in ("开池", "加注", "下注", "raise", "bet", "open")):
        return "raise"
    return None


def _amount_choice(raw: str, by_id: dict) -> str | None:
    """按「金额」匹配选项：模型常把 label 里的金额写成答案（`加注到 5.5BB`）。

    只在**唯一**能对上时才认；金额重复（封顶后多个档位同一数）或没有动作
    提示时返回 None，让调用方走兜底，避免猜错动作。
    """
    amounts = [float(m.group(1)) for m in _AMOUNT_RE.finditer(raw or "")]
    if not amounts:
        return None
    amount = amounts[-1]
    cands = [o for o in by_id.values()
             if o.amount is not None and abs(o.amount - amount) <= 0.011]
    if not cands:
        return None
    hint = _action_hint(raw)
    if hint == "raise":
        narrowed = [o for o in cands if o.kind in ("raise", "bet")]
    elif hint == "allin":
        narrowed = [o for o in cands if o.kind == "allin"]
    elif hint is not None:
        narrowed = [o for o in cands if o.kind == hint]
    else:
        narrowed = cands
    cands = narrowed or cands
    return cands[0].id if len(cands) == 1 else None


def _canonical_choice(choice: str, by_id: dict) -> tuple[str, str | None]:
    """把常见别名映射回合法 id；返回 (canonical, alias_from)。

    除固定别名外，还接受三类自然写法：``open-5.5``（按金额）、
    完整 label（``加注到 5.5BB``）和裸金额（``5.5BB``）。这样模型按 label
    回答案时不会因为 id 是「基础尺度」而直接兜底（翻前兜底 = 弃牌）。
    """
    raw = (choice or "").strip()
    if raw in by_id:
        return raw, None
    key = raw.lower().replace(" ", "")
    target = _OPTION_ALIASES.get(key)
    if target is None and key.startswith("bet-"):
        target = "raise-" + key[4:]
    if target is None and (key.startswith("open-") or key.startswith("raise-")):
        target = _amount_choice(raw, by_id)
    if target is None:
        # 完整 label（或 label 的关键片段）也能认：模型最常抄的就是它。
        exact = [o.id for o in by_id.values()
                 if raw and (o.label == raw or o.label.startswith(raw))]
        if len(exact) == 1:
            target = exact[0]
    if target is None:
        target = _amount_choice(raw, by_id)
    if target not in by_id:
        return raw, None
    if key == "check" and by_id[target].kind != "check":
        return raw, None
    if key.startswith("bet") and by_id[target].kind not in ("bet", "raise"):
        return raw, None
    return target, raw


def _clip_reason(text: str, limit: int = 60) -> str:
    """reason 截到 limit 字：优先在标点处断，避免 JSON 被长理由撑爆。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in "。；，、":
        idx = cut.rfind(mark)
        if idx >= limit // 2:
            return cut[:idx + 1]
    return cut[:max(1, limit - 1)] + "…"


def _read_snapshot(read: OpponentRead) -> dict:
    """画像快照：顶层 `opponent_reads` 与 `context.opponent_reads` 共用一份。

    打法数字（百分比 + 机会数）必须一起存：攻击块的逐座位弃牌率证据、
    画像的「（N 次）」都从这里还原，漏存会让复盘/A-B 的结论和线上不同。
    """
    item = {
        "seat_id": read.seat_id, "key": read.key,
        "display_name": read.display_name, "archetype": read.archetype,
        "confidence": read.confidence, "n": read.n,
        "vpip": read.vpip, "pfr": read.pfr, "af": read.af,
        "source": read.source, "gate": read.gate,
        "prior_dominated": list(read.prior_dominated),
        "opps": dict(read.opps),
        "inferred_ratio": read.inferred_ratio,
    }
    item.update({name: getattr(read, name) for name in TENDENCY_PCT_FIELDS
                 if getattr(read, name) is not None})
    return item


def _advice_context(sit: Situation) -> dict:
    """线上建议的上下文快照：重跑/A-B 必须看到和线上同一份桌况。"""
    stats = sit.table_stats
    dynamics = (sit.table_dynamics or "").strip()
    reads = [_read_snapshot(read) for read in sit.opponent_reads]
    context = {
        "table_dynamics": dynamics or None,
        "table_dynamics_sha": (hashlib.sha256(dynamics.encode("utf-8")).hexdigest()
                               if dynamics else None),
        "table_stats": pooled_stats_to_dict(stats),
        "table_stats_scope": stats.scope if stats is not None else None,
        "opponent_reads": reads or None,
    }
    return {key: value for key, value in context.items()
            if value not in (None, "", {})}


def _sha256_text(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_digest(system: str, user: str) -> str:
    """与 replay 侧同一口径：system + 固定分隔符 + user。"""
    return hashlib.sha256(
        (system + "\n---\n" + user).encode("utf-8")).hexdigest()


def _facts_snapshot(sit: Situation, options=None) -> dict:
    """只服务复盘/审计的轻量事实快照。

    不 import replay.decisions，避免 live 反向依赖控制台；字段与
    `DecisionPoint` 的 facts 保持同名/同义，方便前端并排看。
    """
    snap = sit.snap
    option_space = list(sit.options) if options is None else list(options)
    facts = {
        "hand_tier": sit.hand_tier,
        # 这次注入的是哪套牌型规则（听牌/成牌/空气）；与 prompt_version 一起
        # 用于复盘归因「建议变了是提示词哪一支换了」。
        "hand_class": sit.hand_playing_class,
        "hand_label": sit.hand_label,
        "hand_label_en": sit.hand_label_en,
        "equity_pct": (round(sit.equity_pct, 1)
                       if sit.equity_pct is not None else None),
        "pot": snap.pot,
        "to_call": round(float(snap.to_call or 0.0), 2),
        "hero_stack": snap.hero_stack,
        "pot_type": sit.pot_type,
        "spr": sit.spr,
        "opponents_in_hand": sit.opponents_in_hand,
        "range_verdict": sit.range_verdict,
        "explosive_pot": bool(sit.explosive_pot),
        "bomb_pot": bool(snap.bomb_pot),
        "warnings": (
            list(sit.warnings or ())
            # 未映射的牌面告警只进这里（复盘可查），不进 prompt：
            # 模型看到「另有 N 条未展开」既读不懂也用不上。
            + [f"board warning unmapped: {w}"
               for w in unmapped_gt_warnings(sit.ground_truth)]
        ),
        "options": [option.to_dict() for option in option_space],
    }
    # 事实漂移 vs 模型漂移：没有这几个数，A/B 里动作变了只能猜。范围/条件
    # 弃牌率/牌面排名都是「当时模型看到的数」，一起留档。
    hr = getattr(sit, "hand_ranges", None)
    if hr is not None:
        facts["hand_ranges"] = {
            "equity": round(hr.equity, 4),
            "behind_any": round(hr.behind_any, 4),
            "tie_any": round(hr.tie_any, 4),
            "samples": hr.samples,
            "random_opponents": getattr(hr, "random_opponents", 0),
            "opponents": [
                {"seat": e.seat_id, "position": e.position,
                 "range": e.range_id,
                 "width": round(e.eff_width_pct or e.width_pct, 2),
                 "behind": round(e.behind, 4), "tie": round(e.tie, 4)}
                for e in hr.opponents
            ],
        }
    fe = getattr(sit, "fold_eq", None)
    if fe is not None:
        facts["fold_eq"] = {
            "fold_equity": round(fe.fold_equity, 4),
            "observed": (round(fe.observed, 4)
                         if fe.observed is not None else None),
            "combos": fe.combos,
            "opponents": fe.opponents,
            "lines": [
                {"label": line.label, "size": line.size,
                 "fold_equity": round(line.fold_equity, 4),
                 "breakeven": round(line.breakeven, 4),
                 "ev": round(line.ev, 3)}
                for line in fe.lines
            ],
        }
    gt = getattr(sit, "ground_truth", None)
    if gt is not None:
        facts["ground_truth"] = {
            "made": gt.made.name,
            "detail": gt.made.detail,
            "nuts": gt.hero_is_nuts,
            "beat_pct": round(gt.rank.beat_pct, 4),
            "beat_at_least_pct": round(gt.rank.beat_at_least_pct, 4),
            "draws": {
                "flush": gt.draws.flush_draw,
                "straight": gt.draws.straight_draw,
                "outs": gt.draws.total_outs,
            },
        }
    return {key: value for key, value in facts.items()
            if value not in (None, "", [], {})}


def _model_params(settings: AISettings | None) -> dict:
    """可公开的采样参数快照；永远不写 api_key。"""
    if settings is None:
        return {}
    params = {
        "model": settings.model,
        "base_url": settings.base_url,
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "max_tokens": settings.max_tokens,
        "thinking": settings.thinking,
        "reasoning_effort": settings.reasoning_effort,
        "reasoning": settings.reasoning,
    }
    return {key: value for key, value in params.items()
            if value is not None and value != ""}


def advise(sit: Situation, settings: AISettings | None,
           timeout: float = 20.0, complete=None,
           prompt_override: tuple[str, str] | None = None,
           option_space: list[Option] | None = None) -> Advice:
    """出一次建议。`complete` 可注入（单测用），默认走 complete_json。

    只有一套中文 lean：不注入 Math gate，不做弃牌重问，不加载技能库。
    模型调用发生的那一刻，把 system/user/raw/reason_full 一起带进 Advice；
    没有调用模型（未配置/异常帧）时显式标 prompt_missing。

    `prompt_override=(system, user)` 用于 A/B 的 baseline：直接重放线上/历史
    prompt 原文，跳过 `build_messages()`；选项校验、reason 截断、fallback
    仍走同一套逻辑。

    `option_space` 是 baseline 的**存储选项空间**：重放旧 prompt 时，当前代码
    的选项 id/金额可能已经变了；用存储的那一套校验，模型按旧 prompt 回的旧
    id 才不会被误判成「不在选项里」而兜底（否则 baseline 被系统性判差）。
    """
    started = time.monotonic()
    options = list(option_space) if option_space else list(sit.options)
    by_id = {o.id: o for o in options}

    def fallback() -> tuple[Option, str, str]:
        """模型不可用时的确定性兜底：翻后先用规则引擎，其余按最保守。"""
        hit = _rule_fallback(sit)
        if hit is not None:
            oid, reason = hit
            option = by_id.get(oid) or next(
                (o for o in sit.options if o.id == oid), None)
            if option is not None:
                return option, reason, "fallback_rules"
        oid = fallback_option(sit)
        reason = "未配置模型/模型不可用，按最保守的默认动作"
        if oid in by_id:
            return by_id[oid], reason, "fallback"
        return (next((o for o in sit.options if o.id == oid), sit.options[0]),
                reason, "fallback")
    prompt_holder: dict[str, str | None] = {}
    raw_holder: dict[str, str | None] = {}

    def make(option: Option, reason: str, source: str,
             error: str | None = None, *, plan: str = "",
             confidence: float | None = None,
             alias_from: str | None = None,
             meta: dict | None = None,
             prompt_missing: bool = False,
             prompt_missing_reason: str | None = None,
             reason_full: str | None = None,
             cross_check: str | None = None) -> Advice:
        snap = sit.snap
        meta = meta or {}
        usage = meta.get("usage") or {}
        raw = raw_holder.get("raw")
        system = prompt_holder.get("system")
        user = prompt_holder.get("user")
        return Advice(
            snap.hand_id, snap.street, option, reason, source,
            settings.model if settings else None,
            (),
            int((time.monotonic() - started) * 1000),
            error, snap.pot, snap.to_call,
            snap.hero_cards, snap.board,
            decision_key=snap.decision_key,
            preset_amounts=tuple(preset_amounts(snap).items()),
            opponent_reads=tuple(sit.opponent_reads),
            action_index=sit.action_index,
            context=_advice_context(sit),
            plan=plan, confidence=confidence,
            prompt_version=PROMPT_VERSION,
            finish_reason=meta.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            alias_from=alias_from,
            prompt_system=system,
            prompt_user=user,
            prompt_sha256=prompt_holder.get("sha"),
            raw_response=raw,
            response_sha256=_sha256_text(raw),
            reason_full=reason_full or reason,
            facts=_facts_snapshot(sit, options),
            model_params=_model_params(settings),
            captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            prompt_missing=prompt_missing,
            prompt_missing_reason=prompt_missing_reason,
            cross_check=cross_check,
        )

    if settings is None:
        option, why, source = fallback()
        return make(option, why, source,
                    "未配置模型凭据",
                    prompt_missing=True,
                    prompt_missing_reason="no_settings")
    if sit.abnormal_no_opponents:
        # 0 对手异常帧不调模型：任何牌力/赔率/筹码数字都是伪事实。
        option, why, source = fallback()
        return make(option, why, source,
                    "状态帧异常：在局对手为 0",
                    prompt_missing=True,
                    prompt_missing_reason="abnormal_no_opponents")

    system, user = (prompt_override if prompt_override
                    else build_messages(sit))
    prompt_holder.update({
        "system": system,
        "user": user,
        "sha": _prompt_digest(system, user),
    })
    caller = complete or complete_json

    def ask(prompt: str) -> dict:
        data = caller(settings, system, prompt, timeout=timeout)
        try:
            raw_holder["raw"] = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_holder["raw"] = str(data)
        return data

    try:
        data = ask(user)
        choice, alias_from = _canonical_choice(
            str(data.get("option", "")).strip(), by_id)
        if choice not in by_id:
            raise AIError(f"模型给了不在选项里的答案：{data.get('option')!r}")
        raw_reason = " ".join(str(data.get("reason", "")).split())
        reason = _clip_reason(raw_reason or "（模型未给理由）")
        plan = str(data.get("plan", "") or "").strip()[:160]
        cc = _cross_action(sit, by_id[choice])
        return make(by_id[choice], reason, "model", plan=plan,
                    confidence=_confidence(data.get("confidence")),
                    alias_from=alias_from,
                    meta=data.get("_meta") if isinstance(data, dict) else None,
                    reason_full=raw_reason or None,
                    cross_check=cc.conflict)
    except AIError as exc:
        # prompt 已经构建/发出去过，必须保留；raw 可能为空（连接失败）。
        option, why, source = fallback()
        return make(option, why, source, str(exc))


# ─────────────────────────── 后台执行 ───────────────────────────

class AdviceRunner:
    """把模型调用放到后台线程，**绝不阻塞 OCR 主循环**。

    实时链路一帧约 0.5s，模型一次要几秒；同步调用会让采样停顿、画面延迟越积越大。
    这里只保留「最新一个待办」：新的决策点覆盖旧的，旧结果仍然返回并落库
    （它可能已经过时，但复盘时有用）。
    """

    def __init__(self, settings: AISettings | None, *, timeout: float = 20.0,
                 complete=None, log=None) -> None:
        import queue
        import threading

        self.settings = settings
        self.timeout = timeout
        self._complete = complete
        self._log = log or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._done: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        # 模型调用进行中。主循环据此暂停识别 —— 等待期间画面只会越来越旧
        self._busy = threading.Event()
        self._busy_since: float | None = None
        self._dropped = 0
        self._thread = threading.Thread(target=self._loop, name="advice",
                                        daemon=True)
        self._thread.start()

    def submit(self, sit: Situation) -> bool:
        """投递一个决策点。队列里已有待办时**用新的覆盖旧的**（最新局面更重要）。"""
        import queue

        try:
            self._queue.put_nowait(sit)
            return True
        except queue.Full:
            # 丢掉还没开始的旧决策点，换成最新的。取走与放回之间 worker
            # 可能刚好取走队列，所以放回也要允许失败
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(sit)
                self._dropped += 1
                return True
            except queue.Full:
                return False

    def poll(self) -> list[Advice]:
        """主循环里调用：取走已完成的结果。"""
        import queue

        out: list[Advice] = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    def _loop(self) -> None:
        import queue

        while not self._stop.is_set():
            try:
                sit = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if sit is None:
                return
            self._busy_since = time.monotonic()
            self._busy.set()
            try:
                try:
                    advice = advise(sit, self.settings, self.timeout,
                                    self._complete)
                except Exception as exc:  # noqa: BLE001 — 后台线程不能死
                    self._log(f"[建议] 生成失败：{type(exc).__name__}: {exc}")
                    continue
                self._done.put(advice)
            finally:
                self._busy_since = None
                self._busy.clear()

    def close(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Exception:                 # noqa: BLE001 — 队列满就直接退出
            pass
        self._thread.join(timeout=2.0)

    @property
    def busy(self) -> bool:
        """模型调用进行中（从开始调用到结果放进 `_done`）。"""
        return self._busy.is_set()

    @property
    def busy_seconds(self) -> float:
        """这一次模型调用已经跑了多少秒；不在调用中时为 0。

        主循环用它打「生成中…」心跳：等模型时 OCR 是暂停的，没有这行日志，
        控制台会先静默几秒、再把建议和恢复后的识别日志一次性吐出来。
        """
        since = self._busy_since
        return max(time.monotonic() - since, 0.0) if since is not None else 0.0

    @property
    def dropped(self) -> int:
        return self._dropped
