#!/usr/bin/env python3
"""牌桌识别 HTTP API。

    .venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000

接口文档（自动生成，可直接给对接方看）：http://localhost:8000/docs

    POST /ocr/table   上传一张牌桌截图，返回桌号 + 每个座位的位置/名称/筹码
    GET  /health      就绪探针（模型加载完才算就绪）

设计要点：
  - OCR 模型在启动时加载一次，逐请求复用。加载要一两秒，放进请求里会让首调超时。
  - 推理串行化（一把锁）。onnxruntime 的 session 不保证线程安全，
    并发跑同一份模型可能互相踩内存。
  - 接口本身是同步函数，FastAPI 会把它丢进线程池，不阻塞事件循环。
  - 开了 CORS。浏览器跨域调时先发 OPTIONS 预检，没有中间件的话会被
    路由判成方法不对而返回 405，见下面 add_middleware 处的注释。
"""

from __future__ import annotations

import dataclasses
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from poker_ocr.coach import advise
from poker_ocr.dealer import build_detector
from poker_ocr.ocr import OcrEngine
from poker_ocr.pipeline import FrameResult, extract
from poker_ocr.profile import DEFAULT_PROFILE, Profile

# 上传体积上限。截图 1440x3200 也就 3MB 上下，20MB 足够宽松又能挡住
# 明显的恶意大文件（解码一张几亿像素的图会直接把内存打满）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


# ─────────────────────────── 响应模型 ───────────────────────────

class SeatOut(BaseModel):
    name: str | None = Field(None, description="玩家昵称。读不出时为 null")
    position: str | None = Field(
        None, description="位置名：SB/BB/UTG/UTG+1/UTG+2/LJ/HJ/CO/BTN。"
                          "没找到庄位推不出来时为 null")
    stack: int | float | None = Field(
        None, description="筹码数量，纯数字。读不出时为 null。单位见 stack_unit")
    is_me: bool = Field(False, description="是不是我自己（界面上底部中间那个座位）")


class ActionIn(BaseModel):
    actor: str = Field(..., description="行动的人，位置名或昵称")
    action: str = Field(..., description="check | bet | call | raise | fold | allin")
    amount: float | None = Field(
        None, description="bet/raise 用「加到总额」口径：bet 4 = 投到 4，"
                          "raise 12 = 加到 12。这样「我下注 4 被加到 12」要额外跟的是 12−4")
    street: str = Field(..., description="preflop | flop | turn | river")


class CoachIn(BaseModel):
    hero_cards: str = Field(..., description="我的手牌，如 KhQh")
    board: str = Field(..., description="公共牌，如 Ks7d2c")
    street: str = Field("flop", description="flop | turn | river")
    pot: float = Field(..., gt=0, description="轮到我行动时桌面上的底池总额（BB）"
                                              "（含我之前的下注与对手的加注）。赔率与尺度都用它")
    actions: list[ActionIn] = Field(
        default_factory=list, description="前面的行动，含翻前与本街，按发生顺序")
    hero_position: str | None = Field(None, description="我的位置名，判断是否 PFR 用")
    opponents: int | None = Field(None, description="对手数；不给就从 actions 推")
    read: str = Field("未知", description="对手形象：松弱/紧弱/松凶/紧凶/未知")
    stack_bb: float | None = Field(
        None, description="有效筹码（BB）。转牌跟注的隐含赔率、套池比例的分母都用它")
    hero_invested_bb: float | None = Field(
        None, description="我已投入底池的总筹码（BB）。≥3/4×stack_bb 即「套池」，不允许弃牌")


class CoachOut(BaseModel):
    ok: bool = Field(..., description="false 表示落在覆盖范围外，没有给建议")
    action: str | None = Field(None, description="fold | check | call | bet | raise")
    sizing_bb: float | None = Field(None, description="下注/加注的目标量（BB）")
    hand_label: str | None = Field(None, description="我的牌力，如「顶对顶踢」")
    hand_tier: str | None = Field(None, description="规则表用的分档键")
    texture: str | None = Field(None, description="牌面质地：干燥/花面/顺面/花顺面/公对面")
    reason: str | None = Field(None, description="一句话理由，含尺度数字")
    opponent_strength: dict | None = Field(
        None, description="从对手动作读出的牌力 {value:0-100, label:弱/中/强/极强, reason}")
    warnings: list[str] = Field(default_factory=list, description="数据不足等提示")


