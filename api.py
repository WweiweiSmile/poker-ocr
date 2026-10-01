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
        if s.status == "empty":
            continue          # 空座位不发牌、不占位置，不进数组（seats 长度 == table_size）

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
    if any(s.position is None for s in result.seats if s.status != "empty"):
        warnings.append("有座位没拿到位置")
    unread_name = [s.seat_id for s in result.seats if s.status != "empty" and not s.name]
    if unread_name:
        warnings.append(f"这些座位没读出名字：{', '.join(unread_name)}")
    unread_stack = [s.seat_id for s in result.seats if s.status != "empty" and s.stack is None]
    if unread_stack:
        warnings.append(f"这些座位没读出筹码：{', '.join(unread_stack)}")
    review = [s.seat_id for s in result.seats if s.name_review]
    if review:
        warnings.append(f"这些座位的名字置信度偏低，建议复核：{', '.join(review)}")
    uncertain = [s.seat_id for s in result.seats if s.status == "uncertain"]
    if uncertain:
        warnings.append(f"这些座位有人但读不出内容：{', '.join(uncertain)}")

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
