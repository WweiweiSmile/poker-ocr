#!/usr/bin/env python3
"""HTTP 接口测试，用 FastAPI 的 TestClient，不需要起服务。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_api.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402

FAILS: list[str] = []
IMAGE = ROOT / "table.jpg"


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def jpeg_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def blank_felt() -> bytes:
    """纯毛毡图：一个座位都没有，也没有庄位。"""
    img = np.full((3200, 1440, 3), (80, 102, 2), dtype=np.uint8)
    return jpeg_bytes(img)


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    check(r.status_code == 200, f"/health 应 200，实际 {r.status_code}")
    check(r.json().get("ready") is True, f"/health 应 ready=true，实际 {r.json()}")


def test_real_image(client: TestClient) -> None:
    if not IMAGE.exists():
        print(f"  跳过正例：{IMAGE.name} 不在")
        return
    with IMAGE.open("rb") as f:
        r = client.post("/ocr/table", files={"file": (IMAGE.name, f, "image/jpeg")})
    check(r.status_code == 200, f"应 200，实际 {r.status_code}: {r.text[:200]}")
    d = r.json()

    check(set(d) >= {"table_size", "seats"}, f"响应缺字段：{sorted(d)}")
    check(d["table_size"] == 9, f"桌号应 9，实际 {d['table_size']}")
    # 契约：只返回有人的座位，seats 长度就等于桌号
    check(len(d["seats"]) == d["table_size"],
          f"seats 长度 {len(d['seats'])} 应等于 table_size {d['table_size']}")

    # warnings 分两类，要分开看：
    #   硬失败 —— 名字/筹码/位置读不出来，结果不可用
    #   复核提示 —— 读出来了但置信度偏低或有分歧，结果仍可用
    # 参考图上「悍将v1」的检测结果是小写、阶梯读成「悍将V1」，分差在阈值内会报复核提示。
    # 这是设计成这样的，不该当成失败。
    hard = [w for w in d["warnings"] if "复核" not in w]
    check(hard == [], f"正常识别不该有硬失败 warning：{hard}")

    me = [s for s in d["seats"] if s["is_me"]]
    check(len(me) == 1, f"is_me 应恰好一个，实际 {len(me)}")
    check(d["seats"][0]["is_me"] is True, "hero 应排在数组第一个")

    positions = [s["position"] for s in d["seats"]]
    check(all(p for p in positions), f"不该有 null 位置：{positions}")
    check(len(set(positions)) == len(positions), f"位置有重复：{positions}")
    check("BTN" in positions, f"应有一个 BTN：{positions}")

    for s in d["seats"]:
        check(set(s) >= {"name", "position", "stack", "is_me"},
              f"座位缺字段：{sorted(s)}")
        check(isinstance(s["stack"], (int, float)) and not isinstance(s["stack"], bool),
              f"stack 应是数字，实际 {s['stack']!r}")

    # 整数筹码不该被写成浮点（302 而不是 302.0）
    ints = [s["stack"] for s in d["seats"] if float(s["stack"]).is_integer()]
    check(all(isinstance(v, int) for v in ints),
          f"整数值的筹码应保持整数：{[v for v in ints if not isinstance(v, int)]}")


def test_no_dealer_returns_200_with_warnings(client: TestClient) -> None:
    """没庄位不是请求错误 —— 名字和筹码仍然可用，不能整份丢掉。"""
    if not IMAGE.exists():
        return
    img = cv2.imread(str(IMAGE))
    felt = np.median(img[1100:1160, 200:400].reshape(-1, 3), axis=0).astype(np.uint8)
    img[2210:2300, 462:552] = felt          # 把 D 圆片涂掉
    r = client.post("/ocr/table", files={"file": ("x.jpg", jpeg_bytes(img), "image/jpeg")})
    check(r.status_code == 200, f"应 200，实际 {r.status_code}")
    d = r.json()
    check(any("庄位" in w for w in d["warnings"]),
          f"应有庄位相关的 warning，实际 {d['warnings']}")
    check(all(s["position"] is None for s in d["seats"]),
          "没庄位时位置应全为 null")
    check(any(s["name"] for s in d["seats"]), "名字仍应读出，不该整份丢掉")


def test_empty_table(client: TestClient) -> None:
    """一个人都没有：table_size 0，seats 空数组，且不能崩。"""
    r = client.post("/ocr/table", files={"file": ("x.jpg", blank_felt(), "image/jpeg")})
    check(r.status_code == 200, f"应 200，实际 {r.status_code}: {r.text[:200]}")
    d = r.json()
    check(d["table_size"] == 0, f"桌号应 0，实际 {d['table_size']}")
    check(d["seats"] == [], f"seats 应为空，实际 {d['seats']}")


def test_bad_inputs(client: TestClient) -> None:
    r = client.post("/ocr/table", files={"file": ("x.txt", b"not an image", "text/plain")})
    check(r.status_code == 400, f"非图片应 400，实际 {r.status_code}")

    r = client.post("/ocr/table", files={"file": ("x.jpg", b"", "image/jpeg")})
    check(r.status_code == 400, f"空文件应 400，实际 {r.status_code}")

    r = client.post("/ocr/table")
    check(r.status_code == 422, f"缺文件应 422，实际 {r.status_code}")

    if IMAGE.exists():
        r = client.post("/ocr/table",
                        files={"file": ("x.jpg", IMAGE.read_bytes(), "image/jpeg")},
                        data={"dealer_seat": "nope"})
        check(r.status_code == 400, f"非法庄位应 400，实际 {r.status_code}")
        r = client.post("/ocr/table",
                        files={"file": ("x.jpg", IMAGE.read_bytes(), "image/jpeg")},
                        data={"hero_seat": "nope"})
        check(r.status_code == 400, f"非法 hero 座位应 400，实际 {r.status_code}")


def test_manual_override(client: TestClient) -> None:
    """人工指定庄位时位置应整体重算。"""
    if not IMAGE.exists():
        return
    r = client.post("/ocr/table",
                    files={"file": ("x.jpg", IMAGE.read_bytes(), "image/jpeg")},
                    data={"dealer_seat": "midR"})
    check(r.status_code == 200, f"应 200，实际 {r.status_code}")
    d = r.json()
    by_pos = {s["position"]: s for s in d["seats"]}
    check(by_pos.get("BTN", {}).get("name") == "悍将v1",
          f"midR 是悍将v1，应被指定为 BTN，实际 {by_pos.get('BTN')}")
    check(by_pos.get("SB", {}).get("name") == "到晚上就想",
          f"庄位左侧应是 SB，实际 {by_pos.get('SB')}")


def main() -> int:
    if not IMAGE.exists():
        print(f"注意：{IMAGE.name} 不在，正例会被跳过")

    with TestClient(app) as client:
        tests = [
            test_health,
            test_real_image,
            test_no_dealer_returns_200_with_warnings,
            test_empty_table,
            test_bad_inputs,
            test_manual_override,
        ]
        for t in tests:
            before = len(FAILS)
            t(client)
            print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 接口测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