class TableOut(BaseModel):
    table_size: int = Field(..., description="几人桌，等于 seats 的长度（只数入局的玩家）")
    stack_unit: str = Field("BB", description="stack 的单位。界面按 BB 显示，所以原样透出")
    seats: list[SeatOut] = Field(
        ..., description="从我（hero）开始，沿行动方向绕一圈。hero 恒在第一个")
    warnings: list[str] = Field(
        default_factory=list, description="识别不完整的原因。空数组表示一切正常")


# ─────────────────────────── 应用状态 ───────────────────────────

class _State:
    profile: Profile | None = None
    engine: OcrEngine | None = None
    lock = threading.Lock()          # 串行化推理，见模块注释
    ready = False


STATE = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE.profile = Profile.load(DEFAULT_PROFILE)
    STATE.engine = OcrEngine()
    STATE.ready = True
    yield
    STATE.ready = False


app = FastAPI(
    title="牌桌识别 API",
    version="1.0",
    description="上传一张 9 人德州扑克桌截图，返回桌号与每个座位的名称/位置/筹码。",
    lifespan=lifespan,
)

# 跨域。前端页面跟 API 不同源时，浏览器在真正发 POST 之前会先发一个
# OPTIONS 预检请求；没有这个中间件，OPTIONS 会被路由当成「方法不对」，
# 直接 405，请求根本到不了业务函数。
#
# 这里**不能**写 allow_origins=["*"]：前端是带 withCredentials 调的，
# 浏览器的规定是「凭证模式下 Allow-Origin 必须是具体域名」，回 * 会被
# 直接拒掉（报 must not be the wildcard '*' when credentials mode is 'include'）。
# 所以只能列白名单，前端加域名就往这里加一行。
# 两个坑：① 结尾**不能带斜杠** —— 浏览器的 Origin 头永远不带，带上就永远
#            匹配不上，预检照样失败；
#         ② allow_credentials=True 和 "*" 互斥，这是浏览器的规定，不是 FastAPI 的。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://call.qwnet.top",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,             # 预检结果缓存 10 分钟，别让每个请求都多一轮 OPTIONS
)


# ─────────────────────────── 转换 ───────────────────────────

def to_table_out(result: FrameResult, profile: Profile) -> TableOut:
    """把管线结果收敛成对外的精简结构。"""
    seats: list[SeatOut] = []
    for s in result.seats:
        if not s.in_hand:
            continue          # 不在场的不返回：空座位、以及筹码没读出来的座位

        seats.append(SeatOut(
            name=s.name,
            position=s.position,
            stack=s.stack,
            is_me=(s.seat_id == profile.hero_seat_id),
        ))

    warnings: list[str] = []
    if result.dealer.status != "found":
        warnings.append({
            "not_found": "未检出庄位，位置推不出来",
            "ambiguous": "庄位有多个候选，无法确定",
        }.get(result.dealer.status, f"庄位状态异常：{result.dealer.status}"))
    if any(s.position is None for s in result.seats if s.in_hand):
        warnings.append("有座位没拿到位置")
    unread_name = [s.seat_id for s in result.seats if s.in_hand and not s.name]
    if unread_name:
        warnings.append(f"这些座位没读出名字：{', '.join(unread_name)}")
    review = [s.seat_id for s in result.seats if s.in_hand and s.name_review]
    if review:
        warnings.append(f"这些座位的名字置信度偏低，建议复核：{', '.join(review)}")
    # 筹码读不出来的座位按空座位处理、不返回，所以这句必须说清"算了几个人"——
    # 不然下游看到 seats 少了会以为漏识别（uncertain 的那些也在这里一起交代）
    dropped = [s.seat_id for s in result.seats
               if not s.in_hand and s.status != "empty" and s.seat_id != profile.hero_seat_id]
    if dropped:
        suspect = [s.seat_id for s in result.seats
                   if s.seat_id in dropped and s.status == "uncertain"]
        warnings.append(
            f"这些座位没读出筹码，已按空座位处理（没计入人数）：{', '.join(dropped)}"
            + (f"；其中 {', '.join(suspect)} 疑似有人但名字也读不出" if suspect else "")
        )
    # hero 例外：他算在场，但筹码得由用户补
    hero = next((s for s in result.seats if s.seat_id == profile.hero_seat_id), None)
    if hero is not None and hero.in_hand and hero.stack is None:
        warnings.append("hero 座位没读出筹码：仍算在场，这个数请你手动补")

    return TableOut(
        table_size=result.table_size,
        stack_unit="BB",
        seats=seats,
        warnings=warnings,
    )


def decode_image(raw: bytes) -> np.ndarray:
    if not raw:
        raise HTTPException(status_code=400, detail="上传内容为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"图片超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限")
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="解不开这张图，确认是 PNG/JPEG 图片")
    return img


def resolve_profile(base: Profile, hero_seat: str | None) -> Profile:
    if not hero_seat:
        return base
    try:
        base.seat(hero_seat)
    except KeyError:
        raise HTTPException(status_code=400,
                            detail=f"hero_seat={hero_seat} 不是合法座位，"
                                   f"可选：{[s.seat_id for s in base.seats]}")
    return dataclasses.replace(base, hero_seat_id=hero_seat)


# ─────────────────────────── 接口 ───────────────────────────

@app.get("/health", summary="就绪探针")
def health() -> dict:
    return {"ready": STATE.ready}


@app.post("/coach/advice", response_model=CoachOut, summary="翻后建议（规则引擎，不走模型）")
def coach_advice(payload: CoachIn) -> CoachOut:
    """确定性规则给出的建议，不加载任何模型 —— 所以**不受** STATE.ready 约束。

    覆盖翻牌/转牌/河牌三条街。落在覆盖范围外（street 非法、公共牌张数不对、
    缺 pot、牌不合法）时 ok=false，reason 说明原因。
    """
    decision = advise(
        hero_cards=payload.hero_cards,
        board=payload.board,
        street=payload.street,
        actions=[a.model_dump() for a in payload.actions],
        pot=payload.pot,
        hero_position=payload.hero_position,
        opponents=payload.opponents,
        read_name=payload.read,
        stack_bb=payload.stack_bb,
        hero_invested_bb=payload.hero_invested_bb,
    )
    return CoachOut(**decision.to_dict())


@app.post("/ocr/table", response_model=TableOut, summary="识别牌桌")
def ocr_table(
    file: UploadFile = File(..., description="牌桌截图（PNG/JPEG）"),
    dealer_seat: str | None = Form(
        None, description="人工指定庄位座位 id，如 upL。不指定就走自动检测"),
    hero_seat: str | None = Form(
        None, description="人工指定哪个座位是我。不指定用配置里的默认（底部中间）"),
    debug: bool = Query(False, description="返回完整中间结果，排查用"),
):
    if not STATE.ready or STATE.profile is None or STATE.engine is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")

    img = decode_image(file.file.read())
    profile = resolve_profile(STATE.profile, hero_seat)

    if dealer_seat:
        try:
            profile.seat(dealer_seat)
        except KeyError:
            raise HTTPException(status_code=400,
                                detail=f"dealer_seat={dealer_seat} 不是合法座位，"
                                       f"可选：{[s.seat_id for s in profile.seats]}")

    detector = build_detector(profile, dealer_seat, engine=STATE.engine)

    # 串行推理：onnxruntime 的 session 不保证线程安全
    with STATE.lock:
        result = extract(img, profile, STATE.engine, detector,
                         frame_id=file.filename or "upload")

    payload = to_table_out(result, profile)
    if debug:
        return {**payload.model_dump(), "debug": result.to_dict()}
    return payload
